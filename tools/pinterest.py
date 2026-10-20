"""Pinterest downloader untuk SENP4II: /pinterest url:<URL>

Alur:
  validasi URL -> (resolve pin.it) -> primary `pinterest-downloader`
  -> jika gagal / tidak ada media valid: fallback `pinterest-dl` SATU kali
  -> embed + tombol Download / Buka Pin Asli.

Prinsip:
  * Hanya Pin tunggal yang publik (bukan board/profil), tanpa login/bypass.
  * URL awal dan URL media hasil ekstraksi divalidasi (host + skema).
  * Tidak ada media yang disimpan di disk. Video kecil dikirim lewat memori
    (BytesIO); fallback memakai folder sementara yang otomatis dihapus.
  * Detail internal / stack trace hanya masuk log, tidak ke pengguna.

CATATAN VERIFIKASI: nama class/method/field library di bawah diambil dari
README resmi kedua library, BUKAN dari source versi yang terpasang (source
tidak bisa dibaca saat kode ini ditulis). Setelah `pip install`, cek ulang
`_primary_fetch` dan `_fallback_fetch` terhadap versi yang terpasang.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
import discord
from discord import app_commands

# --- Konfigurasi -----------------------------------------------------------

EMBED_COLOR = 0xE60023  # sesuaikan dengan warna embed tool lain di bot
FOOTER_TEXT = "Powered by SENP4II"

LIB_TIMEOUT = 25  # detik, per library
HTTP_TIMEOUT = 10  # detik, probe / resolve
DOWNLOAD_TIMEOUT = 40  # detik, unduh video untuk di-upload
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024  # batas upload Discord tanpa boost
HARD_UPLOAD_CAP = 25 * 1024 * 1024  # tidak pernah upload lebih dari ini
UPLOAD_MARGIN = 64 * 1024
MAX_REDIRECTS = 4
MAX_URL_LEN = 300
MAX_VIDEO_VARIANTS = 3
COOLDOWN_SECONDS = 15.0

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SENP4II-Bot)"}

PIN_DOMAINS = frozenset(
    {
        "pinterest.com",
        "pinterest.co.uk",
        "pinterest.ca",
        "pinterest.fr",
        "pinterest.de",
        "pinterest.es",
        "pinterest.it",
        "pinterest.jp",
        "pinterest.ph",
        "pinterest.pt",
        "pinterest.nz",
        "pinterest.se",
        "pinterest.dk",
        "pinterest.ch",
        "pinterest.at",
        "pinterest.ie",
        "pinterest.nl",
        "pinterest.ru",
        "pinterest.com.au",
        "pinterest.com.mx",
        "pinterest.co.kr",
        "pinterest.cl",
    }
)
SHORT_HOST = "pin.it"
MEDIA_DOMAIN = "pinimg.com"
PIN_PATH_RE = re.compile(r"^/pin/[A-Za-z0-9_\-]+/?$")

KIND_LABEL = {"image": "Gambar", "video": "Video (MP4)", "gif": "GIF"}

MSG_BAD_URL = (
    "URL tidak valid. Kirim link Pin Pinterest, contoh: "
    "`https://www.pinterest.com/pin/123456789/` atau `https://pin.it/abcdef`."
)
MSG_NOT_PIN = "Hanya link Pin tunggal yang didukung (bukan board atau profil)."
MSG_FAILED = (
    "Gagal mengambil media dari Pin ini. Pastikan Pin bersifat publik dan masih "
    "tersedia, lalu coba lagi nanti."
)
MSG_GENERIC = "Terjadi kesalahan. Coba lagi nanti."


class PinError(Exception):
    """Error yang pesannya aman ditampilkan ke pengguna."""


# --- Validasi URL ----------------------------------------------------------


def _is_pin_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in PIN_DOMAINS)


def validate_user_url(raw: str) -> str:
    """Validasi input pengguna. Mengembalikan URL https ternormalisasi
    (tanpa query/fragment) atau melempar PinError."""
    text = (raw or "").strip().strip("<>").strip()
    if not text or len(text) > MAX_URL_LEN or re.search(r"\s", text):
        raise PinError(MSG_BAD_URL)
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise PinError(MSG_BAD_URL) from None
    if parts.scheme not in ("http", "https") or not host:
        raise PinError(MSG_BAD_URL)
    if parts.username or parts.password or port not in (None, 80, 443):
        raise PinError(MSG_BAD_URL)

    if host == SHORT_HOST:
        if len(parts.path.strip("/")) < 2 or "/" in parts.path.strip("/"):
            raise PinError(MSG_BAD_URL)
        return f"https://{SHORT_HOST}{parts.path}"
    if _is_pin_host(host):
        if not PIN_PATH_RE.match(parts.path):
            raise PinError(MSG_NOT_PIN)
        return f"https://{host}{parts.path}"
    raise PinError(MSG_BAD_URL)


async def resolve_short(session: aiohttp.ClientSession, url: str) -> str:
    """Ikuti redirect pin.it secara manual (host tiap hop divalidasi).
    Jika tidak bisa di-resolve, kembalikan URL asli; library yang mendukung
    pin.it akan menanganinya."""
    if urlsplit(url).hostname != SHORT_HOST:
        return url
    cur = url
    for _ in range(MAX_REDIRECTS):
        loc: str | None = None
        try:
            async with session.get(
                cur,
                headers=HEADERS,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
            ) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    return url
                loc = resp.headers.get("Location")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return url
        if not loc:
            return url
        nxt = urljoin(cur, loc)
        host = (urlsplit(nxt).hostname or "").lower()
        if host == SHORT_HOST:
            cur = nxt
            continue
        if _is_pin_host(host):
            return validate_user_url(nxt)  # PinError jika bukan Pin tunggal
        raise PinError(MSG_BAD_URL)
    raise PinError(MSG_BAD_URL)


def is_valid_media_url(u: object) -> bool:
    """Media hasil ekstraksi harus https dan berasal dari CDN Pinterest."""
    if not isinstance(u, str) or not u or len(u) > 2000:
        return False
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    return host == MEDIA_DOMAIN or host.endswith("." + MEDIA_DOMAIN)


# --- Model data ------------------------------------------------------------


@dataclass
class RawPin:
    """Bentuk netral dari hasil kedua library."""

    title: str = ""
    kind: str = "image"  # image | video | gif
    image_url: str | None = None
    poster_url: str | None = None
    videos: list[tuple[int, str]] = field(default_factory=list)  # (tinggi, url mp4)


@dataclass
class PinMedia:
    pin_url: str
    title: str
    kind: str
    media_url: str  # tautan Download (kualitas terbaik yang valid)
    size: int | None
    preview_url: str | None
    # video: (url, ukuran) untuk tiap varian MP4 valid, kualitas tinggi -> rendah
    variants: list[tuple[str, int | None]] = field(default_factory=list)


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _clean_title(t: object) -> str:
    return re.sub(r"\s+", " ", t).strip() if isinstance(t, str) else ""


# --- Library (blocking; dipanggil lewat asyncio.to_thread) ------------------


def _primary_fetch(url: str) -> RawPin | None:
    """pinterest-downloader. Berdasarkan README: `Pinterest().get_pin(url)`
    -> {"ok": bool, "pin": {title, media_type, images{"orig":{url}},
    video{formats[{url,height,...}], poster}}}. Library tidak melempar
    exception; kegagalan = {"ok": False, "error": {...}}."""
    # import dinamis + lazy: bot tetap jalan walau library belum terpasang,
    # dan editor tidak menandai import yang tidak ter-resolve.
    module = importlib.import_module("pinterest_downloader")
    data: Any = module.Pinterest().get_pin(url)
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    pin: Any = data.get("pin")
    if not isinstance(pin, dict):
        return None

    raw_images: Any = pin.get("images")
    images: dict[str, Any] = raw_images if isinstance(raw_images, dict) else {}
    image_url: str | None = None
    for key in ("orig", "736x", "474x", "236x"):
        cand = images.get(key)
        if isinstance(cand, dict) and isinstance(cand.get("url"), str):
            image_url = cand["url"]
            break

    raw_video: Any = pin.get("video")
    video: dict[str, Any] = raw_video if isinstance(raw_video, dict) else {}
    raw_formats: Any = video.get("formats")
    formats: list[Any] = raw_formats if isinstance(raw_formats, list) else []
    videos: list[tuple[int, str]] = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        u = f.get("url")
        if isinstance(u, str) and ".mp4" in u.lower():
            videos.append((_int(f.get("height")), u))

    media_type = str(pin.get("media_type") or "").lower()
    if media_type == "video" or (videos and media_type != "gif"):
        kind = "video"
    elif media_type == "gif" or (image_url or "").lower().endswith(".gif"):
        kind = "gif"
    else:
        kind = "image"

    raw_poster: Any = video.get("poster")
    poster: str | None = raw_poster if isinstance(raw_poster, str) else None
    poster_url: str | None = poster or image_url
    return RawPin(
        title=_clean_title(pin.get("title") or pin.get("description")),
        kind=kind,
        image_url=image_url,
        poster_url=poster_url,
        videos=videos,
    )


# Fallback dipasang terpisah di vendor/pinterest_dl (pip --no-deps --target).
# Paket GitHub-nya bernama "pinterest-downloader", sama dengan primary, jadi
# tidak bisa dipasang di environment yang sama tanpa saling menimpa.
VENDOR_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor",
    "pinterest_dl",
)


def _ensure_vendor_path() -> None:
    # append (bukan insert) agar tidak menimpa paket di site-packages
    if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
        sys.path.append(VENDOR_DIR)


def _fallback_fetch(url: str) -> RawPin | None:
    """pinterest-dl. Berdasarkan README: `PinterestService(output_dir=...,
    preferred_quality="originals").scrape(url).items`, tiap item punya
    `image_url`, `video_url`, `is_video`. Hanya scrape (metadata); folder
    sementara dihapus otomatis."""
    _ensure_vendor_path()
    service = importlib.import_module("pinterest_dl.service")  # lazy + dinamis

    with tempfile.TemporaryDirectory(prefix="senp_pin_") as tmp:
        svc = service.PinterestService(output_dir=tmp, preferred_quality="originals")
        scraped = svc.scrape(url)
        items = list(getattr(scraped, "items", None) or [])

    for item in items[:1]:  # Pin tunggal: cukup item pertama
        image_url = getattr(item, "image_url", None)
        video_url = getattr(item, "video_url", None)
        is_video = bool(getattr(item, "is_video", False) or video_url)
        if is_video:
            vids = [(0, video_url)] if isinstance(video_url, str) else []
            return RawPin(
                kind="video", image_url=image_url, poster_url=image_url, videos=vids
            )
        if isinstance(image_url, str):
            kind = "gif" if image_url.lower().endswith(".gif") else "image"
            return RawPin(kind=kind, image_url=image_url, poster_url=image_url)
    return None


# --- HTTP helper -----------------------------------------------------------


async def _probe(
    session: aiohttp.ClientSession, url: str, want: str
) -> tuple[bool, int | None]:
    """Cek URL media valid (host CDN, status 200/206, content-type sesuai)
    dan ambil ukurannya tanpa mengunduh isinya (Range 0-0)."""
    if not is_valid_media_url(url):
        return False, None
    try:
        async with session.get(
            url,
            headers={**HEADERS, "Range": "bytes=0-0"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        ) as resp:
            if resp.status not in (200, 206):
                return False, None
            ctype = resp.headers.get("Content-Type", "").lower()
            prefix = "video/" if want == "video" else "image/"
            if not ctype.startswith(prefix):
                return False, None
            size = None
            cr = resp.headers.get("Content-Range", "")
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                size = int(cr.rsplit("/", 1)[1])
            elif (
                resp.status == 200 and resp.headers.get("Content-Length", "").isdigit()
            ):
                size = int(resp.headers["Content-Length"])
            return True, size
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False, None


async def build_media(
    session: aiohttp.ClientSession, pin_url: str, raw: RawPin
) -> PinMedia | None:
    """Ubah RawPin jadi PinMedia hanya jika media benar-benar valid."""
    if raw.kind == "video":
        uniq: dict[str, int] = {}
        for h, u in raw.videos:
            uniq.setdefault(u, h)
        cands = sorted(uniq.items(), key=lambda kv: kv[1], reverse=True)[
            :MAX_VIDEO_VARIANTS
        ]
        if not cands:
            return None
        probes = await asyncio.gather(*(_probe(session, u, "video") for u, _ in cands))
        variants = [(u, s) for (u, _), (ok, s) in zip(cands, probes) if ok]
        if not variants:
            return None
        preview = raw.poster_url if is_valid_media_url(raw.poster_url) else None
        return PinMedia(
            pin_url=pin_url,
            title=raw.title,
            kind="video",
            media_url=variants[0][0],
            size=variants[0][1],
            preview_url=preview,
            variants=variants,
        )

    if not raw.image_url:
        return None
    ok, size = await _probe(session, raw.image_url, "image")
    if not ok:
        return None
    return PinMedia(
        pin_url=pin_url,
        title=raw.title,
        kind=raw.kind if raw.kind in ("image", "gif") else "image",
        media_url=raw.image_url,
        size=size,
        preview_url=raw.image_url,
    )


async def fetch_pin(session: aiohttp.ClientSession, url: str) -> PinMedia | None:
    """Primary dulu; fallback hanya jika primary gagal / tidak menghasilkan
    media valid. Masing-masing dicoba tepat satu kali, tidak paralel."""
    for name, fn in (("primary", _primary_fetch), ("fallback", _fallback_fetch)):
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(fn, url), LIB_TIMEOUT)
            if raw is not None:
                media = await build_media(session, url, raw)
                if media is not None:
                    print(f"[pinterest] berhasil via {name} ({media.kind})")
                    return media
            print(f"[pinterest] {name}: tidak ada media valid")
        except asyncio.TimeoutError:
            print(f"[pinterest] {name}: timeout")
        except Exception as e:  # noqa: BLE001 - jangan bocor ke pengguna
            print(f"[pinterest] {name} gagal: {type(e).__name__}: {str(e)[:200]}")
    return None


async def download_bytes(
    session: aiohttp.ClientSession, url: str, limit: int
) -> io.BytesIO | None:
    """Unduh ke memori dengan batas ukuran ketat. None jika gagal/terlalu besar."""
    if not is_valid_media_url(url):
        return None
    try:
        async with session.get(
            url,
            headers=HEADERS,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            if resp.content_length and resp.content_length > limit:
                return None
            buf = io.BytesIO()
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > limit:
                    return None
                buf.write(chunk)
            buf.seek(0)
            return buf
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


# --- Tampilan Discord ------------------------------------------------------


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(n / 1024, 0.1):.0f} KB"


def upload_limit(interaction: discord.Interaction) -> int:
    guild = interaction.guild
    limit = getattr(guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT
    return max(min(limit, HARD_UPLOAD_CAP) - UPLOAD_MARGIN, 0)


def build_embed(media: PinMedia, uploaded: bool) -> discord.Embed:
    embed = discord.Embed(
        title=(media.title or "Pinterest Pin")[:256],
        url=media.pin_url,
        color=EMBED_COLOR,
    )
    embed.add_field(name="Jenis Media", value=KIND_LABEL.get(media.kind, "Media"))
    if media.size:
        embed.add_field(name="Ukuran", value=_fmt_size(media.size))
    if media.kind in ("image", "gif"):
        embed.set_image(url=media.media_url)
    else:
        if not uploaded:
            embed.description = (
                "Video terlalu besar untuk dikirim langsung. "
                "Gunakan tombol **Download**."
            )
            if media.preview_url:
                embed.set_image(url=media.preview_url)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def build_view(media: PinMedia) -> discord.ui.View:
    view = discord.ui.View()
    if len(media.media_url) <= 512:
        view.add_item(
            discord.ui.Button(
                label="Download", style=discord.ButtonStyle.link, url=media.media_url
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Buka Pin Asli", style=discord.ButtonStyle.link, url=media.pin_url
        )
    )
    return view


# --- Command ---------------------------------------------------------------


def setup(bot: Any) -> None:
    @app_commands.command(
        name="pinterest",
        description="Download gambar / video / GIF dari Pin Pinterest publik",
    )
    @app_commands.describe(url="Link Pin (pinterest.com/pin/... atau pin.it/...)")
    @app_commands.checks.cooldown(1, COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def pinterest_cmd(interaction: discord.Interaction, url: str):
        # Validasi dulu (murah) supaya pesan error bisa ephemeral.
        try:
            clean = validate_user_url(url)
        except PinError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        session = bot.http_session
        try:
            try:
                pin_url = await resolve_short(session, clean)
            except PinError as e:
                await interaction.edit_original_response(content=str(e))
                return

            media = await fetch_pin(session, pin_url)
            if media is None:
                await interaction.edit_original_response(content=MSG_FAILED)
                return

            file = None
            if media.kind == "video":
                limit = upload_limit(interaction)
                pick = next(
                    ((u, s) for u, s in media.variants if s is not None and s <= limit),
                    None,
                )
                if pick:
                    buf = await download_bytes(session, pick[0], limit)
                    if buf:
                        file = discord.File(buf, filename="pinterest.mp4")

            kwargs: dict = {
                "content": None,
                "embed": build_embed(media, uploaded=file is not None),
                "view": build_view(media),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if file is not None:
                kwargs["attachments"] = [file]
            await interaction.edit_original_response(**kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"[error] /pinterest: {type(e).__name__}: {str(e)[:200]}")
            try:
                await interaction.edit_original_response(content=MSG_GENERIC)
            except discord.HTTPException:
                pass

    bot.tree.add_command(pinterest_cmd)
