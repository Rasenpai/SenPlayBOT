"""Anime Radio: pemutar audio berbasis dataset data/radio.json.

Hanya lagu yang terdaftar di dataset yang bisa diputar. Tidak ada pencarian
otomatis YouTube dan tidak ada penambahan lagu di luar dataset.

Jenis sumber (source_type):
  local   -> file di direktori audio/ (relatif terhadap direktori itu)
  direct  -> URL audio langsung (MP3 dsb.)
  radio   -> URL stream audio yang kompatibel dengan FFmpeg
  youtube -> URL video YouTube yang ada di dataset (diambil via yt-dlp)

Mode 24/7:
  /senplay-join  -> bot masuk ke voice channel Anda dan "tinggal" di sana.
                    Pemicu command ini menjadi OWNER; hanya owner yang bisa
                    memakai /leave. Bot menulis embed ke chat voice channel itu.
                    Owner + channel disimpan ke data/senplay_state.json sehingga
                    bot tersambung kembali otomatis setelah restart/terputus.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import shlex
import socket
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = BASE_DIR / "data" / "radio.json"
STATE_PATH = BASE_DIR / "data" / "senplay_state.json"
AUDIO_DIR = BASE_DIR / "audio"

VALID_SOURCE_TYPES = {"local", "direct", "radio", "youtube"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

MAX_QUEUE = 50
MAX_CONSECUTIVE_FAILURES = 3  # setelah ini pemutaran dihentikan (tanpa retry tak terbatas)
MIN_PLAY_SECONDS = 3.0  # lagu yang berhenti lebih cepat dari ini dianggap gagal
MAX_REDIRECTS = 3
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)
YOUTUBE_TIMEOUT = 30

COLOR_OK = discord.Color.from_rgb(88, 101, 242)
COLOR_PLAY = discord.Color.from_rgb(46, 204, 113)
COLOR_WARN = discord.Color.from_rgb(241, 196, 15)
COLOR_ERR = discord.Color.from_rgb(231, 76, 60)

# -protocol_whitelist mencegah FFmpeg membuka file:// atau protokol lain
# lewat redirect/playlist. -rw_timeout (mikrodetik) mencegah menggantung.
URL_BEFORE_OPTIONS = (
    "-protocol_whitelist http,https,tcp,tls,crypto "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 3 "
    "-rw_timeout 15000000"
)


class SourceError(Exception):
    """Sumber audio tidak bisa dipakai. Pesannya aman ditampilkan ke pengguna."""


def make_embed(
    title: str, description: str = "", color: discord.Color = COLOR_OK
) -> discord.Embed:
    return discord.Embed(title=title, description=description or None, color=color)


def voice_of(guild: discord.Guild) -> discord.VoiceClient | None:
    """guild.voice_client bertipe VoiceProtocol; persempit ke VoiceClient."""
    vc = guild.voice_client
    return vc if isinstance(vc, discord.VoiceClient) else None


# --------------------------------------------------------------------------
# Dataset
# --------------------------------------------------------------------------
@dataclass(eq=False)
class Song:
    title: str
    artist: str
    anime: str
    type: str
    aliases: list[str]
    source_type: str
    source: str
    unavailable: bool = False  # ditandai bila file lokal hilang

    @classmethod
    def from_dict(cls, d: dict) -> "Song":
        if not isinstance(d, dict):
            raise TypeError("entri harus berupa objek")
        title = str(d["title"]).strip()
        source = str(d["source"]).strip()
        source_type = str(d["source_type"]).strip().lower()
        if not title or not source:
            raise ValueError("title/source kosong")
        if source_type not in VALID_SOURCE_TYPES:
            raise ValueError(f"source_type tidak didukung: {source_type}")
        aliases = d.get("aliases") or []
        if not isinstance(aliases, list):
            raise TypeError("aliases harus berupa list")
        return cls(
            title=title,
            artist=str(d.get("artist", "")).strip(),
            anime=str(d.get("anime", "")).strip(),
            type=str(d.get("type", "")).strip(),
            aliases=[str(a).strip() for a in aliases if str(a).strip()],
            source_type=source_type,
            source=source,
        )

    @property
    def label(self) -> str:
        parts = [self.title]
        if self.anime:
            parts.append(self.anime + (f" {self.type}" if self.type else ""))
        return " — ".join(parts)


class Dataset:
    """Membaca radio.json dan memuat ulang otomatis bila file berubah."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._songs: list[Song] = []

    def _load(self) -> list[Song] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[radio] gagal membaca dataset: {exc}")
            return None
        if not isinstance(raw, list):
            print("[radio] dataset harus berupa list")
            return None
        songs: list[Song] = []
        for i, item in enumerate(raw):
            try:
                songs.append(Song.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[radio] entri #{i} dilewati: {exc!r}")
        return songs

    def songs(self) -> list[Song]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._songs
        if mtime != self._mtime:
            loaded = self._load()
            if loaded is not None:  # bila gagal, pertahankan data lama
                self._songs = loaded
            self._mtime = mtime
        return self._songs

    def search(self, query: str, limit: int = 1) -> list[Song]:
        q = query.strip().casefold()
        scored: list[tuple[int, Song]] = []
        for s in self.songs():
            fields = [f.casefold() for f in (s.title, s.artist, s.anime, *s.aliases) if f]
            if not q:
                score = 3
            elif q == s.title.casefold() or q in (a.casefold() for a in s.aliases):
                score = 0
            elif any(f.startswith(q) for f in fields):
                score = 1
            elif any(q in f for f in fields):
                score = 2
            else:
                continue
            scored.append((score, s))
        scored.sort(key=lambda x: x[0])  # sort stabil: urutan dataset dipertahankan
        return [s for _, s in scored[:limit]]


dataset = Dataset(DATASET_PATH)


# --------------------------------------------------------------------------
# Resolusi & validasi sumber
# --------------------------------------------------------------------------
async def validate_public_url(url: str) -> None:
    """Tolak URL yang menuju localhost, jaringan privat/internal, atau metadata cloud."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SourceError("URL sumber tidak valid (hanya http/https).")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise SourceError("Host sumber tidak ditemukan.")
    if not infos:
        raise SourceError("Host sumber tidak ditemukan.")
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        # is_global=False mencakup private, loopback, link-local (169.254.x.x
        # termasuk metadata cloud), reserved, dan CGNAT.
        if not ip.is_global or ip.is_multicast:
            raise SourceError("Alamat sumber tidak diizinkan.")


async def resolve_direct(session: aiohttp.ClientSession, url: str) -> str:
    """Probe URL, ikuti redirect secara manual agar tiap hop tervalidasi.

    Mengembalikan URL final yang akan diberikan ke FFmpeg.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        await validate_public_url(current)
        try:
            async with session.get(
                current, allow_redirects=False, timeout=PROBE_TIMEOUT
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise SourceError("Redirect sumber tidak valid.")
                    current = urljoin(current, location)
                    continue
                if resp.status >= 400:
                    raise SourceError(f"Sumber mengembalikan HTTP {resp.status}.")
                return current  # keluar dari `async with` menutup koneksi tanpa baca body
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise SourceError("URL sumber tidak aktif atau tidak dapat diakses.")
    raise SourceError("Terlalu banyak redirect pada URL sumber.")


def resolve_local(source: str) -> Path:
    root = AUDIO_DIR.resolve()
    # `source` ditulis relatif terhadap root proyek, mis. "audio/lagu-lokal.mp3".
    path = (BASE_DIR / source).resolve()
    if not path.is_relative_to(root):  # cegah path traversal (../ atau path absolut)
        raise SourceError("Path file lokal tidak diizinkan.")
    if not path.is_file():
        raise SourceError("File audio lokal tidak ditemukan.")
    return path


def _extract_youtube(url: str) -> tuple[str, str | None]:
    import yt_dlp  # diimpor di sini agar yt-dlp opsional bila tak ada entri YouTube

    # Any: stub yt-dlp memakai TypedDict yang ketat, sedangkan opsi ini valid saat runtime.
    opts: Any = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 2,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        raw = ydl.extract_info(url, download=False)
    if not raw:
        raise ValueError("yt-dlp tidak mengembalikan info")
    info = cast("dict[str, Any]", raw)
    headers: dict[str, str] = info.get("http_headers") or {}
    return str(info["url"]), headers.get("User-Agent")


async def resolve_youtube(url: str) -> tuple[str, str | None]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise SourceError("URL YouTube tidak valid.")
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _extract_youtube, url),
            timeout=YOUTUBE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise SourceError("Pengambilan audio YouTube melebihi batas waktu.")
    except ImportError:
        raise SourceError("yt-dlp belum terpasang di server.")
    except Exception as exc:  # yt_dlp.DownloadError, KeyError, dsb.
        print(f"[radio] yt-dlp gagal untuk {url}: {type(exc).__name__}: {exc}")
        raise SourceError("Video YouTube tidak dapat diputar (privat, dibatasi, atau dihapus).")


# --------------------------------------------------------------------------
# Pemutar
# --------------------------------------------------------------------------
@dataclass(eq=False)
class GuildPlayer:
    text_channel: discord.abc.Messageable | None = None
    queue: deque[Song] = field(default_factory=deque)
    current: Song | None = None
    autoplay: bool = False
    failures: int = 0
    skipping: bool = False
    started_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Voice channel tempat bot "tinggal". Diisi saat /senplay-join, /play, /radio
    # dan dikosongkan hanya oleh /leave. Dipakai watchdog untuk menyambung ulang.
    home_channel_id: int | None = None
    # User yang memicu join. Hanya dia yang boleh /leave.
    owner_id: int | None = None
    # Chat tujuan embed bot (chat voice channel). Disimpan agar selamat restart.
    text_channel_id: int | None = None
    # Loop: "off" | "song" (ulang lagu ini) | "playlist" (ulang dari lagu pertama).
    loop_mode: str = "off"
    # Urutan lagu yang sudah diputar dari antrean (dasar loop playlist).
    cycle: list[Song] = field(default_factory=list)
    # Berapa lagu di depan antrean yang merupakan pengulangan (tidak dicatat ke cycle lagi).
    replay_remaining: int = 0

    def reset_loop(self) -> None:
        self.loop_mode = "off"
        self.cycle.clear()
        self.replay_remaining = 0


class RadioPlayer:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.states: dict[int, GuildPlayer] = {}
        self.watchdog_task: asyncio.Task[None] | None = None
        self._load_state()

    # ---- persistensi (agar 24/7 selamat dari restart) ----
    def _load_state(self) -> None:
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        for gid, d in raw.items():
            try:
                self.states[int(gid)] = GuildPlayer(
                    home_channel_id=int(d["channel"]),
                    owner_id=int(d["owner"]),
                    text_channel_id=int(d["text"]) if d.get("text") else None,
                )
            except (KeyError, TypeError, ValueError):
                continue

    def save_state(self) -> None:
        data = {
            str(gid): {
                "channel": st.home_channel_id,
                "owner": st.owner_id,
                "text": st.text_channel_id,
            }
            for gid, st in self.states.items()
            if st.home_channel_id and st.owner_id
        }
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"[radio] gagal menyimpan state: {exc}")

    # ---- util ----
    async def _notify(
        self,
        st: GuildPlayer,
        text: str,
        *,
        title: str = "Anime Radio",
        color: discord.Color = COLOR_OK,
    ) -> None:
        channel = st.text_channel
        if channel is None and st.text_channel_id:
            ch = self.bot.get_channel(st.text_channel_id)
            if isinstance(ch, discord.abc.Messageable):
                channel = st.text_channel = ch
        if channel is None:
            return
        try:
            await channel.send(embed=make_embed(title, text, color))
        except discord.HTTPException:
            pass

    def _next_song(self, st: GuildPlayer) -> Song | None:
        if st.queue:
            song = st.queue.popleft()
            if st.replay_remaining > 0:
                st.replay_remaining -= 1  # pengulangan, jangan dicatat lagi
            elif len(st.cycle) < MAX_QUEUE:
                st.cycle.append(song)
            return song
        if st.loop_mode == "playlist" and st.cycle:
            # Antrean habis: ulang dari lagu pertama playlist.
            st.queue.extend(st.cycle)
            st.replay_remaining = len(st.cycle)
            return self._next_song(st)
        if st.autoplay:
            candidates = [s for s in dataset.songs() if not s.unavailable and s is not st.current]
            if not candidates:
                candidates = [s for s in dataset.songs() if not s.unavailable]
            if candidates:
                return random.choice(candidates)
            st.autoplay = False
        return None

    async def _halt(self, st: GuildPlayer) -> None:
        st.queue.clear()
        st.reset_loop()
        st.autoplay = False
        st.failures = 0
        await self._notify(
            st,
            "Terlalu banyak sumber gagal berturut-turut. Pemutaran otomatis dihentikan.",
            title="⛔ Dihentikan",
            color=COLOR_ERR,
        )

    async def _create_source(self, song: Song) -> discord.AudioSource:
        if song.source_type == "local":
            path = resolve_local(song.source)
            song.unavailable = False
            return discord.FFmpegPCMAudio(str(path), options="-vn")

        if song.source_type in ("direct", "radio"):
            session: aiohttp.ClientSession | None = getattr(self.bot, "http_session", None)
            if session is None:
                raise SourceError("HTTP session bot belum siap.")
            url = await resolve_direct(session, song.source)
            return discord.FFmpegPCMAudio(url, before_options=URL_BEFORE_OPTIONS, options="-vn")

        # youtube
        url, user_agent = await resolve_youtube(song.source)
        before = URL_BEFORE_OPTIONS
        if user_agent:
            before += f" -user_agent {shlex.quote(user_agent)}"
        return discord.FFmpegPCMAudio(url, before_options=before, options="-vn")

    # ---- alur pemutaran ----
    async def play_next(self, guild: discord.Guild) -> None:
        st = self.states.get(guild.id)
        if st is None:
            return
        async with st.lock:
            while True:
                vc = voice_of(guild)
                if vc is None or not vc.is_connected():
                    return  # state dipertahankan; watchdog akan menyambung ulang
                if vc.is_playing():
                    return
                song = self._next_song(st)
                if song is None:
                    st.current = None
                    return
                try:
                    audio = await self._create_source(song)
                except Exception as exc:  # antrean tidak boleh berhenti total
                    if isinstance(exc, SourceError):
                        reason = str(exc)
                    else:
                        print(f"[radio] gagal membuat sumber: {type(exc).__name__}: {exc}")
                        reason = "Sumber gagal dimuat."
                    if song.source_type == "local":
                        song.unavailable = True
                    st.failures += 1
                    await self._notify(
                        st,
                        f"**{song.title}** dilewati: {reason}",
                        title="⚠️ Dilewati",
                        color=COLOR_WARN,
                    )
                    if st.failures >= MAX_CONSECUTIVE_FAILURES:
                        await self._halt(st)
                        return
                    continue

                st.current = song
                st.skipping = False
                st.started_at = asyncio.get_running_loop().time()
                vc.play(
                    audio,
                    after=lambda err, st=st, song=song: asyncio.run_coroutine_threadsafe(
                        self._after(guild, st, song, err), self.bot.loop
                    ),
                )
                embed = make_embed("🎶 Sekarang memutar", f"**{song.title}**", COLOR_PLAY)
                if song.artist:
                    embed.add_field(name="Artis", value=song.artist, inline=True)
                if song.anime:
                    embed.add_field(
                        name="Anime",
                        value=song.anime + (f" ({song.type})" if song.type else ""),
                        inline=True,
                    )
                if st.autoplay:
                    embed.set_footer(text="📻 Mode radio acak aktif")
                await self._send_embed(st, embed)
                return

    async def _send_embed(self, st: GuildPlayer, embed: discord.Embed) -> None:
        channel = st.text_channel
        if channel is None and st.text_channel_id:
            ch = self.bot.get_channel(st.text_channel_id)
            if isinstance(ch, discord.abc.Messageable):
                channel = st.text_channel = ch
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _after(
        self, guild: discord.Guild, st: GuildPlayer, song: Song, err: Exception | None
    ) -> None:
        if self.states.get(guild.id) is not st:
            return  # state sudah dihentikan/diganti
        was_skipped = st.skipping
        if err:
            print(f"[radio] error pemutaran {song.title}: {type(err).__name__}: {err}")
        elapsed = asyncio.get_running_loop().time() - st.started_at
        if st.skipping or elapsed >= MIN_PLAY_SECONDS:
            st.failures = 0
        else:
            st.failures += 1
            await self._notify(
                st,
                f"**{song.title}** berhenti terlalu cepat dan dilewati.",
                title="⚠️ Dilewati",
                color=COLOR_WARN,
            )
            if st.failures >= MAX_CONSECUTIVE_FAILURES:
                await self._halt(st)
                return
        st.skipping = False
        st.current = None
        if st.loop_mode == "song" and not was_skipped:
            # Loop satu lagu: taruh lagu yang sama di depan antrean.
            st.queue.appendleft(song)
            st.replay_remaining += 1
        await self.play_next(guild)

    async def stop_playback(self, guild: discord.Guild) -> None:
        """Hentikan lagu dan kosongkan antrean, tetapi tetap di voice channel."""
        st = self.states.get(guild.id)
        if st is not None:
            st.queue.clear()
            st.reset_loop()
            st.autoplay = False
            st.failures = 0
            st.skipping = True  # agar berhenti manual tidak dihitung sebagai gagal
        vc = voice_of(guild)
        if vc is not None:
            vc.stop()

    async def leave(self, guild: discord.Guild) -> None:
        """Satu-satunya jalan keluar yang disengaja: hapus state lalu disconnect."""
        self.states.pop(guild.id, None)  # tanpa state, watchdog tidak menyambung ulang
        self.save_state()
        vc = voice_of(guild)
        if vc is not None:
            vc.stop()
            await vc.disconnect()

    async def watchdog(self) -> None:
        """Menyambung ulang ke voice channel bila bot terputus tanpa /leave
        (termasuk setelah restart, karena state dimuat dari disk)."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            for guild_id, st in list(self.states.items()):
                try:
                    if st.home_channel_id is None:
                        continue
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    vc = voice_of(guild)
                    if vc is not None and vc.is_connected():
                        continue
                    channel = guild.get_channel(st.home_channel_id)
                    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                        continue
                    if vc is not None:  # koneksi basi, bersihkan dulu
                        await vc.disconnect(force=True)
                    await channel.connect(timeout=15)
                    print(f"[radio] tersambung ulang ke {channel.name} ({guild.name})")
                    await self.play_next(guild)
                except Exception as exc:  # loop tidak boleh mati karena satu server
                    print(f"[radio] watchdog gagal ({guild_id}): {type(exc).__name__}: {exc}")
            await asyncio.sleep(30)

    # ---- voice ----
    async def ensure_voice(
        self, interaction: discord.Interaction, *, claim: bool = False
    ) -> discord.VoiceClient | None:
        """Pastikan bot ada di voice channel user.

        claim=True (/senplay-join): user menjadi owner dan boleh memindahkan bot.
        claim=False (/play, /radio): bot tidak dipindah bila sudah ada di channel lain.
        """
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.followup.send(
                embed=make_embed("Masuk ke voice channel dulu", color=COLOR_WARN), ephemeral=True
            )
            return None
        channel = user.voice.channel
        guild = interaction.guild
        assert guild is not None

        st = self.states.get(guild.id)
        if claim and st is not None and st.owner_id not in (None, user.id):
            await interaction.followup.send(
                embed=make_embed(
                    "Bot sedang dipakai",
                    f"Bot dipanggil oleh <@{st.owner_id}>. Hanya dia yang bisa memindahkan "
                    "atau mengeluarkan bot (/leave).",
                    COLOR_WARN,
                ),
                ephemeral=True,
            )
            return None

        vc = voice_of(guild)
        try:
            if vc is not None and not vc.is_connected():
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                vc = await channel.connect(timeout=15)
            elif vc.channel != channel:
                if not claim:
                    await interaction.followup.send(
                        embed=make_embed(
                            "Bot ada di channel lain",
                            f"Bot sedang berada di {vc.channel.mention}. Masuk ke channel itu.",
                            COLOR_WARN,
                        ),
                        ephemeral=True,
                    )
                    return None
                await vc.move_to(channel)
        except (discord.DiscordException, asyncio.TimeoutError, RuntimeError) as exc:
            print(f"[radio] gagal join voice: {type(exc).__name__}: {exc}")
            await interaction.followup.send(
                embed=make_embed(
                    "Gagal masuk voice",
                    "Tidak bisa masuk ke voice channel (cek izin bot dan dukungan voice).",
                    COLOR_ERR,
                ),
                ephemeral=True,
            )
            return None

        st = self.get_state(interaction)
        st.home_channel_id = channel.id
        if st.owner_id is None or claim:
            st.owner_id = user.id
        if claim or st.text_channel_id is None:
            # Embed bot dikirim ke chat voice channel (VoiceChannel bisa .send()).
            st.text_channel = channel
            st.text_channel_id = channel.id
        self.save_state()
        return vc

    def get_state(self, interaction: discord.Interaction) -> GuildPlayer:
        assert interaction.guild is not None
        st = self.states.get(interaction.guild.id)
        if st is None:
            st = GuildPlayer()
            self.states[interaction.guild.id] = st
        if st.text_channel is None and st.text_channel_id is None:
            st.text_channel = interaction.channel  # type: ignore[assignment]
        return st


# --------------------------------------------------------------------------
# Registrasi command
# --------------------------------------------------------------------------
def setup(bot: discord.Client) -> None:
    player = RadioPlayer(bot)
    tree = cast(app_commands.CommandTree, getattr(bot, "tree"))

    async def on_voice_state_update(
        member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        # Bila bot dipindahkan moderator, anggap channel baru sebagai "rumah" baru.
        if bot.user is None or member.id != bot.user.id or after.channel is None:
            return
        st = player.states.get(member.guild.id)
        if st is not None:
            st.home_channel_id = after.channel.id
            st.text_channel = after.channel
            st.text_channel_id = after.channel.id
            player.save_state()

    # discord.Client tidak punya add_listener (hanya commands.Bot). Client memanggil
    # handler lewat getattr(self, "on_voice_state_update") saat dispatch, jadi kita
    # pasang handler di instance dan rangkai dengan handler lama bila ada.
    if hasattr(bot, "add_listener"):
        cast("commands.Bot", bot).add_listener(on_voice_state_update, "on_voice_state_update")
    else:
        previous = getattr(bot, "on_voice_state_update", None)

        async def chained_voice_state_update(
            member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
        ) -> None:
            if previous is not None:
                try:
                    await previous(member, before, after)
                except Exception as exc:
                    print(f"[radio] handler voice lama gagal: {type(exc).__name__}: {exc}")
            await on_voice_state_update(member, before, after)

        setattr(bot, "on_voice_state_update", chained_voice_state_update)
    player.watchdog_task = asyncio.create_task(player.watchdog())  # setup dipanggil dari setup_hook

    @tree.command(name="senplay-join", description="Bot masuk ke voice Anda dan stay 24/7 sampai /leave")
    @app_commands.guild_only()
    async def senplay_join(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        vc = await player.ensure_voice(interaction, claim=True)
        if vc is None:
            return
        embed = make_embed(
            "✅ SenPlay masuk voice",
            f"Bot masuk ke {vc.channel.mention} dan akan stay 24/7.\n"
            "Gunakan chat voice channel ini untuk /play, /radio, /queue, dll.",
            COLOR_PLAY,
        )
        embed.add_field(name="Owner", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Hanya owner yang bisa mengeluarkan bot dengan /leave")
        await interaction.followup.send(embed=embed)

    @tree.command(name="play", description="Putar lagu dari dataset Anime Radio")
    @app_commands.describe(query="Judul, artis, anime, atau alias lagu")
    @app_commands.guild_only()
    async def play(interaction: discord.Interaction, query: str):
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(thinking=True)
        matches = dataset.search(query, limit=1)
        if not matches:
            await interaction.followup.send(
                embed=make_embed(
                    "Tidak ditemukan",
                    f"Lagu **{query[:100]}** belum tersedia dalam dataset.",
                    COLOR_WARN,
                ),
                ephemeral=True,
            )
            return
        song = matches[0]
        if await player.ensure_voice(interaction) is None:
            return
        st = player.get_state(interaction)
        if len(st.queue) >= MAX_QUEUE:
            await interaction.followup.send(
                embed=make_embed("Antrean penuh", color=COLOR_WARN), ephemeral=True
            )
            return
        st.queue.append(song)
        vc = voice_of(guild)
        busy = vc is not None and vc.is_playing()
        await interaction.followup.send(
            embed=make_embed(
                "➕ Ditambahkan ke antrean" if busy else "▶️ Menyiapkan",
                f"**{song.label}**",
                COLOR_OK if busy else COLOR_PLAY,
            )
        )
        if not busy:
            await player.play_next(guild)

    @play.autocomplete("query")
    async def play_autocomplete(interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=s.label[:100], value=s.title[:100])
            for s in dataset.search(current, limit=25)
        ]

    @tree.command(name="radio", description="Putar acak dari dataset tanpa henti")
    @app_commands.guild_only()
    async def radio(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(thinking=True)
        if not dataset.songs():
            await interaction.followup.send(
                embed=make_embed("Dataset kosong", color=COLOR_WARN), ephemeral=True
            )
            return
        if await player.ensure_voice(interaction) is None:
            return
        st = player.get_state(interaction)
        st.autoplay = True
        await interaction.followup.send(
            embed=make_embed(
                "📻 Mode radio aktif", "Lagu diputar acak dari dataset.", COLOR_PLAY
            )
        )
        await player.play_next(guild)

    LOOP_LABELS = {
        "off": "❌ Loop mati",
        "song": "🔂 Loop satu lagu",
        "playlist": "🔁 Loop playlist",
    }

    @tree.command(name="loop", description="Atur loop: satu lagu atau seluruh playlist")
    @app_commands.describe(mode="Jenis loop")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Satu lagu", value="song"),
            app_commands.Choice(name="Playlist", value="playlist"),
            app_commands.Choice(name="Matikan", value="off"),
        ]
    )
    @app_commands.guild_only()
    async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        guild = interaction.guild
        st = player.states.get(guild.id) if guild else None
        if st is None:
            await interaction.response.send_message(
                embed=make_embed("Bot belum masuk voice. Pakai /senplay-join dulu.", color=COLOR_WARN),
                ephemeral=True,
            )
            return
        if mode.value == "off":
            st.loop_mode = "off"
            # Buang pengulangan yang sudah dijadwalkan; lagu baru dari user tetap ada.
            for _ in range(min(st.replay_remaining, len(st.queue))):
                st.queue.popleft()
            st.replay_remaining = 0
            st.cycle.clear()
            desc = "Lagu akan diputar sesuai antrean lalu berhenti."
        elif mode.value == "song":
            st.loop_mode = "song"
            desc = (
                f"**{st.current.label}** akan diulang terus. Pakai /skip untuk lanjut ke antrean."
                if st.current
                else "Lagu berikutnya yang diputar akan diulang terus."
            )
        else:
            st.loop_mode = "playlist"
            desc = (
                "Lagu sekarang → antrean → saat antrean habis, ulang dari lagu pertama.\n"
                "Lagu yang kamu /play akan masuk antrean dan ikut ke dalam siklus."
            )
        await interaction.response.send_message(
            embed=make_embed(LOOP_LABELS[mode.value], desc, COLOR_OK)
        )

    @tree.command(name="skip", description="Lewati lagu yang sedang diputar")
    @app_commands.guild_only()
    async def skip(interaction: discord.Interaction):
        guild = interaction.guild
        st = player.states.get(guild.id) if guild else None
        vc = voice_of(guild) if guild else None
        if st is None or vc is None or st.current is None:
            await interaction.response.send_message(
                embed=make_embed("Tidak ada lagu yang diputar", color=COLOR_WARN), ephemeral=True
            )
            return
        st.skipping = True
        vc.stop()  # memicu _after -> lagu berikutnya
        await interaction.response.send_message(embed=make_embed("⏭️ Dilewati"))

    @tree.command(name="stop", description="Hentikan lagu dan kosongkan antrean (bot tetap di voice)")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or voice_of(guild) is None:
            await interaction.response.send_message(
                embed=make_embed("Bot tidak sedang di voice channel", color=COLOR_WARN),
                ephemeral=True,
            )
            return
        await player.stop_playback(guild)
        await interaction.response.send_message(
            embed=make_embed(
                "⏹️ Dihentikan",
                "Bot tetap di voice channel. Owner bisa keluarkan dengan /leave.",
            )
        )

    @tree.command(name="leave", description="Keluarkan bot dari voice (hanya yang memanggil /senplay-join)")
    @app_commands.guild_only()
    async def leave(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or voice_of(guild) is None:
            await interaction.response.send_message(
                embed=make_embed("Bot tidak sedang di voice channel", color=COLOR_WARN),
                ephemeral=True,
            )
            return
        st = player.states.get(guild.id)
        if st is not None and st.owner_id not in (None, interaction.user.id):
            await interaction.response.send_message(
                embed=make_embed(
                    "Tidak diizinkan",
                    f"Hanya <@{st.owner_id}> (yang memanggil /senplay-join) yang bisa "
                    "mengeluarkan bot.",
                    COLOR_ERR,
                ),
                ephemeral=True,
            )
            return
        await player.leave(guild)
        await interaction.response.send_message(
            embed=make_embed("👋 Keluar dari voice channel", color=COLOR_OK)
        )

    @tree.command(name="queue", description="Lihat lagu yang sedang diputar dan antrean")
    @app_commands.guild_only()
    async def queue(interaction: discord.Interaction):
        guild = interaction.guild
        st = player.states.get(guild.id) if guild else None
        if st is None or (st.current is None and not st.queue):
            await interaction.response.send_message(
                embed=make_embed("Antrean kosong", color=COLOR_WARN), ephemeral=True
            )
            return
        embed = make_embed("📜 Antrean", color=COLOR_OK)
        if st.current:
            embed.add_field(name="🎶 Sekarang", value=st.current.label[:1024], inline=False)
        if st.queue:
            lines = [f"{i}. {s.label}" for i, s in enumerate(list(st.queue)[:10], 1)]
            if len(st.queue) > 10:
                lines.append(f"… dan {len(st.queue) - 10} lagu lagi")
            embed.add_field(name="Berikutnya", value="\n".join(lines)[:1024], inline=False)
        notes = []
        if st.loop_mode != "off":
            notes.append(LOOP_LABELS[st.loop_mode])
        if st.autoplay:
            notes.append("📻 Mode radio acak aktif")
        if notes:
            embed.set_footer(text=" • ".join(notes))
        await interaction.response.send_message(embed=embed)