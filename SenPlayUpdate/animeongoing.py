"""SenPlayUpdate: kirim embed otomatis saat ada episode anime baru (ongoing)."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

API_URL = "https://api.senplay.web.id/api/animasu/anime/ongoing?page=1"
CHANNEL_ID = 1561638645130199130
CHECK_INTERVAL_SECONDS = 300  # cek API tiap 5 menit
SEND_DELAY_SECONDS = 1.5  # jeda antar pesan supaya aman dari rate limit
EMBED_COLOR = 0x8B5CF6

WIB = timezone(timedelta(hours=7))
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "animeongoing_state.json"
)


def _load_state() -> dict[str, int] | None:
    """Return {slug: episode terakhir}. None kalau file belum ada (run pertama)."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        print(
            "[animeongoing] state rusak, di-reset (data lama dianggap sudah terkirim)"
        )
        return None


def _save_state(state: dict[str, int]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _footer_time() -> str:
    now = datetime.now(WIB)
    return f"Today at {now.strftime('%I:%M %p').lstrip('0')}"


def build_embed(
    title: str, episode: int, slug: str, poster: str | None
) -> discord.Embed:
    url = f"https://anime.senplay.web.id/anime/{slug}?source=animasu"
    embed = discord.Embed(
        title="🎬 Episode Anime Terbaru Telah Rilis!",
        description=(
            f"**{title}** — Episode {episode} kini sudah tersedia di SenPlay!\n"
            "Selamat menonton dan nikmati episode terbarunya. 🍿\n\n"
            f"▶️ [Tonton Episode Terbaru]({url})\n\n"
            "Enjoy watching di SenPlay! 💜"
        ),
        url=url,
        color=EMBED_COLOR,
    )
    if poster:
        embed.set_image(url=poster)
    embed.set_footer(text=f"anime.senplay.web.id • {_footer_time()}")
    return embed


class AnimeOngoing:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.state: dict[str, int] = {}
        self.first_run = False
        loaded = _load_state()
        if loaded is None:
            self.first_run = True
        else:
            self.state = loaded
        self.check_loop.start()

    async def _fetch(self) -> list[dict]:
        session = getattr(self.bot, "http_session", None)
        own_session = None
        if session is None:
            own_session = session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        try:
            async with session.get(API_URL) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        finally:
            if own_session:
                await own_session.close()

        results = (payload.get("data") or {}).get("results") or []
        return [r for r in results if r.get("slug") and r.get("title")]

    async def _get_channel(self):
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHANNEL_ID)
        return channel

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_loop(self):
        try:
            await self._check()
        except Exception as e:  # jangan sampai loop mati
            print(f"[animeongoing] error: {type(e).__name__}: {e}")

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check(self):
        items = await self._fetch()
        if not items:
            return

        # Run pertama: simpan data yang ada saja, jangan kirim 50 pesan sekaligus.
        if self.first_run:
            for it in items:
                ep = _to_int(it.get("episode"))
                if ep is not None:
                    self.state[it["slug"]] = ep
            _save_state(self.state)
            self.first_run = False
            print(f"[animeongoing] inisialisasi: {len(self.state)} anime tersimpan")
            return

        # Cari yang baru: slug belum pernah dilihat, atau episodenya naik.
        new_items = []
        for it in items:
            ep = _to_int(it.get("episode"))
            if ep is None:
                continue
            last = self.state.get(it["slug"])
            if last is None or ep > last:
                new_items.append((it, ep))

        if not new_items:
            return

        channel = await self._get_channel()
        # API urut terbaru -> terlama, kirim dari yang paling lama biar urutan rapi.
        for it, ep in reversed(new_items):
            embed = build_embed(it["title"], ep, it["slug"], it.get("image"))
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                print(f"[animeongoing] gagal kirim {it['slug']}: {e}")
                continue  # state tidak diubah, dicoba lagi di cek berikutnya
            self.state[it["slug"]] = ep
            _save_state(self.state)
            await asyncio.sleep(SEND_DELAY_SECONDS)


_instance: AnimeOngoing | None = None  # simpan referensi agar tidak di-garbage-collect


def setup(bot: discord.Client):
    global _instance
    if _instance is None:  # cegah loop ganda kalau setup dipanggil dua kali
        _instance = AnimeOngoing(bot)
