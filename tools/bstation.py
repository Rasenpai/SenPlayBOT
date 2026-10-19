import asyncio
import os
import re
import time
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

API = os.getenv("BSTATION_API", "https://bstation-api.senplay.web.id").rstrip("/")

POLL_INTERVAL = 1.0  # detik antar polling job
EDIT_INTERVAL = 2.0  # detik minimal antar edit pesan (hindari rate limit)
POLL_MAX_FAILURES = 5
ACTIVE_STATUSES = {"starting", "downloading", "processing"}
HOST_RE = re.compile(r"(^|\.)(bilibili\.tv|bstation\.tv)$", re.IGNORECASE)

# Parameter pelacak yang dibuang dari URL sebelum dikirim ke backend.
TRACKING_PARAMS = {
    "bstar_from",
    "from",
    "from_spmid",
    "spm_id_from",
    "share_source",
    "share_medium",
    "share_plat",
    "share_session_id",
    "share_tag",
    "timestamp",
    "utm_source",
    "utm_medium",
    "utm_campaign",
}

STATUS_LABEL = {
    "starting": "Menyiapkan download...",
    "downloading": "Mengunduh...",
    "processing": "Memproses (menggabungkan video & audio)...",
    "completed": "Selesai",
}

COLOR_INFO = discord.Color.from_rgb(0, 174, 236)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.red()

# User yang sedang punya download aktif (satu user satu download).
ACTIVE_USERS: set[int] = set()


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


async def api(session: aiohttp.ClientSession, path, body=None, headers=None):
    method = "POST" if body is not None else "GET"
    async with session.request(
        method, f"{API}{path}", json=body, headers=headers
    ) as res:
        try:
            data = await res.json(content_type=None)
        except Exception:
            data = None

        if res.status >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            retry = res.headers.get("Retry-After")
            raise ApiError(
                (
                    detail
                    if isinstance(detail, str) and detail
                    else f"Server membalas dengan status {res.status}."
                ),
                res.status,
                int(retry) if retry and retry.isdigit() else None,
            )
        return data


def describe_error(err: Exception) -> str:
    if isinstance(err, ApiError):
        if err.status == 429 and err.retry_after:
            return f"{err} Coba lagi dalam {err.retry_after} detik."
        return str(err)
    if isinstance(err, (aiohttp.ClientError, asyncio.TimeoutError)):
        return "Tidak bisa terhubung ke server Bstation. Pastikan server aktif."
    return str(err) or "Gagal memproses link."


async def poll_job(session, job_id, token, on_update):
    """Polling sampai job selesai/gagal. on_update(job, force) dipanggil tiap respons."""
    failures = 0
    while True:
        try:
            job = await api(
                session,
                f"/api/bstation/job/{job_id}",
                headers={"X-Job-Token": token},
            )
            failures = 0
        except ApiError as e:
            if e.status == 429:
                await asyncio.sleep(e.retry_after or 5)
                continue
            if e.status == 404:
                raise ApiError("Job tidak ditemukan atau sudah kedaluwarsa.", 404)
            failures += 1
            if failures >= POLL_MAX_FAILURES:
                raise
            await asyncio.sleep(2)
            continue
        except (aiohttp.ClientError, asyncio.TimeoutError):
            failures += 1
            if failures >= POLL_MAX_FAILURES:
                raise
            await asyncio.sleep(2)
            continue

        finished = job.get("status") not in ACTIVE_STATUSES
        await on_update(job, finished)
        if finished:
            return job
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def normalize_source_url(url: str) -> str | None:
    """Validasi & bersihkan link Bstation. Return None kalau bukan link Bstation.

    Contoh:
      https://www.bilibili.tv/id/play/2432940?bstar_from=bstar-web.homepage.anime.all
      -> https://www.bilibili.tv/id/play/2432940
    """
    parsed = urlparse(url.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or not HOST_RE.search(parsed.hostname)
    ):
        return None

    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def build_download_url(download_url: str) -> str:
    """Gabungkan dengan host API dan encode path/query supaya valid.

    Persen-encoding yang sudah ada dibiarkan (karakter % tidak di-encode ulang).
    """
    full = urljoin(f"{API}/", download_url)
    parts = urlsplit(full)
    api_parts = urlsplit(API)

    # Kalau backend mengembalikan URL absolut ke localhost, arahkan ke host API publik.
    netloc = parts.netloc
    scheme = parts.scheme
    if parts.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        netloc = api_parts.netloc
        scheme = api_parts.scheme

    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%+:@/?!$'()*,;-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


def format_speed(bps):
    if not bps:
        return "-"
    mb = bps / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB/s"
    return f"{max(1, round(bps / 1024))} KB/s"


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "-"
    s = round(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def progress_bar(pct, width=12):
    filled = round(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled)


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def base_embed(info, color=COLOR_INFO, url=None):
    uploader = (info.get("uploader") or {}).get("name")
    duration = info.get("duration_text")
    byline = " • ".join(
        x
        for x in [
            uploader if uploader and uploader != "-" else None,
            duration if duration and duration != "-" else None,
        ]
        if x
    )
    embed = discord.Embed(
        title=clip(info.get("title") or "Video Bstation", 256),
        description=byline or None,
        color=color,
        url=url,
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


def progress_embed(info, fmt, job):
    status = (job or {}).get("status", "starting")
    pct = max(0, min(100, float((job or {}).get("progress") or 0)))
    embed = base_embed(info)
    embed.add_field(
        name="Kualitas", value=f"{fmt['quality']} · {fmt['codec']}", inline=False
    )
    line = f"{progress_bar(pct)} **{round(pct)}%**\n{STATUS_LABEL.get(status, 'Memproses...')}"
    if status == "downloading" and job:
        line += f"\n{format_speed(job.get('speed'))} • ETA {format_eta(job.get('eta'))}"
    embed.add_field(name="Progress", value=line, inline=False)
    return embed


def done_embed(info, fmt, job, link):
    # Link hanya tampil sebagai teks berlabel (masked), bukan URL mentah.
    embed = base_embed(info, COLOR_OK, url=link)
    embed.add_field(
        name="Kualitas", value=f"{fmt['quality']} · {fmt['codec']}", inline=False
    )
    if job.get("filename"):
        embed.add_field(name="File", value=clip(job["filename"], 1024), inline=False)

    fallback = f"[Klik di sini untuk mengunduh]({link})"
    if len(fallback) <= 1024:
        embed.add_field(name="Download", value=fallback, inline=False)

    embed.set_footer(text="Selesai. File hanya tersedia sementara, segera unduh.")
    return embed


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class FormatSelect(discord.ui.Select):
    def __init__(self, formats):
        options = []
        for i, f in enumerate(formats[:25]):
            size = f.get("total_filesize_text")
            size = size if size and size != "-" else None
            fps = f"{round(f['fps'])} fps" if f.get("fps") else None
            desc = " • ".join(x for x in [size, fps] if x) or "MP4"
            options.append(
                discord.SelectOption(
                    label=clip(f"{f['quality']} · {f['codec']}", 100),
                    description=clip(desc, 100),
                    value=str(i),
                )
            )
        super().__init__(placeholder="Pilih kualitas...", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "FormatView" = self.view
        fmt = view.formats[int(self.values[0])]
        view.stop()
        await interaction.response.edit_message(
            embed=progress_embed(view.info, fmt, None), view=None
        )
        await start_download(
            interaction.client,
            interaction.message,
            view.owner_id,
            view.info,
            view.url,
            fmt,
        )


class FormatView(discord.ui.View):
    def __init__(self, owner_id, info, url):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.info = info
        self.url = url
        self.formats = info["formats"][:25]
        self.message = None
        self.add_item(FormatSelect(self.formats))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Menu ini bukan milikmu. Jalankan `/bstation` sendiri ya.",
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


class DownloadView(discord.ui.View):
    """Tombol biasa: saat diklik, bot membalas link berlabel yang bisa diklik."""

    def __init__(self, link):
        super().__init__(timeout=None)
        self.link = link

    @discord.ui.button(label="Unduh file", style=discord.ButtonStyle.success)
    async def download(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            f"[Klik di sini untuk mengunduh]({self.link})",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Download flow
# ---------------------------------------------------------------------------


async def start_download(bot, message, owner_id, info, url, fmt):
    session = bot.http_session

    if owner_id in ACTIVE_USERS:
        await message.edit(
            embed=info_embed(info, "Kamu masih punya download yang berjalan."),
            view=None,
        )
        return
    ACTIVE_USERS.add(owner_id)

    async def back_to_formats(error):
        view = FormatView(owner_id, info, url)
        view.message = message
        await message.edit(embed=info_embed(info, error), view=view)

    last_edit = 0.0

    async def on_update(job, force):
        nonlocal last_edit
        now = time.monotonic()
        if not force and now - last_edit < EDIT_INTERVAL:
            return
        if job.get("status") in ACTIVE_STATUSES:
            last_edit = now
            try:
                await message.edit(embed=progress_embed(info, fmt, job))
            except discord.HTTPException:
                pass

    try:
        created = await api(
            session,
            "/api/bstation/download",
            body={"url": url, "format_id": fmt["format_id"]},
        )
        job = await poll_job(session, created["id"], created["token"], on_update)

        if job.get("status") == "completed" and job.get("download_url"):
            link = build_download_url(job["download_url"])
            await message.edit(
                embed=done_embed(info, fmt, job, link), view=DownloadView(link)
            )
        else:
            await back_to_formats(
                job.get("error") or "Download gagal. Silakan coba lagi."
            )
    except Exception as err:  # noqa: BLE001
        try:
            await back_to_formats(describe_error(err))
        except discord.HTTPException:
            pass
    finally:
        ACTIVE_USERS.discard(owner_id)


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: discord.Client):
    @bot.tree.command(name="bstation", description="Download video dari Bstation")
    @app_commands.describe(url="Link video Bstation (bilibili.tv / bstation.tv)")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def bstation(interaction: discord.Interaction, url: str):
        clean_url = normalize_source_url(url)
        if not clean_url:
            await interaction.response.send_message(
                "Link tidak valid. Tempel link lengkap dari Bstation "
                "(bilibili.tv / bstation.tv).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            info = await api(
                bot.http_session, "/api/bstation/info", body={"url": clean_url}
            )
            if not isinstance(info.get("formats"), list) or not info["formats"]:
                raise ApiError("Tidak ada format yang tersedia untuk video ini.")
        except Exception as err:  # noqa: BLE001
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Gagal memproses link",
                    description=clip(describe_error(err), 4000),
                    color=COLOR_ERR,
                )
            )
            return

        view = FormatView(interaction.user.id, info, clean_url)
        view.message = await interaction.followup.send(
            embed=info_embed(info), view=view, wait=True
        )
