"""SenPlayUpdate: kirim embed otomatis saat ada anime baru di tabel `anime` (Supabase)."""

import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

CHANNEL_ID = 1561638645130199130  # ganti kalau anime punya channel sendiri
CHECK_INTERVAL_SECONDS = 15  # cek tiap 15 detik (query ringan: hanya kolom id)
SEND_DELAY_SECONDS = 1.5
FETCH_LIMIT = 200  # jendela id published terbaru (urut updated_at)
MAX_SEND_PER_CHECK = 15  # cegah banjir pesan kalau ada import massal
MAX_STATE = 2000  # batas id yang disimpan di file state
EMBED_COLOR = 0xEF4444

SITE = "anime.senplay.web.id"
DETAIL_URL = "https://anime.senplay.web.id/anime-project/{slug}"  # sesuaikan rute situs
ADDED_BY = "@senpaii._28"

# Hanya anime berstatus ini yang diumumkan (draft / unpublished tidak ikut).
PUBLISHED_STATUS = "published"

# poster_url di tabel anime sudah URL penuh. poster_path (kalau poster_url kosong):
#   "/abc.jpg" -> path TMDB, "abc.jpg" -> file di Supabase Storage.
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
POSTER_BUCKET = "posters"

COLUMNS = (
    "id,title,slug,description,format,season,season_year,release_year,"
    "airing_status,total_episodes,duration_minutes,is_adult,rating,genre,"
    "studios,poster_url,poster_path,created_at,updated_at"
)

AIRING_LABEL = {
    "FINISHED": "Tamat",
    "RELEASING": "Sedang Tayang",
    "NOT_YET_RELEASED": "Belum Tayang",
    "CANCELLED": "Dibatalkan",
    "HIATUS": "Hiatus",
}
SEASON_LABEL = {
    "WINTER": "Winter",
    "SPRING": "Spring",
    "SUMMER": "Summer",
    "FALL": "Fall",
}
FORMAT_LABEL = {
    "TV": "TV",
    "TV_SHORT": "TV Short",
    "MOVIE": "Movie",
    "SPECIAL": "Special",
    "OVA": "OVA",
    "ONA": "ONA",
    "MUSIC": "Music",
}

WIB = timezone(timedelta(hours=7))
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "animeupdate_state.json"
)


# ---------- state ----------
def _load_state() -> list[str] | None:
    """Daftar id anime yang sudah diproses. None = run pertama."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return [str(x) for x in json.load(f)]
    except (OSError, ValueError, TypeError):
        print("[animeupdate] state rusak, di-reset")
        return None


def _save_state(ids: list[str]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ids[-MAX_STATE:], f)
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


def _duration(minutes) -> str:
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} jam {mins} menit"
    if hours:
        return f"{hours} jam"
    return f"{mins} menit"


def _poster_url(row: dict, supabase_url: str) -> str | None:
    url = row.get("poster_url")
    if url and str(url).startswith("http"):
        return str(url)
    path = row.get("poster_path")
    if not path:
        return None
    path = str(path).strip()
    if path.startswith("/"):
        return TMDB_IMG + path
    return f"{supabase_url}/storage/v1/object/public/{POSTER_BUCKET}/{path}"


def _season_text(row: dict) -> str:
    season = SEASON_LABEL.get(row.get("season") or "")
    year = row.get("season_year") or row.get("release_year")
    if season and year:
        return f"{season} {year}"
    return str(year) if year else ""


def build_embed(anime: dict, supabase_url: str) -> discord.Embed:
    title = anime["title"]
    url = DETAIL_URL.format(slug=anime["slug"])
    # updated_at = waktu terakhir diubah (termasuk saat di-publish)
    added = _parse_time(anime.get("updated_at") or anime.get("created_at"))
    is_adult = bool(anime.get("is_adult"))

    embed = discord.Embed(
        title="🌸 Anime Baru Telah Ditambahkan!",
        description=(
            f"**{title}** kini sudah tersedia di SenPlay!\n"
            "Siapkan cemilan dan selamat menonton. 🎬\n\n"
            f"▶️ [Tonton Sekarang]({url})\n\n"
            f"🕒 *Di tambahkan pada pukul {added.strftime('%H.%M')} WIB oleh {ADDED_BY}*"
        ),
        url=url,
        color=EMBED_COLOR,
    )

    rating = _to_float(anime.get("rating"))
    if rating:
        embed.add_field(name="⭐ Rating", value=f"{rating:g}", inline=True)
    season = _season_text(anime)
    if season:
        embed.add_field(name="📅 Musim", value=season, inline=True)
    fmt = FORMAT_LABEL.get(anime.get("format") or "")
    if fmt:
        embed.add_field(name="🎞️ Format", value=fmt, inline=True)

    episodes = anime.get("total_episodes")
    if episodes:
        embed.add_field(name="📺 Episode", value=str(episodes), inline=True)
    duration = _duration(anime.get("duration_minutes"))
    if duration:
        embed.add_field(name="⏱️ Durasi", value=duration, inline=True)
    status = AIRING_LABEL.get(anime.get("airing_status") or "")
    if status:
        embed.add_field(name="📡 Status", value=status, inline=True)

    studios = _join(anime.get("studios"), limit=3)
    if studios:
        embed.add_field(name="🏢 Studio", value=studios, inline=False)
    genres = _join(anime.get("genre"))
    if is_adult:
        genres = f"🔞 18+ • {genres}" if genres else "🔞 18+"
    if genres:
        embed.add_field(name="🏷️ Genre", value=genres, inline=False)

    synopsis = _clean_synopsis(anime.get("description"))
    if synopsis:
        embed.add_field(name="📝 Sinopsis", value=synopsis, inline=False)

    # Konten dewasa: poster tidak ditampilkan di channel.
    poster = None if is_adult else _poster_url(anime, supabase_url)
    if poster:
        embed.set_image(url=poster)

    embed.set_footer(text=f"{SITE} • {_footer_time()}")
    return embed


# ---------- tugas utama ----------
class AnimeUpdate:
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
        self.seen_set: set[str] = set(self.seen)
        self._warned_empty = False
        self.check_loop.start()

    async def _get_json(self, params: dict):
        """GET ke tabel anime, pakai session bot (atau session sementara)."""
        session = getattr(self.bot, "http_session", None)
        own_session = None
        if session is None:
            own_session = session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        try:
            async with session.get(
                f"{self.base_url}/rest/v1/anime", params=params, headers=self.headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"Supabase HTTP {resp.status}: {body}")
                return await resp.json()
        finally:
            if own_session:
                await own_session.close()

    async def _fetch_ids(self) -> list[str]:
        """Query ringan: hanya id anime published terbaru."""
        rows = await self._get_json(
            {
                "select": "id",
                "status": f"eq.{PUBLISHED_STATUS}",
                "order": "updated_at.desc",
                "limit": str(FETCH_LIMIT),
            }
        )
        return [str(r["id"]) for r in rows if r.get("id")]

    async def _fetch_full(self, ids: list[str]) -> list[dict]:
        """Ambil data lengkap hanya untuk id yang baru."""
        rows = await self._get_json(
            {
                "select": COLUMNS,
                "id": f"in.({','.join(ids)})",
                "status": f"eq.{PUBLISHED_STATUS}",
            }
        )
        rows = [r for r in rows if r.get("id") and r.get("title") and r.get("slug")]
        # kirim dari yang paling lama diubah
        rows.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "")
        return rows

    async def _get_channel(self):
        channel = self.bot.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHANNEL_ID)
        return channel

    def _mark_seen(self, anime_id: str) -> None:
        self.seen.append(anime_id)
        self.seen_set.add(anime_id)
        _save_state(self.seen)

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_loop(self):
        try:
            await self._check()
        except Exception as e:
            print(f"[animeupdate] error: {type(e).__name__}: {e}")

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check(self):
        ids = await self._fetch_ids()
        if not ids:
            if not self._warned_empty:
                print(
                    "[animeupdate] fetch kosong (cek status=published / RLS / SUPABASE_KEY)"
                )
                self._warned_empty = True
            return
        self._warned_empty = False

        # Run pertama: simpan saja, jangan kirim semua anime sekaligus.
        if self.first_run:
            self.seen = list(reversed(ids))
            self.seen_set = set(self.seen)
            _save_state(self.seen)
            self.first_run = False
            print(f"[animeupdate] inisialisasi: {len(self.seen)} anime tersimpan")
            return

        new_ids = [i for i in ids if i not in self.seen_set]
        if not new_ids:
            return

        rows = await self._fetch_full(new_ids[: MAX_SEND_PER_CHECK * 2])
        if not rows:
            return

        channel = await self._get_channel()
        for anime in rows[:MAX_SEND_PER_CHECK]:
            try:
                await channel.send(embed=build_embed(anime, self.base_url))
            except discord.HTTPException as e:
                print(f"[animeupdate] gagal kirim {anime['id']}: {e}")
                continue  # dicoba lagi di cek berikutnya
            self._mark_seen(str(anime["id"]))
            print(f"[animeupdate] terkirim: {anime['title']}")
            await asyncio.sleep(SEND_DELAY_SECONDS)


_instance: AnimeUpdate | None = None  # simpan referensi agar tidak di-garbage-collect


def setup(bot: discord.Client):
    global _instance
    if _instance is not None:
        return
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        print(
            "[animeupdate] SUPABASE_URL / SUPABASE_KEY belum diisi di .env, fitur dimatikan"
        )
        return
    _instance = AnimeUpdate(bot, url, key)
