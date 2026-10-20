"""HTTP session helpers for Pinterest pages and CDN assets."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class PinterestClient:
    """Thin httpx wrapper with browser-like defaults and retries."""

    def __init__(
        self,
        *,
        timeout: float = 45.0,
        proxy: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged = dict(DEFAULT_HEADERS)
        if headers:
            merged.update(headers)
        self._client = httpx.Client(
            headers=merged,
            follow_redirects=True,
            timeout=timeout,
            proxy=proxy,
            http2=False,
        )
        # Warm cookies (csrftoken / session) — improves some page payloads.
        try:
            self._client.get("https://www.pinterest.com/")
        except httpx.HTTPError:
            pass

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PinterestClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.6, min=0.6, max=6),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    def get_text(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        """GET a URL and return (final_url, text)."""
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        return str(resp.url), resp.text

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.4, max=5),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    def head(self, url: str, *, referer: str = "https://www.pinterest.com/") -> httpx.Response:
        return self._client.head(url, headers={"Referer": referer})

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.4, max=5),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    def download_bytes(
        self,
        url: str,
        *,
        referer: str = "https://www.pinterest.com/",
        as_video: bool = False,
    ) -> tuple[bytes, str | None]:
        if as_video:
            accept = "video/mp4,video/*;q=0.9,application/octet-stream;q=0.8,*/*;q=0.7"
            dest = "video"
        else:
            accept = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            dest = "image"
        headers = {
            "Referer": referer,
            "Accept": accept,
            "Sec-Fetch-Dest": dest,
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type")
        return resp.content, ctype
