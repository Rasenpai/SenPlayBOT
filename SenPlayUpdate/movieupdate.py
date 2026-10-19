"""SenPlayUpdate: kirim embed otomatis saat ada film baru di tabel `movies` (Supabase)."""

import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import tasks

CHANNEL_ID = 1561638645130199130
CHECK_INTERVAL_SECONDS = 300  # cek tiap 5 menit
SEND_DELAY_SECONDS = 1.5
FETCH_LIMIT = 50  # ambil 50 film terbaru tiap cek
MAX_SEND_PER_CHECK = 15  # cegah banjir pesan kalau ada import massal
MAX_STATE = 1000  # batas id yang disimpan di file state
EMBED_COLOR = 0xEF4444

SITE = "movie.senplay.web.id"
DETAIL_URL = "https://movie.senplay.web.id/movie/detail/{slug}"
ADDED_BY = "@senpaii._28"

# Hanya film berstatus ini yang diumumkan (draft tidak ikut).
# Sesuaikan dengan nilai enum movie_status di database kalau berbeda.
PUBLISHED_STATUS = "published"

# poster_path di tabel `movies` bukan URL penuh:
#   "/abc.jpg"  -> path TMDB
#   "abc.jpg"   -> file di Supabase Storage (bucket di bawah)
TMDB_IMG = "https://image.tmdb.org/t/p/w500"
POSTER_BUCKET = "posters"

COLUMNS = (
    "id,title,slug,description,poster_path,release_year,genre,"
    "duration_seconds,rating,created_at"
)

WIB = timezone(timedelta(hours=7))
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "movieupdate_state.json"
)


# ---------- state ----------
def _load_state() -> list[str] | None:
    """Daftar id film yang sudah diproses. None = run pertama."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return [str(x) for x in json.load(f)]
    except (OSError, ValueError, TypeError):
        print("[movieupdate] state rusak, di-reset")
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
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _footer_time() -> str:
    now = datetime.now(WIB)
    return f"Today at {now.strftime('%I:%M %p').lstrip('0')}"


def _duration(seconds) -> str:
    """5400 -> '1 jam 30 menit'."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, minutes = divmod(total // 60, 60)
    if hours and minutes:
        return f"{hours} jam {minutes} menit"
    if hours:
        return f"{hours} jam"
    return f"{minutes} menit"


def _genres(value) -> str:
    if not value:
        return ""
    items = value.split(",") if isinstance(value, str) else value
    return " • ".join(str(g).strip() for g in items if str(g).strip())


def _poster_url(path: str | None, supabase_url: str) -> str | None:
    if not path:
        return None
    path = str(path).strip()
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("/"):
        return TMDB_IMG + path
    return f"{supabase_url}/storage/v1/object/public/{POSTER_BUCKET}/{path}"


def build_embed(movie: dict, supabase_url: str) -> discord.Embed:
    title = movie["title"]
    url = DETAIL_URL.format(slug=movie["slug"])
    added = _parse_time(movie.get("created_at"))

    embed = discord.Embed(
        title="🍿 Film Baru Telah Ditambahkan!",
        description=(
            f"**{title}** kini sudah tersedia di SenPlay!\n"
            "Siapkan cemilan dan selamat menonton. 🎬\n\n"
            f"▶️ [Tonton Sekarang]({url})\n\n"
            f"🕒 *Di tambahkan pada pukul {added.strftime('%H.%M')} WIB oleh {ADDED_BY}*"
        ),
        url=url,
        color=EMBED_COLOR,
    )

    rating = _to_float(movie.get("rating"))
    if rating:
        embed.add_field(name="⭐ Rating", value=f"{rating:g}", inline=True)
    if movie.get("release_year"):
        embed.add_field(name="📅 Tahun", value=str(movie["release_year"]), inline=True)
    duration = _duration(movie.get("duration_seconds"))
    if duration:
        embed.add_field(name="⏱️ Durasi", value=duration, inline=True)
    genres = _genres(movie.get("genre"))
    if genres:
        embed.add_field(name="🏷️ Genre", value=genres, inline=False)

    synopsis = _clean_synopsis(movie.get("description"))
    if synopsis:
        embed.add_field(name="📝 Sinopsis", value=synopsis, inline=False)

    poster = _poster_url(movie.get("poster_path"), supabase_url)
    if poster:
        embed.set_image(url=poster)

    embed.set_footer(text=f"{SITE} • {_footer_time()}")
    return embed


# ---------- tugas utama ----------
class MovieUpdate:
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

    async def _fetch(self) -> list[dict]:
        session = getattr(self.bot, "http_session", None)
        own_session = None
        if session is None:
            own_session = session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        params = {
            "select": COLUMNS,
            "status": f"eq.{PUBLISHED_STATUS}",
            "order": "created_at.desc",
            "limit": str(FETCH_LIMIT),
        }
        try:
            async with session.get(
                f"{self.base_url}/rest/v1/movies", params=params, headers=self.headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"Supabase HTTP {resp.status}: {body}")
                rows = await resp.json()
        finally:
            if own_session:
                await own_session.close()
        return [r for r in rows if r.get("id") and r.get("title") and r.get("slug")]

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
            print(f"[movieupdate] error: {type(e).__name__}: {e}")

    @check_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _check(self):
        rows = await self._fetch()
        if not rows:
            return

        # Run pertama: simpan saja, jangan kirim 50 film sekaligus.
        if self.first_run:
            self.seen = [r["id"] for r in reversed(rows)]
            _save_state(self.seen)
            self.first_run = False
            print(f"[movieupdate] inisialisasi: {len(self.seen)} film tersimpan")
            return

        seen_set = set(self.seen)
        new_rows = [r for r in rows if r["id"] not in seen_set]
        if not new_rows:
            return

        channel = await self._get_channel()
        # Data urut terbaru -> terlama, kirim dari yang paling lama.
        for movie in list(reversed(new_rows))[:MAX_SEND_PER_CHECK]:
            try:
                await channel.send(embed=build_embed(movie, self.base_url))
            except discord.HTTPException as e:
                print(f"[movieupdate] gagal kirim {movie['id']}: {e}")
                continue  # dicoba lagi di cek berikutnya
            self.seen.append(movie["id"])
            _save_state(self.seen)
            await asyncio.sleep(SEND_DELAY_SECONDS)


_instance: MovieUpdate | None = None  # simpan referensi agar tidak di-garbage-collect


def setup(bot: discord.Client):
    global _instance
    if _instance is not None:
        return
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not url or not key:
        print(
            "[movieupdate] SUPABASE_URL / SUPABASE_KEY belum diisi di .env, fitur dimatikan"
        )
        return
    _instance = MovieUpdate(bot, url, key)
