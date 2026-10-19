import os

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

from tools import (
    bstation,
    converter,
    instagram,
    svg2am,
    tiktok,
    youtube,
)  # noqa: E402  (harus setelah load_dotenv)
from tools import amprem  # noqa: E402
from Moderation import automod  # noqa: E402
from SenPlayUpdate import animeongoing  # noqa: E402
from SenPlayUpdate import komikupdate  # noqa: E402
from SenPlayUpdate import movieupdate  # noqa: E402
from SenPlayUpdate import animeupdate, seriesupdate  # noqa: E402
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


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Tunggu {error.retry_after:.0f} detik sebelum pakai command ini lagi."
    elif isinstance(error, amprem.WrongLocation):
        msg = str(error)
    elif isinstance(error, app_commands.MissingRole):
        msg = amprem.MSG_NOT_PREMIUM
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = amprem.MSG_WRONG_SERVER
    else:
        orig = getattr(error, "original", error)
        cmd_name = getattr(interaction.command, "name", "?")
        print(
            f"[error] /{cmd_name}: {type(error).__name__} <- {type(orig).__name__}: {orig}"
        )
        msg = "Terjadi kesalahan. Coba lagi nanti."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_ready():
    if bot.user:
        print(f"Login sebagai {bot.user} (id: {bot.user.id})")


bot.run(TOKEN)
