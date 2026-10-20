"""Extract high-quality media items from Pinterest HTML / embedded JSON."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from pinterest_dl.models import QUALITY_RANK, MediaItem, ScrapeResult, SourceKind
from pinterest_dl.urls import (
    PINIMG_RE,
    PINVIDEO_MP4_RE,
    build_pinimg_url,
    candidate_urls,
    classify_url,
    extract_pin_id,
    normalize_url,
    parse_search_query,
    pick_best_video_url,
    pinimg_parts,
    safe_filename,
    video_url_fallbacks,
)

RELAY_CALL_RE = re.compile(
    r"window\.__PWS_RELAY_REGISTER_COMPLETED_REQUEST__\(\s*\"([^\"]+)\"\s*,",
    re.S,
)
META_RE = re.compile(
    r"<meta\b([^>]+)>",
    re.I,
)
ATTR_RE = re.compile(
    r"""([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""",
    re.I,
)


def _meta_tags(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in META_RE.finditer(html):
        attrs = {a[0].lower(): (a[1] or a[2] or a[3] or "") for a in ATTR_RE.findall(m.group(1))}
        key = attrs.get("property") or attrs.get("name")
        content = attrs.get("content")
        if key and content:
            out[key.lower()] = unescape(content)
    return out


def _balanced_json_object(text: str, start: int) -> str | None:
    """Return JSON object substring starting at text[start] == '{'."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_relay_payloads(html: str) -> list[Any]:
    payloads: list[Any] = []
    for m in RELAY_CALL_RE.finditer(html):
        # second arg is a raw JS object literal
        j = m.end()
        while j < len(html) and html[j].isspace():
            j += 1
        blob = _balanced_json_object(html, j)
        if not blob:
            continue
        try:
            payloads.append(json.loads(blob))
        except json.JSONDecodeError:
            continue
    return payloads


def _quality_from_url(url: str) -> str:
    parts = pinimg_parts(url)
    if parts:
        return parts[0]
    m = re.search(r"pinimg\.com/([^/]+)/", url)
    return m.group(1) if m else "unknown"


def _ext_from_url(url: str) -> str:
    parts = pinimg_parts(url)
    if parts:
        return parts[2]
    m = re.search(r"\.([a-zA-Z0-9]+)(?:\?|$)", url)
    ext = (m.group(1) if m else "jpg").lower()
    return "jpg" if ext == "jpeg" else ext


def _signature_from_url(url: str) -> str | None:
    parts = pinimg_parts(url)
    if not parts:
        return None
    return parts[1].split("/")[-1]


def _video_signature_from_url(url: str) -> str | None:
    m = re.search(
        r"v\d*\.pinimg\.com/videos/(?:mc/)?[^/]+/(?:[a-f0-9]{2}/){3}([a-f0-9]+)",
        url,
        re.I,
    )
    if m:
        return m.group(1)
    m = re.search(r"/([a-f0-9]{32})(?:_t\d+)?\.(?:mp4|m3u8)(?:\?|$)", url, re.I)
    return m.group(1) if m else None


def _collect_video_urls(pin: dict[str, Any]) -> list[str]:
    """Collect progressive/HLS video URLs from classic or GraphQL pin payloads."""
    found: list[str] = []

    def add(u: Any) -> None:
        if isinstance(u, str) and u.startswith("http") and ("pinimg.com" in u or ".mp4" in u):
            if u not in found:
                found.append(u)

    videos = pin.get("videos")
    if isinstance(videos, dict):
        for key in ("videoUrls", "video_urls"):
            raw = videos.get(key)
            if isinstance(raw, list):
                for u in raw:
                    add(u)
        vlist = videos.get("video_list") or videos.get("videoList") or {}
        if isinstance(vlist, dict):
            for key, fmt in vlist.items():
                if key in {"__typename", "id", "type"}:
                    continue
                if isinstance(fmt, dict):
                    add(fmt.get("url"))
                    add(fmt.get("thumbnail"))
                elif isinstance(fmt, str):
                    add(fmt)
        # occasional flat url fields
        for key, val in videos.items():
            if isinstance(val, str) and ("/videos/" in val or val.endswith(".mp4")):
                add(val)
    elif isinstance(videos, list):
        for entry in videos:
            if isinstance(entry, str):
                add(entry)
            elif isinstance(entry, dict):
                add(entry.get("url"))

    # story pin blocks sometimes nest video urls
    story = pin.get("storyPinData") or pin.get("story_pin_data")
    if isinstance(story, dict):
        def walk(o: Any) -> None:
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in {"url", "videoUrl", "video_url"} and isinstance(v, str):
                        add(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(story)

    return found


def _pick_best_url(urls: list[str], preferred: str = "originals") -> str:
    def rank(u: str) -> tuple[int, int]:
        q = _quality_from_url(u)
        try:
            qi = QUALITY_RANK.index(q)
        except ValueError:
            qi = 99
        # prefer preferred quality
        if q == preferred:
            qi = -1
        return qi, -len(u)

    return sorted(urls, key=rank)[0]


def _item_from_pin_dict(pin: dict[str, Any], *, source_url: str, preferred: str) -> MediaItem | None:
    """Build MediaItem from either classic images{} or GraphQL images_* fields."""
    urls: list[str] = []
    width = height = None
    signature = pin.get("imageSignature") or pin.get("image_signature")
    title = (
        pin.get("gridTitle")
        or pin.get("grid_title")
        or pin.get("closeupUnifiedTitle")
        or pin.get("title")
        or pin.get("seoTitle")
        or ""
    )
    description = (
        pin.get("closeupUnifiedDescription")
        or pin.get("description")
        or pin.get("gridDescription")
        or pin.get("seoDescription")
        or ""
    )
    pin_id = str(
        pin.get("entityId")
        or pin.get("id")
        or pin.get("pin_id")
        or signature
        or ""
    )
    # GraphQL numeric id sometimes base64 — keep digits if entityId missing
    if pin_id.startswith("UGlu:"):
        pin_id = pin_id.split(":", 1)[-1]
    if not pin_id.isdigit():
        # try decode-ish: leave as is if signature available later
        pass

    images = pin.get("images")
    if isinstance(images, dict):
        for key, val in images.items():
            if isinstance(val, dict) and val.get("url"):
                urls.append(val["url"])
                if key in {"orig", "originals"} or key == preferred:
                    width = val.get("width") or width
                    height = val.get("height") or height
            elif isinstance(val, str) and val.startswith("http"):
                urls.append(val)

    # GraphQL flattened fields: images_orig, images_736x, imageLargeUrl, ...
    for key, val in pin.items():
        if key.startswith("images_") and isinstance(val, dict) and val.get("url"):
            urls.append(val["url"])
            if "orig" in key:
                width = val.get("width") or width
                height = val.get("height") or height
        if key in {"imageLargeUrl", "image_large_url"} and isinstance(val, str):
            urls.append(val)

    # Videos — progressive MP4 preferred; poster/thumbnail used as image fallback
    video_candidates = _collect_video_urls(pin)
    best_video = pick_best_video_url(
        [u for u in video_candidates if not re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", u, re.I)]
    )
    # Poster thumbnails from video formats
    for u in video_candidates:
        if "i.pinimg.com" in u and re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", u, re.I):
            urls.append(u)

    is_video = bool(
        best_video
        or pin.get("isVideo")
        or pin.get("is_video")
        or (isinstance(pin.get("videos"), dict) and pin.get("videos"))
    )
    # seoTitle often contains "[Video]" for idea pins even when isVideo is false
    seo_title = str(pin.get("seoTitle") or pin.get("seo_title") or "")
    if "[video]" in seo_title.lower():
        is_video = True

    urls = [u for u in urls if "i.pinimg.com" in u]
    if not urls and signature:
        urls = candidate_urls(str(signature), preferred=preferred)
    if not urls and not best_video:
        return None

    if urls:
        best = _pick_best_url(urls, preferred=preferred)
        # Force preferred quality cascade head
        best = candidate_urls(best, preferred=preferred)[0]
        sig = signature or _signature_from_url(best)
        alts = [u for u in candidate_urls(best, preferred=preferred) if u != best][:8]
        ext = _ext_from_url(best)
        quality = _quality_from_url(best)
    else:
        # video-only pin (no still)
        best = ""
        sig = signature or (_video_signature_from_url(best_video) if best_video else None)
        alts = []
        ext = "jpg"
        quality = "video"

    if not pin_id or not str(pin_id).isdigit():
        pin_id = sig or safe_filename(best or best_video or "pin")[-40:]

    pin_url = pin.get("seoUrl") or pin.get("link")
    if isinstance(pin_url, str) and pin_url.startswith("/"):
        pin_url = "https://www.pinterest.com" + pin_url
    if not pin_url and str(pin_id).isdigit():
        pin_url = f"https://www.pinterest.com/pin/{pin_id}/"

    video_alts: list[str] = []
    if best_video:
        video_alts = [
            u
            for u in video_url_fallbacks(best_video, video_candidates)
            if u != best_video and (u.lower().endswith(".mp4") or ".mp4?" in u.lower())
        ][:8]

    return MediaItem(
        id=str(pin_id),
        image_url=best,
        quality=quality if not (is_video and quality == "unknown") else (quality or "video"),
        title=str(title or "")[:300],
        description=str(description or "")[:1000],
        pin_url=pin_url if isinstance(pin_url, str) else None,
        source_url=source_url,
        width=int(width) if isinstance(width, int) else None,
        height=int(height) if isinstance(height, int) else None,
        signature=str(sig) if sig else None,
        extension=ext,
        alt_urls=alts,
        is_video=bool(is_video or best_video),
        video_url=best_video,
        video_urls=video_alts,
        video_extension="mp4",
    )


def _walk_pins(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def looks_like_pin(d: dict[str, Any]) -> bool:
        if any(k.startswith("images_") for k in d):
            return True
        if isinstance(d.get("images"), dict):
            return True
        if d.get("imageSignature") or d.get("image_signature"):
            return True
        if d.get("__typename") == "Pin":
            return True
        if isinstance(d.get("videos"), (dict, list)) and (
            d.get("entityId") or d.get("id") or d.get("imageSignature")
        ):
            return True
        return False

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if looks_like_pin(o):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


def extract_pinimg_items_from_html(
    html: str,
    *,
    source_url: str,
    preferred: str = "originals",
    limit: int | None = None,
) -> list[MediaItem]:
    """Fallback: harvest unique pinimg hashes from raw HTML and build HQ URLs."""
    # Skip obvious UI icons by requiring hash path form
    seen: dict[str, MediaItem] = {}
    for m in PINIMG_RE.finditer(html):
        size, sig_path, ext = m.group("size"), m.group("hash"), m.group("ext").lower()
        # skip tiny avatars / RS crops when better exists
        if size.endswith("_RS") or size in {"30x30", "60x60", "75x75", "140x140_RS", "136x136"}:
            # still record signature but mark weak
            pass
        sig = sig_path.split("/")[-1]
        best = build_pinimg_url(sig_path, preferred if preferred else "originals", ext)
        # keep highest-quality observation per signature
        prev = seen.get(sig)
        candidate = MediaItem(
            id=sig,
            image_url=best,
            quality=preferred,
            title="",
            description="",
            pin_url=None,
            source_url=source_url,
            signature=sig,
            extension=ext if ext != "jpeg" else "jpg",
            alt_urls=candidate_urls(best, preferred=preferred)[1:8],
        )
        if prev is None:
            seen[sig] = candidate
        else:
            # upgrade quality preference already originals; keep existing metadata
            pass
        if limit and len(seen) >= limit * 3:
            # collect extra then filter
            break

    items = list(seen.values())
    # Filter out tiny logo-like assets: originals of pinterest logo hash known? keep all hash media
    if limit:
        items = items[:limit]
    return items


def extract_from_html(
    html: str,
    *,
    source_url: str,
    preferred_quality: str = "originals",
    limit: int | None = None,
) -> ScrapeResult:
    source_url = normalize_url(source_url)
    kind_s = classify_url(source_url)
    kind = {
        "pin": SourceKind.PIN,
        "board": SourceKind.BOARD,
        "search": SourceKind.SEARCH,
        "profile": SourceKind.PROFILE,
        "direct_image": SourceKind.DIRECT_IMAGE,
        "direct_video": SourceKind.DIRECT_VIDEO,
        "short": SourceKind.UNKNOWN,
        "unknown": SourceKind.UNKNOWN,
    }.get(kind_s, SourceKind.UNKNOWN)

    meta = _meta_tags(html)
    title = meta.get("og:title") or meta.get("twitter:title") or ""
    description = meta.get("og:description") or meta.get("description") or ""
    warnings: list[str] = []
    items_by_sig: dict[str, MediaItem] = {}

    def rank_video_better(a: str, b: str) -> bool:
        from pinterest_dl.urls import rank_video_url

        return rank_video_url(a) < rank_video_url(b)

    def add(item: MediaItem | None) -> None:
        if not item:
            return
        key = item.signature or item.id or item.image_url or item.video_url or ""
        prev = items_by_sig.get(key)
        if prev is None:
            items_by_sig[key] = item
            return
        # merge metadata, keep best title/desc/video
        if not prev.title and item.title:
            prev.title = item.title
        if not prev.description and item.description:
            prev.description = item.description
        if not prev.pin_url and item.pin_url:
            prev.pin_url = item.pin_url
        if (prev.width or 0) < (item.width or 0):
            prev.width = item.width
            prev.height = item.height
        if item.video_url and (
            not prev.video_url or rank_video_better(item.video_url, prev.video_url)
        ):
            prev.video_url = item.video_url
            prev.video_urls = item.video_urls or prev.video_urls
            prev.is_video = True
        elif item.is_video:
            prev.is_video = True
        if item.video_urls:
            merged = list(prev.video_urls)
            for u in item.video_urls:
                if u not in merged and u != prev.video_url:
                    merged.append(u)
            prev.video_urls = merged[:8]
        if not prev.image_url and item.image_url:
            prev.image_url = item.image_url
            prev.quality = item.quality
            prev.extension = item.extension
            prev.alt_urls = item.alt_urls

    # 1) GraphQL / relay SSR payloads (best metadata)
    for payload in extract_relay_payloads(html):
        for pin in _walk_pins(payload):
            add(_item_from_pin_dict(pin, source_url=source_url, preferred=preferred_quality))

    # 2) JSON script tags (initial props etc.)
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        sid = script.get("id") or ""
        raw = script.string or script.get_text() or ""
        if not raw or len(raw) < 20:
            continue
        if sid in {"__PWS_ROUTES__"}:
            continue
        if (
            "pinimg.com" not in raw
            and "imageSignature" not in raw
            and "images_" not in raw
            and "video_list" not in raw
            and "videoList" not in raw
            and "videoUrls" not in raw
        ):
            # still try __PWS_INITIAL_PROPS__ sometimes embeds escaped urls
            if "pinimg" not in raw and sid not in {"__PWS_INITIAL_PROPS__", "__PWS_DATA__"}:
                continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for pin in _walk_pins(data):
            add(_item_from_pin_dict(pin, source_url=source_url, preferred=preferred_quality))

    # 3) og:image for single pin pages
    og = meta.get("og:image") or meta.get("twitter:image:src") or meta.get("twitter:image")
    if og and "pinimg.com" in og:
        pin_id = extract_pin_id(source_url) or extract_pin_id(meta.get("og:url", "")) or ""
        best = candidate_urls(og, preferred=preferred_quality)[0]
        add(
            MediaItem(
                id=pin_id or (_signature_from_url(best) or "og-image"),
                image_url=best,
                quality=_quality_from_url(best),
                title=title,
                description=description,
                pin_url=meta.get("og:url") or source_url,
                source_url=source_url,
                signature=_signature_from_url(best),
                extension=_ext_from_url(best),
                alt_urls=candidate_urls(best, preferred=preferred_quality)[1:8],
            )
        )

    # 4) Raw pinimg harvest (boards / grids)
    before = len(items_by_sig)
    for item in extract_pinimg_items_from_html(
        html, source_url=source_url, preferred=preferred_quality, limit=limit
    ):
        add(item)
    if len(items_by_sig) > before and kind in {SourceKind.BOARD, SourceKind.SEARCH, SourceKind.PROFILE}:
        warnings.append(
            "Board/search pages only expose the first loaded batch in HTML "
            "(no official free API). For huge boards, paste multiple section URLs "
            "or re-run after scrolling in a browser export."
        )

    # 5) Raw video MP4 harvest — attach to primary pin when possible
    html_videos = _extract_video_urls_from_html(html)
    if html_videos:
        best_v = pick_best_video_url(html_videos)
        alts_v = [
            u
            for u in video_url_fallbacks(best_v or html_videos[0], html_videos)
            if u != best_v and (u.lower().endswith(".mp4") or ".mp4?" in u.lower())
        ][:8]
        pin_id_hint = extract_pin_id(source_url) or ""
        attached = False
        if pin_id_hint:
            for item in items_by_sig.values():
                if item.id == pin_id_hint:
                    if not item.video_url or (
                        best_v and rank_video_better(best_v, item.video_url)
                    ):
                        item.video_url = best_v
                        item.video_urls = alts_v or item.video_urls
                    item.is_video = True
                    attached = True
                    break
        if not attached and kind == SourceKind.PIN and items_by_sig:
            # single primary item after filtering may not exist yet — attach to first
            primary_item = next(iter(items_by_sig.values()))
            if not primary_item.video_url:
                primary_item.video_url = best_v
                primary_item.video_urls = alts_v
                primary_item.is_video = True
                attached = True
        if not attached and best_v:
            v_sig = _video_signature_from_url(best_v) or safe_filename(best_v)[-40:]
            add(
                MediaItem(
                    id=pin_id_hint or v_sig,
                    image_url="",
                    quality="video",
                    title=title,
                    description=description,
                    pin_url=meta.get("og:url") or source_url,
                    source_url=source_url,
                    signature=v_sig if len(v_sig) >= 16 else None,
                    extension="mp4",
                    is_video=True,
                    video_url=best_v,
                    video_urls=alts_v,
                    video_extension="mp4",
                )
            )

    items = list(items_by_sig.values())

    # Single-pin preference: keep the primary pin image; drop avatars/logos.
    pin_id = extract_pin_id(source_url)
    if kind == SourceKind.PIN and pin_id:
        primary = [i for i in items if i.id == pin_id]
        if not primary and og and "pinimg.com" in og:
            og_sig = _signature_from_url(og)
            primary = [i for i in items if i.signature == og_sig]
        if primary:
            # merge any video-only orphans into primary
            main = primary[0]
            for other in items:
                if other is main:
                    continue
                if other.video_url and (
                    not main.video_url or rank_video_better(other.video_url, main.video_url)
                ):
                    main.video_url = other.video_url
                    main.video_urls = other.video_urls or main.video_urls
                    main.is_video = True
            items = [main]
        else:
            # fall back: largest non-avatar titles first
            items = sorted(items, key=lambda i: (bool(i.title), i.id == pin_id), reverse=True)[:1]

    # De-noise board grids: drop signatures that only ever appear as avatar crops
    # (already upgraded to originals — hard to know; keep all unique hashes)

    if limit is not None:
        items = items[: max(1, limit)]

    if not title:
        if kind == SourceKind.SEARCH:
            q = parse_search_query(source_url)
            title = f"Search: {q}" if q else "Pinterest search"
        elif kind == SourceKind.BOARD:
            title = "Pinterest board"
        elif kind == SourceKind.PIN:
            title = items[0].title if items else "Pinterest pin"
        else:
            title = "Pinterest"

    if not items:
        warnings.append(
            "No images or videos found. The pin/board may be private, deleted, or region-blocked."
        )

    return ScrapeResult(
        source_url=source_url,
        kind=kind,
        title=title,
        items=items,
        warnings=warnings,
    )


def _extract_video_urls_from_html(html: str) -> list[str]:
    found: list[str] = []
    for m in PINVIDEO_MP4_RE.finditer(html):
        url = m.group(0)
        if url not in found:
            found.append(url)
    return found


def extract_direct_image(url: str, preferred_quality: str = "originals") -> ScrapeResult:
    url = normalize_url(url)
    best = candidate_urls(url, preferred=preferred_quality)[0]
    sig = _signature_from_url(best) or "image"
    item = MediaItem(
        id=sig,
        image_url=best,
        quality=_quality_from_url(best),
        title=sig,
        signature=sig if len(sig) == 32 else None,
        extension=_ext_from_url(best),
        alt_urls=candidate_urls(best, preferred=preferred_quality)[1:8],
        source_url=url,
    )
    return ScrapeResult(
        source_url=url,
        kind=SourceKind.DIRECT_IMAGE,
        title=sig,
        items=[item],
    )


def extract_direct_video(url: str) -> ScrapeResult:
    url = normalize_url(url)
    sig = _video_signature_from_url(url) or safe_filename(url)[-40:]
    item = MediaItem(
        id=sig,
        image_url="",
        quality="video",
        title=sig,
        signature=sig if len(sig) >= 16 else None,
        extension="mp4",
        source_url=url,
        is_video=True,
        video_url=url,
        video_urls=[],
        video_extension="mp4",
    )
    return ScrapeResult(
        source_url=url,
        kind=SourceKind.DIRECT_VIDEO,
        title=sig,
        items=[item],
    )
