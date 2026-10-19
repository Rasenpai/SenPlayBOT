"""Automod: hapus otomatis pesan scam / judi online (judol) / crypto / phishing.

Cara kerja: tiap pesan diberi skor dari sinyal yang terdeteksi (frasa, link,
pesan sama di banyak channel). Skor >= AUTOMOD_THRESHOLD -> pesan dihapus,
user di-timeout (opsional), dan dicatat ke channel log (opsional).
"""

import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord


def _int(name, default):
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


ENABLED = os.getenv("AUTOMOD_ENABLED", "1").strip() != "0"
THRESHOLD = _int("AUTOMOD_THRESHOLD", 4)  # skor minimal untuk dihapus
TIMEOUT_MINUTES = _int("AUTOMOD_TIMEOUT_MINUTES", 10)  # 0 = tanpa timeout
LOG_CHANNEL_ID = _int("AUTOMOD_LOG_CHANNEL_ID", 0)  # 0 = tanpa channel log
EXEMPT_STAFF = os.getenv("AUTOMOD_EXEMPT_STAFF", "1").strip() != "0"

SPAM_CHANNELS = 3  # pesan sama di >= 3 channel ...
SPAM_WINDOW = 60  # ... dalam 60 detik
MIN_DUP_LEN = 20  # pesan lebih pendek dari ini tidak dihitung duplikat

# --------------------------------------------------------------------------
# Daftar sinyal: (label, regex, bobot)
#   4 = hampir pasti spam, 3 = kuat, 2 = sedang, 1 = lemah
# Satu sinyal lemah/sedang tidak cukup; harus kombinasi sampai THRESHOLD.
# --------------------------------------------------------------------------
_PATTERNS = [
    # Judi online
    ("slot gacor", r"slot\s*gacor", 4),
    ("situs judi/slot", r"situs\s*(slot|judi|togel|casino)", 4),
    ("agen/bandar judi", r"(agen|bandar)\s*(judi|slot|togel|bola|casino)", 4),
    ("sbobet", r"\bsbobet\b", 4),
    ("judi online", r"judi\s*(online|bola|slot)|\bjudol\b", 3),
    ("togel", r"\btogel\b", 3),
    ("casino online", r"casino\s*online|online\s*casino", 3),
    ("slot online", r"slot\s*(online|88|777|pragmatic|pg\s*soft)", 3),
    ("rtp slot", r"\brtp\b.{0,15}(slot|live|hari ini)|rtp\s*slot", 3),
    ("anti rungkad", r"anti\s*rungkad", 3),
    ("link alternatif", r"link\s*alternatif", 3),
    ("maxwin", r"\bmaxwin\b", 2),
    (
        "bonus member",
        r"bonus\s*(new\s*member|member\s*baru|deposit|harian|cashback)",
        2,
    ),
    ("deposit pulsa/e-wallet", r"deposit\s*(pulsa|dana|ovo|gopay|qris)", 2),
    ("wd cepat", r"\b(wd|withdraw)\s*(cepat|lancar|instan|instant)", 2),
    ("gampang menang", r"(gampang|mudah|pasti)\s*(menang|jp|maxwin|cuan)", 2),
    ("gacor", r"\bgacor\b", 1),
    ("jackpot/scatter", r"\b(jackpot|scatter)\b", 1),
    ("ajakan daftar", r"(daftar|register|gabung)\s*(sekarang|disini|di\s*sini|now)", 1),
    # Crypto / investasi bodong
    (
        "free/claim crypto",
        r"(free|claim)\s+(your\s+)?(crypto|bitcoin|btc|eth|usdt|nfts?|airdrop)",
        3,
    ),
    ("airdrop", r"\bairdrop\b", 2),
    ("seed phrase", r"(seed|recovery|secret\s*recovery)\s*phrase", 3),
    ("connect wallet", r"connect\s*(your\s*)?wallet|walletconnect|wallet\s*connect", 3),
    (
        "double your",
        r"(double|triple|10x)\s*your\s*(btc|crypto|money|bitcoin|eth|usdt|deposit|investment)",
        4,
    ),
    (
        "guaranteed profit",
        r"guaranteed\s*(profit|return|income)s?|risk[-\s]*free\s*profit",
        3,
    ),
    ("crypto casino", r"crypto\s*casino|bitcoin\s*casino", 3),
    ("binary options", r"binary\s*options?", 3),
    ("withdraw bonus", r"withdraw\s*bonus|bonus\s*withdraw", 3),
    ("promo code", r"(promo|bonus|referral)\s*code|use\s*code\b", 2),
    (
        "giveaway palsu (selebriti)",
        r"(mrbeast|elon\s*musk|vitalik).{0,80}(giveaway|airdrop|claim|bonus|prize)"
        r"|(giveaway|airdrop|claim|bonus|prize).{0,80}(mrbeast|elon\s*musk|vitalik)",
        3,
    ),
    (
        "profit/investasi cepat",
        r"(profit|cuan|untung)\s*(harian|tiap\s*hari|jutaan|cepat)|investasi\s*(cepat|modal\s*kecil|pasti\s*untung)",
        3,
    ),
    ("sinyal trading", r"(sinyal|signal)\s*(trading|forex|crypto)", 2),
    (
        "janji uang cepat",
        r"(earn|make)\s+(up\s+to\s+)?\$?\d[\d,.k]*\s*(usd|usdt|dollars)?\s*"
        r"(daily|per\s*day|a\s*day|every\s*day|weekly|per\s*week)",
        3,
    ),
    (
        "dm untuk cuan",
        r"\b(dm|pm|message)\s*me\b.{0,40}(earn|profit|invest|trade|btc|crypto)",
        2,
    ),
    # Phishing hadiah
    (
        "free nitro",
        r"(free|claim)\s*(discord\s*)?nitro|nitro\s*(gift|giveaway|for\s*free)",
        4,
    ),
    (
        "free steam",
        r"free\s*steam\s*(gift|wallet|game)s?|steam\s*gift\s*(card\s*)?giveaway",
        3,
    ),
    # Tekanan waktu / kelangkaan
    ("post will be deleted", r"(post|message)\s*will\s*be\s*deleted", 1),
    (
        "only the fastest",
        r"only\s*(the\s*)?(first|fastest)|first\s*come\s*first\s*served",
        1,
    ),
    ("limited / buruan", r"limited\s*(spots|time\s*offer)|hurry\s*up|\bburuan\b", 1),
]
PATTERNS = [(label, re.compile(rx), weight) for label, rx, weight in _PATTERNS]

# --------------------------------------------------------------------------
# Link
# --------------------------------------------------------------------------
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\[\]]+"
    r"|\b(?:t\.me|wa\.me|telegram\.me|chat\.whatsapp\.com|bit\.ly|tinyurl\.com"
    r"|cutt\.ly|s\.id|is\.gd|rb\.gy|shorturl\.at)/[^\s<>()\[\]]*"
)
HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/\s:?#]+)")
CHAT_HOSTS = {"t.me", "wa.me", "telegram.me", "chat.whatsapp.com"}
SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "cutt.ly",
    "s.id",
    "is.gd",
    "rb.gy",
    "shorturl.at",
}
BAD_TLDS = (".xyz", ".top", ".vip", ".click", ".icu", ".win", ".bet", ".cc", ".fun")
GAMBLING_HOST = re.compile(
    r"slot|togel|gacor|judi|casino|maxwin|sbobet|toto\d|bet\d{2,}|\d{2,}bet|jp\d+"
)

# --------------------------------------------------------------------------
# Normalisasi (lawan trik huruf aneh, spasi, leetspeak)
# --------------------------------------------------------------------------
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
SPACED_RE = re.compile(r"\b(?:[a-z][\s.\-_*|]+){2,}[a-z]\b")  # "s l o t" / "s.l.o.t"


def normalize(text):
    text = unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH).lower()
    return re.sub(r"\s+", " ", text).strip()


def deobfuscate(norm):
    text = norm.translate(LEET)
    return SPACED_RE.sub(lambda m: re.sub(r"[\s.\-_*|]+", "", m.group(0)), text)


def link_signals(norm):
    found = {}
    for m in URL_RE.finditer(norm):
        url = m.group(0)
        host = HOST_RE.match(url).group(1).rstrip(".,!?")
        found["ada link"] = 1
        if host in CHAT_HOSTS:
            found["link telegram/whatsapp"] = 2
        if host in SHORTENERS:
            found["link pendek"] = 1
        if GAMBLING_HOST.search(host):
            found["domain judi"] = 3
        if host.endswith(BAD_TLDS):
            found["domain mencurigakan"] = 1
    return found


def score_text(norm):
    """Kembalikan (skor, daftar label sinyal) untuk teks yang sudah dinormalisasi."""
    variants = (norm, deobfuscate(norm))
    found = {}
    for label, rx, weight in PATTERNS:
        if any(rx.search(v) for v in variants):
            found[label] = weight
    found.update(link_signals(norm))
    if "@everyone" in norm or "@here" in norm:
        found["@everyone/@here"] = 1
    return sum(found.values()), list(found)


# --------------------------------------------------------------------------
# Deteksi pesan sama di banyak channel
# --------------------------------------------------------------------------
RECENT: dict[int, deque] = defaultdict(
    deque
)  # user_id -> (ts, channel_id, message_id, key)
_seen = 0


def _purge(dq, now):
    while dq and now - dq[0][0] > SPAM_WINDOW:
        dq.popleft()


def track_duplicate(message, norm):
    """Catat pesan; kembalikan (jumlah channel berbeda, entri pesan sama sebelumnya)."""
    global _seen
    if len(norm) < MIN_DUP_LEN:
        return 1, []
    now = time.monotonic()
    _seen += 1
    if _seen % 500 == 0:  # bersihkan user lama supaya memori tidak membengkak
        for uid, old in list(RECENT.items()):
            _purge(old, now)
            if not old:
                del RECENT[uid]
    dq = RECENT[message.author.id]
    _purge(dq, now)
    key = hash(norm)
    same = [e for e in dq if e[3] == key and e[2] != message.id]
    dq.append((now, message.channel.id, message.id, key))
    return len({e[1] for e in same} | {message.channel.id}), same


# --------------------------------------------------------------------------
# Aksi
# --------------------------------------------------------------------------
async def _delete(message):
    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except discord.HTTPException as e:  # Forbidden dll
        print(
            f"[automod] gagal hapus pesan: {type(e).__name__} (cek izin Manage Messages)"
        )
        return False


async def _delete_earlier(bot, same):
    for _, channel_id, message_id, _ in same:
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass


async def _timeout(author):
    if TIMEOUT_MINUTES <= 0 or not isinstance(author, discord.Member):
        return False
    if author.is_timed_out():
        return False
    try:
        await author.timeout(
            timedelta(minutes=TIMEOUT_MINUTES),
            reason="Automod: pesan scam/judi/crypto",
        )
        return True
    except discord.HTTPException as e:
        print(
            f"[automod] gagal timeout: {type(e).__name__} (cek izin Moderate Members / urutan role bot)"
        )
        return False


async def _log(bot, message, score, signals, deleted, timed_out):
    channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if channel is None:
        return
    author = message.author
    action = "Pesan dihapus" if deleted else "Pesan GAGAL dihapus (cek izin bot)"
    if timed_out:
        action += f" + ⏳ timeout {TIMEOUT_MINUTES} menit"
    snippet = message.content[:300].replace("`", "'").replace("http", "hxxp")
    embed = discord.Embed(
        title="🚨 Anti-Scam Triggered!",
        description=(
            f"**User:** {author.mention} `{author.id}`\n"
            f"**Channel:** {message.channel.mention}\n"
            f"**Skor:** {score} (batas {THRESHOLD})\n"
            f"**Sinyal terdeteksi:** {', '.join(signals[:10])}\n"
            f"**Action:** {action}"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Isi pesan (link dinonaktifkan)", value=f"```{snippet}```", inline=False
    )
    embed.set_footer(text="Automod")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"[automod] gagal kirim log: {type(e).__name__}")


async def handle(bot, message):
    if not ENABLED or message.guild is None or message.author.bot or message.webhook_id:
        return
    if not message.content:
        return
    author = message.author
    if EXEMPT_STAFF and isinstance(author, discord.Member):
        perms = author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

    norm = normalize(message.content)
    score, signals = score_text(norm)

    channels, same = track_duplicate(message, norm)
    if channels >= SPAM_CHANNELS:
        score += 3
        signals.append(f"pesan sama di {channels} channel")

    if score < THRESHOLD:
        return

    deleted = await _delete(message)
    await _delete_earlier(bot, same)
    timed_out = await _timeout(author)
    await _log(bot, message, score, signals, deleted, timed_out)
    print(f"[automod] user {author.id} skor {score}: {', '.join(signals[:10])}")


def setup(bot: discord.Client):
    if not getattr(bot.intents, "message_content", False):
        print(
            "[automod] PERINGATAN: intent message_content belum aktif, automod tidak bisa membaca pesan."
        )

    @bot.event
    async def on_message(message: discord.Message):
        try:
            await handle(bot, message)
        except Exception as e:  # automod tidak boleh menjatuhkan bot
            print(f"[automod] error: {type(e).__name__}: {e}")

    state = "aktif" if ENABLED else "nonaktif"
    print(
        f"[automod] {state} | batas skor {THRESHOLD} | timeout {TIMEOUT_MINUTES} menit | "
        f"log channel {LOG_CHANNEL_ID or '-'}"
    )
