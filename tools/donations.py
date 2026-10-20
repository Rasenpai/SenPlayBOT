import discord
from discord.ext import tasks
from datetime import datetime
from typing import Any

API_URL = "https://api.senpanime.xyz/api/donations/summary"
CHANNEL_ID = 1561638837535379529
POLL_INTERVAL = 60  # detik
REF_PREFIX = (
    "ref:"  # penanda di footer embed, dipakai untuk melanjutkan setelah restart
)

COLOR_SUMMARY = discord.Color.from_rgb(255, 105, 180)
COLOR_THANKS = discord.Color.gold()

NO_MENTIONS = discord.AllowedMentions.none()

# None = belum pernah diproses sejak bot menyala.
_state: dict[str, str | None] = {"last": None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_text(text):
    """Bersihkan teks dari user supaya tidak merusak format / mention."""
    text = discord.utils.escape_mentions(str(text or ""))
    return discord.utils.escape_markdown(text)


def level_label(level):
    labels = {"supporter": "Supporter", "supporter_plus": "Supporter+"}
    if not level:
        return None
    return labels.get(level, str(level).replace("_", " ").title())


def donation_key(d):
    return f"{d.get('createdAt')}|{d.get('displayName')}|{d.get('amount')}"


def to_timestamp(iso):
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (TypeError, ValueError):
        return None


def rel_time(iso):
    ts = to_timestamp(iso)
    return f"<t:{ts}:D> (<t:{ts}:R>)" if ts else "-"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def fetch_summary(bot):
    async with bot.http_session.get(API_URL) as resp:
        if resp.status != 200:
            raise RuntimeError(f"API HTTP {resp.status}")
        data = await resp.json(content_type=None)
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError("Respons API donasi tidak valid")
    return data


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------


def summary_embeds(data):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    top_lines = []
    for t in data.get("topDonators") or []:
        rank = t.get("rank")
        badge = medals.get(rank, f"#{rank}")
        level = level_label(t.get("supporterLevel"))
        line = (
            f"{badge} **{clean_text(t.get('displayName'))}** — "
            f"{t.get('displayTotalAmount')} ({t.get('donationCount')}x)"
        )
        if level:
            line += f" · {level}"
        top_lines.append(line)

    top = discord.Embed(
        title="🏆 Top Donatur SenPlay",
        description=clip("\n".join(top_lines) or "Belum ada donatur.", 4000),
        color=COLOR_SUMMARY,
    )

    recent_lines = []
    for d in data.get("recentDonations") or []:
        line = (
            f"💖 **{clean_text(d.get('displayName'))}** — "
            f"{d.get('displayAmount')} · {rel_time(d.get('createdAt'))}"
        )
        if d.get("message"):
            line += f"\n> {clip(clean_text(d['message']), 200)}"
        recent_lines.append(line)

    recent = discord.Embed(
        title="🕒 Donasi Terbaru",
        description=clip("\n\n".join(recent_lines) or "Belum ada donasi.", 4000),
        color=COLOR_SUMMARY,
    )

    latest = (data.get("recentDonations") or [None])[0]
    if latest:
        recent.set_footer(text=f"{REF_PREFIX}{donation_key(latest)}")
    return [top, recent]


def thanks_embed(d):
    name = clean_text(d.get("displayName") or "Seseorang")
    amount = d.get("displayAmount") or "-"

    embed = discord.Embed(
        title="💖 Terima kasih atas donasinya!",
        description=(
            f"Terima kasih banyak **{name}** sudah berdonasi **{amount}** "
            "untuk SenPlay! Dukunganmu sangat berarti buat kami. 🙏"
        ),
        color=COLOR_THANKS,
    )
    if d.get("message"):
        embed.add_field(
            name="Pesan",
            value=clip(f"> {clean_text(d['message'])}", 1024),
            inline=False,
        )
    level = level_label(d.get("supporterLevel"))
    if level:
        embed.add_field(name="Level", value=level, inline=True)
    embed.add_field(name="Waktu", value=rel_time(d.get("createdAt")), inline=True)
    embed.set_footer(text=f"{REF_PREFIX}{donation_key(d)}")
    return embed


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


async def get_channel(bot) -> Any:
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    return channel


async def find_last_ref(channel, bot_user):
    """Cari penanda donasi terakhir yang pernah dikirim bot di channel ini."""
    async for msg in channel.history(limit=100):
        if msg.author.id != bot_user.id:
            continue
        for embed in msg.embeds:
            text = embed.footer.text if embed.footer else None
            if text and text.startswith(REF_PREFIX):
                return text[len(REF_PREFIX) :]
    return None


async def poll(bot):
    data = await fetch_summary(bot)
    recent = data.get("recentDonations") or []
    channel = await get_channel(bot)

    # Pertama kali: kirim semua data (kecuali sudah pernah dikirim sebelum restart).
    if _state["last"] is None:
        last = await find_last_ref(channel, bot.user)
        if last is None:
            await channel.send(
                embeds=summary_embeds(data), allowed_mentions=NO_MENTIONS
            )
            _state["last"] = donation_key(recent[0]) if recent else ""
            return
        _state["last"] = last

    # Selanjutnya: cukup baca donasi paling atas.
    if not recent:
        return
    latest = recent[0]
    key = donation_key(latest)
    if key == _state["last"]:
        return

    await channel.send(embed=thanks_embed(latest), allowed_mentions=NO_MENTIONS)
    _state["last"] = key


# ---------------------------------------------------------------------------
# Registrasi (dipanggil dari bot.py)
# ---------------------------------------------------------------------------


def setup(bot: Any):
    @tasks.loop(seconds=POLL_INTERVAL)
    async def donation_loop():
        try:
            await poll(bot)
        except Exception as err:  # noqa: BLE001
            print(f"[donations] gagal: {type(err).__name__}: {err}")

    @donation_loop.before_loop
    async def _before():
        await bot.wait_until_ready()

    bot.donation_loop = donation_loop  # simpan referensi supaya tidak di-GC
    donation_loop.start()
