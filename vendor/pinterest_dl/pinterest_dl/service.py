"""High-level scrape + download orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pinterest_dl.client import PinterestClient
from pinterest_dl.downloader import Downloader
from pinterest_dl.extractor import extract_direct_image, extract_direct_video, extract_from_html
from pinterest_dl.models import DownloadResult, ScrapeResult
from pinterest_dl.urls import classify_url, is_pinimg_url, is_pinterest_url, is_pinvideo_url, normalize_url


class PinterestService:
    def __init__(
        self,
        *,
        output_dir: str | Path = "downloads",
        workers: int = 6,
        preferred_quality: str = "originals",
        skip_existing: bool = True,
        proxy: str | None = None,
        timeout: float = 45.0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.workers = workers
        self.preferred_quality = preferred_quality
        self.skip_existing = skip_existing
        self.proxy = proxy
        self.timeout = timeout

    def scrape(self, url: str, *, limit: int | None = None) -> ScrapeResult:
        url = normalize_url(url)
        if is_pinvideo_url(url):
            return extract_direct_video(url)
        if is_pinimg_url(url):
            return extract_direct_image(url, preferred_quality=self.preferred_quality)

        if not is_pinterest_url(url) and classify_url(url) == "unknown":
            # allow bare pinimg already handled; otherwise require pinterest
            if "pinterest." not in url and "pin.it" not in url and "pinimg.com" not in url:
                raise ValueError(
                    "URL must be a Pinterest pin/board/search link or pinimg CDN URL"
                )

        with PinterestClient(timeout=self.timeout, proxy=self.proxy) as client:
            final_url, html = client.get_text(url)
            result = extract_from_html(
                html,
                source_url=final_url,
                preferred_quality=self.preferred_quality,
                limit=limit,
            )
            if limit is not None:
                result.items = result.items[: max(0, limit)]
            return result

    def download(
        self,
        url: str,
        *,
        limit: int | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[ScrapeResult, list[DownloadResult]]:
        scraped = self.scrape(url, limit=limit)
        with PinterestClient(timeout=self.timeout, proxy=self.proxy) as client:
            dl = Downloader(
                client,
                output_dir=self.output_dir,
                workers=self.workers,
                skip_existing=self.skip_existing,
                preferred_quality=self.preferred_quality,
            )
            results = dl.download_many(scraped.items, progress=progress)
        return scraped, results

    def download_items(
        self,
        scraped: ScrapeResult,
        *,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> list[DownloadResult]:
        with PinterestClient(timeout=self.timeout, proxy=self.proxy) as client:
            dl = Downloader(
                client,
                output_dir=self.output_dir,
                workers=self.workers,
                skip_existing=self.skip_existing,
                preferred_quality=self.preferred_quality,
            )
            return dl.download_many(scraped.items, progress=progress)
