"""Concurrent media downloader with quality fallback (images + videos)."""

from __future__ import annotations

import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import httpx

from pinterest_dl.client import PinterestClient
from pinterest_dl.models import DownloadResult, MediaItem
from pinterest_dl.urls import candidate_urls, safe_filename, video_url_fallbacks

ProgressCb = Callable[[str, int, int], None]


def _ext_from_content_type(ctype: str | None, fallback: str = "jpg") -> str:
    if not ctype:
        return fallback
    ctype = ctype.split(";")[0].strip().lower()
    if ctype in {"video/mp4", "application/mp4"}:
        return "mp4"
    if ctype.startswith("video/"):
        guessed = mimetypes.guess_extension(ctype) or ""
        guessed = guessed.lstrip(".")
        return guessed or fallback
    ext = mimetypes.guess_extension(ctype) or ""
    ext = ext.lstrip(".")
    if ext == "jpe":
        ext = "jpg"
    if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
        return "jpg" if ext == "jpeg" else ext
    return fallback


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 10_000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate unique filename for {path}")


def _looks_like_media(data: bytes, *, expect_video: bool = False) -> bool:
    if not data or len(data) < 100:
        return False
    head = data[:200].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml")):
        return False
    if expect_video:
        # ftyp box (mp4/mov) or MPEG-TS / ID3
        if data[4:8] == b"ftyp" or data[:3] == b"ID3" or data[:2] == b"\x1aE":
            return True
        # some CDNs send bare mdat; accept non-html binary bodies
        return b"<html" not in head
    return True


class Downloader:
    def __init__(
        self,
        client: PinterestClient,
        *,
        output_dir: str | Path = "downloads",
        workers: int = 6,
        skip_existing: bool = True,
        preferred_quality: str = "originals",
    ) -> None:
        self.client = client
        self.output_dir = Path(output_dir)
        self.workers = max(1, workers)
        self.skip_existing = skip_existing
        self.preferred_quality = preferred_quality

    def _target_path(self, item: MediaItem, ext: str, *, suffix: str = "") -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        title_part = safe_filename(item.title) if item.title else ""
        base = safe_filename(item.id)
        if title_part and title_part.lower() != base.lower():
            name = f"{base}_{title_part}"
        else:
            name = base
        name = safe_filename(name, max_len=100)
        if suffix:
            name = f"{name}{suffix}"
        return self.output_dir / f"{name}.{ext}"

    def _existing_path(self, item: MediaItem, extensions: tuple[str, ...]) -> Path | None:
        candidates: set[Path] = set()
        for ext in extensions:
            candidates.add(self._target_path(item, ext))
            candidates.add(self.output_dir / f"{safe_filename(item.id)}.{ext}")
            if item.is_video or item.video_url:
                candidates.add(self._target_path(item, ext, suffix="_poster"))
        for p in candidates:
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def _download_url(
        self,
        url: str,
        *,
        item: MediaItem,
        default_ext: str,
        cascade: list[str] | None = None,
        path_suffix: str = "",
        expect_video: bool = False,
    ) -> tuple[Path | None, int, str | None]:
        """Return (path, bytes, error)."""
        errors: list[str] = []
        urls = cascade or [url]
        for attempt in urls:
            if not attempt:
                continue
            try:
                data, ctype = self.client.download_bytes(attempt, as_video=expect_video)
                if not _looks_like_media(data, expect_video=expect_video):
                    errors.append(f"{attempt}: empty/html body")
                    continue
                ext = _ext_from_content_type(ctype, default_ext)
                if expect_video and ext not in {"mp4", "webm", "mov", "m4v"}:
                    # trust URL extension if CDN content-type is generic
                    if attempt.lower().endswith(".mp4") or ".mp4?" in attempt.lower():
                        ext = "mp4"
                path = _unique_path(self._target_path(item, ext, suffix=path_suffix))
                path.write_bytes(data)
                return path, len(data), None
            except httpx.HTTPError as exc:
                errors.append(f"{attempt}: {exc}")
                continue
        return None, 0, "; ".join(errors[-3:]) or "download failed"

    def download_one(self, item: MediaItem) -> DownloadResult:
        try:
            saved: list[Path] = []
            total_bytes = 0
            errors: list[str] = []
            all_skipped = True

            # --- video first (when present) ---
            if item.video_url:
                video_exts = (item.video_extension or "mp4", "mp4", "webm", "mov")
                existing_v = None
                if self.skip_existing:
                    existing_v = self._existing_path(item, video_exts)
                    # only treat as skip if it's actually a video file
                    if existing_v and existing_v.suffix.lower() not in {
                        ".mp4",
                        ".webm",
                        ".mov",
                        ".m4v",
                    }:
                        existing_v = None
                    # re-check plain video paths
                    if not existing_v:
                        for ext in video_exts:
                            for p in (
                                self._target_path(item, ext),
                                self.output_dir / f"{safe_filename(item.id)}.{ext}",
                            ):
                                if p.exists() and p.stat().st_size > 0:
                                    existing_v = p
                                    break
                            if existing_v:
                                break
                if existing_v:
                    saved.append(existing_v)
                    total_bytes += existing_v.stat().st_size
                else:
                    all_skipped = False
                    cascade = video_url_fallbacks(item.video_url, item.video_urls)
                    path, nbytes, err = self._download_url(
                        item.video_url,
                        item=item,
                        default_ext=item.video_extension or "mp4",
                        cascade=cascade,
                        expect_video=True,
                    )
                    if path:
                        saved.append(path)
                        total_bytes += nbytes
                        item.video_url = cascade[0] if cascade else item.video_url
                        item.is_video = True
                        item.quality = "video" if item.quality in {"unknown", ""} else item.quality
                    elif err:
                        errors.append(err)

            # --- still image / poster ---
            if item.image_url:
                img_exts = (item.extension, "jpg", "png", "webp", "gif")
                existing_i = None
                if self.skip_existing:
                    # Prefer non-video paths for image skip
                    for ext in img_exts:
                        for p in (
                            self._target_path(
                                item,
                                ext,
                                suffix="_poster" if (item.video_url and item.is_video) else "",
                            ),
                            self._target_path(item, ext),
                            self.output_dir / f"{safe_filename(item.id)}.{ext}",
                        ):
                            if p.exists() and p.stat().st_size > 0 and p.suffix.lower() != ".mp4":
                                existing_i = p
                                break
                        if existing_i:
                            break
                if existing_i:
                    if existing_i not in saved:
                        saved.append(existing_i)
                        total_bytes += existing_i.stat().st_size
                else:
                    all_skipped = False
                    # When saving both, use _poster suffix if video also present so names don't collide
                    path_suffix = "_poster" if item.video_url else ""
                    cascade = candidate_urls(item.image_url, preferred=self.preferred_quality)
                    path, nbytes, err = self._download_url(
                        item.image_url,
                        item=item,
                        default_ext=item.extension or "jpg",
                        cascade=cascade,
                        path_suffix=path_suffix,
                        expect_video=False,
                    )
                    if path:
                        saved.append(path)
                        total_bytes += nbytes
                        # keep image_url pointing at successful CDN URL when possible
                        item.image_url = cascade[0] if cascade else item.image_url
                        m = re.search(r"pinimg\.com/([^/]+)/", item.image_url)
                        if m and m.group(1) != "videos":
                            item.quality = m.group(1)
                        item.extension = path.suffix.lstrip(".") or item.extension
                    elif err:
                        errors.append(err)

            if not saved and not item.image_url and not item.video_url:
                return DownloadResult(
                    item=item,
                    path=None,
                    ok=False,
                    error="no media URL on item",
                )

            if not saved:
                return DownloadResult(
                    item=item,
                    path=None,
                    ok=False,
                    error="; ".join(errors[-3:]) or "download failed",
                )

            # Prefer video path as primary when present
            primary = next((p for p in saved if p.suffix.lower() in {".mp4", ".webm", ".mov"}), saved[0])
            skipped = all_skipped and bool(saved)
            return DownloadResult(
                item=item,
                path=primary,
                ok=True,
                bytes_written=total_bytes,
                skipped=skipped,
                paths=[str(p) for p in saved],
            )
        except Exception as exc:  # noqa: BLE001 — surface to UI/CLI
            return DownloadResult(item=item, path=None, ok=False, error=str(exc))

    def download_many(
        self,
        items: list[MediaItem],
        *,
        progress: ProgressCb | None = None,
    ) -> list[DownloadResult]:
        total = len(items)
        results: list[DownloadResult] = []
        if total == 0:
            return results

        done = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.download_one, item): item for item in items}
            for fut in as_completed(futures):
                res = fut.result()
                results.append(res)
                done += 1
                if progress:
                    label = res.item.title or res.item.id
                    progress(label, done, total)
        # stable order like input
        ordered: list[DownloadResult] = []
        for item in items:
            match = next((r for r in results if r.item.id == item.id and r not in ordered), None)
            if match:
                ordered.append(match)
        return ordered or results
