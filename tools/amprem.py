import asyncio
import json
import re
from typing import Any, Protocol

import aiohttp
import discord
from discord import app_commands

API_URL = "https://anita-studio.netlify.app/.netlify/functions/amprem"
PREMIUM_ROLE_ID = 1561692064352174100  # role "Premium User"
ALLOWED_GUILD_ID = 1561577570720284705  # server yang diizinkan
ALLOWED_CHANNEL_ID = 1561700168989016064  # channel yang diizinkan
INVITE_URL = "https://discord.gg/5rCDYCTsG2"
OWNER_ID = 1215281826092810281  # owner bot: bebas dari cooldown /amprem

MSG_NOT_PREMIUM = (
    "🔒 Command ini hanya bisa digunakan oleh user dengan role **Premium User**.\n"
    "Kamu juga bisa menjadi user Premium dan menggunakan command ini. "
    "Silakan kirim pesan ke owner: "
    "[@senpaii._28](https://discord.com/users/1215281826092810281)."
)
MSG_WRONG_SERVER = (
    "🔒 Command ini hanya tersedia di server kami.\n"
    f"Silakan gabung terlebih dahulu lewat link berikut: {INVITE_URL}"
)
MSG_WRONG_CHANNEL = "📍 Command ini hanya bisa digunakan di <#{channel_id}>."
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VIEW_TIMEOUT = 600  # detik
COOLDOWN = 300.0  # 5 menit per user (berdasarkan user id)

# User yang sedang punya proses berjalan (mencegah proses ganda per user).
ACTIVE: set[int] = set()


class BotLike(Protocol):
    """Bentuk minimal bot yang dipakai modul ini (hanya untuk type checker)."""

    tree: app_commands.CommandTree[Any]
    http_session: aiohttp.ClientSession | None


class ApiError(Exception):
    pass


class WrongLocation(app_commands.CheckFailure):
    pass


def cooldown_factory(interaction: discord.Interaction) -> app_commands.Cooldown | None:
    """Cooldown per user; owner dikembalikan None sehingga tanpa cooldown."""
    if interaction.user.id == OWNER_ID:
        return None
    return app_commands.Cooldown(1, COOLDOWN)


def only_allowed_server(interaction: discord.Interaction) -> bool:
    if interaction.guild_id != ALLOWED_GUILD_ID:
        raise WrongLocation(MSG_WRONG_SERVER)
    return True


def only_allowed_channel(interaction: discord.Interaction) -> bool:
    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        raise WrongLocation(MSG_WRONG_CHANNEL.format(channel_id=ALLOWED_CHANNEL_ID))
    return True


async def safe_error_reply(interaction: discord.Interaction, msg: str) -> None:
    """Balas error tanpa melempar ulang jika interaction sudah kedaluwarsa."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound as e:
        print(
            f"[amprem] tidak bisa membalas error, interaction kedaluwarsa (code {e.code})"
        )
    except discord.HTTPException as e:
        print(f"[amprem] gagal membalas error: HTTP {e.status} code {e.code}: {e.text}")


def short(value, limit=300):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "..."


async def post(bot, action, data):
    session = bot.http_session
    if session is None:
        raise ApiError("Sesi HTTP bot belum siap.")
    try:
        async with session.post(
            API_URL,
            json={"action": action, **data},
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            text = await resp.text()
            status = resp.status
    except asyncio.TimeoutError:
        raise ApiError("Request ke API timeout. Coba lagi nanti.")
    except aiohttp.ClientError:
        raise ApiError("Gagal menghubungi API.")

    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ApiError(f"Respons API tidak valid (HTTP {status}).")
    if status >= 400:
        raise ApiError(short(body.get("message")) or f"API error (HTTP {status}).")
    print(f"[amprem] {action} -> HTTP {status}")  # tanpa data sensitif
    return body


class LinkModal(discord.ui.Modal, title="Masukkan Magic Link"):
    link = discord.ui.TextInput(
        label="URL magic link dari email",
        style=discord.TextStyle.paragraph,
        placeholder="https://...",
        max_length=4000,
    )

    def __init__(self, bot, email, parent):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot
        self.email = email
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        raw_link = self.link.value.strip()
        if not raw_link.lower().startswith(("https://", "http://")):
            await interaction.response.send_message(
                "❌ Itu bukan URL yang valid. Salin seluruh link dari email.",
                ephemeral=True,
            )
            return
        uid = interaction.user.id
        if uid in ACTIVE:
            await interaction.response.send_message(
                "⏳ Kamu masih punya proses yang berjalan.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ACTIVE.add(uid)
        try:
            await self._run(interaction, raw_link)
        finally:
            ACTIVE.discard(uid)
            await self.parent.finish()

    async def _run(self, interaction, raw_link):
        async def send(text):
            await interaction.followup.send(text, ephemeral=True)

        id_token = None
        try:
            ver = await post(
                self.bot,
                "verify-account",
                {"email": self.email, "rawLink": raw_link},
            )
            if not ver.get("success"):
                await send(
                    "❌ Verifikasi gagal: "
                    + (short(ver.get("message")) or "tanpa pesan dari API")
                    + "\nJalankan `/amprem` lagi untuk meminta link baru."
                )
                return

            profile = ver.get("profile")
            id_token = ver.get("idToken") or (
                profile.get("idToken") if isinstance(profile, dict) else None
            )
            if not id_token:
                await send(
                    "❌ Verifikasi berhasil, tapi API tidak mengembalikan "
                    "idToken. Proses dihentikan."
                )
                return

            await send("✅ Akun terverifikasi. Menerapkan premium...")
            prem = await post(
                self.bot,
                "apply-premium",
                {"email": self.email, "idToken": id_token},
            )
            note = short(prem.get("message"))
            if prem.get("success") is True:
                await send(
                    "✅ API mengonfirmasi permintaan premium berhasil."
                    + (f"\nPesan API: {note}" if note else "")
                )
            else:
                await send(
                    "❌ Premium tidak dikonfirmasi oleh API: "
                    + (note or "tanpa pesan dari API")
                )
        except ApiError as e:
            await send(f"❌ {e}")
        finally:
            id_token = None

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        detail = (
            f" (HTTP {error.status}, code {error.code})"
            if isinstance(error, discord.HTTPException)
            else ""
        )
        print(f"[amprem] modal error: {type(error).__name__}{detail}")
        await safe_error_reply(interaction, "❌ Terjadi kesalahan. Coba lagi nanti.")


class LinkView(discord.ui.View):
    def __init__(self, bot, email, owner_id):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot
        self.email = email
        self.owner_id = owner_id
        self.message: discord.Message | discord.WebhookMessage | None = None

    @discord.ui.button(label="Masukkan Magic Link", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Ini bukan sesi kamu.", ephemeral=True
            )
            return
        if self.owner_id in ACTIVE:
            await interaction.response.send_message(
                "⏳ Proses masih berjalan.", ephemeral=True
            )
            return
        await interaction.response.send_modal(LinkModal(self.bot, self.email, self))

    async def finish(self):
        self.stop()
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self.finish()


class EmailModal(discord.ui.Modal, title="AMPrem - Email"):
    email = discord.ui.TextInput(
        label="Email akun",
        placeholder="nama@email.com",
        max_length=254,
    )

    def __init__(self, bot):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        email = self.email.value.strip()
        if not EMAIL_RE.match(email):
            await interaction.response.send_message(
                "❌ Format email tidak valid.", ephemeral=True
            )
            return
        uid = interaction.user.id
        if uid in ACTIVE:
            await interaction.response.send_message(
                "⏳ Kamu masih punya proses yang berjalan.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ACTIVE.add(uid)
        try:
            body = await post(self.bot, "send-magiclink", {"email": email})
        except ApiError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        finally:
            ACTIVE.discard(uid)

        if not body.get("success"):
            await interaction.followup.send(
                "❌ Gagal meminta magic link: "
                + (short(body.get("message")) or "tanpa pesan dari API"),
                ephemeral=True,
            )
            return

        view = LinkView(self.bot, email, uid)
        view.message = await interaction.followup.send(
            "✅ Permintaan magic link sudah dikirim ke email kamu.\n"
            "Buka email, salin URL magic link-nya, lalu klik tombol di bawah "
            "dan tempel URL tersebut.",
            view=view,
            ephemeral=True,
            wait=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        detail = (
            f" (HTTP {error.status}, code {error.code})"
            if isinstance(error, discord.HTTPException)
            else ""
        )
        print(f"[amprem] modal error: {type(error).__name__}{detail}")
        await safe_error_reply(interaction, "❌ Terjadi kesalahan. Coba lagi nanti.")


def setup(bot: BotLike):
    @bot.tree.command(name="amprem", description="Proses AMPrem lewat magic link email")
    # Urutan pengecekan (dari bawah ke atas): server -> role -> channel -> cooldown.
    # Cooldown 5 menit per user, kecuali OWNER_ID (lihat cooldown_factory).
    @app_commands.guild_only()
    @app_commands.checks.dynamic_cooldown(cooldown_factory, key=lambda i: i.user.id)
    @app_commands.check(only_allowed_channel)
    @app_commands.checks.has_role(PREMIUM_ROLE_ID)
    @app_commands.check(only_allowed_server)
    async def amprem(interaction: discord.Interaction):
        # Diagnostik: umur interaction saat callback mulai. Modal harus dikirim
        # sebagai respons awal dalam 3 detik sejak interaction dibuat. Jika umur
        # sudah besar di sini, keterlambatan terjadi SEBELUM kode ini jalan
        # (event loop terblokir / gateway lag / instance bot ganda), bukan di
        # dalam command ini. Kegagalan send_modal ditangani di bot.py.
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        if age > 2.0:
            print(
                f"[amprem] PERINGATAN: interaction berumur {age:.2f}s saat "
                f"callback mulai (batas respons 3s) user={interaction.user.id}"
            )
        await interaction.response.send_modal(EmailModal(bot))
