"""Page operations as pure helpers over the page list, plus the cross-window page clipboard.

Every edit to the page list goes through `commands.SetPages`, so each operation is one undo step."""
from __future__ import annotations

import atexit
import os
import re
import uuid
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QPointF, QRectF
from PySide6.QtGui import QGuiApplication, QImage

from . import commands as C
from .document import PageRef, Source

CLIP_MIME = "application/x-leaf-pages"
MM = 25.4 / 72
PAPERS = [  # name, width mm, height mm (portrait)
    ("A0", 841, 1189), ("A1", 594, 841), ("A2", 420, 594), ("A3", 297, 420), ("A4", 210, 297), ("A5", 148, 210),
    ("A6", 105, 148), ("B4", 250, 353), ("B5", 176, 250), ("Letter", 215.9, 279.4), ("Legal", 215.9, 355.6),
    ("Tabloid", 279.4, 431.8), ("Executive", 184.2, 266.7),
]


# ------------------------------------------------------------------ ranges
def parse_ranges(text: str, n: int) -> list[int]:
    """'1-3, 7, 10-' -> 0-based rows (in the given order, duplicates dropped).

    Accepts open ends ('-3', '10-'), reversed spans ('5-3'), 'all', 'odd', 'even', 'last' and
    commas, semicolons or spaces as separators. Raises ValueError with a readable message."""
    text = (text or "").strip().lower()
    if n <= 0:
        return []
    if text in ("", "all", "*"):
        return list(range(n))
    out: list[int] = []
    seen: set[int] = set()

    def num(s: str) -> int:
        if s in ("last", "end", "$"):
            return n
        if not s.isdigit():
            raise ValueError(f"'{s}' isn't a page number")
        v = int(s)
        if not 1 <= v <= n:
            raise ValueError(f"Page {v} doesn't exist (1–{n})")
        return v

    tokens = [t for t in re.split(r"[,;\s]+", re.sub(r"\s*[-–]\s*", "-", text)) if t]
    for tok in tokens:
        if tok in ("odd", "even"):
            rows = list(range(0 if tok == "odd" else 1, n, 2))
        elif "-" in tok:
            a, _, b = tok.partition("-")
            if "-" in b:
                raise ValueError(f"'{tok}' isn't a range")
            lo = num(a) if a else 1
            hi = num(b) if b else n
            step = 1 if hi >= lo else -1
            rows = [v - 1 for v in range(lo, hi + step, step)]
        else:
            rows = [num(tok) - 1]
        for r in rows:
            if r not in seen:
                seen.add(r)
                out.append(r)
    if not out:
        raise ValueError("No pages selected")
    return out


def format_ranges(rows) -> str:
    """0-based rows -> '1-3, 7' (sorted)."""
    rows = sorted(set(rows))
    parts = []
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1] == rows[j] + 1:
            j += 1
        a, b = rows[i] + 1, rows[j] + 1
        parts.append(str(a) if a == b else f"{a}-{b}")
        i = j + 1
    return ", ".join(parts)


def split_every(n: int, k: int) -> list[list[int]]:
    k = max(1, int(k))
    return [list(range(i, min(n, i + k))) for i in range(0, n, k)]


def split_at(n: int, starts) -> list[list[int]]:
    """Split before each row in `starts` (row 0 is always a start)."""
    cuts = sorted({s for s in starts if 0 < s < n} | {0})
    cuts.append(n)
    return [list(range(cuts[i], cuts[i + 1])) for i in range(len(cuts) - 1) if cuts[i] < cuts[i + 1]]


def numbered_paths(path: str, count: int) -> list[str]:
    base, ext = os.path.splitext(path)
    ext = ext or ".pdf"
    width = max(1, len(str(count)))
    return [f"{base}-{i + 1:0{width}d}{ext}" for i in range(count)]


# --------------------------------------------------------------- page info
def paper_name(w_pt: float, h_pt: float) -> str:
    w, h = sorted((w_pt * MM, h_pt * MM))
    for name, pw, ph in PAPERS:
        if abs(w - pw) <= 2 and abs(h - ph) <= 2:
            return name
    return ""


def size_text(w_pt: float, h_pt: float) -> str:
    """'A4 · 210 × 297 mm' (portrait/landscape kept as displayed)."""
    name = paper_name(w_pt, h_pt)
    mm = f"{w_pt * MM:.0f} × {h_pt * MM:.0f} mm"
    if name:
        return f"{name}{' landscape' if w_pt > h_pt * 1.01 else ''} · {mm}"
    return mm


def page_tooltip(doc, ref: PageRef, row: int) -> str:
    w, h = doc.display_size(ref)
    lines = [f"Page {row + 1}", size_text(w, h), f"{w / 72:.2f} × {h / 72:.2f} in · {w:.0f} × {h:.0f} pt"]
    own = doc.page(ref).rotation
    if ref.rotation or own:
        lines.append(f"Rotated {(own + ref.rotation) % 360}°" + (f" ({ref.rotation:+d}° in Leaf)" if ref.rotation else ""))
    if ref.src is not doc.primary:
        lines.append(f"From {ref.src.name}, page {ref.index + 1}")
    return "\n".join(lines)


def page_label(doc, ref: PageRef, row: int) -> str:
    """The PDF's own page label when it differs from the position (e.g. 'iv'), else ''."""
    try:
        has = ref.src.__dict__.get("_has_labels")
        if has is None:
            has = ref.src.__dict__["_has_labels"] = bool(ref.src.doc.get_page_labels())
        label = doc.page(ref).get_label() if has else ""
    except Exception:  # noqa: BLE001 - labels are cosmetic
        label = ""
    return label if label and label != str(row + 1) else ""


# --------------------------------------------------------------- ordering
def move_block(uids: list, moving: set, row: int) -> list:
    """Move the `moving` items, in their current order, to insertion index `row` (index into `uids`)."""
    block = [u for u in uids if u in moving]
    if not block:
        return list(uids)
    before = sum(1 for u in uids[:row] if u not in moving)
    rest = [u for u in uids if u not in moving]
    return rest[:before] + block + rest[before:]


def move_by(uids: list, moving: set, delta: int) -> list:
    """Move each selected item one step (items blocked at an edge stay put, like a list box)."""
    out = list(uids)
    idx = range(1, len(out)) if delta < 0 else range(len(out) - 2, -1, -1)
    for i in idx:
        j = i - 1 if delta < 0 else i + 1
        if out[i] in moving and out[j] not in moving:
            out[i], out[j] = out[j], out[i]
    return out


# ------------------------------------------------------------ operations
def state(doc) -> list[tuple]:
    return [(r, r.rotation) for r in doc.pages]


def push(canvas, new: list[tuple], text: str) -> None:
    canvas.undo.push(C.SetPages(canvas, state(canvas.doc), new, text))


def reorder(canvas, uids: list, text: str = "Reorder pages") -> None:
    by_uid = {r.uid: r for r in canvas.doc.pages}
    if [r.uid for r in canvas.doc.pages] != list(uids):
        push(canvas, [(by_uid[u], by_uid[u].rotation) for u in uids], text)


def move_rows(canvas, rows, row: int, text: str = "Move pages") -> None:
    uids = [r.uid for r in canvas.doc.pages]
    reorder(canvas, move_block(uids, {uids[i] for i in rows}, row), text)


def reverse(canvas, rows) -> None:
    """Reverse the selected pages among their own positions (all pages when fewer than two are given)."""
    old = state(canvas.doc)
    rows = sorted(set(rows)) if len(set(rows)) > 1 else list(range(len(old)))
    new = list(old)
    for pos, src in zip(rows, reversed(rows)):
        new[pos] = old[src]
    if new != old:
        push(canvas, new, "Reverse page order")


def rotate(canvas, rows, delta: int) -> None:
    rows = set(rows)
    old = state(canvas.doc)
    push(canvas, [(r, (rot + delta) % 360 if i in rows else rot) for i, (r, rot) in enumerate(old)], "Rotate pages")


def delete(canvas, rows) -> bool:
    rows = set(rows)
    old = state(canvas.doc)
    if not rows or len(rows) >= len(old):
        return False
    push(canvas, [s for i, s in enumerate(old) if i not in rows], f"Delete {len(rows)} page(s)")
    return True


def insert(canvas, entries: list[tuple], row: int, text: str) -> list[int]:
    old = state(canvas.doc)
    row = max(0, min(row, len(old)))
    push(canvas, old[:row] + entries + old[row:], text)
    return [r.uid for r, _ in entries]


def insert_blank(canvas, row: int, like: int | None = None) -> list[int]:
    """Insert a blank page at `row`, sized like the page at `like` as it's displayed (default: a neighbour)."""
    doc = canvas.doc
    if like is None:
        like = min(max(row - 1, 0), len(doc.pages) - 1)
    w, h = doc.display_size(doc.pages[like]) if doc.pages else (595.0, 842.0)
    src = doc.add_blank_source(w, h)
    return insert(canvas, [(PageRef(src, 0), 0)], row, "Insert blank page")


def insert_source_pages(canvas, src: Source, indices, row: int) -> list[int]:
    entries = [(PageRef(src, i), 0) for i in indices]
    return insert(canvas, entries, row, f"Insert {len(entries)} page(s)")


def image_source(doc, img: QImage, like: tuple[float, float] | None = None) -> Source:
    """A one-page Source holding `img`, fitted on a page the size of `like` (turned to match the image)."""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    w, h = like or (img.width() * 0.75, img.height() * 0.75)
    if (img.width() > img.height()) != (w > h):
        w, h = h, w
    d = pymupdf.open()
    page = d.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=bytes(ba), keep_proportion=True)
    data = d.tobytes(deflate=True)
    d.close()
    return doc.add_pdf_bytes("Pasted image", data)


# ----------------------------------------------------------- clipboard
@dataclass(eq=False)
class ClipPage:
    src: Source
    index: int
    rotation: int
    values: dict = field(default_factory=dict)  # (field name, rect key) -> value
    overlays: list = field(default_factory=list)  # detached clones
    crop: tuple | None = None  # optimize: PageRef.crop


_clip: list[ClipPage] = []
_clip_token = ""


@atexit.register
def _release_clipboard() -> None:
    """Drop clipped pages (and their MuPDF documents) before interpreter teardown; freeing MuPDF objects during
    module cleanup can crash the process on exit."""
    _clip.clear()


def clone_overlay(it):
    """A detached copy of an overlay item (same class, props and geometry), or None for unknown types."""
    from .items import BoxItem, HighlightItem, ImageItem, InkItem, LineItem, TextItem

    if hasattr(it, "clone_overlay"):  # notes, replies, adopted annotations (leaf/annots.py) know their own copies
        return it.clone_overlay()
    props = dict(it.props)
    if isinstance(it, ImageItem):
        c = type(it)(it.kind, QRectF(), it.image, props)
        c.set_geometry(it.geometry())
    elif isinstance(it, BoxItem):
        c = type(it)(it.kind, QRectF(), props)
        c.set_geometry(it.geometry())
    elif isinstance(it, LineItem):
        c = type(it)(it.kind, QPointF(), QPointF(), props)
        c.set_geometry(it.geometry())
    elif isinstance(it, InkItem):
        c = type(it)(QPointF(), props)
        c.pts = [QPointF(p) for p in it.pts]
        c.widths = list(getattr(it, "widths", []))  # pen pressure
        c.setPos(it.pos())
    elif isinstance(it, HighlightItem):
        c = type(it)(it.rects, props)
        c.setPos(it.pos())
    elif isinstance(it, TextItem):
        c = it.clone()  # keeps run formatting
    else:
        return None
    c.setZValue(it.zValue())
    c.setTransform(it.transform())
    return c


def snapshot(canvas, rows) -> list[ClipPage]:
    """Detached copies of the pages at `rows`: source page, rotation, form values and overlays."""
    doc = canvas.doc
    out = []
    for i in sorted(set(rows)):
        ref = doc.pages[i]
        vals = {(name, rk): v for (uid, name, rk), v in doc.form_values.items() if uid == ref.uid}
        ovs = [c for c in (clone_overlay(o) for o in canvas.overlays_for(ref)) if c is not None]
        out.append(ClipPage(ref.src, ref.index, ref.rotation, vals, ovs, ref.crop))
    return out


def materialize(canvas, pages: list[ClipPage]) -> list[tuple]:
    """New page-list entries for `pages` in this canvas's document (sources imported from bytes if needed)."""
    doc = canvas.doc
    entries = []
    for cp in pages:
        ref = PageRef(doc.import_source(cp.src), cp.index, cp.rotation, crop=cp.crop)
        for (name, rk), v in cp.values.items():
            doc.form_values[(ref.uid, name, rk)] = v
        if cp.overlays:
            content = canvas.page_item(ref).content
            for o in cp.overlays:
                c = clone_overlay(o)
                if c is not None:
                    c.setParentItem(content)
        entries.append((ref, cp.rotation))
    return entries


def duplicate(canvas, rows) -> list[int]:
    rows = sorted(set(rows))
    if not rows:
        return []
    entries = materialize(canvas, snapshot(canvas, rows))
    return insert(canvas, entries, rows[-1] + 1, f"Duplicate {len(entries)} page(s)")


def copy_pages(canvas, rows) -> int:
    global _clip, _clip_token
    _clip = snapshot(canvas, rows)
    _clip_token = uuid.uuid4().hex
    try:
        md = QMimeData()
        md.setData(CLIP_MIME, _clip_token.encode())
        md.setText(f"{len(_clip)} page(s) from Leaf")
        QGuiApplication.clipboard().setMimeData(md)
    except Exception:  # noqa: BLE001 - the in-process clipboard still works
        pass
    return len(_clip)


def clipboard_kind() -> str:
    """'pages', 'image' or '' — what a paste into the page list would insert."""
    try:
        md = QGuiApplication.clipboard().mimeData()
    except Exception:  # noqa: BLE001
        md = None
    if md is not None and md.hasFormat(CLIP_MIME) and _clip:
        return "pages"
    if md is not None and md.hasImage():
        return "image"
    return "pages" if _clip else ""


def has_clip_pages() -> bool:
    return bool(_clip)


def paste_pages(canvas, row: int) -> list[int]:
    if not _clip:
        return []
    entries = materialize(canvas, _clip)
    return insert(canvas, entries, row, f"Paste {len(entries)} page(s)")


def paste_image(canvas, row: int, img: QImage | None = None) -> list[int]:
    if img is None:
        img = QGuiApplication.clipboard().image()
    if img is None or img.isNull():
        return []
    doc = canvas.doc
    like = doc.display_size(doc.pages[min(max(row - 1, 0), len(doc.pages) - 1)]) if doc.pages else (595.0, 842.0)
    src = image_source(doc, img, like)
    return insert(canvas, [(PageRef(src, 0), 0)], row, "Paste image as page")
