import discord
from discord import app_commands

from tools import amprem, converter, instagram, svgcore, tiktok, youtube
from tools.youtube import EXTERNAL_MAX, format_bytes

COLOR_HELP = discord.Color.from_rgb(88, 101, 242)


def minutes(seconds):
    return f"{int(seconds // 60)} menit"


def build_embed(interaction: discord.Interaction, bot: discord.Client):
    embed = discord.Embed(
        title="Bantuan SenPlay Bot",
        description=(
            "Download video/audio dan konversi gambar.\n"
            "Ketik `/`, **klik command dari popup**, isi kolomnya, lalu Enter."
        ),
        color=COLOR_HELP,
    )

    embed.add_field(
        name="Bstation",
        value=(
            "`/bstation url:<link>`\n"
            "Pilih kualitas dari dropdown, lalu klik **Unduh file** untuk dapat link download.\n"
            "Link: `bilibili.tv` / `bstation.tv`"
        ),
        inline=False,
    )

    embed.add_field(
        name="YouTube",
        value=(
            "`/ytmp4 url:<link>` video MP4, pilih kualitas 144p sampai 1080p\n"
            "`/ytmp3 url:<link>` audio MP3 "
            f"{youtube.MP3_BITRATE} kbps\n"
            f"Maks durasi {minutes(youtube.MAX_DURATION)}. Link: `youtube.com` / `youtu.be`"
        ),
        inline=False,
    )

    embed.add_field(
        name="TikTok",
        value=(
            "`/ttmp4 url:<link>` video MP4 + info kreator, statistik, musik, hashtag\n"
            "`/ttmp3 url:<link>` audio MP3\n"
            f"Maks durasi {minutes(tiktok.MAX_DURATION)}. "
            "Link: `tiktok.com` / `vm.tiktok.com` / `vt.tiktok.com`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Instagram",
        value=(
            "`/igmp4 url:<link>` video, reel, foto, atau carousel (maks "
            f"{instagram.MAX_ITEMS} item)\n"
            "`/igmp3 url:<link>` audio MP3\n"
            f"Maks durasi {minutes(instagram.MAX_DURATION)}. "
            "Hanya post publik. Link: `instagram.com`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Konverter",
        value=(
            "`/svg file:<gambar>` ubah PNG/JPG/WebP/BMP/GIF/TIFF ke SVG (vektor)\n"
            "Opsi: `mode` (Warna / Hitam-Putih) dan `detail` (Rendah / Sedang / Tinggi)\n"
            f"Maks input {format_bytes(converter.MAX_INPUT)}. Cocok untuk logo, ikon, "
            "dan ilustrasi. Foto detail hasilnya kurang bagus dan file SVG besar.\n"
            "`/svg2am file:<svg>` ubah SVG jadi file project XML Alight Motion "
            "(satu shape per elemen). Opsi `format: ZIP layer SVG` untuk SVG per layer. "
            f"Maks {format_bytes(svgcore.MAX_BYTES)}, maks {svgcore.MAX_LAYERS} layer. "
            "Kurva diratakan; gradient, teks, dan gambar tidak didukung."
        ),
        inline=False,
    )

    embed.add_field(
        name="AM Premium",
        value=(
            "`/amprem` proses AMPrem lewat magic link email. "
            "**Khusus member dengan role Premium User.**\n"
            "Isi email di form, buka email lalu salin URL magic link, "
            "kemudian klik tombol **Masukkan Magic Link** dan tempel URL-nya. "
            "Status proses hanya terlihat oleh kamu.\n"
            f"Hanya bisa dipakai di <#{amprem.ALLOWED_CHANNEL_ID}>."
        ),
        inline=False,
    )

    limit = youtube.upload_limit(interaction)
    embed.add_field(
        name="Pengiriman file",
        value=(
            f"File sampai {format_bytes(limit)} dikirim langsung ke channel.\n"
            f"Lebih besar dari itu dikirim lewat link sementara (maks {format_bytes(EXTERNAL_MAX)}), "
            "segera unduh karena link akan kedaluwarsa."
        ),
        inline=False,
    )

    embed.add_field(
        name="Aturan",
        value=(
            "Cooldown 10 detik per command downloader, 15 detik untuk `/svg` dan `/svg2am`, "
            f"{int(amprem.COOLDOWN // 60)} menit per user untuk `/amprem`.\n"
            "Satu user hanya bisa menjalankan satu proses dalam satu waktu.\n"
            "Live stream, post private, dan video melebihi batas durasi tidak didukung."
        ),
        inline=False,
    )

    total = len(bot.tree.get_commands(guild=interaction.guild)) or len(
        bot.tree.get_commands()
    )
    embed.set_footer(text=f"{total} command tersedia • /help")
    return embed


def setup(bot: discord.Client):
    @bot.tree.command(name="help", description="Daftar command dan cara pakai bot")
    async def help_command(interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=build_embed(interaction, bot), ephemeral=True
        )
