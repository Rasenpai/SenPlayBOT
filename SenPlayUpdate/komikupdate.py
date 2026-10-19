"""SenPlayUpdate: kirim embed otomatis saat ada chapter komik baru (project & mirror)."""

import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

CHECK_INTERVAL_SECONDS = 300  # cek API tiap 5 menit
SEND_DELAY_SECONDS = 1.5  # jeda antar pesan supaya aman dari rate limit

# GANTI: domain web komik & pola link halaman komik (API tidak punya slug, pakai id)
KOMIK_SITE = "komik.senplay.web.id"
KOMIK_URL_TEMPLATE = "https://komik.senplay.web.id/komik/{id}"

# GANTI channel_id sesuai kebutuhan (boleh beda channel untuk project & mirror)
CATEGORIES = {
    "project": {
        "api": "https://api.senplay.web.id/api/shinigami/manga/update/project?page=1",
        "channel_id": 1561638645130199130,
        "header": "📖 Komik Project Rilis Chapter Baru!",
        "color": 0xF59E0B,
    },
    "mirror": {
        "api": "https://api.senplay.web.id/api/shinigami/manga/update/mirror?page=1",
        "channel_id": 1561638645130199130,
        "header": "📚 Update Chapter Baru Komik Mirror!",
        "color": 0x8B5CF6,
    },
}

WIB = timezone(timedelta(hours=7))
FLAGS = {"KR": "🇰🇷", "CN": "🇨🇳", "JP": "🇯🇵"}
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "komikupdate_state.json"
)


# ---------- state ----------
def _load_state() -> dict[str, dict[str, float]]:
    """{kategori: {manga_id: chapter terakhir}}. Kategori yang belum ada = belum diinisialisasi."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            cat: {str(k): float(v) for k, v in items.items()}
            for cat, items in raw.items()
        }
    except (OSError, ValueError, TypeError, AttributeError):
        print("[komikupdate] state rusak, di-reset")
        return {}


def _save_state(state: dict[str, dict[str, float]]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---------- helper format ----------
def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_chapter(ch: float) -> str:
    return str(int(ch)) if float(ch).is_integer() else str(ch)


def _footer_time() -> str:
    now = datetime.now(WIB)
    return f"Today at {now.strftime('%I:%M %p').lstrip('0')}"


def _clean_synopsis(text: str | None, limit: int = 220) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\\", "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _names(items: list | None, limit: int) -> str:
    names = []
    for it in items or []:
        name = (it or {}).get("name")
        if name and name not in names:
            names.append(name)
    return ", ".join(names[:limit])


def _unix(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def build_embed(item: dict, chapter: float, cat_cfg: dict) -> discord.Embed:
    title = item["title"]
    url = KOMIK_URL_TEMPLATE.format(id=item["id"])
    tax = item.get("taxonomy") or {}

    embed = discord.Embed(
        title=cat_cfg["header"],
        description=(
            f"**{title}** — Chapter {_fmt_chapter(chapter)} kini sudah tersedia di SenPlay!\n"
            f"▶️ [Baca Chapter Terbaru]({url})"
        ),
        url=url,
        color=cat_cfg["color"],
    )

    synopsis = _clean_synopsis(item.get("description"))
    if synopsis:
        embed.add_field(name="📝 Sinopsis", value=synopsis, inline=False)

    fmt = _names(tax.get("formats"), 1) or "Komik"
    flag = FLAGS.get(str(item.get("country", "")).upper(), "")
    embed.add_field(name="📚 Tipe", value=f"{fmt} {flag}".strip(), inline=True)

    rating = _to_float(item.get("rating"))
    if rating:
        embed.add_field(name="⭐ Rating", value=f"{rating:g}", inline=True)

    ts = _unix(item.get("latestChapterTime"))
    if ts:
        embed.add_field(name="🕒 Rilis", value=f"<t:{ts}:R>", inline=True)

    genres = _names(tax.get("genres"), 5)
    if genres:
        embed.add_field(
            name="🏷️ Genre", value=genres.replace(", ", " • "), inline=False
        )

    author = _names(tax.get("authors"), 2)
    if author:
        embed.add_field(name="✍️ Author", value=author, inline=True)

    cover = item.get("cover") or item.get("coverPortrait")
    portrait = item.get("coverPortrait")
    if cover:
        embed.set_image(url=cover)
    if portrait and portrait != cover:
        embed.set_thumbnail(url=portrait)

    embed.set_footer(text=f"{KOMIK_SITE} • {_footer_time()}")
    return embed


# ---------- tugas utama ----------
class KomikUpdate:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.state = _load_state()
        self.check_loop.start()

    async def _fetch(self, api_url: str) -> list[dict]:
        session = getattr(self.bot, "http_session", None)
        own_session = None
        if session is None:
            own_session = session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        try:
            async with session.get(api_url) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        finally:
            if own_session:
                await own_session.close()

        results = (payload.get("data") or {}).get("data") or []
        return [r for r in results if r.get("id") and r.get("title")]

    async def _get_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        return channel

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_loop(self):
        for cat, cfg in CATEGORIES.items():
            try:
                await self._check_category(cat, cfg)
            except Exception as e:  # satu kategori gagal tidak mematikan yang lain
                print(f"[komikupdate:{cat}] error: {type(e).__name__}: {e}")

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check_category(self, cat: str, cfg: dict):
        items = await self._fetch(cfg["api"])
        if not items:
            return

        # Run pertama untuk kategori ini: simpan saja, jangan kirim semuanya.
        if cat not in self.state:
            self.state[cat] = {
                it["id"]: ch
                for it in items
                if (ch := _to_float(it.get("latestChapter"))) is not None
            }
            _save_state(self.state)
            print(
                f"[komikupdate:{cat}] inisialisasi: {len(self.state[cat])} komik tersimpan"
            )
            return

        seen = self.state[cat]
        new_items = []
        for it in items:
            ch = _to_float(it.get("latestChapter"))
            if ch is None:
                continue
            last = seen.get(it["id"])
            if last is None or ch > last:
                new_items.append((it, ch))

        if not new_items:
            return

        channel = await self._get_channel(cfg["channel_id"])
        # API urut terbaru -> terlama, kirim dari yang paling lama biar urutan rapi.
        for it, ch in reversed(new_items):
            try:
                await channel.send(embed=build_embed(it, ch, cfg))
            except discord.HTTPException as e:
                print(f"[komikupdate:{cat}] gagal kirim {it['id']}: {e}")
                continue  # state tidak diubah, dicoba lagi di cek berikutnya
            seen[it["id"]] = ch
            _save_state(self.state)
            await asyncio.sleep(SEND_DELAY_SECONDS)


_instance: KomikUpdate | None = None  # simpan referensi agar tidak di-garbage-collect


def setup(bot: discord.Client):
    global _instance
    if _instance is None:  # cegah loop ganda kalau setup dipanggil dua kali
        _instance = KomikUpdate(bot)
