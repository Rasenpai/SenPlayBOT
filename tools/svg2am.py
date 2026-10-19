import asyncio
import os
import tempfile
from pathlib import Path

import discord
from discord import app_commands

from tools import amxml, svgcore
from tools.svgcore import SvgError
from tools.youtube import (
    ascii_filename,
    clean_error,
    clip,
    format_bytes,
    safe_filename,
    upload_external,
    upload_limit,
)

CONVERT_TIMEOUT = 60  # detik (konversi + pembuatan file)
SHOW_LAYERS = 15  # jumlah nama layer yang ditampilkan di embed

COLOR_INFO = discord.Color.from_rgb(0, 200, 180)
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.dark_red()

ACTIVE_USERS: set[int] = set()

# Metadata per jenis output: (label file, ekstensi, nama di nama file, catatan, panduan)
KINDS = {
    "xml": dict(
        label="XML",
        suffix=".xml",
        size_name="Ukuran XML",
        note=(
            "Struktur XML mengikuti tiga sampel project asli Alight Motion 5.0.273. "
            "Kurva diratakan jadi garis pendek; gradient, teks, gambar, clip, dan filter "
            "belum didukung. **Belum diuji impor langsung di aplikasi**, jadi kalau "
            "gagal atau posisinya aneh, kirim hasilnya agar bisa diperbaiki."
        ),
        guide=(
            "💻 **PC:** arahkan kursor ke file `.xml`, klik titik tiga (**⋯ More**), "
            "lalu **Download**.\n"
            "📱 **HP:** tap file `.xml`, otomatis terunduh.\n"
            "Lalu impor file XML itu dari dalam Alight Motion. "
            "Layer disusun dari bawah ke atas mengikuti urutan SVG."
        ),
    ),
    "zip": dict(
        label="ZIP",
        suffix="_layers.zip",
        size_name="Ukuran ZIP",
        note=(
            "Hasilnya SVG per layer, **bukan** file project XML. Pilih format XML "
            "kalau ingin file project. Hasil impor ke Alight Motion belum diuji."
        ),
        guide=(
            "💻 **PC:** arahkan kursor ke file ZIP, klik titik tiga (**⋯ More**), lalu **Download**.\n"
            "📱 **HP:** tap file ZIP, otomatis terunduh.\n"
            "Ekstrak ZIP, lalu impor file di folder `layers/` ke Alight Motion. "
            "Nomor kecil = layer paling bawah. Detail ada di `README.txt` dalam ZIP."
        ),
    ),
}


def base_embed(file, kind, color=COLOR_INFO):
    desc = (
        "Ubah SVG jadi file project XML Alight Motion"
        if kind == "xml"
        else "Pecah SVG jadi layer terpisah (ZIP) untuk Alight Motion"
    )
    return discord.Embed(title=clip(file.filename, 256), description=desc, color=color)


def progress_embed(file, kind, text):
    embed = base_embed(file, kind)
    embed.add_field(name="Status", value=text, inline=False)
    return embed


def done_embed(file, kind, conversion, out_size, link=None, note=None):
    k = KINDS[kind]
    embed = base_embed(file, kind, COLOR_OK)
    c = conversion.canvas
    embed.add_field(
        name="Kanvas",
        value=f"{svgcore.fmt(c.width)} x {svgcore.fmt(c.height)} px",
        inline=True,
    )
    embed.add_field(name="Layer", value=str(len(conversion.layers)), inline=True)
    embed.add_field(name=k["size_name"], value=format_bytes(out_size), inline=True)

    names = [
        f"`{l.index}.` {l.name} ({l.tag})" for l in conversion.layers[:SHOW_LAYERS]
    ]
    extra = len(conversion.layers) - SHOW_LAYERS
    if extra > 0:
        names.append(f"... dan {extra} layer lagi")
    embed.add_field(
        name="Daftar layer (bawah ke atas)",
        value=clip("\n".join(names), 1024),
        inline=False,
    )

    r = conversion.report
    notes = []
    if r.removed:
        notes.append(
            "Dibuang demi keamanan: "
            + ", ".join(f"{key} ({v})" for key, v in r.removed.items())
        )
    if r.unsupported:
        notes.append(
            "Tidak didukung / diubah: "
            + ", ".join(f"{key} ({v})" for key, v in r.unsupported.items())
        )
    if r.hidden:
        notes.append(f"Elemen tersembunyi dilewati: {r.hidden}")
    notes.extend(r.warnings)
    if notes:
        embed.add_field(
            name="Catatan", value=clip("\n".join(notes), 1024), inline=False
        )

    if link:
        embed.add_field(
            name=f"Download {k['label']}",
            value=f"[Klik di sini untuk mengunduh]({link})",
            inline=False,
        )
        embed.set_footer(text=f"Selesai. {note or 'Link bersifat sementara.'}")
    else:
        embed.add_field(name="Cara download", value=k["guide"], inline=False)
        embed.set_footer(text=f"Selesai. File {k['label']} dikirim di pesan di bawah.")

    embed.add_field(name="Kompatibilitas", value=clip(k["note"], 1024), inline=False)
    return embed


def error_embed(title, description):
    return discord.Embed(
        title=title, description=clip(description, 4000), color=COLOR_ERR
    )


async def handle(interaction: discord.Interaction, file: discord.Attachment, kind: str):
    if not file.filename.lower().endswith(".svg"):
        await interaction.response.send_message(
            "Format tidak didukung. Upload file berekstensi `.svg`.", ephemeral=True
        )
        return
    if file.size > svgcore.MAX_BYTES:
        await interaction.response.send_message(
            f"File terlalu besar ({format_bytes(file.size)}). "
            f"Maksimal {format_bytes(svgcore.MAX_BYTES)}.",
            ephemeral=True,
        )
        return

    owner_id = interaction.user.id
    if owner_id in ACTIVE_USERS:
        await interaction.response.send_message(
            "Kamu masih punya konversi yang berjalan.", ephemeral=True
        )
        return

    k = KINDS[kind]
    await interaction.response.defer(thinking=True)
    ACTIVE_USERS.add(owner_id)
    limit = upload_limit(interaction)
    message = await interaction.followup.send(
        embed=progress_embed(file, kind, "Membaca dan mengonversi SVG..."), wait=True
    )

    async def on_fail(text):
        await message.edit(embed=error_embed("Konversi gagal", text), view=None)

    try:
        data = await file.read()
        stem = safe_filename(Path(file.filename).stem) or "svg"
        out_name = f"{stem}{k['suffix']}"
        loop = asyncio.get_running_loop()

        with tempfile.TemporaryDirectory(prefix="svg2am_") as tmp:
            out_path = os.path.join(tmp, out_name)

            def work():
                if kind == "xml":
                    result = amxml.convert_to_xml(data, stem)
                    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(result.xml)
                    return result.conversion
                conv = svgcore.convert(data)
                svgcore.build_zip(conv, file.filename, out_path)
                return conv

            try:
                conversion = await asyncio.wait_for(
                    loop.run_in_executor(None, work),
                    timeout=CONVERT_TIMEOUT,
                )
            except SvgError as err:
                await on_fail(str(err))
                return
            except asyncio.TimeoutError:
                await on_fail(
                    f"Konversi melebihi {CONVERT_TIMEOUT} detik. SVG terlalu kompleks."
                )
                return

            size = os.path.getsize(out_path)

            if size <= limit:
                try:
                    await interaction.followup.send(
                        file=discord.File(out_path, filename=out_name)
                    )
                    await message.edit(
                        embed=done_embed(file, kind, conversion, size), view=None
                    )
                    return
                except discord.HTTPException as err:
                    print(f"[svg2am] Upload ke Discord gagal, pakai link: {err}")

            try:
                await message.edit(
                    embed=progress_embed(
                        file,
                        kind,
                        "Ukuran melebihi batas Discord, mengunggah ke link...",
                    )
                )
            except discord.HTTPException:
                pass
            try:
                link, _host, note = await upload_external(
                    out_path,
                    ascii_filename(stem + ("" if kind == "xml" else "_layers"), kind),
                    size,
                )
            except RuntimeError as err:
                await on_fail(str(err))
                return
            await message.edit(
                embed=done_embed(file, kind, conversion, size, link, note), view=None
            )
    except Exception as err:  # noqa: BLE001
        try:
            await on_fail(clean_error(err))
        except discord.HTTPException:
            pass
    finally:
        ACTIVE_USERS.discard(owner_id)


def setup(bot: discord.Client):
    @bot.tree.command(
        name="svg2am",
        description="Ubah file SVG jadi project XML Alight Motion (atau ZIP layer SVG)",
    )
    @app_commands.describe(
        file=f"File SVG (.svg, maks {svgcore.MAX_BYTES // svgcore.MB} MB)",
        format="XML project Alight Motion (default) atau ZIP berisi SVG per layer",
    )
    @app_commands.choices(
        format=[
            app_commands.Choice(name="XML project (default)", value="xml"),
            app_commands.Choice(name="ZIP layer SVG", value="zip"),
        ]
    )
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: i.user.id)
    async def svg2am(
        interaction: discord.Interaction,
        file: discord.Attachment,
        format: app_commands.Choice[str] | None = None,
    ):
        await handle(interaction, file, format.value if format else "xml")
