"""SenPlayUpdate: kirim embed otomatis saat ada series baru (tabel `series` & `clear_series`)."""

import asyncio
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

CHANNEL_ID = 1561638645130199130  # ganti kalau series punya channel sendiri
CHECK_INTERVAL_SECONDS = 300  # cek tiap 5 menit
SEND_DELAY_SECONDS = 1.5
FETCH_LIMIT = 50  # ambil 50 data terbaru per tabel tiap cek
MAX_SEND_PER_CHECK = 15  # cegah banjir pesan kalau ada import massal
MAX_STATE = 1000  # batas id yang disimpan di file state
EMBED_COLOR = 0xEF4444

SITE = "movie.senplay.web.id"
# Sesuaikan rute situs. `series` memakai provider_slug (tabel itu tidak punya kolom slug).
SERIES_URL = "https://movie.senplay.web.id/series/detail/{slug}"
CLEAR_SERIES_URL = "https://movie.senplay.web.id/series/detail/{slug}"
ADDED_BY = "@senpaii._28"

# Hanya clear_series berstatus ini yang diumumkan (draft tidak ikut).
CLEAR_PUBLISHED_STATUS = "published"

# poster_path: "/abc.jpg" -> path TMDB, "abc.jpg" -> file di Supabase Storage.
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
POSTER_BUCKET = "posters"

SERIES_COLUMNS = (
    "id,title,provider_slug,description,poster_path,release_date,region,country,"
    "status,genres,rating,total_episodes,category,type,created_at"
)
CLEAR_COLUMNS = (
    "id,title,slug,description,poster_path,release_year,release_date,rating,"
    "genres,country,category,total_episodes,number_of_seasons,airing_status,"
    "networks,content_rating,created_at"
)

AIRING_LABEL = {"complete": "Tamat", "ongoing": "Sedang Tayang"}

WIB = timezone(timedelta(hours=7))
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seriesupdate_state.json"
)


@dataclass
class Feed:
    key: str  # awalan id di file state ("series:<id>" / "clear:<id>")
    table: str
    columns: str
    filters: dict
    slug_field: str  # kolom untuk URL detail
    url_tpl: str


FEEDS = [
    Feed("series", "series", SERIES_COLUMNS, {}, "provider_slug", SERIES_URL),
    Feed(
        "clear",
        "clear_series",
        CLEAR_COLUMNS,
        {"status": f"eq.{CLEAR_PUBLISHED_STATUS}"},
        "slug",
        CLEAR_SERIES_URL,
    ),
]


# ---------- state ----------
def _load_state() -> list[str] | None:
    """Daftar kunci ('feed:id') yang sudah diproses. None = run pertama."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return [str(x) for x in json.load(f)]
    except (OSError, ValueError, TypeError):
        print("[seriesupdate] state rusak, di-reset")
        return None


def _save_state(keys: list[str]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys[-MAX_STATE:], f)
    os.replace(tmp, STATE_FILE)


# ---------- helper ----------
def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(iso: str | None) -> datetime:
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(WIB)
        except ValueError:
            pass
    return datetime.now(WIB)


def _clean_synopsis(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _footer_time() -> str:
    now = datetime.now(WIB)
    return f"Today at {now.strftime('%I:%M %p').lstrip('0')}"


def _join(value, limit: int | None = None) -> str:
    if not value:
        return ""
    items = value.split(",") if isinstance(value, str) else value
    items = [str(i).strip() for i in items if str(i).strip()]
    return " • ".join(items[:limit] if limit else items)


def _poster_url(path: str | None, supabase_url: str) -> str | None:
    if not path:
        return None
    path = str(path).strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("/"):
        return TMDB_IMG + path
    return f"{supabase_url}/storage/v1/object/public/{POSTER_BUCKET}/{path}"


def _year(row: dict) -> str:
    if row.get("release_year"):
        return str(row["release_year"])
    date = str(row.get("release_date") or "")
    return date[:4] if len(date) >= 4 else ""


def build_embed(feed: Feed, row: dict, supabase_url: str) -> discord.Embed:
    title = row["title"]
    url = feed.url_tpl.format(slug=row[feed.slug_field])
    added = _parse_time(row.get("created_at"))

    embed = discord.Embed(
        title="📺 Series Baru Telah Ditambahkan!",
        description=(
            f"**{title}** kini sudah tersedia di SenPlay!\n"
            "Siapkan cemilan dan selamat menonton. 🎬\n\n"
            f"▶️ [Tonton Sekarang]({url})\n\n"
            f"🕒 *Di tambahkan pada pukul {added.strftime('%H.%M')} WIB oleh {ADDED_BY}*"
        ),
        url=url,
        color=EMBED_COLOR,
    )

    rating = _to_float(row.get("rating"))
    if rating:
        embed.add_field(name="⭐ Rating", value=f"{rating:g}", inline=True)
    year = _year(row)
    if year:
        embed.add_field(name="📅 Tahun", value=year, inline=True)

    country = row.get("country") or row.get("region")
    if country:
        embed.add_field(name="🌏 Negara", value=str(country), inline=True)

    seasons = row.get("number_of_seasons")
    if seasons:
        embed.add_field(name="🗂️ Season", value=str(seasons), inline=True)
    episodes = row.get("total_episodes")
    if episodes:
        embed.add_field(name="📺 Episode", value=str(episodes), inline=True)

    # clear_series punya airing_status; tabel series hanya status teks bebas.
    status = AIRING_LABEL.get(row.get("airing_status") or "") or row.get("status")
    if status:
        embed.add_field(name="📡 Status", value=str(status), inline=True)

    category = row.get("category") or row.get("type")
    if category:
        embed.add_field(name="🎭 Kategori", value=str(category), inline=True)
    if row.get("content_rating"):
        embed.add_field(
            name="🔖 Kelayakan", value=str(row["content_rating"]), inline=True
        )
    networks = _join(row.get("networks"), limit=3)
    if networks:
        embed.add_field(name="📡 Network", value=networks, inline=True)

    genres = _join(row.get("genres"))
    if genres:
        embed.add_field(name="🏷️ Genre", value=genres, inline=False)

    synopsis = _clean_synopsis(row.get("description"))
    if synopsis:
        embed.add_field(name="📝 Sinopsis", value=synopsis, inline=False)

    poster = _poster_url(row.get("poster_path"), supabase_url)
    if poster:
        embed.set_image(url=poster)

    embed.set_footer(text=f"{SITE} • {_footer_time()}")
    return embed


# ---------- tugas utama ----------
class SeriesUpdate:
    def __init__(self, bot: discord.Client, supabase_url: str, supabase_key: str):
        self.bot = bot
        self.base_url = supabase_url.rstrip("/")
        self.headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        }
        loaded = _load_state()
        self.first_run = loaded is None
        self.seen: list[str] = loaded or []
        self.check_loop.start()

    async def _fetch_feed(
        self, session: aiohttp.ClientSession, feed: Feed
    ) -> list[dict]:
        params = {
            "select": feed.columns,
            "order": "created_at.desc",
            "limit": str(FETCH_LIMIT),
            **feed.filters,
        }
        async with session.get(
            f"{self.base_url}/rest/v1/{feed.table}", params=params, headers=self.headers
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"Supabase HTTP {resp.status}: {body}")
            rows = await resp.json()
        return [
            r for r in rows if r.get("id") and r.get("title") and r.get(feed.slug_field)
        ]

    async def _fetch(self) -> tuple[list[tuple[Feed, dict]], bool]:
        """Return (baris dari semua tabel, True kalau semua tabel berhasil diambil)."""
        session = getattr(self.bot, "http_session", None)
        own_session = None
        if session is None:
            own_session = session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        items: list[tuple[Feed, dict]] = []
        all_ok = True
        try:
            for feed in FEEDS:
                try:
                    for row in await self._fetch_feed(session, feed):
                        items.append((feed, row))
                except Exception as e:  # satu tabel gagal, yang lain tetap jalan
                    all_ok = False
                    print(
                        f"[seriesupdate] gagal ambil {feed.table}: {type(e).__name__}: {e}"
                    )
        finally:
            if own_session:
                await own_session.close()
        # lama -> baru supaya pengiriman berurutan
        items.sort(key=lambda it: _parse_time(it[1].get("created_at")))
        return items, all_ok

    async def _get_channel(self):
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHANNEL_ID)
        return channel

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_loop(self):
        try:
            await self._check()
        except Exception as e:
            print(f"[seriesupdate] error: {type(e).__name__}: {e}")

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check(self):
        items, all_ok = await self._fetch()

        # Run pertama: simpan saja, jangan kirim puluhan series sekaligus.
        # Tunggu sampai kedua tabel berhasil dibaca supaya state lengkap.
        if self.first_run:
            if not all_ok or not items:
                return
            self.seen = [f"{feed.key}:{row['id']}" for feed, row in items]
            _save_state(self.seen)
            self.first_run = False
            print(f"[seriesupdate] inisialisasi: {len(self.seen)} series tersimpan")
            return

        seen_set = set(self.seen)
        new_items = [
            (feed, row)
            for feed, row in items
            if f"{feed.key}:{row['id']}" not in seen_set
        ]
        if not new_items:
            return

        channel = await self._get_channel()
        for feed, row in new_items[:MAX_SEND_PER_CHECK]:
            key = f"{feed.key}:{row['id']}"
            try:
                await channel.send(embed=build_embed(feed, row, self.base_url))
            except discord.HTTPException as e:
                print(f"[seriesupdate] gagal kirim {key}: {e}")
                continue  # dicoba lagi di cek berikutnya
            self.seen.append(key)
            _save_state(self.seen)
            await asyncio.sleep(SEND_DELAY_SECONDS)


_instance: SeriesUpdate | None = None  # simpan referensi agar tidak di-garbage-collect


def setup(bot: discord.Client):
    global _instance
    if _instance is not None:
        return
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        print(
            "[seriesupdate] SUPABASE_URL / SUPABASE_KEY belum diisi di .env, fitur dimatikan"
        )
        return
    _instance = SeriesUpdate(bot, url, key)
