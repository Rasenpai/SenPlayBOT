import asyncio
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse, urlunsplit

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError

# Pakai ulang helper dari youtube.py & tiktok.py.
from tools.tiktok import compact
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

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_COOKIES_NAME = "instagram_cookies.txt"

IG_API_ENDPOINT = os.getenv(
    "IG_API_ENDPOINT", "https://www.sankavollerei.web.id/download/instagram"
).strip()
IG_API_KEY = os.getenv("IG_API_KEY", "").strip()


def resolve_cookies():
    raw = os.getenv("IG_COOKIES", "").strip() or DEFAULT_COOKIES_NAME
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path) if path.is_file() else None


COOKIES_FILE = resolve_cookies()
print(
    f"[instagram] Cookies ditemukan: {COOKIES_FILE}"
    if COOKIES_FILE
    else f"[instagram] Cookies tidak ada (dicari: {BASE_DIR / DEFAULT_COOKIES_NAME})."
)
print(
    "[instagram] Mode cadangan API aktif."
    if IG_API_KEY
    else "[instagram] IG_API_KEY kosong, mode cadangan API nonaktif."
)

ALLOWED_CHANNEL_ID = 1561641498523729940  # command Instagram hanya di channel ini
EDIT_INTERVAL = 2.0
MAX_DURATION = 10 * 60
MAX_ITEMS = 10
HOST_RE = re.compile(r"(^|\.)(instagram\.com|instagr\.am)$", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#[^\s#]+", re.UNICODE)
LOGIN_RE = re.compile(
    r"login|log in|rate-limit|empty media|cookies|restricted|private", re.IGNORECASE
)

COLOR_INFO = discord.Color.from_rgb(225, 48, 108)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()
DOWNLOAD_SLOTS = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_url(url):
    parsed = urlparse(url.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not HOST_RE.search(parsed.hostname)
    ):
        return None
    # Buang query/fragment (igsh, utm, dll).
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def explain_error(err):
    text = clean_error(err)
    if LOGIN_RE.search(text):
        text += (
            "\n\nCatatan: Instagram meminta login. Taruh cookies Instagram "
            "(format Netscape) di `instagram_cookies.txt` sejajar bot.py, "
            "lalu restart bot."
        )
    return text


def flatten(info):
    """Carousel -> (info_utama, daftar_entri)."""
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return info, [info]
    head = {**entries[0]}
    for k in (
        "title",
        "description",
        "uploader",
        "uploader_id",
        "uploader_url",
        "webpage_url",
        "timestamp",
        "upload_date",
        "like_count",
        "comment_count",
        "view_count",
        "channel",
        "id",
    ):
        if info.get(k):
            head[k] = info[k]
    head["_count"] = len(entries)
    return head, entries


def get_resolution(info):
    h, w = info.get("height"), info.get("width")
    if not h:
        sizes = [
            (f.get("width"), f.get("height"))
            for f in info.get("formats") or []
            if f.get("height") and f.get("vcodec") not in (None, "none")
        ]
        if sizes:
            w, h = max(sizes, key=lambda x: x[1])
    if h and w:
        return f"{w}x{h}"
    return f"{h}p" if h else None


def get_music(info):
    track = info.get("track")
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    if track and artist:
        return f"{track} — {artist}"
    return track or artist or None


def get_hashtags(info):
    seen, out = set(), []
    for t in HASHTAG_RE.findall(info.get("description") or ""):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return " ".join(out[:15]) or None


def get_stats(info):
    parts = [
        ("👁", compact(info.get("view_count"))),
        ("❤️", compact(info.get("like_count"))),
        ("💬", compact(info.get("comment_count"))),
    ]
    return "  ".join(f"{i} {v}" for i, v in parts if v) or None


# ---------------------------------------------------------------------------
# yt-dlp (blocking, dijalankan di thread executor)
# ---------------------------------------------------------------------------


@contextmanager
def cookie_opts(use_cookies):
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "playlist_items": f"1-{MAX_ITEMS}",
    }
    tmp_cookie = None
    try:
        if use_cookies and COOKIES_FILE:
            fd, tmp_cookie = tempfile.mkstemp(prefix="igcookie_", suffix=".txt")
            os.close(fd)
            shutil.copyfile(COOKIES_FILE, tmp_cookie)
            opts["cookiefile"] = tmp_cookie
        yield opts
    finally:
        if tmp_cookie and os.path.exists(tmp_cookie):
            try:
                os.remove(tmp_cookie)
            except OSError:
                pass


def with_fallback(fn):
    order = ([True] if COOKIES_FILE else []) + [False]
    last = None
    for use_cookies in order:
        try:
            return fn(use_cookies)
        except YoutubeDLError as err:
            last = err
            print(f"[instagram] cookies={use_cookies} gagal: {clean_error(err)[:120]}")
    raise last or RuntimeError("Gagal memproses link.")


def _extract_info(url):
    def attempt(use_cookies):
        with cookie_opts(use_cookies) as opts:
            with YoutubeDL(cast(Any, {**opts, "skip_download": True})) as ydl:
                return ydl.extract_info(url, download=False)

    return with_fallback(attempt)


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

    ext = ".mp4" if kind == "mp4" else ".mp3"

    def attempt(use_cookies):
        with cookie_opts(use_cookies) as base:
            opts: dict[str, Any] = {
                **base,
                "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
                "progress_hooks": [hook],
            }
            if kind == "mp4":
                opts["format"] = "bv*+ba/b"
                opts["merge_output_format"] = "mp4"
            else:
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": str(MP3_BITRATE),
                    }
                ]
            state.update(phase="starting", pct=0)
            with YoutubeDL(cast(Any, opts)) as ydl:
                ydl.download([url])

    with_fallback(attempt)
    return sorted(
        os.path.join(outdir, n) for n in os.listdir(outdir) if n.endswith(ext)
    )


# ---------------------------------------------------------------------------
# API cadangan (endpoint yang sama dengan Tool.jsx)
# ---------------------------------------------------------------------------


def detect_kind(media_url, fallback):
    m = re.search(r"[?&]type=(video|image)\b", media_url)
    if m:
        return m.group(1)
    return "video" if fallback == "video" else "image"


async def api_lookup(url):
    if not IG_API_KEY:
        raise RuntimeError("API cadangan tidak aktif (IG_API_KEY kosong).")
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            IG_API_ENDPOINT, params={"apikey": IG_API_KEY, "url": url}
        ) as res:
            if res.status != 200:
                raise RuntimeError(f"API cadangan membalas status {res.status}.")
            data = await res.json(content_type=None)

    result = (data or {}).get("result") or {}
    media = result.get("media")
    if not data.get("status") or not isinstance(media, list) or not media:
        raise RuntimeError(
            (data or {}).get("message")
            or "Media tidak ditemukan. Pastikan postingan bersifat publik."
        )

    items = [
        (m["url"], detect_kind(m["url"], m.get("type")), m.get("thumbnail"))
        for m in media
        if m.get("url")
    ][:MAX_ITEMS]
    info = {
        "id": None,
        "webpage_url": url,
        "uploader": result.get("author"),
        "description": result.get("caption"),
        "title": (
            (result.get("caption") or "Instagram").strip().splitlines()[0]
            if result.get("caption")
            else "Instagram"
        ),
        "thumbnail": next((t for _, _, t in items if t), None),
        "_count": len(items),
        "_api": True,
    }
    return info, items


async def api_fetch(items, tmp, state):
    paths = []
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i, (media_url, kind, _) in enumerate(items, 1):
            async with session.get(media_url) as res:
                if res.status != 200:
                    raise RuntimeError(
                        f"Gagal mengunduh media {i} (status {res.status})."
                    )
                ctype = res.headers.get("Content-Type", "")
                if kind == "video" or "video" in ctype:
                    ext = "mp4"
                elif "png" in ctype:
                    ext = "png"
                elif "webp" in ctype:
                    ext = "webp"
                else:
                    ext = "jpg"
                path = os.path.join(tmp, f"ig_{i}.{ext}")
                with open(path, "wb") as f:
                    async for chunk in res.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                paths.append(path)
            state.update(phase="downloading", pct=i / len(items) * 100)
    return paths


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def kind_label(kind, info=None):
    info = info or {}
    if kind == "mp3":
        return f"MP3 · {MP3_BITRATE} kbps"
    if info.get("_api"):
        return "Foto / Video"
    res = get_resolution(info)
    return f"MP4 · {res}" if res else "MP4 · Video"


def base_embed(info, color=COLOR_INFO):
    caption = (info.get("description") or info.get("title") or "Instagram").strip()
    embed = discord.Embed(
        title=clip(caption.splitlines()[0] if caption else "Instagram", 256),
        url=info.get("webpage_url"),
        color=color,
    )

    username = info.get("uploader_id") or info.get("channel")
    name = info.get("uploader") or username
    if name:
        author = name
        if username and username != name:
            author += f" (@{str(username).lstrip('@')})"
        profile = info.get("uploader_url") or (
            f"https://www.instagram.com/{str(username).lstrip('@')}/"
            if username
            else None
        )
        embed.set_author(name=clip(author, 256), url=profile)

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

    res = None if info.get("_api") else get_resolution(info)
    if res:
        embed.add_field(name="Resolusi", value=res, inline=True)

    if info.get("_count", 1) > 1:
        embed.add_field(name="Slide", value=f"{info['_count']} item", inline=True)

    music = get_music(info)
    if music:
        embed.add_field(name="Musik", value=clip(music, 1024), inline=False)

    tags = get_hashtags(info)
    if tags:
        embed.add_field(name="Hashtag", value=clip(tags, 1024), inline=False)

    if info.get("thumbnail"):
        embed.set_image(url=info["thumbnail"])
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
        line = f"{progress_bar(pct)} **{round(pct)}%**\nMengunduh..."
        if state.get("speed"):
            line += f"\n{format_speed(state.get('speed'))} • ETA {format_eta(state.get('eta'))}"
    embed.add_field(name="Progress", value=line, inline=False)
    if info.get("id"):
        embed.set_footer(text=f"ID: {info['id']}")
    return embed


def done_embed(info, kind, total, direct, links, note):
    embed = base_embed(info, COLOR_OK)
    embed.add_field(name="Format", value=kind_label(kind, info), inline=True)
    embed.add_field(name="Ukuran", value=format_bytes(total), inline=True)
    if links:
        if len(links) == 1:
            text = f"[Klik di sini untuk mengunduh]({links[0]})"
        else:
            text = "\n".join(f"[Unduh {i}]({u})" for i, u in enumerate(links, 1))
        embed.add_field(name="Download", value=clip(text, 1024), inline=False)
    footer = (
        f"Selesai. {note or 'Link bersifat sementara.'}"
        if links and not direct
        else "Selesai. File dikirim di pesan di bawah."
    )
    if links and direct:
        footer = "Selesai. Sebagian file dikirim langsung, sisanya via link sementara."
    if info.get("id"):
        footer += f" • ID: {info['id']}"
    embed.set_footer(text=footer)
    return embed


def error_embed(title, description):
    return discord.Embed(
        title=title, description=clip(description, 4000), color=COLOR_ERR
    )


# ---------------------------------------------------------------------------
# Pengiriman file (langsung ke Discord, atau link kalau melebihi batas)
# ---------------------------------------------------------------------------


async def deliver(interaction, files, info, limit, state):
    base = safe_filename(info.get("title") or info.get("id") or "instagram")
    multi = len(files) > 1

    def name_of(i, p):
        suffix = Path(p).suffix
        return f"{base}_{i + 1}{suffix}" if multi else f"{base}{suffix}"

    direct, external = [], []
    for i, p in enumerate(files):
        (direct if os.path.getsize(p) <= limit else external).append((i, p))

    batches, cur, cur_size = [], [], 0
    for item in direct:
        s = os.path.getsize(item[1])
        if cur and (len(cur) >= 10 or cur_size + s > limit):
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append(item)
        cur_size += s
    if cur:
        batches.append(cur)

    sent = 0
    for batch in batches:
        try:
            await interaction.followup.send(
                files=[discord.File(p, filename=name_of(i, p)) for i, p in batch]
            )
            sent += len(batch)
        except discord.HTTPException as err:
            print(f"[instagram] Upload ke Discord gagal, pakai link: {err}")
            external.extend(batch)

    links, errors, note = [], [], None
    if external:
        state.update(phase="uploading", pct=100)
        for i, p in sorted(external):
            ext = Path(p).suffix.lstrip(".")
            try:
                link, _host, note = await upload_external(
                    p, ascii_filename(f"{base}_{i + 1}", ext), os.path.getsize(p)
                )
                links.append(link)
            except RuntimeError as err:
                errors.append(str(err))

    total = sum(os.path.getsize(p) for p in files)
    return sent, links, errors, note, total


# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------


async def run_job(interaction, message, owner_id, info, kind, produce, limit):
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
            with tempfile.TemporaryDirectory(prefix="ig_") as tmp:
                try:
                    files = await produce(tmp, state)
                except YoutubeDLError as err:
                    finished.set()
                    await on_fail(explain_error(err))
                    return
                except RuntimeError as err:
                    finished.set()
                    await on_fail(str(err))
                    return

                if not files:
                    finished.set()
                    await on_fail("File hasil download tidak ditemukan.")
                    return

                sent, links, errors, note, total = await deliver(
                    interaction, files, info, limit, state
                )
                finished.set()

                if not sent and not links:
                    await on_fail(errors[0] if errors else "Gagal mengirim file.")
                    return

                embed = done_embed(info, kind, total, sent, links, note)
                if errors:
                    embed.add_field(
                        name="Sebagian gagal",
                        value=clip("\n".join(errors), 1024),
                        inline=False,
                    )
                await message.edit(embed=embed, view=None)
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


async def run_api_fallback(interaction, url, limit, first_error):
    """Mode cadangan untuk /igmp4 lewat API (foto, carousel, atau post yang minta login)."""
    try:
        info, items = await api_lookup(url)
    except Exception as err:  # noqa: BLE001
        await interaction.followup.send(
            embed=error_embed(
                "Gagal memproses link",
                f"{explain_error(first_error)}\n\nAPI cadangan: {clip(err, 300)}",
            )
        )
        return

    message = await interaction.followup.send(
        embed=progress_embed(info, "mp4", {}), wait=True
    )

    async def produce(tmp, state):
        return await api_fetch(items, tmp, state)

    await run_job(
        interaction, message, interaction.user.id, info, "mp4", produce, limit
    )


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


async def handle(interaction: discord.Interaction, url: str, kind: str):
    # Batasi hanya di channel yang diizinkan.
    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"Command ini hanya bisa dipakai di <#{ALLOWED_CHANNEL_ID}>. "
            "Silakan kirim ulang command kamu di channel tersebut.",
            ephemeral=True,
        )
        return

    clean_url = normalize_url(url)
    if not clean_url:
        await interaction.response.send_message(
            "Link tidak valid. Tempel link lengkap dari Instagram "
            "(instagram.com/p/..., /reel/..., /tv/...).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    limit = upload_limit(interaction)

    try:
        raw = await extract_info(clean_url)
    except Exception as err:  # noqa: BLE001
        if kind == "mp4" and IG_API_KEY:
            await run_api_fallback(interaction, clean_url, limit, err)
            return
        await interaction.followup.send(
            embed=error_embed("Gagal memproses link", explain_error(err))
        )
        return

    info, entries = flatten(raw)

    duration = sum(e.get("duration") or 0 for e in entries)
    if duration > MAX_DURATION:
        await interaction.followup.send(
            embed=error_embed(
                "Video terlalu panjang",
                f"Durasi maksimal {format_duration(MAX_DURATION)}, "
                f"video ini {format_duration(duration)}.",
            )
        )
        return

    if kind == "mp3" and duration * MP3_BITRATE * 1000 / 8 > EXTERNAL_MAX:
        await interaction.followup.send(
            embed=error_embed(
                "File terlalu besar",
                "Perkiraan ukuran MP3 melebihi batas maksimal yang bisa dikirim via link.",
            )
        )
        return

    message = await interaction.followup.send(
        embed=progress_embed(info, kind, {}), wait=True
    )

    async def produce(tmp, state):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _download, clean_url, tmp, kind, state)

    await run_job(interaction, message, interaction.user.id, info, kind, produce, limit)


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: Any):
    @bot.tree.command(name="igmp4", description="Download video/foto Instagram (MP4)")
    @app_commands.describe(url="Link post / reel Instagram")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def igmp4(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp4")

    @bot.tree.command(name="igmp3", description="Download audio Instagram (MP3)")
    @app_commands.describe(url="Link post / reel Instagram")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def igmp3(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp3")
