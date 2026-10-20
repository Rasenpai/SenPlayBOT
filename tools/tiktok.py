import asyncio
import os
import re
import tempfile
import time
from typing import Any, Literal, cast
from urllib.parse import quote, urlparse, urlunsplit

import aiohttp
import discord
from discord import app_commands
from yarl import URL
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError

# Pakai ulang helper dari youtube.py (hosting link eksternal, formatter, dll).
from tools.youtube import (
    EXTERNAL_MAX,
    MP3_BITRATE,
    ascii_filename,
    clean_error,
    clip,
    format_bytes,
    format_duration,
    format_eta,
    format_speed,
    progress_bar,
    safe_filename,
    upload_external,
    upload_limit,
)

# Urutan sumber: SocialKit -> API lama -> yt-dlp.
# SocialKit (pilihan pertama). API key dibaca dari .env: SOCIALKIT_API
SOCIALKIT_URL = "https://api.socialkit.dev/tiktok/download"
SOCIALKIT_KEY = os.getenv("SOCIALKIT_API", "").strip()
SOCIALKIT_TIMEOUT = 90  # detik; SocialKit memproses download secara sinkron

# API lama (pilihan kedua).
API_URL = os.getenv("TIKTOK_API_URL", "https://tdownv4.sl-bjs.workers.dev/")
API_TIMEOUT = 20  # detik, untuk ambil info
API_DOWNLOAD_TIMEOUT = 180  # detik, total untuk download file
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Header minimal untuk panggilan ke API (mirip browser biasa, tanpa Referer).
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
}
# Header untuk mengunduh file media dari CDN TikTok.
MEDIA_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.tiktok.com/",
}

Quality = Literal["360p", "480p", "720p", "1080p"]

ALLOWED_GUILD_ID = 1561577570720284705
ALLOWED_CHANNEL_ID = 1561641325613424760
EDIT_INTERVAL = 2.0  # detik minimal antar edit pesan
MAX_DURATION = 15 * 60  # batas durasi video (detik)
HOST_RE = re.compile(r"(^|\.)tiktok\.com$", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#[^\s#]+", re.UNICODE)
VIDEO_ID_RE = re.compile(r"/video/(\d+)")
USERNAME_RE = re.compile(r"/@([^/?#]+)")

COLOR_INFO = discord.Color.from_rgb(254, 44, 85)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()
DOWNLOAD_SLOTS = asyncio.Semaphore(2)  # maksimal 2 download berjalan bersamaan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def check_location(interaction: discord.Interaction) -> bool:
    """Return True kalau command dipakai di server & channel yang benar.
    Kalau salah, kirim pesan ephemeral dan return False."""
    if interaction.guild_id != ALLOWED_GUILD_ID:
        await interaction.response.send_message(
            "Command ini hanya bisa dipakai di server SenPlay resmi. "
            "Silakan bergabung ke server tersebut untuk memakainya.",
            ephemeral=True,
        )
        return False

    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"Command ini hanya bisa dipakai di <#{ALLOWED_CHANNEL_ID}>. "
            f"Silakan kirim ulang command kamu di channel tersebut.",
            ephemeral=True,
        )
        return False

    return True


def normalize_url(url):
    parsed = urlparse(url.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not HOST_RE.search(parsed.hostname)
    ):
        return None
    # Link pendek (vm/vt.tiktok.com) jangan dipotong query-nya.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def api_target_url(url):
    """Link untuk dikirim ke API: buang query kalau link video lengkap."""
    parsed = urlparse(url)
    if "/video/" in parsed.path:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return url


def base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
    }


def explain_error(err):
    text = clean_error(err)
    if re.search(r"unsupported url|photo|no video formats", text, re.IGNORECASE):
        text += "\n\nCatatan: postingan foto/slideshow TikTok belum didukung. Coba link video."
    return text


def compact(n):
    """1234 -> 1.2K, 1500000 -> 1.5M."""
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= limit:
            value = f"{n / limit:.1f}".rstrip("0").rstrip(".")
            return f"{value}{suffix}"
    return str(n)


def get_resolution(info):
    height = info.get("height")
    width = info.get("width")
    if not height:
        heights = [
            (f.get("width"), f.get("height"))
            for f in info.get("formats") or []
            if f.get("height") and f.get("vcodec") not in (None, "none")
        ]
        if heights:
            width, height = max(heights, key=lambda x: x[1])
    if height and width:
        return f"{width}x{height}"
    return f"{height}p" if height else None


def get_music(info):
    track = info.get("track")
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    if track and artist:
        return f"{track} — {artist}"
    return track or artist or None


def get_hashtags(info):
    tags = HASHTAG_RE.findall(info.get("description") or "")
    seen, result = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            result.append(t)
    return " ".join(result[:15]) or None


def get_stats(info):
    parts = [
        ("👁", compact(info.get("view_count"))),
        ("❤️", compact(info.get("like_count"))),
        ("💬", compact(info.get("comment_count"))),
        ("🔁", compact(info.get("repost_count"))),
        ("🔖", compact(info.get("save_count"))),
    ]
    text = "  ".join(f"{icon} {val}" for icon, val in parts if val)
    return text or None


# ---------------------------------------------------------------------------
# SocialKit (pilihan pertama)
# ---------------------------------------------------------------------------


async def socialkit_extract(url, kind, quality):
    """Minta SocialKit memproses video, lalu bentuk hasilnya mirip dict info
    yt-dlp supaya embed builder tetap bisa dipakai.

    Struktur respons (sesuai docs.socialkit.dev):
      {"success": true, "data": {"title", "durationSeconds", "thumbnail",
       "downloadUrl", "fileSize", "format", "quality", ...}}
    Error: {"success": false, "message": ..., "errorCode"?: ..., "retryable"?: ...}
    """
    if not SOCIALKIT_KEY:
        raise RuntimeError("SOCIALKIT_API belum diatur di .env")

    payload = {"access_key": SOCIALKIT_KEY, "url": url, "format": kind}
    if kind == "mp4":
        payload["quality"] = quality  # diabaikan SocialKit untuk format audio

    timeout = aiohttp.ClientTimeout(total=SOCIALKIT_TIMEOUT, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(SOCIALKIT_URL, json=payload) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                body = None

    if not isinstance(body, dict):
        raise RuntimeError(f"SocialKit HTTP {status}: respons tidak valid")

    if status != 200 or not body.get("success"):
        message = str(body.get("message") or "tanpa pesan")
        code = body.get("errorCode") or body.get("code")
        label = f"SocialKit HTTP {status}" + (f" [{code}]" if code else "")
        raise RuntimeError(f"{label}: {message}")

    data = body.get("data")
    if not isinstance(data, dict) or not data.get("downloadUrl"):
        raise RuntimeError("SocialKit tidak mengembalikan link download")

    title = str(data.get("title") or "").strip()
    got_quality = str(data.get("quality") or quality)
    height = None
    if kind == "mp4" and re.fullmatch(r"\d+p", got_quality):
        height = int(got_quality[:-1])

    video_id = VIDEO_ID_RE.search(url)
    user = USERNAME_RE.search(url)
    username = user.group(1) if user else None

    return {
        "_source": "socialkit",
        "id": video_id.group(1) if video_id else None,
        "title": title or "Video TikTok",
        "description": title,
        "uploader": username,
        "uploader_url": f"https://www.tiktok.com/@{username}" if username else None,
        "duration": data.get("durationSeconds"),
        "thumbnail": data.get("thumbnail"),
        "height": height,
        "webpage_url": url,
        "media_url": data["downloadUrl"],
    }


# ---------------------------------------------------------------------------
# API lama (pilihan kedua)
# ---------------------------------------------------------------------------


def _pick(data, key):
    """Ambil field dari level atas, kalau tidak ada coba di dalam 'author'."""
    value = data.get(key)
    if value is None:
        value = (data.get("author") or {}).get(key)
    return value


async def _api_request(session, request_url):
    async with session.get(request_url) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200].replace("\n", " ")
            raise RuntimeError(f"API HTTP {resp.status}: {body}")
        return await resp.json(content_type=None)


async def api_extract(url):
    """Ambil info + link download dari API eksternal, hasilnya dibentuk
    mirip dict info milik yt-dlp supaya embed builder tetap bisa dipakai."""
    target = api_target_url(url)
    # Percobaan 1: URL mentah (persis seperti di browser).
    # Percobaan 2: URL ter-encode.
    attempts = [
        URL(f"{API_URL}?down={target}", encoded=True),
        URL(f"{API_URL}?down={quote(target, safe='')}", encoded=True),
    ]

    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    data = None
    last_err = None
    async with aiohttp.ClientSession(timeout=timeout, headers=API_HEADERS) as session:
        for request_url in attempts:
            try:
                data = await _api_request(session, request_url)
                break
            except Exception as err:  # noqa: BLE001
                last_err = err
                print(f"[tiktok] API request gagal ({request_url}): {err}")

    if data is None:
        raise RuntimeError(str(last_err) if last_err else "API tidak merespons")

    if not isinstance(data, dict):
        raise RuntimeError("Respons API tidak valid")

    content_type = (data.get("content_type") or "video").lower()
    if content_type != "video":
        raise RuntimeError(f"Tipe konten tidak didukung: {content_type}")

    download_url = data.get("download_url")
    audio_url = _pick(data, "audio_url")
    if not download_url and not audio_url:
        raise RuntimeError("API tidak mengembalikan link download")

    author = data.get("author") or {}
    username = author.get("username")
    title = (data.get("title") or "").strip()

    return {
        "_source": "api",
        "id": data.get("video_id"),
        "title": title or "Video TikTok",
        "description": title,
        "uploader": username,
        "creator": author.get("nickname") or username,
        "uploader_url": f"https://www.tiktok.com/@{username}" if username else None,
        "avatar": author.get("avatar"),
        "duration": _pick(data, "duration"),
        "view_count": _pick(data, "view_count"),
        "like_count": _pick(data, "like_count"),
        "comment_count": _pick(data, "comment_count"),
        "webpage_url": url,
        "download_url": download_url,
        "audio_url": audio_url,
    }


async def api_download(info, outdir, kind, state):
    """Download file langsung dari link yang diberikan SocialKit / API lama."""
    src = info.get("media_url") or (
        info.get("download_url") if kind == "mp4" else info.get("audio_url")
    )
    if not src:
        raise RuntimeError("Sumber tidak menyediakan link untuk format ini")

    # Referer TikTok hanya perlu untuk CDN TikTok (API lama), bukan link SocialKit.
    headers = MEDIA_HEADERS if info.get("_source") == "api" else {"User-Agent": USER_AGENT}

    path = os.path.join(outdir, f"{info.get('id') or 'tiktok'}.{kind}")
    timeout = aiohttp.ClientTimeout(total=API_DOWNLOAD_TIMEOUT, sock_connect=15)
    done = 0

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(URL(src, encoded=True)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download HTTP {resp.status}")

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/" in ctype or "json" in ctype:
                raise RuntimeError("Link download tidak mengembalikan file media")

            total = resp.content_length or 0
            if total and total > EXTERNAL_MAX:
                raise RuntimeError("File melebihi batas ukuran maksimal")

            started = time.monotonic()
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if done > EXTERNAL_MAX:
                        raise RuntimeError("File melebihi batas ukuran maksimal")
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = done / elapsed
                    state.update(
                        phase="downloading",
                        pct=(done / total * 100) if total else 0,
                        speed=speed,
                        eta=((total - done) / speed) if total and speed else None,
                    )

    if done <= 0:
        raise RuntimeError("File hasil download kosong")
    state.update(phase="processing", pct=100)
    return path


# ---------------------------------------------------------------------------
# yt-dlp (cadangan terakhir; blocking, dijalankan di thread executor)
# ---------------------------------------------------------------------------


def _extract_info(url) -> dict[str, Any]:
    opts: dict[str, Any] = {**base_opts(), "skip_download": True}
    with YoutubeDL(cast(Any, opts)) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp tidak mengembalikan info video")
    return cast(dict[str, Any], info)


async def extract_info(url):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_info, url)


def _download(url, outdir, kind, state):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            state.update(
                phase="downloading",
                pct=(done / total * 100) if total else 0,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            state.update(phase="processing", pct=100)

    opts: dict[str, Any] = {
        **base_opts(),
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
    }

    if kind == "mp4":
        ext = ".mp4"
        # Utamakan H.264 supaya bisa diputar di Discord / semua perangkat.
        opts["format"] = "bv*[vcodec^=h264]+ba/b[vcodec^=h264]/b[ext=mp4]/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ]
    else:
        ext = ".mp3"
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(MP3_BITRATE),
            }
        ]

    with YoutubeDL(cast(Any, opts)) as ydl:
        ydl.download([url])

    for name in os.listdir(outdir):
        if name.endswith(ext):
            return os.path.join(outdir, name)
    return None


# ---------------------------------------------------------------------------
# Strategi: SocialKit -> API lama -> yt-dlp
# ---------------------------------------------------------------------------


async def get_info(url, kind, quality):
    try:
        return await socialkit_extract(url, kind, quality)
    except Exception as err:  # noqa: BLE001
        print(f"[tiktok] SocialKit info gagal, pakai API lama: {err}")

    try:
        return await api_extract(url)
    except Exception as err:  # noqa: BLE001
        print(f"[tiktok] API lama info gagal, pakai yt-dlp: {err}")

    return await extract_info(url)


def _reset_download(outdir, state):
    """Bersihkan file parsial dan reset progress sebelum mencoba sumber lain."""
    state.update(phase="starting", pct=0, speed=None, eta=None)
    for name in os.listdir(outdir):
        try:
            os.remove(os.path.join(outdir, name))
        except OSError:
            pass


async def fetch_file(info, url, outdir, kind, state):
    source = info.get("_source")

    if source == "socialkit":
        try:
            return await api_download(info, outdir, kind, state)
        except Exception as err:  # noqa: BLE001
            print(f"[tiktok] SocialKit download gagal, pakai API lama: {err}")
            _reset_download(outdir, state)

        # Ambil link baru dari API lama.
        try:
            legacy = await api_extract(url)
            return await api_download(legacy, outdir, kind, state)
        except Exception as err:  # noqa: BLE001
            print(f"[tiktok] API lama download gagal, pakai yt-dlp: {err}")
            _reset_download(outdir, state)

    elif source == "api":
        try:
            return await api_download(info, outdir, kind, state)
        except Exception as err:  # noqa: BLE001
            print(f"[tiktok] API lama download gagal, pakai yt-dlp: {err}")
            _reset_download(outdir, state)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download, url, outdir, kind, state)


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def kind_label(kind, info=None):
    if kind == "mp3":
        return f"MP3 · {MP3_BITRATE} kbps"
    res = get_resolution(info or {})
    return f"MP4 · {res}" if res else "MP4 · Video"


def base_embed(info, color=COLOR_INFO):
    caption = (info.get("description") or info.get("title") or "Video TikTok").strip()
    embed = discord.Embed(
        title=clip(caption.splitlines()[0] if caption else "Video TikTok", 256),
        url=info.get("webpage_url"),
        color=color,
    )

    username = info.get("uploader") or info.get("uploader_id")
    nickname = info.get("creator") or info.get("channel") or username
    if nickname:
        author = nickname
        if username and username != nickname:
            author += f" (@{str(username).lstrip('@')})"
        embed.set_author(
            name=clip(author, 256),
            url=info.get("uploader_url") or info.get("channel_url"),
        )

    stats = get_stats(info)
    if stats:
        embed.add_field(name="Statistik", value=stats, inline=False)

    duration = format_duration(info.get("duration"))
    if duration:
        embed.add_field(name="Durasi", value=duration, inline=True)

    ts = info.get("timestamp")
    if ts:
        embed.add_field(
            name="Diunggah", value=f"<t:{int(ts)}:D> (<t:{int(ts)}:R>)", inline=True
        )
    elif info.get("upload_date"):
        d = info["upload_date"]
        embed.add_field(
            name="Diunggah", value=f"{d[6:8]}-{d[4:6]}-{d[0:4]}", inline=True
        )

    res = get_resolution(info)
    if res:
        embed.add_field(name="Resolusi", value=res, inline=True)

    music = get_music(info)
    if music:
        embed.add_field(name="Musik", value=clip(music, 1024), inline=False)

    tags = get_hashtags(info)
    if tags:
        embed.add_field(name="Hashtag", value=clip(tags, 1024), inline=False)

    if info.get("thumbnail"):
        embed.set_image(url=info["thumbnail"])
    elif info.get("avatar"):
        embed.set_thumbnail(url=info["avatar"])
    return embed


def progress_embed(info, kind, state):
    phase = state.get("phase", "starting")
    pct = max(0.0, min(100.0, float(state.get("pct") or 0)))
    embed = base_embed(info)
    embed.add_field(name="Format", value=kind_label(kind, info), inline=False)

    if phase == "starting":
        line = "Menyiapkan download..."
    elif phase == "processing":
        line = f"{progress_bar(100)} **100%**\nMemproses..."
    elif phase == "uploading":
        line = (
            f"{progress_bar(100)} **100%**\n"
            "Ukuran melebihi batas Discord, mengunggah ke link download..."
        )
    else:
        line = (
            f"{progress_bar(pct)} **{round(pct)}%**\nMengunduh...\n"
            f"{format_speed(state.get('speed'))} • ETA {format_eta(state.get('eta'))}"
        )
    embed.add_field(name="Progress", value=line, inline=False)
    if info.get("id"):
        embed.set_footer(text=f"ID: {info['id']}")
    return embed


def done_embed(info, kind, size, link=None, note=None):
    embed = base_embed(info, COLOR_OK)
    embed.add_field(name="Format", value=kind_label(kind, info), inline=True)
    embed.add_field(name="Ukuran", value=format_bytes(size), inline=True)
    if link:
        embed.add_field(
            name="Download",
            value=f"[Klik di sini untuk mengunduh]({link})",
            inline=False,
        )
        footer = f"Selesai. {note or 'Link bersifat sementara.'}"
    else:
        footer = "Selesai. File dikirim di pesan di bawah."
    if info.get("id"):
        footer += f" • ID: {info['id']}"
    embed.set_footer(text=footer)
    return embed


def error_embed(title, description):
    return discord.Embed(
        title=title, description=clip(description, 4000), color=COLOR_ERR
    )


# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------


async def run_download(interaction, message, owner_id, info, url, kind, limit):
    async def on_fail(error):
        await message.edit(embed=error_embed("Download gagal", error), view=None)

    if owner_id in ACTIVE_USERS:
        await on_fail("Kamu masih punya download yang berjalan.")
        return
    ACTIVE_USERS.add(owner_id)

    state = {"phase": "starting", "pct": 0}
    finished = asyncio.Event()

    async def updater():
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=EDIT_INTERVAL)
            except asyncio.TimeoutError:
                pass
            if finished.is_set():
                break
            try:
                await message.edit(embed=progress_embed(info, kind, state))
            except discord.HTTPException:
                pass

    updater_task = asyncio.create_task(updater())

    try:
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="tiktok_") as tmp:
                try:
                    path = await fetch_file(info, url, tmp, kind, state)
                except YoutubeDLError as err:
                    finished.set()
                    await on_fail(explain_error(err))
                    return
                except RuntimeError as err:
                    finished.set()
                    await on_fail(str(err))
                    return

                finished.set()

                if not path or not os.path.isfile(path):
                    await on_fail("File hasil download tidak ditemukan.")
                    return

                size = os.path.getsize(path)
                sent_direct = False

                # 1) Di bawah batas: kirim langsung sebagai file Discord.
                if size <= limit:
                    name_base = info.get("title") or info.get("id") or "tiktok"
                    filename = f"{safe_filename(name_base)}.{kind}"
                    try:
                        await interaction.followup.send(
                            file=discord.File(path, filename=filename)
                        )
                        sent_direct = True
                    except discord.HTTPException as err:
                        print(f"[tiktok] Upload ke Discord gagal, pakai link: {err}")

                if sent_direct:
                    await message.edit(embed=done_embed(info, kind, size), view=None)
                    return

                # 2) Melebihi batas (atau upload Discord gagal): kirim sebagai link.
                state.update(phase="uploading", pct=100)
                try:
                    await message.edit(embed=progress_embed(info, kind, state))
                except discord.HTTPException:
                    pass

                try:
                    link, host, note = await upload_external(
                        path,
                        ascii_filename(info.get("title") or info.get("id"), kind),
                        size,
                    )
                except RuntimeError as err:
                    await on_fail(str(err))
                    return

                await message.edit(
                    embed=done_embed(info, kind, size, link, note), view=None
                )
    except Exception as err:  # noqa: BLE001
        finished.set()
        try:
            await on_fail(clean_error(err))
        except discord.HTTPException:
            pass
    finally:
        finished.set()
        updater_task.cancel()
        ACTIVE_USERS.discard(owner_id)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


async def handle(
    interaction: discord.Interaction, url: str, kind: str, quality: str = "480p"
):
    # 0) Batasi hanya di guild & channel yang diizinkan.
    if not await check_location(interaction):
        return

    # 1) Validasi link.
    clean_url = normalize_url(url)
    if not clean_url:
        await interaction.response.send_message(
            "Link tidak valid. Tempel link lengkap dari TikTok "
            "(tiktok.com / vm.tiktok.com / vt.tiktok.com).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    limit = upload_limit(interaction)

    # 2) Ambil info video (SocialKit -> API lama -> yt-dlp).
    try:
        info = await get_info(clean_url, kind, quality)
    except Exception as err:  # noqa: BLE001
        await interaction.followup.send(
            embed=error_embed("Gagal memproses link", explain_error(err))
        )
        return

    try:
        duration = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    # 3) Cek batas durasi dan perkiraan ukuran.
    problem = None
    if duration > MAX_DURATION:
        problem = (
            "Video terlalu panjang",
            f"Durasi maksimal {format_duration(MAX_DURATION)}, "
            f"video ini {format_duration(duration)}.",
        )
    elif kind == "mp3" and duration * MP3_BITRATE * 1000 / 8 > EXTERNAL_MAX:
        problem = (
            "File terlalu besar",
            "Perkiraan ukuran MP3 melebihi batas maksimal yang bisa dikirim via link.",
        )

    if problem:
        await interaction.followup.send(embed=error_embed(*problem))
        return

    # 4) Mulai download.
    message = await interaction.followup.send(
        embed=progress_embed(info, kind, {}), wait=True
    )
    await run_download(
        interaction, message, interaction.user.id, info, clean_url, kind, limit
    )


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: Any):
    @bot.tree.command(name="ttmp4", description="Download video TikTok (MP4)")
    @app_commands.describe(
        url="Link video TikTok", kualitas="Kualitas video (default 480p)"
    )
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ttmp4(
        interaction: discord.Interaction, url: str, kualitas: Quality = "480p"
    ):
        await handle(interaction, url, "mp4", kualitas)

    @bot.tree.command(name="ttmp3", description="Download audio TikTok (MP3)")
    @app_commands.describe(url="Link video TikTok")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ttmp3(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp3")