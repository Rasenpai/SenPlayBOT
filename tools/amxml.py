"""Exporter SVG -> file project XML Alight Motion. Tidak bergantung pada Discord.

Format dipelajari dari tiga sampel project asli (Alight Motion 5.0.273, Android):
  <scene ...>                                  kanvas + durasi
    <shape id label startTime endTime fillType mediaFillMode>
      <transform><location/><opacity/></transform>
      <fillColor value="#aarrggbb"/>
      <path-stroke direction end-size><color/><size/></path-stroke>
      <path d="M x yL x y..."/>                titik relatif terhadap location

Yang TERBUKTI dari sampel: struktur di atas, path dengan segmen garis lurus (M/L),
warna #aarrggbb, opacity 0..1, stroke centered. Karena sampel hanya berisi garis
lurus, kurva (Bezier/arc) DIRATAKAN jadi garis kecil. Gradient, teks, gambar, clip
dan filter belum ada sampelnya sehingga tidak didukung (dilaporkan di Report).
"""

import io
import math
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

import svgelements as se

from tools import svgcore
from tools.svgcore import Canvas, Conversion, Layer, Report, SvgError

TARGET_SIDE = (
    1440  # sisi terpanjang project (sama dengan sampel); vektor, jadi bebas skala
)
DURATION_MS = 5000
FPS = 30
FLATTEN_TOLERANCE = (
    0.2  # px project: simpangan maksimum garis hasil ratakan dari kurva asli
)
MAX_SEGMENTS_PER_CURVE = 48
ID_BASE = 300000000

# Atribut <scene> disalin dari sampel project asli supaya versi format cocok.
SCENE_EXTRA = (
    'precompose="dynamicResolution" bgcolor="#ff000000" '
    f'fps="{FPS}" amver="1002351" ffver="106" '
    'am="com.alightcreative.motion/5.0.272.1028383" amplatform="android" '
    'retime="freeze" retimeAdaptFPS="false"'
)

UNSUPPORTED_NODES = {"text": "text", "image": "image"}


@dataclass
class XmlResult:
    conversion: Conversion  # dipakai ulang untuk embed (kanvas, daftar layer, catatan)
    xml: str


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------


def _num(v):
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _f6(v):
    return f"{float(v):.6f}"


def _even(v):
    n = max(2, int(round(v)))
    return n + (n % 2)


def _css_prop(style, name):
    m = re.search(
        r"(?:^|;)\s*" + re.escape(name) + r"\s*:\s*([^;]+)", style or "", re.I
    )
    return m.group(1).strip() if m else None


def _strip_css_prop(style, name):
    return re.sub(
        r"(?:^|(?<=;))\s*" + re.escape(name) + r"\s*:\s*[^;]*;?",
        "",
        style or "",
        flags=re.I,
    )


def _to_float(text, default=None):
    try:
        return float(str(text).strip().rstrip("%")) / (
            100 if str(text).strip().endswith("%") else 1
        )
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pra-proses pohon SVG yang sudah disanitasi
# ---------------------------------------------------------------------------

CONTAINERS = {"svg", "g", "a", "switch"}
SKIP_SUBTREES = set(svgcore.DEFS_TAGS) | {"title", "desc", "metadata"}


def _bake_opacity(root):
    """Kalikan opacity grup ke setiap bentuk di dalamnya (opacity grup tidak diwariskan
    dengan benar oleh parser), lalu hapus dari grup."""
    stack = [(root, 1.0)]
    while stack:
        node, inherited = stack.pop()
        for child in list(node):
            name = svgcore.local(child.tag)
            if name in SKIP_SUBTREES:
                continue
            style = child.get("style", "")
            own = _to_float(_css_prop(style, "opacity"))
            if own is None:
                own = _to_float(child.get("opacity"), 1.0)
            own = min(max(own, 0.0), 1.0)
            if "opacity" in style.lower():
                child.set("style", _strip_css_prop(style, "opacity"))
            eff = inherited * own
            if name in CONTAINERS:
                child.attrib.pop("opacity", None)
                stack.append((child, eff))
            else:
                child.set("opacity", f"{eff:.6f}")


def _gradient_colors(root):
    """id gradient -> warna rata-rata dari stop-nya (pendekatan kasar)."""
    ids = {el.get("id"): el for el in root.iter() if el.get("id")}
    out = {}

    def stops_of(el, depth=0):
        found = [c for c in el if svgcore.local(c.tag) == "stop"]
        if found or depth > 3:
            return found
        href = el.get("href") or el.get(f"{{{svgcore.XLINK_NS}}}href") or ""
        ref = ids.get(href[1:]) if href.startswith("#") else None
        return stops_of(ref, depth + 1) if ref is not None else []

    for gid, el in ids.items():
        if svgcore.local(el.tag) not in ("linearGradient", "radialGradient"):
            continue
        cols = []
        for st in stops_of(el):
            style = st.get("style", "")
            raw = _css_prop(style, "stop-color") or st.get("stop-color") or "black"
            try:
                cols.append(se.Color(raw))
            except Exception:  # noqa: BLE001
                pass
        if cols:
            r = sum(c.red for c in cols) / len(cols)
            g = sum(c.green for c in cols) / len(cols)
            b = sum(c.blue for c in cols) / len(cols)
            out[gid] = (round(r), round(g), round(b))
    return out


# ---------------------------------------------------------------------------
# Warna & paint
# ---------------------------------------------------------------------------


def _argb(rgb, alpha):
    a = min(max(alpha, 0.0), 1.0)
    return "#{:02x}{:02x}{:02x}{:02x}".format(round(a * 255), rgb[0], rgb[1], rgb[2])


def _paint(color, raw_value, grads, report):
    """Kembalikan (rgb, alpha 0..1) atau None bila tidak ada cat."""
    raw = str(raw_value or "").strip().lower()
    if raw in ("none", "transparent"):
        return None
    alpha = getattr(color, "alpha", None)
    if alpha is None or alpha == 0:
        return None
    if raw.startswith("url("):
        m = re.match(r"url\(\s*['\"]?#([^'\")\s]+)", raw)
        gid = m.group(1) if m else None
        rgb = grads.get(gid)
        report.unsupported["gradient (diganti warna rata-rata)"] += 1
        if rgb is None:
            rgb = (128, 128, 128)
        return rgb, alpha / 255
    return (color.red, color.green, color.blue), alpha / 255


# ---------------------------------------------------------------------------
# Pemrosesan path: ratakan kurva jadi garis (satu-satunya bentuk yang terbukti)
# ---------------------------------------------------------------------------


def _cubic_points(p0, p1, p2, p3):
    """Titik hasil ratakan Bezier kubik (tanpa titik awal). Jumlah segmen ditentukan
    dari kelengkungan: kurva hampir lurus hanya butuh sedikit titik."""
    m = max(
        math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
        math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]),
    )
    n = max(
        1,
        min(MAX_SEGMENTS_PER_CURVE, math.ceil(math.sqrt(0.75 * m / FLATTEN_TOLERANCE))),
    )
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        out.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    return out


def _quad_points(p0, p1, p2):
    m = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
    n = max(
        1,
        min(MAX_SEGMENTS_PER_CURVE, math.ceil(math.sqrt(m / (4 * FLATTEN_TOLERANCE)))),
    )
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append(
            (
                u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
            )
        )
    return out


def _subpaths(path, scale):
    """Daftar (titik[(x,y)], tertutup) dalam koordinat project (sudah diskalakan)."""
    subs, cur = [], None

    def pt(p):
        return (float(p.x) * scale, float(p.y) * scale)

    def flush(closed):
        nonlocal cur
        if cur is not None and len(cur) > 1:
            subs.append((cur, closed))
        cur = None

    for seg in path:
        if isinstance(seg, se.Move):
            flush(False)
            cur = [pt(seg.end)]
            continue
        if isinstance(seg, se.Close):
            flush(True)
            continue
        if cur is None:
            if seg.start is None:
                continue
            cur = [pt(seg.start)]
        if isinstance(seg, se.Line):
            cur.append(pt(seg.end))
        elif isinstance(seg, se.CubicBezier):
            cur.extend(
                _cubic_points(
                    pt(seg.start), pt(seg.control1), pt(seg.control2), pt(seg.end)
                )
            )
        elif isinstance(seg, se.QuadraticBezier):
            cur.extend(_quad_points(pt(seg.start), pt(seg.control), pt(seg.end)))
        elif isinstance(seg, se.Arc):
            for cb in seg.as_cubic_curves():
                cur.extend(
                    _cubic_points(
                        pt(cb.start), pt(cb.control1), pt(cb.control2), pt(cb.end)
                    )
                )
        else:
            cur.append(pt(seg.end))
    flush(False)
    return subs


def _path_data(subs, filled, cx, cy):
    """String atribut d: 'M x yL x y...' dengan titik relatif ke (cx, cy)."""
    parts = []
    for points, closed in subs:
        pts, last = [], None
        for x, y in points:
            q = (round(x - cx, 3), round(y - cy, 3))
            if q != last:
                pts.append(q)
                last = q
        if len(pts) < 2:
            continue
        if (closed or filled) and pts[0] != pts[-1]:
            pts.append(pts[0])  # sampel menutup path dengan mengulang titik awal
        parts.append(f"M {_num(pts[0][0])} {_num(pts[0][1])}")
        parts.extend(f"L {_num(x)} {_num(y)}" for x, y in pts[1:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Konversi utama
# ---------------------------------------------------------------------------


def _layer_label(el_id, index):
    cleaned = re.sub(r"\s+", " ", (el_id or "").strip())[:60]
    return cleaned or f"Shape {index}"


def convert_to_xml(data: bytes, title: str = "SVG Project") -> XmlResult:
    report = Report()
    root = svgcore.parse_svg(data)
    svgcore.sanitize(root, report)
    canvas = svgcore.get_canvas(root)

    # Peringatan untuk fitur yang diabaikan parser.
    for el in root.iter():
        name = svgcore.local(el.tag)
        if name in UNSUPPORTED_NODES:
            report.unsupported[UNSUPPORTED_NODES[name]] += 1
        elif name in ("clipPath", "mask", "filter"):
            report.unsupported[f"{name} (diabaikan)"] += 1
        if el.get("stroke-dasharray") not in (None, "none"):
            report.unsupported["garis putus-putus (jadi garis penuh)"] += 1

    grads = _gradient_colors(root)
    _bake_opacity(root)

    # Seragamkan root supaya viewBox dipetakan ke (0,0,w,h) oleh parser.
    for key in ("x", "y"):
        root.attrib.pop(key, None)
    root.set("width", svgcore.fmt(canvas.width))
    root.set("height", svgcore.fmt(canvas.height))
    root.set("viewBox", " ".join(svgcore.fmt(n) for n in canvas.viewbox))
    markup = ET.tostring(root, encoding="unicode")

    try:
        doc = se.SVG.parse(io.StringIO(markup), reify=True)
    except Exception as err:  # noqa: BLE001
        raise SvgError(f"SVG tidak bisa diproses: {err}")

    scale = TARGET_SIDE / max(canvas.width, canvas.height)
    am_w, am_h = _even(canvas.width * scale), _even(canvas.height * scale)
    cx, cy = canvas.width * scale / 2, canvas.height * scale / 2

    shapes, compound, evenodd = [], 0, 0
    for el in doc.elements():
        if not isinstance(el, se.Shape):
            continue
        if str(el.values.get("visibility", "")).lower() in ("hidden", "collapse"):
            report.hidden += 1
            continue
        fill = _paint(el.fill, el.values.get("fill"), grads, report)
        stroke = _paint(el.stroke, el.values.get("stroke"), grads, report)
        if fill is None and stroke is None:
            continue

        path = se.Path(el)
        path.reify()
        subs = _subpaths(path, scale)
        d = _path_data(subs, fill is not None, cx, cy)
        if not d:
            continue
        if len(subs) > 1:
            compound += 1
            if str(el.values.get("fill-rule", "")).lower() == "evenodd":
                evenodd += 1

        opacity = _to_float(el.values.get("opacity"), 1.0)
        shapes.append(
            dict(
                label=_layer_label(el.id, len(shapes) + 1),
                tag=type(el).__name__.lower(),
                fill=fill,
                stroke=stroke,
                stroke_w=float(el.stroke_width or 0) * scale,
                opacity=min(max(opacity, 0.0), 1.0),
                d=d,
            )
        )
        if len(shapes) > svgcore.MAX_LAYERS:
            raise SvgError(
                f"Terlalu banyak layer (lebih dari {svgcore.MAX_LAYERS}). "
                "Gabungkan objek di editor SVG, lalu coba lagi."
            )

    if not shapes:
        raise SvgError("SVG tidak berisi bentuk yang bisa dijadikan layer.")

    # --- tulis XML ---
    out = [
        "<?xml version='1.0' encoding='UTF-8' ?>",
        f'<scene title={quoteattr(title[:80])} width="{am_w}" height="{am_h}" '
        f'exportWidth="{am_w}" exportHeight="{am_h}" {SCENE_EXTRA} '
        f'totalTime="{DURATION_MS}" modifiedTime="{int(time.time() * 1000)}">',
    ]
    layers, used = [], Counter()
    for i, s in enumerate(shapes, 1):
        label = s["label"]
        used[label] += 1
        if used[label] > 1:
            label = f"{label} {used[label]}"
        out.append(
            f'  <shape id="{ID_BASE + i}" label={quoteattr(label)} startTime="0" '
            f'endTime="{DURATION_MS}" fillType="color" mediaFillMode="fill">'
        )
        out.append("    <transform>")
        out.append(f'      <location value="{_f6(cx)},{_f6(cy)},0.000000" />')
        if s["opacity"] < 0.999:
            out.append(f"      <opacity value=\"{_f6(s['opacity'])}\" />")
        out.append("    </transform>")
        if s["fill"]:
            out.append(f"    <fillColor value=\"{_argb(*s['fill'])}\" />")
        else:
            out.append('    <fillColor value="#00000000" />')
        if s["stroke"] and s["stroke_w"] > 0:
            out.append('    <path-stroke direction="centered" end-size="1.500000">')
            out.append(f"      <color value=\"{_argb(*s['stroke'])}\" />")
            out.append(f"      <size value=\"{_f6(s['stroke_w'])}\" />")
            out.append("    </path-stroke>")
        out.append(f"    <path d=\"{s['d']}\" />")
        out.append("  </shape>")
        layers.append(Layer(i, label, s["tag"], "", ""))
    out.append("</scene>")
    xml = "\n".join(out) + "\n"

    if len(xml.encode("utf-8")) > svgcore.MAX_OUTPUT:
        raise SvgError(
            f"Hasil XML melebihi {svgcore.MAX_OUTPUT // svgcore.MB} MB. "
            "Sederhanakan SVG (kurangi titik/objek), lalu coba lagi."
        )

    if compound:
        report.warnings.append(
            f"{compound} bentuk punya lebih dari satu sub-path (mis. lubang). "
            "Cara Alight Motion menyimpan lubang belum ada di sampel, jadi hasilnya "
            "belum terverifikasi."
        )
    if evenodd:
        report.warnings.append(
            f"{evenodd} bentuk memakai fill-rule evenodd; aturan isian belum terverifikasi."
        )
    if any(s["stroke"] for s in shapes):
        report.warnings.append(
            "Garis tepi: hanya warna dan tebal yang dibawa (cap/join diabaikan)."
        )
    report.warnings.append(
        "Kurva diratakan jadi garis-garis pendek (satu-satunya bentuk path yang terbukti di sampel)."
    )

    return XmlResult(
        Conversion(Canvas(am_w, am_h, (0.0, 0.0, am_w, am_h)), layers, report), xml
    )
