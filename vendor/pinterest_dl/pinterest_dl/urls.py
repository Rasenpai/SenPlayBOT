"""URL helpers and high-quality pinimg URL construction."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from pinterest_dl.models import QUALITY_RANK

PIN_ID_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?pinterest\.[a-z.]+/pin/(?:[^/]*?--)?(\d{6,})/?",
    re.I,
)
BOARD_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?pinterest\.[a-z.]+/"
    r"(?P<user>[^/?#]+)/(?P<board>[^/?#]+)/?(?:$|[?#])",
    re.I,
)
SEARCH_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?pinterest\.[a-z.]+/search/pins/\?.*\bq=([^&]+)",
    re.I,
)
SHORT_RE = re.compile(r"(?:https?://)?(?:www\.)?pin\.it/[\w-]+/?", re.I)
PINIMG_RE = re.compile(
    r"https://i\.pinimg\.com/"
    r"(?P<size>[A-Za-z0-9_]+)/"
    r"(?P<hash>(?:[a-f0-9]{2}/){3}[a-f0-9]+)\."
    r"(?P<ext>jpe?g|png|webp|gif)",
    re.I,
)
# Also accept non-hash paths cautiously
PINIMG_ANY_RE = re.compile(r"https://i\.pinimg\.com/[a-zA-Z0-9_./-]+\.(?:jpe?g|png|webp|gif)", re.I)
# Progressive / HLS video CDN (v1.pinimg.com, v.pinimg.com, …)
PINVIDEO_MP4_RE = re.compile(
    r"https://v\d*\.pinimg\.com/videos/"
    r"(?P<path>[^\s\"'<>]+?)\.mp4",
    re.I,
)
PINVIDEO_ANY_RE = re.compile(
    r"https://v\d*\.pinimg\.com/videos/[^\s\"'<>]+\.(?:mp4|m3u8)",
    re.I,
)

PINTEREST_HOST_RE = re.compile(r"(?:^|\.)pinterest\.[a-z.]+$", re.I)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def is_pinterest_url(url: str) -> bool:
    try:
        host = urlparse(normalize_url(url)).netloc.lower()
    except Exception:
        return False
    if host.endswith("pin.it"):
        return True
    return bool(PINTEREST_HOST_RE.search(host))


def is_pinimg_url(url: str) -> bool:
    return "i.pinimg.com/" in url


def is_pinvideo_url(url: str) -> bool:
    return bool(re.search(r"v\d*\.pinimg\.com/videos/", url, re.I))


def classify_url(url: str) -> str:
    url = normalize_url(url)
    if is_pinvideo_url(url):
        return "direct_video"
    if is_pinimg_url(url):
        return "direct_image"
    if SHORT_RE.match(url):
        return "short"
    if PIN_ID_RE.search(url):
        return "pin"
    if SEARCH_RE.search(url):
        return "search"
    if BOARD_RE.search(url):
        user = BOARD_RE.search(url)
        assert user
        u = user.group("user").lower()
        b = user.group("board").lower()
        if u in {"pin", "search", "ideas", "today", "categories", "resource"}:
            return "unknown"
        if b.startswith("_"):
            return "profile"
        return "board"
    path = urlparse(url).path.strip("/")
    if path and "/" not in path and not path.startswith("pin"):
        return "profile"
    return "unknown"


def extract_pin_id(url: str) -> str | None:
    m = PIN_ID_RE.search(url)
    return m.group(1) if m else None


def parse_search_query(url: str) -> str | None:
    m = SEARCH_RE.search(url)
    if not m:
        qs = parse_qs(urlparse(url).query)
        if "q" in qs and qs["q"]:
            return unquote(qs["q"][0])
        return None
    return unquote(m.group(1))


def pinimg_parts(url: str) -> tuple[str, str, str] | None:
    m = PINIMG_RE.search(url)
    if not m:
        return None
    return m.group("size"), m.group("hash"), m.group("ext").lower()


def build_pinimg_url(signature_path: str, quality: str = "originals", ext: str = "jpg") -> str:
    """Build CDN URL.

    signature_path may be either:
      - full path hash like 'a7/66/56/a76656e966b1958f568d63c3f1c05aec'
      - bare 32-char hex signature
    """
    sig = signature_path.strip().strip("/")
    if re.fullmatch(r"[a-f0-9]{32}", sig):
        sig = f"{sig[0:2]}/{sig[2:4]}/{sig[4:6]}/{sig}"
    ext = ext.lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    return f"https://i.pinimg.com/{quality}/{sig}.{ext}"


def candidate_urls(
    url_or_sig: str,
    *,
    ext: str = "jpg",
    preferred: str = "originals",
) -> list[str]:
    """Return quality cascade URLs, highest first."""
    parts = pinimg_parts(url_or_sig) if url_or_sig.startswith("http") else None
    if parts:
        _, sig_path, found_ext = parts
        ext = found_ext or ext
    elif re.fullmatch(r"[a-f0-9]{32}", url_or_sig) or re.fullmatch(
        r"(?:[a-f0-9]{2}/){3}[a-f0-9]+", url_or_sig
    ):
        sig_path = url_or_sig
    else:
        # Unknown form — just try rewriting size segment if present
        rewritten = re.sub(
            r"/(?:\d+x\d*(?:_\w+)?|originals)/",
            f"/{preferred}/",
            url_or_sig,
            count=1,
        )
        urls = [rewritten]
        for q in QUALITY_RANK:
            u = re.sub(r"/(?:\d+x\d*(?:_\w+)?|originals)/", f"/{q}/", url_or_sig, count=1)
            if u not in urls:
                urls.append(u)
        if url_or_sig not in urls:
            urls.append(url_or_sig)
        return urls

    order = [preferred] + [q for q in QUALITY_RANK if q != preferred]
    # Try common extensions if originals 404s as wrong type
    exts = [ext] + [e for e in ("jpg", "png", "webp") if e != ext]
    out: list[str] = []
    for e in exts:
        for q in order:
            u = build_pinimg_url(sig_path, q, e)
            if u not in out:
                out.append(u)
    return out


def upgrade_to_best(url: str, preferred: str = "originals") -> str:
    return candidate_urls(url, preferred=preferred)[0]


def rank_video_url(url: str) -> tuple[int, int]:
    """Lower is better. Prefer progressive H.264 720p MP4 over HLS/HEVC trials."""
    ul = url.lower()
    score = 100
    if ul.endswith(".m3u8") or "/hls/" in ul or "/h265/" in ul:
        score += 10_000
    if not (ul.endswith(".mp4") or ".mp4?" in ul):
        score += 5_000
    if "/720p/" in ul:
        score -= 200
    if re.search(r"/\d{3,4}p/", ul) and "/720p/" not in ul:
        score -= 80
    if "hevc" in ul:
        score += 120
    if "expmp4" in ul:
        score += 90
    # trial ABR ladder files (..._t1.mp4) are partial/experimental
    if re.search(r"_t\d+\.mp4(?:\?|$)", ul):
        score += 150
    return score, -len(url)


def pick_best_video_url(urls: list[str]) -> str | None:
    """Pick best progressive MP4; skip pure HLS when any MP4 exists."""
    cleaned = [u.strip() for u in urls if isinstance(u, str) and u.startswith("http")]
    if not cleaned:
        return None
    mp4s = [u for u in cleaned if u.lower().endswith(".mp4") or ".mp4?" in u.lower()]
    pool = mp4s or cleaned
    return sorted(pool, key=rank_video_url)[0]


def video_url_fallbacks(primary: str, extras: list[str] | None = None) -> list[str]:
    """Ordered unique video URL attempts (MP4 first)."""
    urls: list[str] = []
    for u in [primary, *(extras or [])]:
        if u and u not in urls:
            urls.append(u)
    # Prefer MP4 order via rank
    mp4s = sorted(
        [u for u in urls if u.lower().endswith(".mp4") or ".mp4?" in u.lower()],
        key=rank_video_url,
    )
    rest = [u for u in urls if u not in mp4s and not u.lower().endswith(".m3u8")]
    return mp4s + rest


def safe_filename(name: str, max_len: int = 120) -> str:
    name = unquote(name).strip()
    name = re.sub(r"[<>:\"/\\\\|?*\x00-\x1f]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        name = "pinterest"
    return name[:max_len]
