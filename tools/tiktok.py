import asyncio
import os
import re
import tempfile
from urllib.parse import urlparse, urlunsplit

import discord
from discord import app_commands
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

EDIT_INTERVAL = 2.0  # detik minimal antar edit pesan
MAX_DURATION = 15 * 60  # batas durasi video (detik)
HOST_RE = re.compile(r"(^|\.)tiktok\.com$", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#[^\s#]+", re.UNICODE)

COLOR_INFO = discord.Color.from_rgb(254, 44, 85)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()
DOWNLOAD_SLOTS = asyncio.Semaphore(2)  # maksimal 2 download berjalan bersamaan


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
    # Link pendek (vm/vt.tiktok.com) jangan dipotong query-nya.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def base_opts():
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
# yt-dlp (blocking, dijalankan di thread executor)
# ---------------------------------------------------------------------------


def _extract_info(url):
    with YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


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

    opts = {
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

    with YoutubeDL(opts) as ydl:
        ydl.download([url])

    for name in os.listdir(outdir):
        if name.endswith(ext):
            return os.path.join(outdir, name)
    return None


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
    return embed


def progress_embed(info, kind, state):
    phase = state.get("phase", "starting")
    pct = max(0.0, min(100.0, float(state.get("pct") or 0)))
    embed = base_embed(info)
    embed.add_field(name="Format", value=kind_label(kind, info), inline=False)

    if phase == "starting":
        line = "Menyiapkan download..."
    elif phase == "processing":
        line = f"{progress_bar(100)} **100%**\nMemproses (konversi)..."
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
                loop = asyncio.get_running_loop()
                try:
                    path = await loop.run_in_executor(
                        None, _download, url, tmp, kind, state
                    )
                except YoutubeDLError as err:
                    finished.set()
                    await on_fail(explain_error(err))
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


async def handle(interaction: discord.Interaction, url: str, kind: str):
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

    try:
        info = await extract_info(clean_url)
    except Exception as err:  # noqa: BLE001
        await interaction.followup.send(
            embed=error_embed("Gagal memproses link", explain_error(err))
        )
        return

    duration = info.get("duration") or 0
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
    await run_download(
        interaction, message, interaction.user.id, info, clean_url, kind, limit
    )


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: discord.Client):
    @bot.tree.command(name="ttmp4", description="Download video TikTok (MP4)")
    @app_commands.describe(url="Link video TikTok")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ttmp4(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp4")

    @bot.tree.command(name="ttmp3", description="Download audio TikTok (MP3)")
    @app_commands.describe(url="Link video TikTok")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ttmp3(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp3")
