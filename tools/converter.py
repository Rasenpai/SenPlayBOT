import asyncio
import os
import tempfile
from pathlib import Path

import discord
import vtracer
from discord import app_commands
from PIL import Image, ImageOps

from tools.youtube import (
    EXTERNAL_MAX,
    ascii_filename,
    clean_error,
    clip,
    format_bytes,
    safe_filename,
    upload_external,
    upload_limit,
)

MB = 1024 * 1024
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_INPUT = 8 * MB  # ukuran file input maksimal
MAX_SIDE = 1600  # sisi terpanjang gambar diperkecil ke ini (biar cepat)
CONVERT_TIMEOUT = 180  # detik

# Cegah gambar raksasa (decompression bomb).
Image.MAX_IMAGE_PIXELS = 40_000_000

COLOR_INFO = discord.Color.from_rgb(255, 154, 0)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()
CONVERT_SLOTS = asyncio.Semaphore(2)  # maksimal 2 konversi berjalan bersamaan

MODE_LABEL = {"color": "Warna", "bw": "Hitam-Putih"}
DETAIL_LABEL = {"low": "Rendah", "mid": "Sedang", "high": "Tinggi"}

GUIDE_TEXT = (
    "Discord menampilkan isi file SVG sebagai kode, itu normal.\n"
    "💻 **PC:** arahkan kursor ke file `.svg`, klik titik tiga (**⋯ More**), "
    "lalu klik **Download**.\n"
    "📱 **HP:** tap file `.svg`, otomatis terunduh.\n"
    "Setelah itu buka di browser, atau impor ke Figma / Inkscape / Canva."
)

# path_precision = jumlah angka di belakang koma pada koordinat (makin kecil, file makin ringan).
PRESETS = {
    "low": dict(
        filter_speckle=10,
        color_precision=4,
        layer_difference=32,
        corner_threshold=60,
        length_threshold=5.0,
        path_precision=1,
    ),
    "mid": dict(
        filter_speckle=4,
        color_precision=6,
        layer_difference=16,
        corner_threshold=60,
        length_threshold=4.0,
        path_precision=2,
    ),
    "high": dict(
        filter_speckle=2,
        color_precision=8,
        layer_difference=8,
        corner_threshold=60,
        length_threshold=3.5,
        path_precision=3,
    ),
}


# ---------------------------------------------------------------------------
# Konversi (blocking, dijalankan di thread executor)
# ---------------------------------------------------------------------------


def _convert(src, outdir, mode, detail):
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)

    # Ratakan transparansi ke latar putih supaya hasil trace bersih.
    img = img.convert("RGBA")
    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
    background.alpha_composite(img)
    img = background.convert("RGB")

    original_size = img.size
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

    prepared = os.path.join(outdir, "prepared.png")
    img.save(prepared)

    out = os.path.join(outdir, "result.svg")
    vtracer.convert_image_to_svg_py(
        prepared,
        out,
        colormode="binary" if mode == "bw" else "color",
        hierarchical="stacked",
        mode="spline",
        max_iterations=10,
        splice_threshold=45,
        **PRESETS[detail],
    )
    return out, original_size, img.size


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def base_embed(file, mode, detail, color=COLOR_INFO):
    embed = discord.Embed(
        title=clip(file.filename, 256),
        description="Konversi gambar ke SVG (vektor)",
        color=color,
    )
    embed.add_field(name="Mode", value=MODE_LABEL[mode], inline=True)
    embed.add_field(name="Detail", value=DETAIL_LABEL[detail], inline=True)
    embed.add_field(name="Ukuran asli", value=format_bytes(file.size), inline=True)
    if file.content_type and file.content_type.startswith("image/"):
        embed.set_thumbnail(url=file.url)
    return embed


def progress_embed(file, mode, detail, phase):
    embed = base_embed(file, mode, detail)
    text = (
        "Mengonversi ke SVG..."
        if phase == "converting"
        else "Ukuran melebihi batas Discord, mengunggah ke link download..."
    )
    embed.add_field(name="Status", value=text, inline=False)
    return embed


def done_embed(file, mode, detail, svg_size, sizes, link=None, note=None):
    embed = base_embed(file, mode, detail, COLOR_OK)
    original_size, used_size = sizes
    embed.add_field(name="Ukuran SVG", value=format_bytes(svg_size), inline=True)
    embed.add_field(
        name="Dimensi", value=f"{used_size[0]}x{used_size[1]} px", inline=True
    )
    if used_size != original_size:
        embed.add_field(
            name="Diperkecil dari",
            value=f"{original_size[0]}x{original_size[1]} px",
            inline=True,
        )

    if link:
        embed.add_field(
            name="Download SVG",
            value=f"[Klik di sini untuk mengunduh]({link})",
            inline=False,
        )
        embed.set_footer(text=f"Selesai. {note or 'Link bersifat sementara.'}")
    else:
        embed.add_field(name="Cara download", value=GUIDE_TEXT, inline=False)
        embed.set_footer(text="Selesai. File SVG dikirim di pesan di bawah.")
    return embed


def error_embed(title, description):
    return discord.Embed(
        title=title, description=clip(description, 4000), color=COLOR_ERR
    )


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


async def handle(interaction, file, mode, detail):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        await interaction.response.send_message(
            "Format tidak didukung. Gunakan: "
            + ", ".join(sorted(e.lstrip(".") for e in ALLOWED_EXT))
            + ".",
            ephemeral=True,
        )
        return
    if file.size > MAX_INPUT:
        await interaction.response.send_message(
            f"File terlalu besar ({format_bytes(file.size)}). "
            f"Maksimal {format_bytes(MAX_INPUT)}.",
            ephemeral=True,
        )
        return

    owner_id = interaction.user.id
    if owner_id in ACTIVE_USERS:
        await interaction.response.send_message(
            "Kamu masih punya konversi yang berjalan.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    ACTIVE_USERS.add(owner_id)
    limit = upload_limit(interaction)

    message = await interaction.followup.send(
        embed=progress_embed(file, mode, detail, "converting"), wait=True
    )

    async def on_fail(error):
        await message.edit(embed=error_embed("Konversi gagal", error), view=None)

    try:
        async with CONVERT_SLOTS:
            with tempfile.TemporaryDirectory(prefix="svg_") as tmp:
                src = os.path.join(tmp, f"input{ext}")
                await file.save(src)

                loop = asyncio.get_running_loop()
                try:
                    out, original_size, used_size = await asyncio.wait_for(
                        loop.run_in_executor(None, _convert, src, tmp, mode, detail),
                        timeout=CONVERT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    await on_fail(
                        "Konversi terlalu lama. Coba detail lebih rendah "
                        "atau gambar yang lebih kecil."
                    )
                    return
                except Exception as err:  # noqa: BLE001
                    await on_fail("Gambar tidak bisa diproses: " + clean_error(err))
                    return

                if not os.path.isfile(out):
                    await on_fail("File hasil konversi tidak ditemukan.")
                    return

                size = os.path.getsize(out)
                stem = safe_filename(Path(file.filename).stem) or "image"
                sizes = (original_size, used_size)

                # 1) Di bawah batas: kirim langsung sebagai file Discord.
                if size <= limit:
                    try:
                        await interaction.followup.send(
                            file=discord.File(out, filename=f"{stem}.svg")
                        )
                        await message.edit(
                            embed=done_embed(file, mode, detail, size, sizes),
                            view=None,
                        )
                        return
                    except discord.HTTPException as err:
                        print(f"[converter] Upload ke Discord gagal, pakai link: {err}")

                # 2) Melebihi batas (atau upload Discord gagal): kirim sebagai link.
                try:
                    await message.edit(
                        embed=progress_embed(file, mode, detail, "uploading")
                    )
                except discord.HTTPException:
                    pass

                try:
                    link, _host, note = await upload_external(
                        out, ascii_filename(stem, "svg"), size
                    )
                except RuntimeError as err:
                    await on_fail(
                        f"{err} Coba detail lebih rendah supaya file SVG lebih kecil."
                    )
                    return

                await message.edit(
                    embed=done_embed(file, mode, detail, size, sizes, link, note),
                    view=None,
                )
    except Exception as err:  # noqa: BLE001
        try:
            await on_fail(clean_error(err))
        except discord.HTTPException:
            pass
    finally:
        ACTIVE_USERS.discard(owner_id)


# ---------------------------------------------------------------------------
# Registrasi command (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: discord.Client):
    @bot.tree.command(name="svg", description="Konversi gambar (PNG/JPG/dll) ke SVG")
    @app_commands.describe(
        file="Gambar yang mau dikonversi (png, jpg, webp, bmp, gif, tiff)",
        mode="Warna (default) atau Hitam-Putih",
        detail="Tingkat detail (default: Sedang)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Warna", value="color"),
            app_commands.Choice(name="Hitam-Putih", value="bw"),
        ],
        detail=[
            app_commands.Choice(name="Rendah (file kecil)", value="low"),
            app_commands.Choice(name="Sedang", value="mid"),
            app_commands.Choice(name="Tinggi (lebih akurat)", value="high"),
        ],
    )
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    async def svg(
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: app_commands.Choice[str] | None = None,
        detail: app_commands.Choice[str] | None = None,
    ):
        await handle(
            interaction,
            file,
            mode.value if mode else "color",
            detail.value if detail else "mid",
        )
