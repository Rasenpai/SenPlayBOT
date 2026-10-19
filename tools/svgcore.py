"""Parser & pemecah layer SVG. Tidak bergantung pada Discord.

Alur: parse aman -> sanitasi -> keluarkan defs -> temukan layer -> bangun SVG per layer.
Exporter Alight Motion XML belum ada: butuh sampel project asli untuk diverifikasi.
"""

import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

MB = 1024 * 1024

MAX_BYTES = 15 * MB  # ukuran file SVG input (sebelumnya 5 MB)
MAX_ELEMENTS = 100000  # dinaikkan agar SVG 15 MB / 5.000 layer muat (sebelumnya 20.000)
MAX_DEPTH = 64
MAX_LAYERS = 5000  # sebelumnya 200
MAX_CANVAS = 20000
MAX_OUTPUT = 20 * MB  # ukuran ZIP hasil konversi
MAX_RAW_OUTPUT = 150 * MB  # total SVG mentah di memori sebelum di-ZIP (pengaman RAM)

# Elemen yang dijadikan layer.
DRAWABLE = {
    "path",
    "rect",
    "circle",
    "ellipse",
    "polygon",
    "polyline",
    "line",
    "text",
    "g",
    "use",
    "image",
    "svg",
}
# Elemen definisi: disalin ke setiap layer (gradient, clip, style, dll).
DEFS_TAGS = {
    "defs",
    "style",
    "linearGradient",
    "radialGradient",
    "pattern",
    "clipPath",
    "mask",
    "filter",
    "symbol",
    "marker",
}
SKIP_TAGS = {"title", "desc", "metadata"}
# Elemen berbahaya / tidak relevan: dibuang saat sanitasi.
DANGEROUS = {
    "script",
    "foreignObject",
    "iframe",
    "object",
    "embed",
    "audio",
    "video",
    "animate",
    "animateTransform",
    "animateMotion",
    "set",
    "animateColor",
}

SAFE_IMAGE_RE = r"data:image/(?:png|jpe?g|gif|webp)"
UNITS = {
    "": 1.0,
    "px": 1.0,
    "pt": 96 / 72,
    "pc": 16.0,
    "mm": 96 / 25.4,
    "cm": 96 / 2.54,
    "in": 96.0,
}


class SvgError(Exception):
    """Error yang pesannya aman ditampilkan ke pengguna."""


@dataclass
class Canvas:
    width: float
    height: float
    viewbox: tuple


@dataclass
class Layer:
    index: int
    name: str
    tag: str
    filename: str
    svg: str


@dataclass
class Report:
    removed: Counter = field(default_factory=Counter)  # elemen berbahaya dibuang
    unsupported: Counter = field(
        default_factory=Counter
    )  # elemen tak didukung (dilewati)
    hidden: int = 0
    external_refs: int = 0
    warnings: list = field(default_factory=list)


@dataclass
class Conversion:
    canvas: Canvas
    layers: list
    report: Report


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def svg_tag(name):
    return f"{{{SVG_NS}}}{name}"


def fmt(n):
    return f"{n:.4f}".rstrip("0").rstrip(".")


def parse_length(value):
    m = re.fullmatch(
        r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*",
        value or "",
    )
    if not m:
        return None
    unit = m.group(2).lower()
    if unit not in UNITS:
        return None  # %, em, ex, dll tidak bisa dipastikan
    result = float(m.group(1)) * UNITS[unit]
    return result if result > 0 else None


def clean_css(text):
    if not text:
        return text
    text = re.sub(r"@import[^;]*;?", "", text, flags=re.I)
    text = re.sub(r"expression\([^)]*\)", "", text, flags=re.I)
    text = re.sub(r"javascript:[^;\"')]*", "", text, flags=re.I)
    # url(...) eksternal dibuang; url(#id) dan data:image aman dipertahankan.
    text = re.sub(
        r"url\(\s*(['\"]?)(?!#|" + SAFE_IMAGE_RE + r")[^)]*\)",
        "none",
        text,
        flags=re.I,
    )
    return text


# ---------------------------------------------------------------------------
# Parse + sanitasi
# ---------------------------------------------------------------------------


def parse_svg(data: bytes):
    if len(data) > MAX_BYTES:
        raise SvgError(f"File terlalu besar (maks {MAX_BYTES // MB} MB).")
    try:
        root = DET.fromstring(data, forbid_dtd=True)
    except DefusedXmlException:
        raise SvgError("SVG ditolak: berisi DTD/entity yang tidak diizinkan.")
    except ET.ParseError as err:
        raise SvgError(f"File bukan XML/SVG yang valid: {err}")
    except RecursionError:
        raise SvgError("Struktur SVG terlalu dalam.")

    if local(root.tag) != "svg":
        raise SvgError("File bukan SVG (elemen akar bukan <svg>).")
    _normalize_namespace(root)
    return root


def _normalize_namespace(root):
    """SVG tanpa xmlns tetap diterima: semua tag diberi namespace SVG."""
    for el in root.iter():
        if isinstance(el.tag, str) and not el.tag.startswith("{"):
            el.tag = svg_tag(el.tag)


def _clean_attributes(el, report):
    for key in list(el.attrib):
        value = el.attrib[key]

        if key.startswith("{"):
            ns, _, name = key[1:].partition("}")
            if ns == INKSCAPE_NS and name == "label" and not el.get("data-name"):
                el.set("data-name", value)  # simpan nama layer dari Inkscape
            if ns == XLINK_NS and name == "href":
                if not _href_ok(value):
                    del el.attrib[key]
                    report.external_refs += 1
            else:
                del el.attrib[key]
            continue

        low = key.lower()
        if low.startswith("on"):
            del el.attrib[key]
            report.removed["event-handler"] += 1
        elif low == "href":
            if not _href_ok(value):
                del el.attrib[key]
                report.external_refs += 1
        elif (
            low == "style" or "url(" in value.lower() or "javascript:" in value.lower()
        ):
            el.attrib[key] = clean_css(value)


def _href_ok(value):
    v = value.strip()
    return v.startswith("#") or re.match(SAFE_IMAGE_RE + r"[;,]", v, re.I) is not None


def sanitize(root, report):
    count = 0
    _clean_attributes(root, report)
    stack = [(root, 0)]
    while stack:
        parent, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise SvgError("Struktur SVG terlalu dalam.")
        for child in list(parent):
            count += 1
            if count > MAX_ELEMENTS:
                raise SvgError(
                    f"SVG terlalu kompleks (lebih dari {MAX_ELEMENTS} elemen)."
                )
            if not isinstance(child.tag, str):
                parent.remove(child)
                continue

            name = local(child.tag)
            if not child.tag.startswith(f"{{{SVG_NS}}}") or name in DANGEROUS:
                parent.remove(child)
                report.removed[name] += 1
                continue

            if name == "a":  # tautan dijadikan grup biasa
                child.tag = svg_tag("g")
                name = "g"

            _clean_attributes(child, report)
            if name == "style":
                child.text = clean_css(child.text)
            stack.append((child, depth + 1))


# ---------------------------------------------------------------------------
# Kanvas
# ---------------------------------------------------------------------------


def get_canvas(root):
    vbox = None
    raw = root.get("viewBox")
    if raw:
        parts = [p for p in re.split(r"[,\s]+", raw.strip()) if p]
        if len(parts) == 4:
            try:
                nums = tuple(float(p) for p in parts)
                if nums[2] > 0 and nums[3] > 0:
                    vbox = nums
            except ValueError:
                pass

    w = parse_length(root.get("width"))
    h = parse_length(root.get("height"))

    if vbox:
        vw, vh = vbox[2], vbox[3]
        if w and h:
            pass
        elif w:
            h = w * vh / vw
        elif h:
            w = h * vw / vh
        else:
            w, h = vw, vh
    else:
        if not (w and h):
            raise SvgError(
                "Ukuran kanvas tidak ditemukan. SVG harus punya viewBox "
                "atau width dan height dalam satuan angka (px, mm, in, dll)."
            )
        vbox = (0.0, 0.0, w, h)

    if max(w, h) > MAX_CANVAS:
        raise SvgError(f"Ukuran kanvas terlalu besar (maks {MAX_CANVAS}px).")
    return Canvas(w, h, vbox)


# ---------------------------------------------------------------------------
# Pemecahan layer
# ---------------------------------------------------------------------------


def extract_defs(root):
    """Pindahkan semua elemen definisi ke satu daftar (disalin ke tiap layer)."""
    bucket = []
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in list(parent):
            if local(child.tag) in DEFS_TAGS:
                parent.remove(child)
                bucket.append(child)
            else:
                stack.append(child)
    return bucket


def _is_hidden(el):
    if el.get("display", "").strip() == "none":
        return True
    return bool(re.search(r"display\s*:\s*none", el.get("style", ""), re.I))


def _shallow(el):
    return ET.Element(el.tag, dict(el.attrib))


def discover_layers(root, report):
    """Turun lewat pembungkus <g> tunggal, lalu tiap anak = satu layer."""
    chain = []
    current = root
    while True:
        kids = []
        for child in current:
            name = local(child.tag)
            if name in SKIP_TAGS:
                continue
            if name not in DRAWABLE:
                report.unsupported[name] += 1
                continue
            if _is_hidden(child):
                report.hidden += 1
                continue
            kids.append(child)

        if len(kids) == 1 and local(kids[0].tag) == "g":
            chain.append(_shallow(kids[0]))
            current = kids[0]
            continue
        return chain, kids


def _id_map(root, bucket):
    ids = {}
    for source in [root, *bucket]:
        for el in source.iter():
            if el.get("id"):
                ids.setdefault(el.get("id"), el)
    return ids


def _use_targets(element, ids, bucket_ids):
    """Target <use> yang ada di luar layer ini (supaya tidak hilang)."""
    inside = {id(e) for e in element.iter()}
    found, queue = {}, [element]
    while queue and len(found) < 50:
        node = queue.pop()
        for el in node.iter():
            if local(el.tag) != "use":
                continue
            href = el.get("href") or el.get(f"{{{XLINK_NS}}}href") or ""
            ref = href[1:] if href.startswith("#") else None
            target = ids.get(ref) if ref else None
            if target is None or ref in found or ref in bucket_ids:
                continue
            if id(target) in inside:
                continue
            found[ref] = target
            queue.append(target)
    return list(found.values())


def _layer_name(el, tag):
    raw = el.get("data-name") or el.get("id") or tag
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")[:40]
    return cleaned or tag


def build_layer_svg(src_root, canvas, bucket, chain, element, extra_defs):
    root = ET.Element(svg_tag("svg"))
    for key, value in src_root.attrib.items():
        if key not in ("width", "height", "viewBox", "x", "y"):
            root.set(key, value)
    root.set("width", fmt(canvas.width))
    root.set("height", fmt(canvas.height))
    root.set("viewBox", " ".join(fmt(n) for n in canvas.viewbox))

    for item in bucket:
        root.append(deepcopy(item))
    if extra_defs:
        defs = ET.SubElement(root, svg_tag("defs"))
        for item in extra_defs:
            defs.append(deepcopy(item))

    parent = root
    for wrapper in chain:
        clone = _shallow(wrapper)
        parent.append(clone)
        parent = clone
    parent.append(deepcopy(element))

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def convert(data: bytes) -> Conversion:
    report = Report()
    root = parse_svg(data)
    sanitize(root, report)
    canvas = get_canvas(root)

    bucket = extract_defs(root)
    ids = _id_map(root, bucket)
    bucket_ids = {el.get("id") for b in bucket for el in b.iter() if el.get("id")}

    chain, elements = discover_layers(root, report)
    if not elements:
        raise SvgError("SVG tidak berisi objek yang bisa dijadikan layer.")
    if len(elements) > MAX_LAYERS:
        raise SvgError(
            f"Terlalu banyak layer ({len(elements)}, maks {MAX_LAYERS}). "
            "Gabungkan objek jadi grup di editor SVG, lalu coba lagi."
        )

    width = max(2, len(str(len(elements))))
    layers, used = [], Counter()
    has_text = False
    raw_total = 0
    for i, el in enumerate(elements, 1):
        tag = local(el.tag)
        has_text = has_text or any(local(e.tag) == "text" for e in el.iter())

        base = _layer_name(el, tag)
        used[base] += 1
        if used[base] > 1:
            base = f"{base}_{used[base]}"

        extra = _use_targets(el, ids, bucket_ids)
        svg = build_layer_svg(root, canvas, bucket, chain, el, extra)

        # Pengaman memori: definisi (gradient dll) disalin ke tiap layer,
        # jadi total bisa membengkak pada SVG dengan banyak layer.
        raw_total += len(svg)
        if raw_total > MAX_RAW_OUTPUT:
            raise SvgError(
                "Hasil konversi terlalu besar (definisi gradient/style disalin "
                "ke tiap layer). Kurangi jumlah layer atau sederhanakan SVG."
            )

        layers.append(Layer(i, base, tag, f"{i:0{width}d}_{base}.svg", svg))

    if has_text:
        report.warnings.append(
            "Ada elemen teks. Font bisa berbeda di Alight Motion; lebih aman ubah "
            "teks jadi path di editor SVG sebelum upload."
        )
    if report.external_refs:
        report.warnings.append(
            f"{report.external_refs} referensi eksternal dibuang demi keamanan."
        )
    return Conversion(canvas, layers, report)


# ---------------------------------------------------------------------------
# Export: ZIP per layer
# ---------------------------------------------------------------------------

README = """SVG per layer untuk Alight Motion
=================================

Isi:
- layers/   satu file SVG per layer. Nomor kecil = layer paling bawah,
            nomor besar = paling atas (urutan gambar SVG asli).
- manifest.json   ukuran kanvas dan daftar layer.

Semua file layer memakai kanvas yang sama dengan SVG asli, jadi posisi dan
ukuran tetap sejajar jika semuanya diimpor ke project dengan ukuran kanvas yang sama.

Cara pakai (garis besar):
1. Buat project di Alight Motion dengan rasio/ukuran sama dengan kanvas di manifest.json.
2. Ekstrak ZIP ini, lalu impor file-file di folder layers/ sebagai media
   (pilih beberapa file sekaligus jika memungkinkan).
3. Atur urutan layer mengikuti nomor file jika perlu.

Catatan:
- Ini BUKAN file project XML Alight Motion. Hasil impor belum diuji di semua versi aplikasi.
- Teks bisa berubah font. Ubah teks jadi path di editor SVG untuk hasil paling konsisten.
"""


def build_zip(conversion: Conversion, source_name: str, dest_path: str):
    c = conversion.canvas
    manifest = {
        "source": source_name,
        "canvas": {
            "width": round(c.width, 4),
            "height": round(c.height, 4),
            "viewBox": list(c.viewbox),
        },
        "layer_count": len(conversion.layers),
        "layers": [
            {
                "order": layer.index,
                "name": layer.name,
                "type": layer.tag,
                "file": f"layers/{layer.filename}",
            }
            for layer in conversion.layers
        ],
    }
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        for layer in conversion.layers:
            z.writestr(f"layers/{layer.filename}", layer.svg)
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("README.txt", README)

    if os.path.getsize(dest_path) > MAX_OUTPUT:
        os.remove(dest_path)
        raise SvgError(
            f"Hasil ZIP melebihi {MAX_OUTPUT // MB} MB. "
            "Kurangi jumlah layer atau sederhanakan SVG, lalu coba lagi."
        )
