import os
import traceback

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

from tools import (
    bstation,
    converter,
    donations,
    instagram,
    svg2am,
    tiktok,
    youtube,
    radio,
)  # noqa: E402  (harus setelah load_dotenv)
from tools import amprem  # noqa: E402
from Moderation import automod  # noqa: E402
from SenPlayUpdate import animeongoing  # noqa: E402
from SenPlayUpdate import komikupdate  # noqa: E402
from SenPlayUpdate import movieupdate  # noqa: E402
from SenPlayUpdate import animeupdate, seriesupdate  # noqa: E402
from tools import website
from tools import amxml  # noqa: E402

from tools import help as help_cmd  # noqa: E402

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()


class SenBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = (
            True  # dipakai automod (aktifkan juga di Developer Portal)
        )
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        # Daftarkan semua tool di sini. Tool baru tinggal dibuat di tools/
        # lalu ditambahkan satu baris setup().
        bstation.setup(self)
        youtube.setup(self)
        tiktok.setup(self)
        instagram.setup(self)
        converter.setup(self)
        svg2am.setup(self)
        amprem.setup(self)
        automod.setup(self)
        help_cmd.setup(self)
        animeongoing.setup(self)
        komikupdate.setup(self)
        movieupdate.setup(self)
        animeupdate.setup(self)
        seriesupdate.setup(self)
        donations.setup(self)
        website.setup(self)
        radio.setup(self)

        if GUILD_ID:
            # Sync ke server tertentu: muncul instan.
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            # Hapus command global lama supaya tidak muncul dobel.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            print(
                f"[sync] {len(synced)} command ke server {GUILD_ID}: "
                f"{', '.join('/' + c.name for c in synced)}"
            )
        else:
            synced = await self.tree.sync()
            print(
                f"[sync] {len(synced)} command global: "
                f"{', '.join('/' + c.name for c in synced)}"
            )

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = SenBot()


def _interaction_age(interaction: discord.Interaction) -> float:
    """Umur interaction (detik) sejak dibuat Discord. Respons awal maksimal 3 detik."""
    return (discord.utils.utcnow() - interaction.created_at).total_seconds()


async def _safe_reply(interaction: discord.Interaction, msg: str) -> None:
    """Kirim balasan error tanpa pernah melempar error baru dari handler.

    - Belum direspons -> respons awal (send_message), hanya sekali.
    - Sudah direspons  -> followup.send.
    - Interaction kedaluwarsa (404) atau gagal kirim -> cukup dicatat di log.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound as e:
        # 10062 Unknown interaction / 10015 Unknown webhook: sudah tidak valid.
        print(
            f"[error] balasan error tidak terkirim, interaction sudah tidak valid "
            f"(code {e.code}, umur {_interaction_age(interaction):.2f}s)"
        )
    except discord.HTTPException as e:
        print(
            f"[error] balasan error gagal dikirim: HTTP {e.status} "
            f"code {e.code}: {e.text}"
        )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    cmd_name = getattr(interaction.command, "name", "?")
    orig = getattr(error, "original", error)

    # Interaction kedaluwarsa (Unknown interaction, 10062): Discord menolak
    # respons karena lewat 3 detik / sudah tidak valid. Tidak ada yang bisa
    # dikirim ke user, jadi cukup catat dengan info diagnostik lalu selesai.
    if isinstance(orig, discord.NotFound) and orig.code == 10062:
        print(
            f"[error] /{cmd_name}: Unknown interaction (10062), umur interaction "
            f"{_interaction_age(interaction):.2f}s, user={interaction.user.id}, "
            f"sudah_direspons={interaction.response.is_done()}. Respons terlambat "
            f"(>3s): cek event loop terblokir, gateway lag, atau instance bot ganda."
        )
        return

    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Tunggu {error.retry_after:.0f} detik sebelum pakai command ini lagi."
    elif isinstance(error, amprem.WrongLocation):
        msg = str(error)
    elif isinstance(error, app_commands.MissingRole):
        msg = amprem.MSG_NOT_PREMIUM
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = amprem.MSG_WRONG_SERVER
    else:
        print(
            f"[error] /{cmd_name}: {type(error).__name__} <- {type(orig).__name__}: {orig}"
        )
        # Traceback lengkap supaya error lain tidak tersamarkan.
        traceback.print_exception(type(orig), orig, orig.__traceback__)
        msg = "Terjadi kesalahan. Coba lagi nanti."
    await _safe_reply(interaction, msg)


@bot.event
async def on_ready():
    if bot.user:
        print(f"Login sebagai {bot.user} (id: {bot.user.id})")


bot.run(TOKEN)
