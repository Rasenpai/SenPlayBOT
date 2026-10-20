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

load_dotenv()

# Folder project (tempat bot.py berada), bukan folder kerja terminal.
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_COOKIES_NAME = "youtube_cookies.txt"


def resolve_cookies():
    """Cari file cookies. Default: youtube_cookies.txt sejajar bot.py.

    YTDLP_COOKIES di .env boleh diisi untuk override (path relatif dihitung
    dari folder project, atau path absolut).
    """
    raw = os.getenv("YTDLP_COOKIES", "").strip() or DEFAULT_COOKIES_NAME
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path) if path.is_file() else None


COOKIES_FILE = resolve_cookies()
if COOKIES_FILE:
    print(f"[youtube] Cookies ditemukan: {COOKIES_FILE}")
else:
    print(
        f"[youtube] Cookies TIDAK ditemukan (dicari: {BASE_DIR / DEFAULT_COOKIES_NAME}). "
        "Lanjut tanpa cookies."
    )

ALLOWED_CHANNEL_ID = 1561641470027636796  # command YouTube hanya di channel ini
EDIT_INTERVAL = 2.0  # detik minimal antar edit pesan
MAX_DURATION = 60 * 60  # batas durasi video (detik)
MAX_HEIGHT = 1080  # kualitas video tertinggi yang ditawarkan
MP3_BITRATE = 192  # kbps
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Error yang memicu percobaan ulang dengan varian lain.
RETRY_RE = re.compile(
    r"needs to be reloaded|sign in to confirm|not a bot|"
    r"requested format is not available|player response|"
    r"HTTP Error 403|cookies? .*(invalid|expired|no longer)",
    re.IGNORECASE,
)

# Urutan percobaan. Varian yang pernah berhasil dicoba duluan.
VARIANTS = []
if COOKIES_FILE:
    VARIANTS.append({"cookies": True, "clients": None})
VARIANTS += [
    {"cookies": False, "clients": None},
    {"cookies": False, "clients": ["tv", "mweb"]},
    {"cookies": False, "clients": ["android_vr"]},
]
PREFERRED = 0

COLOR_INFO = discord.Color.red()
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()
DOWNLOAD_SLOTS = asyncio.Semaphore(2)  # maksimal 2 download berjalan bersamaan

MB = 1024 * 1024
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=900)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_bytes(n):
    if not n:
        return "?"
    mb = n / MB
    return f"{mb:.1f} MB" if mb >= 1 else f"{max(1, round(n / 1024))} KB"


def format_speed(bps):
    return f"{format_bytes(bps)}/s" if bps else "-"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "-"
    s = round(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_duration(seconds):
    if not seconds:
        return None
    return format_eta(seconds)


def progress_bar(pct, width=12):
    filled = round(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled)


def safe_filename(title):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", title or "").strip()
    return (name or "youtube")[:80]


def ascii_filename(title, ext):
    """Nama file aman (ASCII) untuk upload ke hosting eksternal."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", title or "").strip("_")[:60]
    return f"{name or 'youtube'}.{ext}"


def clean_error(err):
    text = ANSI_RE.sub("", str(err)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return clip(text or "Gagal memproses link.", 1000)


def normalize_url(url):
    parsed = urlparse(url.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not HOST_RE.search(parsed.hostname)
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def upload_limit(interaction):
    guild = interaction.guild
    return guild.filesize_limit if guild else DEFAULT_UPLOAD_LIMIT


# ---------------------------------------------------------------------------
# Hosting file sementara (link eksternal untuk file yang melebihi batas Discord)
# ---------------------------------------------------------------------------


async def _upload_catbox(session, path, filename):
    with open(path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("reqtype", "fileupload")
        data.add_field("fileToUpload", f, filename=filename)
        async with session.post("https://catbox.moe/user/api.php", data=data) as res:
            text = (await res.text()).strip()
            if res.status == 200 and text.startswith("http"):
                return text
            raise RuntimeError(f"HTTP {res.status}: {clip(text, 120)}")


async def _upload_uguu(session, path, filename):
    with open(path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("files[]", f, filename=filename)
        async with session.post(
            "https://uguu.se/upload", params={"output": "json"}, data=data
        ) as res:
            try:
                body = await res.json(content_type=None)
            except Exception:
                body = None
            if res.status == 200 and isinstance(body, dict) and body.get("files"):
                return body["files"][0]["url"]
            raise RuntimeError(f"HTTP {res.status}: {clip(str(body), 120)}")


async def _upload_tmpfiles(session, path, filename):
    with open(path, "rb") as f:
        data = aiohttp.FormData()
        data.add_field("file", f, filename=filename)
        async with session.post("https://tmpfiles.org/api/v1/upload", data=data) as res:
            try:
                body = await res.json(content_type=None)
            except Exception:
                body = None
            url = ((body or {}).get("data") or {}).get("url")
            if res.status == 200 and url:
                url = url.replace("http://", "https://", 1)
                if "/dl/" not in url:
                    url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                return url
            raise RuntimeError(f"HTTP {res.status}: {clip(str(body), 120)}")


# (nama, batas ukuran, fungsi upload, keterangan masa aktif). Dicoba berurutan.
EXTERNAL_HOSTS = [
    ("catbox", 200 * MB, _upload_catbox, "Link bersifat sementara, segera unduh."),
    ("uguu", 128 * MB, _upload_uguu, "Link aktif sekitar 3 jam, segera unduh."),
    (
        "tmpfiles",
        100 * MB,
        _upload_tmpfiles,
        "Link aktif sekitar 60 menit, segera unduh.",
    ),
]
EXTERNAL_MAX = max(h[1] for h in EXTERNAL_HOSTS)


async def upload_external(path, filename, size):
    """Upload ke hosting sementara. Return (url, nama_host, catatan)."""
    errors = []
    async with aiohttp.ClientSession(timeout=UPLOAD_TIMEOUT) as session:
        for name, max_size, uploader, note in EXTERNAL_HOSTS:
            if size > max_size:
                continue
            try:
                url = await uploader(session, path, filename)
                print(f"[youtube] Upload eksternal berhasil via {name}")
                return url, name, note
            except Exception as err:  # noqa: BLE001
                print(f"[youtube] Upload via {name} gagal: {err}")
                errors.append(f"{name}: {clip(err, 80)}")
    if errors:
        raise RuntimeError("Semua hosting link gagal (" + "; ".join(errors) + ").")
    raise RuntimeError(
        f"Ukuran file {format_bytes(size)} melebihi batas hosting link "
        f"({format_bytes(EXTERNAL_MAX)})."
    )


# ---------------------------------------------------------------------------
# yt-dlp options + fallback (blocking, dijalankan di thread executor)
# ---------------------------------------------------------------------------


@contextmanager
def variant_opts(variant):
    """Bangun opsi yt-dlp untuk satu varian.

    Cookie disalin ke file sementara supaya file asli tidak ditimpa yt-dlp.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
    }
    if variant["clients"]:
        opts["extractor_args"] = {"youtube": {"player_client": variant["clients"]}}

    tmp_cookie = None
    try:
        if variant["cookies"] and COOKIES_FILE:
            fd, tmp_cookie = tempfile.mkstemp(prefix="ytcookie_", suffix=".txt")
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
    """Jalankan fn(variant); kalau kena error yang bisa dicoba ulang, pakai varian lain."""
    global PREFERRED
    order = list(range(PREFERRED, len(VARIANTS))) + list(range(0, PREFERRED))
    last_err = None
    for idx in order:
        try:
            result = fn(VARIANTS[idx])
            if idx != PREFERRED:
                print(f"[youtube] Varian #{idx} berhasil: {VARIANTS[idx]}")
            PREFERRED = idx
            return result
        except YoutubeDLError as err:
            last_err = err
            if not RETRY_RE.search(ANSI_RE.sub("", str(err))):
                raise
            print(
                f"[youtube] Varian #{idx} gagal ({clean_error(err)[:120]}), coba berikutnya..."
            )
    raise last_err or RuntimeError("Semua varian gagal.")


def _extract_info(url):
    def attempt(variant):
        with variant_opts(variant) as opts:
            with YoutubeDL(cast(Any, {**opts, "skip_download": True})) as ydl:
                return ydl.extract_info(url, download=False)

    return with_fallback(attempt)


async def extract_info(url):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_info, url)


def build_video_options(info):
    """Return list (height, perkiraan_ukuran_bytes_atau_None), tinggi -> rendah."""
    formats = info.get("formats") or []

    audio_size = max(
        (
            f.get("filesize") or f.get("filesize_approx") or 0
            for f in formats
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        ),
        default=0,
    )

    by_height = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none") or h > MAX_HEIGHT:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        by_height[h] = max(by_height.get(h, 0), size)

    return [
        (h, (size + audio_size) if size else None)
        for h, size in sorted(by_height.items(), reverse=True)
    ][:25]


def _download(url, outdir, kind, height, state):
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

    if kind == "mp4":
        ext = ".mp4"
    else:
        ext = ".mp3"

    def attempt(variant):
        with variant_opts(variant) as base:
            opts: dict[str, Any] = {
                **base,
                "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
                "progress_hooks": [hook],
            }
            if kind == "mp4":
                opts["format"] = (
                    f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]"
                    f"/bv*[height<={height}]+ba/b[height<={height}]"
                )
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

    for name in os.listdir(outdir):
        if name.endswith(ext):
            return os.path.join(outdir, name)
    return None


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def kind_label(kind, height):
    return f"MP4 · {height}p" if kind == "mp4" else f"MP3 · {MP3_BITRATE} kbps"


def base_embed(info, color=COLOR_INFO):
    byline = " • ".join(
        x for x in [info.get("uploader"), format_duration(info.get("duration"))] if x
    )
    embed = discord.Embed(
        title=clip(info.get("title") or "Video YouTube", 256),
        description=byline or None,
        color=color,
        url=info.get("webpage_url"),
    )
    if info.get("thumbnail"):
        embed.set_thumbnail(url=info["thumbnail"])
    return embed


def info_embed(info, error=None):
    embed = base_embed(info)
    if error:
        embed.add_field(name="Gagal", value=clip(error, 1024), inline=False)
    embed.set_footer(text="Pilih kualitas video di bawah untuk mulai download.")
    return embed


def progress_embed(info, kind, height, state):
    phase = state.get("phase", "starting")
    pct = max(0.0, min(100.0, float(state.get("pct") or 0)))
    embed = base_embed(info)
    embed.add_field(name="Format", value=kind_label(kind, height), inline=False)

    if phase == "starting":
        line = "Menyiapkan download..."
    elif phase == "processing":
        line = f"{progress_bar(100)} **100%**\nMemproses (menggabungkan / konversi)..."
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
    return embed


def done_embed(info, kind, height, size, link=None, note=None):
    embed = base_embed(info, COLOR_OK)
    embed.add_field(name="Format", value=kind_label(kind, height), inline=True)
    embed.add_field(name="Ukuran", value=format_bytes(size), inline=True)
    if link:
        embed.add_field(
            name="Download",
            value=f"[Klik di sini untuk mengunduh]({link})",
            inline=False,
        )
        embed.set_footer(text=f"Selesai. {note or 'Link bersifat sementara.'}")
    else:
        embed.set_footer(text="Selesai. File dikirim di pesan di bawah.")
    return embed


def error_embed(title, description):
    return discord.Embed(
        title=title, description=clip(description, 4000), color=COLOR_ERR
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class QualitySelect(discord.ui.Select):
    def __init__(self, options_data, limit):
        self.options_data = options_data
        self.limit = limit
        options = []
        for i, (h, size) in enumerate(options_data):
            if size is None:
                desc = "Ukuran tidak diketahui"
            elif size > EXTERNAL_MAX:
                desc = f"⚠️ ~{format_bytes(size)} (terlalu besar)"
            elif size > limit:
                desc = f"~{format_bytes(size)} • dikirim via link"
            else:
                desc = f"~{format_bytes(size)}"
            options.append(
                discord.SelectOption(label=f"{h}p", description=desc, value=str(i))
            )
        super().__init__(placeholder="Pilih kualitas...", options=options)

    async def callback(self, interaction: discord.Interaction):
        view = cast("QualityView", self.view)
        height, size = self.options_data[int(self.values[0])]

        if size is not None and size > EXTERNAL_MAX:
            await interaction.response.send_message(
                f"Perkiraan ukuran {format_bytes(size)} melebihi batas maksimal "
                f"yang bisa dikirim via link ({format_bytes(EXTERNAL_MAX)}). "
                "Pilih kualitas yang lebih rendah.",
                ephemeral=True,
            )
            return

        view.stop()
        await interaction.response.edit_message(
            embed=progress_embed(view.info, "mp4", height, {}), view=None
        )

        message = interaction.message

        async def on_fail(error):
            if message is None:
                return
            new_view = QualityView(
                view.owner_id, view.info, view.url, view.options_data, view.limit
            )
            new_view.message = message
            await message.edit(embed=info_embed(view.info, error), view=new_view)

        await run_download(
            interaction,
            message,
            view.owner_id,
            view.info,
            view.url,
            "mp4",
            height,
            view.limit,
            on_fail,
        )


class QualityView(discord.ui.View):
    def __init__(self, owner_id, info, url, options_data, limit):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.info = info
        self.url = url
        self.options_data = options_data
        self.limit = limit
        self.message: discord.Message | discord.WebhookMessage | None = None
        self.add_item(QualitySelect(options_data, limit))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Menu ini bukan milikmu. Jalankan `/ytmp4` sendiri ya.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------


async def run_download(
    interaction, message, owner_id, info, url, kind, height, limit, on_fail
):
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
                await message.edit(embed=progress_embed(info, kind, height, state))
            except discord.HTTPException:
                pass

    updater_task = asyncio.create_task(updater())

    try:
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="ytdl_") as tmp:
                loop = asyncio.get_running_loop()
                try:
                    path = await loop.run_in_executor(
                        None, _download, url, tmp, kind, height, state
                    )
                except YoutubeDLError as err:
                    finished.set()
                    await on_fail(clean_error(err))
                    return

                finished.set()

                if not path or not os.path.isfile(path):
                    await on_fail("File hasil download tidak ditemukan.")
                    return

                size = os.path.getsize(path)
                sent_direct = False

                # 1) Di bawah batas: kirim langsung sebagai file Discord.
                if size <= limit:
                    filename = f"{safe_filename(info.get('title'))}.{kind}"
                    try:
                        await interaction.followup.send(
                            file=discord.File(path, filename=filename)
                        )
                        sent_direct = True
                    except discord.HTTPException as err:
                        print(f"[youtube] Upload ke Discord gagal, pakai link: {err}")

                if sent_direct:
                    await message.edit(
                        embed=done_embed(info, kind, height, size), view=None
                    )
                    return

                # 2) Melebihi batas (atau upload Discord gagal): kirim sebagai link.
                state.update(phase="uploading", pct=100)
                try:
                    await message.edit(embed=progress_embed(info, kind, height, state))
                except discord.HTTPException:
                    pass

                try:
                    link, host, note = await upload_external(
                        path, ascii_filename(info.get("title"), kind), size
                    )
                except RuntimeError as err:
                    await on_fail(
                        f"{err}"
                        + (" Pilih kualitas lebih rendah." if kind == "mp4" else "")
                    )
                    return

                await message.edit(
                    embed=done_embed(info, kind, height, size, link, note),
                    view=None,
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
            "Link tidak valid. Tempel link lengkap dari YouTube "
            "(youtube.com / youtu.be).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    limit = upload_limit(interaction)

    try:
        info = await extract_info(clean_url)
    except Exception as err:  # noqa: BLE001
        await interaction.followup.send(
            embed=error_embed("Gagal memproses link", clean_error(err))
        )
        return

    if info.get("is_live"):
        await interaction.followup.send(
            embed=error_embed("Tidak didukung", "Live stream tidak bisa diunduh.")
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

    if kind == "mp3":
        est = duration * MP3_BITRATE * 1000 / 8
        if est > EXTERNAL_MAX:
            await interaction.followup.send(
                embed=error_embed(
                    "File terlalu besar",
                    f"Perkiraan ukuran MP3 {format_bytes(est)} melebihi batas maksimal "
                    f"yang bisa dikirim via link ({format_bytes(EXTERNAL_MAX)}).",
                )
            )
            return

        message = await interaction.followup.send(
            embed=progress_embed(info, "mp3", None, {}), wait=True
        )

        async def on_fail(error):
            await message.edit(embed=error_embed("Download gagal", error), view=None)

        await run_download(
            interaction,
            message,
            interaction.user.id,
            info,
            clean_url,
            "mp3",
            None,
            limit,
            on_fail,
        )
        return

    options_data = build_video_options(info)
    if not options_data:
        await interaction.followup.send(
            embed=error_embed("Gagal", "Tidak ada format video yang tersedia.")
        )
        return

    view = QualityView(interaction.user.id, info, clean_url, options_data, limit)
    view.message = await interaction.followup.send(
        embed=info_embed(info), view=view, wait=True
    )


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: Any):
    @bot.tree.command(name="ytmp4", description="Download video YouTube (MP4)")
    @app_commands.describe(url="Link video YouTube")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ytmp4(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp4")

    @bot.tree.command(name="ytmp3", description="Download audio YouTube (MP3)")
    @app_commands.describe(url="Link video YouTube")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def ytmp3(interaction: discord.Interaction, url: str):
        await handle(interaction, url, "mp3")
