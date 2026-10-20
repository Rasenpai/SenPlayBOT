"""Website Analyzer: /website url:<URL> (analisis pasif, hanya di channel tertentu)."""

import asyncio
import io
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import discord
import httpx
from bs4 import BeautifulSoup, Tag
from discord import app_commands

ALLOWED_CHANNEL_ID = 1562016057072418909
# Domain sendiri (termasuk semua subdomain) tidak boleh dianalisis
OWN_DOMAINS = ("senplay.web.id",)

FETCH_TIMEOUT = 10  # detik
SHOT_TIMEOUT = 20  # detik
MAX_BYTES = 2 * 1024 * 1024  # batas baca HTML
MAX_REDIRECTS = 5
UA = "Mozilla/5.0 (compatible; SENP4II-WebAnalyzer/1.0)"

SECURITY_HEADERS = [
    ("strict-transport-security", "HSTS"),
    ("content-security-policy", "CSP"),
    ("x-frame-options", "X-Frame-Options"),
    ("x-content-type-options", "X-Content-Type-Options"),
    ("referrer-policy", "Referrer-Policy"),
    ("permissions-policy", "Permissions-Policy"),
]

TECH_HTML = {
    "WordPress": [r"/wp-content/", r"/wp-includes/"],
    "Next.js": [r"/_next/static", r"__next_data__"],
    "Nuxt": [r"/_nuxt/"],
    "SvelteKit": [r"/_app/immutable/"],
    "React": [r"data-reactroot", r"react-dom"],
    "Vue.js": [r"vue(?:\.runtime)?(?:\.min)?\.js", r"data-v-[0-9a-f]{6,}"],
    "Angular": [r"ng-version=", r"<app-root"],
    "jQuery": [r"jquery"],
    "Bootstrap": [r"bootstrap(?:\.min)?\.(?:css|js)", r"bootstrapcdn"],
    "Tailwind CSS": [r"tailwindcss", r"cdn\.tailwindcss\.com"],
    "Shopify": [r"cdn\.shopify\.com"],
    "Wix": [r"wixstatic\.com"],
    "Font Awesome": [r"font-?awesome"],
    "Google Fonts": [r"fonts\.googleapis\.com"],
    "Google Analytics": [r"google-analytics\.com", r"gtag/js"],
    "Google Tag Manager": [r"googletagmanager\.com/gtm\.js"],
}

TECH_HEADERS = {
    "Cloudflare": r"cf-ray:|server: cloudflare",
    "Vercel": r"x-vercel-id:",
    "Netlify": r"x-nf-request-id:|server: netlify",
    "Nginx": r"server: nginx",
    "Apache": r"server: apache",
    "LiteSpeed": r"server: litespeed",
    "Express": r"x-powered-by: express",
    "PHP": r"x-powered-by: php",
    "ASP.NET": r"x-powered-by: asp\.net|x-aspnet-version",
    "Laravel": r"laravel_session",
}


# ---------- Validasi URL ----------


def _ip_ok(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


async def _check_host(host: str):
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("Domain tidak ditemukan (DNS gagal).")
    if not infos or not all(_ip_ok(str(i[4][0])) for i in infos):
        raise ValueError("Alamat IP internal/privat/localhost tidak diizinkan.")


async def check_url(url: str):
    """Validasi URL. Raise ValueError (pesan aman ditampilkan) jika ditolak."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("URL harus diawali http:// atau https://")
    if p.username or p.password:
        raise ValueError("URL dengan kredensial tidak diizinkan.")
    host = p.hostname.lower().rstrip(".")
    if any(host == d or host.endswith("." + d) for d in OWN_DOMAINS):
        raise ValueError("Website SenPlay tidak bisa dianalisis dengan bot ini.")
    try:
        port = p.port
    except ValueError:
        raise ValueError("Port tidak valid.")
    if port not in (None, 80, 443):
        raise ValueError("Hanya port 80/443 yang diizinkan.")
    await _check_host(host)


# ---------- Fetch & analisis ----------


async def fetch_page(url: str) -> dict:
    chain = []
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await check_url(url)  # cek ulang di setiap hop redirect
            t0 = time.perf_counter()
            async with client.stream("GET", url) as r:
                ttfb = time.perf_counter() - t0
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    chain.append((r.status_code, url))
                    url = urljoin(url, loc)
                    continue
                body = bytearray()
                truncated = False
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        del body[MAX_BYTES:]
                        truncated = True
                        break
                total = time.perf_counter() - t0
                ctype = r.headers.get("content-type", "")
                html = None
                if not ctype or "html" in ctype.lower():
                    try:
                        html = bytes(body).decode(
                            r.encoding or "utf-8", errors="replace"
                        )
                    except LookupError:
                        html = bytes(body).decode("utf-8", errors="replace")
                header_text = "\n".join(
                    f"{k}: {v}" for k, v in r.headers.multi_items()
                ).lower()
                return {
                    "url": url,
                    "status": r.status_code,
                    "reason": r.reason_phrase,
                    "chain": chain,
                    "headers": r.headers,
                    "header_text": header_text,
                    "ctype": ctype.split(";")[0].strip(),
                    "ttfb": ttfb,
                    "total": total,
                    "size": len(body),
                    "truncated": truncated,
                    "html": html,
                }
    raise ValueError("Terlalu banyak redirect.")


def _attr(tag, name: str) -> str:
    """Ambil atribut tag sebagai string (aman untuk type checker)."""
    if not isinstance(tag, Tag):
        return ""
    v = tag.get(name)
    if isinstance(v, list):
        v = " ".join(v)
    return (v or "").strip()


def analyze_html(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    metas: dict[tuple[str, str], str] = {}
    for t in soup.select("meta"):
        for key in ("name", "property"):
            k = _attr(t, key).lower()
            if k:
                metas.setdefault((key, k), _attr(t, "content"))

    def meta(attr: str, name: str) -> str:
        return metas.get((attr, name.lower()), "")

    base_host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    internal = external = 0
    for a in soup.select("a[href]"):
        href = _attr(a, "href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        host = (
            (urlparse(urljoin(base_url, href)).hostname or "")
            .lower()
            .removeprefix("www.")
        )
        if host == base_host:
            internal += 1
        else:
            external += 1

    imgs = soup.select("img")
    scripts = soup.select("script")
    js_ext = sum(1 for s in scripts if _attr(s, "src"))
    title_tag = soup.select_one("title")
    return {
        "title": title_tag.get_text(strip=True) if title_tag else "",
        "description": meta("name", "description"),
        "robots": meta("name", "robots"),
        "viewport": meta("name", "viewport"),
        "generator": meta("name", "generator"),
        "og_title": meta("property", "og:title"),
        "canonical": soup.select_one('link[rel~="canonical"]') is not None,
        "lang": _attr(soup.select_one("html"), "lang"),
        "h1": len(soup.select("h1")),
        "h2": len(soup.select("h2")),
        "links_int": internal,
        "links_ext": external,
        "imgs": len(imgs),
        "imgs_no_alt": sum(1 for i in imgs if not i.has_attr("alt")),
        "js_ext": js_ext,
        "js_inline": len(scripts) - js_ext,
        "css_ext": len(soup.select('link[rel~="stylesheet"]')),
        "css_inline": len(soup.select("style")),
    }


def detect_tech(f: dict, a: dict | None) -> list[str]:
    found = []
    html = (f["html"] or "").lower()
    for name, pats in TECH_HTML.items():
        if html and any(re.search(p, html) for p in pats):
            found.append(name)
    for name, pat in TECH_HEADERS.items():
        if re.search(pat, f["header_text"]):
            found.append(name)
    if a and a["generator"] and a["generator"] not in found:
        found.append(a["generator"][:40])
    return list(dict.fromkeys(found))


# ---------- Screenshot ----------

_shot_lock = asyncio.Semaphore(1)  # 1 browser sekaligus, hemat RAM VPS


async def take_screenshot(url: str) -> bytes:
    from playwright.async_api import async_playwright

    async with _shot_lock:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 720}, user_agent=UA
                )
                cache: dict[str, bool] = {}

                async def guard(route):
                    u = route.request.url
                    p = urlparse(u)
                    if p.scheme in ("data", "blob"):
                        await route.continue_()
                        return
                    key = f"{p.scheme}://{p.netloc}"
                    if key not in cache:
                        try:
                            await check_url(u)
                            cache[key] = True
                        except ValueError:
                            cache[key] = False
                    if cache[key]:
                        await route.continue_()
                    else:
                        await route.abort()

                await ctx.route("**/*", guard)
                page = await ctx.new_page()
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=SHOT_TIMEOUT * 1000
                )
                await page.wait_for_timeout(1500)
                return await page.screenshot(type="jpeg", quality=70)
            finally:
                await browser.close()


# ---------- Embed ----------


def _cut(s: str, n: int = 1024) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def seo_lines(a: dict) -> list[str]:
    out = []

    def add(ok, text):
        out.append(f"{'✅' if ok else '⚠️'} {text}")

    t, d = a["title"], a["description"]
    add(
        bool(t) and 10 <= len(t) <= 70,
        f"Title: {len(t)} karakter" if t else "Title tidak ada",
    )
    add(
        bool(d) and 50 <= len(d) <= 170,
        f"Description: {len(d)} karakter" if d else "Meta description tidak ada",
    )
    add(a["h1"] == 1, f"H1: {a['h1']} (ideal 1)")
    add(a["canonical"], "Canonical ada" if a["canonical"] else "Canonical tidak ada")
    add(
        bool(a["viewport"]),
        "Meta viewport ada" if a["viewport"] else "Meta viewport tidak ada",
    )
    add(
        bool(a["lang"]), f"Lang: {a['lang']}" if a["lang"] else "Atribut lang tidak ada"
    )
    add(
        bool(a["og_title"]),
        "Open Graph ada" if a["og_title"] else "Open Graph tidak ada",
    )
    add(a["imgs_no_alt"] == 0, f"Gambar tanpa alt: {a['imgs_no_alt']}/{a['imgs']}")
    if "noindex" in a["robots"].lower():
        add(False, "robots: noindex")
    return out


def security_lines(f: dict) -> tuple[list[str], int, int]:
    https = f["url"].startswith("https://")
    lines = ["✅ HTTPS" if https else "❌ Tidak memakai HTTPS"]
    ok = 1 if https else 0
    total = 1
    for key, label in SECURITY_HEADERS:
        if key == "strict-transport-security" and not https:
            continue
        total += 1
        if key in f["headers"]:
            ok += 1
            lines.append(f"✅ {label}")
        else:
            lines.append(f"❌ {label}")
    return lines, ok, total


def build_embed(f: dict, a: dict | None, techs: list[str]) -> discord.Embed:
    st = f["status"]
    color = (
        discord.Color.green()
        if st < 300
        else discord.Color.orange() if st < 400 else discord.Color.red()
    )
    host = urlparse(f["url"]).hostname
    embed = discord.Embed(title=f"🌐 {host}", url=f["url"], color=color)
    if a:
        desc = f"**{_cut(a['title'], 200) or '(tanpa title)'}**"
        if a["description"]:
            desc += f"\n{_cut(a['description'], 300)}"
        embed.description = desc

    redirect = (
        " → ".join(f"{c}" for c, _ in f["chain"]) + f" → {st}"
        if f["chain"]
        else "Tidak ada"
    )
    embed.add_field(
        name="📄 Info",
        value=_cut(
            f"**Status:** {st} {f['reason']}\n"
            f"**Tipe:** {f['ctype'] or '-'}\n"
            f"**Redirect:** {redirect}\n"
            f"**URL akhir:** {_cut(f['url'], 150)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚡ Performa",
        value=(
            f"**Respons:** {f['ttfb'] * 1000:.0f} ms\n"
            f"**Total:** {f['total'] * 1000:.0f} ms\n"
            f"**Ukuran:** {_kb(f['size'])}{' (terpotong)' if f['truncated'] else ''}"
        ),
        inline=True,
    )
    embed.add_field(
        name="🧩 Teknologi",
        value=_cut(", ".join(techs) or "Tidak terdeteksi"),
        inline=True,
    )
    if a:
        embed.add_field(
            name="🔎 HTML",
            value=(
                f"**Link:** {a['links_int']} internal / {a['links_ext']} eksternal\n"
                f"**Gambar:** {a['imgs']}\n"
                f"**JS:** {a['js_ext']} eksternal / {a['js_inline']} inline\n"
                f"**CSS:** {a['css_ext']} eksternal / {a['css_inline']} inline\n"
                f"**Heading:** H1 {a['h1']} / H2 {a['h2']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="📈 SEO Dasar", value=_cut("\n".join(seo_lines(a))), inline=True
        )
    lines, ok, total = security_lines(f)
    embed.add_field(
        name=f"🔒 Security Headers ({ok}/{total})",
        value=_cut("\n".join(lines)),
        inline=True,
    )
    return embed


# ---------- Command ----------


def _fetch_error(e: BaseException) -> str:
    if isinstance(e, ValueError):
        return str(e)
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "Timeout saat mengambil halaman."
    if isinstance(e, httpx.HTTPError):
        return f"Koneksi gagal ({type(e).__name__})."
    print(f"[website] fetch error: {type(e).__name__}: {e}")
    return "Gagal mengambil halaman."


def setup(bot):
    @bot.tree.command(
        name="website",
        description="Analisis website publik: info, teknologi, SEO, dan security headers",
    )
    @app_commands.describe(url="URL website, contoh: https://example.com")
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def website(interaction: discord.Interaction, url: str):
        if interaction.channel_id != ALLOWED_CHANNEL_ID:
            await interaction.response.send_message(
                f"Command ini hanya bisa dipakai di <#{ALLOWED_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)

        url = url.strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        try:
            await check_url(url)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}")
            return

        res, shot = await asyncio.gather(
            asyncio.wait_for(fetch_page(url), FETCH_TIMEOUT + 15),
            asyncio.wait_for(take_screenshot(url), SHOT_TIMEOUT + 10),
            return_exceptions=True,
        )
        if isinstance(shot, BaseException):
            print(f"[website] screenshot gagal: {type(shot).__name__}: {shot}")
            shot = None

        if isinstance(res, BaseException):
            embed = discord.Embed(
                title=f"🌐 {urlparse(url).hostname}",
                description=f"❌ {_fetch_error(res)}",
                color=discord.Color.red(),
            )
        else:
            a = None
            if res["html"]:
                try:
                    a = await asyncio.to_thread(analyze_html, res["html"], res["url"])
                except Exception as e:
                    print(f"[website] analisis HTML gagal: {type(e).__name__}: {e}")
            embed = build_embed(res, a, detect_tech(res, a))

        kwargs: dict = {}
        if shot:
            embed.set_image(url="attachment://screenshot.jpg")
            kwargs["file"] = discord.File(io.BytesIO(shot), filename="screenshot.jpg")
        else:
            embed.add_field(name="📸 Screenshot", value="Tidak tersedia.", inline=False)
        embed.set_footer(text="Analisis pasif • SENP4II")
        await interaction.followup.send(embed=embed, **kwargs)
