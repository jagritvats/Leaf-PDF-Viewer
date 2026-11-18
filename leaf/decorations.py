"""Document-wide page decorations: watermarks, headers & footers (page numbers) and Bates numbers.

A decoration is a frozen `Decoration` spec kept in `PdfDocument.decorations`; nothing is drawn into the
pages until save, where `apply_all` runs once the final page order is known (so page numbers follow the
saved positions). The same `layout()` drives both the PDF output and the live canvas preview.

Each decoration is drawn on a one-page PDF the size of the displayed page, wrapped as a marked-content
artifact (`/Artifact <</Type /Pagination /Subtype /Watermark /LeafDecoration /watermark>> BDC`) with its
own opacity, then placed on the output page as a Form XObject with `show_pdf_page` - behind the page's
content (`overlay=False`) or in front of it - rotated so it reads upright whatever the page's /Rotate.
"""
from __future__ import annotations

import html
import itertools
import math
import os
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from typing import Callable

import pymupdf
from PySide6.QtCore import QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QUndoCommand
from PySide6.QtWidgets import QGraphicsItem

from .pageops import parse_ranges

_uid = itertools.count(1)

KINDS = {"watermark": "Watermark", "header_footer": "Header & footer", "bates": "Bates numbering"}
SLOTS = ("tl", "tc", "tr", "bl", "bc", "br")
SLOT_NAMES = {"tl": "Top left", "tc": "Top center", "tr": "Top right",
              "bl": "Bottom left", "bc": "Bottom center", "br": "Bottom right"}
PAGE_MODES = {"all": "All pages", "odd": "Odd pages", "even": "Even pages", "skip_first": "All but the first page",
              "range": "Page range"}
FAMILIES = {"sans": "Helvetica", "serif": "Times", "mono": "Courier"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %B %Y", "%B %d, %Y")
TOKENS = ("{page}", "{pages}", "{label}", "{date}", "{filename}", "{title}")
PRESETS = ("Page {page} of {pages}", "Page {page}", "{page}", "{page} / {pages}", "- {page} -")

_BASE14 = {("sans", False): "helv", ("sans", True): "hebo", ("serif", False): "tiro", ("serif", True): "tibo",
           ("mono", False): "cour", ("mono", True): "cobo"}
_CSS_FAMILY = {"sans": "sans-serif", "serif": "serif", "mono": "monospace"}
_ALIGN = {"l": 0.0, "c": 0.5, "r": 1.0}


@dataclass(frozen=True)
class Decoration:
    kind: str = "header_footer"  # watermark | header_footer | bates
    pages: str = "all"  # all | odd | even | skip_first | range
    page_range: str = ""
    family: str = "sans"
    bold: bool = False
    size: float = 10.0
    color: str = "#000000"
    opacity: float = 1.0
    behind: bool = False
    # header & footer: texts for the six SLOTS
    slots: tuple = ("", "", "", "", "", "")
    margin_x: float = 36.0
    margin_y: float = 24.0
    start: int = 1  # page number of the first page / first Bates number
    date_format: str = "%Y-%m-%d"
    # Bates
    prefix: str = ""
    suffix: str = ""
    digits: int = 6
    slot: str = "br"
    # watermark: text, or an image when `image` is set
    text: str = "CONFIDENTIAL"
    image: bytes = b""
    image_name: str = ""
    fit: bool = True  # text size fills the page along the angle
    diagonal: bool = True  # angle follows the page diagonal
    angle: float = 45.0  # degrees, counter-clockwise
    tiled: bool = False
    scale: float = 0.5  # image width as a fraction of the page width
    uid: int = field(default_factory=lambda: next(_uid), compare=False)


def watermark(**kw) -> Decoration:
    kw = {"size": 60.0, "color": "#808080", "opacity": 0.3, "bold": True, **kw}
    return Decoration(kind="watermark", **kw)


def header_footer(**kw) -> Decoration:
    return Decoration(kind="header_footer", **kw)


def bates(**kw) -> Decoration:
    kw = {"margin_y": 18.0, **kw}
    return Decoration(kind="bates", **kw)


def decorations_of(doc) -> list[Decoration]:
    return list(getattr(doc, "decorations", None) or [])


# ------------------------------------------------------------------ pages
@lru_cache(maxsize=256)
def selection(spec: Decoration, n: int) -> dict[int, int]:
    """Rows (0-based final positions) the decoration applies to -> ordinal among them (for Bates)."""
    mode = spec.pages
    if mode == "odd":
        rows = range(0, n, 2)
    elif mode == "even":
        rows = range(1, n, 2)
    elif mode == "skip_first":
        rows = range(1, n)
    elif mode == "range":
        try:
            rows = sorted(parse_ranges(spec.page_range, n))
        except ValueError:
            rows = []
    else:
        rows = range(n)
    return {r: k for k, r in enumerate(rows)}


def describe_pages(spec: Decoration) -> str:
    if spec.pages == "range":
        return f"pages {spec.page_range or 'all'}"
    return PAGE_MODES.get(spec.pages, "All pages").lower()


def describe(spec: Decoration) -> str:
    """One line for lists and undo texts."""
    if spec.kind == "watermark":
        what = f"image {spec.image_name}" if spec.image else f"“{spec.text}”"
        return f"Watermark {what} · {describe_pages(spec)}"
    if spec.kind == "bates":
        return f"Bates {bates_text(spec, 0)} · {describe_pages(spec)}"
    texts = [t for t in spec.slots if t]
    return f"Header & footer {' | '.join(texts) or '(empty)'} · {describe_pages(spec)}"


# ----------------------------------------------------------------- tokens
@dataclass
class Context:
    n: int
    filename: str = ""
    title: str = ""
    now: datetime = field(default_factory=datetime.now)
    label: Callable[[int], str] | None = None


def expand(template: str, spec: Decoration, row: int, ctx: Context) -> str:
    if "{" not in template:
        return template
    label = (ctx.label(row) if ctx.label else "") or str(row + 1)
    try:
        date = ctx.now.strftime(spec.date_format or "%Y-%m-%d")
    except ValueError:
        date = ctx.now.strftime("%Y-%m-%d")
    stem = os.path.splitext(ctx.filename)[0]
    values = {"{page}": str(spec.start + row), "{pages}": str(spec.start + ctx.n - 1), "{label}": label,
              "{date}": date, "{filename}": ctx.filename, "{title}": ctx.title or stem}
    for k, v in values.items():
        template = template.replace(k, v)
    return template


def bates_text(spec: Decoration, ordinal: int) -> str:
    return f"{spec.prefix}{str(spec.start + ordinal).zfill(max(1, int(spec.digits)))}{spec.suffix}"


# ----------------------------------------------------------------- layout
@dataclass(frozen=True)
class Run:
    """One line of text. The pivot (x, y) is in display space; the text is drawn at (-width * align, dy)
    in a frame rotated counter-clockwise by `angle` around the pivot (dy is the baseline offset)."""
    text: str
    x: float
    y: float
    align: str
    size: float
    family: str
    bold: bool
    color: str
    dy: float = 0.0
    angle: float = 0.0


@dataclass(frozen=True)
class ImageRun:
    x: float
    y: float
    w: float
    h: float
    data: bytes


def base14(family: str, bold: bool) -> str:
    return _BASE14.get((family, bool(bold)), "helv")


def is_latin(text: str) -> bool:
    return all(ord(c) < 256 for c in text)


def text_width(text: str, family: str, bold: bool, size: float) -> float:
    """Width with the base-14 metrics; other scripts are estimated."""
    name = base14(family, bold)
    if is_latin(text):
        return pymupdf.get_text_length(text, fontname=name, fontsize=size)
    w = 0.0
    for c in text:
        if ord(c) < 256:
            w += pymupdf.get_text_length(c, fontname=name, fontsize=size)
        else:
            w += size * (1.0 if unicodedata.east_asian_width(c) in "WF" else 0.62)
    return w


@lru_cache(maxsize=16)
def image_size(data: bytes) -> tuple[int, int]:
    try:
        pix = pymupdf.Pixmap(data)
        return pix.width, pix.height
    except Exception:  # noqa: BLE001 - unreadable image: draw nothing
        return 0, 0


def effective_angle(spec: Decoration, w: float, h: float) -> float:
    return math.degrees(math.atan2(h, w)) if spec.diagonal else float(spec.angle)


def _grid(w: float, h: float, sx: float, sy: float, limit: int = 400):
    sx, sy = max(sx, 8.0), max(sy, 8.0)
    pts = []
    row = 0
    y = -sy / 2
    while y < h + sy and len(pts) < limit:
        x = -sx / 2 + (sx / 2 if row % 2 else 0.0)
        while x < w + sx and len(pts) < limit:
            pts.append((x, y))
            x += sx
        y += sy
        row += 1
    return pts


def layout(spec: Decoration, row: int, ctx: Context, w: float, h: float) -> tuple:
    """What `spec` draws on the page at final position `row` of a `w` x `h` display-space page (empty if
    the page isn't selected)."""
    sel = selection(spec, ctx.n)
    if row not in sel:
        return ()
    style = dict(family=spec.family, bold=spec.bold, color=spec.color)
    if spec.kind == "watermark":
        if spec.image:
            iw, ih = image_size(spec.image)
            if iw <= 0 or ih <= 0:
                return ()
            rw = max(4.0, w * spec.scale)
            rh = rw * ih / iw
            if spec.tiled:
                return tuple(ImageRun(x - rw / 2, y - rh / 2, rw, rh, spec.image)
                             for x, y in _grid(w, h, rw * 1.6, rh * 1.8, 120))
            return (ImageRun((w - rw) / 2, (h - rh) / 2, rw, rh, spec.image),)
        text = expand(spec.text.strip(), spec, row, ctx)
        if not text:
            return ()
        a = effective_angle(spec, w, h)
        w1 = max(text_width(text, spec.family, spec.bold, 1.0), 0.1)
        size = spec.size
        if spec.fit and not spec.tiled:
            # the rotated text box (width w1*size, cap height ~0.75*size) fits inside 90% of the page
            c, s = abs(math.cos(math.radians(a))), abs(math.sin(math.radians(a)))
            size = min(0.9 * w / (w1 * c + 0.75 * s), 0.9 * h / (w1 * s + 0.75 * c), 0.5 * min(w, h))
        size = max(1.0, size)
        if spec.tiled:
            tw = w1 * size
            c, s = abs(math.cos(math.radians(a))), abs(math.sin(math.radians(a)))
            pts = _grid(w, h, tw * c + size * 3, tw * s + size * 3.5)
            return tuple(Run(text, x, y, "c", size, dy=0.35 * size, angle=a, **style) for x, y in pts)
        return (Run(text, w / 2, h / 2, "c", size, dy=0.35 * size, angle=a, **style),)
    size = max(1.0, spec.size)
    if spec.kind == "bates":
        texts = {spec.slot: bates_text(spec, sel[row])}
    else:
        texts = {slot: expand(t, spec, row, ctx) for slot, t in zip(SLOTS, spec.slots) if t}
    out = []
    for slot, text in texts.items():
        if not text:
            continue
        top = slot[0] == "t"
        y = spec.margin_y + 0.8 * size if top else h - spec.margin_y - 0.2 * size
        align = {"l": "l", "c": "c", "r": "r"}[slot[1]]
        x = {"l": spec.margin_x, "c": w / 2, "r": w - spec.margin_x}[align]
        out.append(Run(text, x, y, align, size, **style))
    return tuple(out)


# ------------------------------------------------------------------- PDF
def _rgb(c: str) -> tuple[float, float, float]:
    c = (c or "#000000").lstrip("#")
    if len(c) != 6:
        return (0.0, 0.0, 0.0)
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rotate(vx: float, vy: float, a: float) -> tuple[float, float]:
    """Rotate a display-space vector counter-clockwise (as seen; y points down) by `a` degrees."""
    c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
    return vx * c + vy * s, -vx * s + vy * c


def _draw_run(page: pymupdf.Page, r: Run) -> None:
    if is_latin(r.text):
        name = base14(r.family, r.bold)
        tw = pymupdf.get_text_length(r.text, fontname=name, fontsize=r.size)
        piv = pymupdf.Point(r.x, r.y)
        morph = (piv, pymupdf.Matrix(r.angle)) if r.angle else None
        page.insert_text(piv + (-tw * _ALIGN[r.align], r.dy), r.text, fontsize=r.size, fontname=name,
                         color=_rgb(r.color), morph=morph)
        return
    # Any other script: let MuPDF's HTML layout pick fallback fonts and shape it, on a tile page that is
    # then placed (rotated) around the pivot.
    est = text_width(r.text, r.family, r.bold, r.size)
    tile = pymupdf.open()
    try:
        tp = tile.new_page(width=est * 1.5 + r.size * 4, height=r.size * 3)
        css = (f"* {{font-family: {_CSS_FAMILY.get(r.family, 'sans-serif')}; font-size: {r.size:.2f}px; "
               f"color: {r.color}; font-weight: {'bold' if r.bold else 'normal'}; margin: 0; white-space: nowrap}}")
        tp.insert_htmlbox(tp.rect, html.escape(r.text), css=css)
        bb = pymupdf.Rect()
        for b in tp.get_text("blocks"):
            bb |= pymupdf.Rect(b[:4])
        if bb.is_empty:
            return
        bw, bh = bb.width, bb.height
        x0 = -bw * _ALIGN[r.align]
        y1 = r.dy + 0.22 * r.size
        cx, cy = _rotate(x0 + bw / 2, y1 - bh / 2, r.angle)
        c, s = abs(math.cos(math.radians(r.angle))), abs(math.sin(math.radians(r.angle)))
        W, H = bw * c + bh * s, bw * s + bh * c
        x, y = r.x + cx, r.y + cy
        page.show_pdf_page(pymupdf.Rect(x - W / 2, y - H / 2, x + W / 2, y + H / 2), tile, 0, clip=bb,
                           rotate=r.angle)
    finally:
        tile.close()


def _layer(gdoc: pymupdf.Document, w: float, h: float, content, spec: Decoration) -> int:
    """A page of `gdoc` holding `content`, wrapped as a marked artifact with the spec's opacity; -1 if empty."""
    gp = gdoc.new_page(width=w, height=h)
    img_xref = 0
    for it in content:
        if isinstance(it, ImageRun):
            img_xref = gp.insert_image(pymupdf.Rect(it.x, it.y, it.x + it.w, it.y + it.h),
                                       stream=None if img_xref else it.data, xref=img_xref)
        else:
            _draw_run(gp, it)
    raw = gp.read_contents()
    xrefs = gp.get_contents()
    if not raw.strip() or not xrefs:
        return -1
    gs = ""
    if spec.opacity < 0.999:
        a = max(0.0, min(1.0, spec.opacity))
        name = f"LeafA{round(a * 1000)}"
        t, v = gdoc.xref_get_key(gp.xref, "Resources")
        if t == "xref":
            gdoc.xref_set_key(int(v.split()[0]), f"ExtGState/{name}", f"<</ca {a:.3f}/CA {a:.3f}>>")
        else:
            gdoc.xref_set_key(gp.xref, f"Resources/ExtGState/{name}", f"<</ca {a:.3f}/CA {a:.3f}>>")
        gs = f"/{name} gs "
    sub = "/Subtype /Watermark " if spec.kind == "watermark" else ""
    head = f"/Artifact <</Type /Pagination {sub}/LeafDecoration /{spec.kind}>> BDC q {gs}\n".encode()
    gdoc.update_stream(xrefs[0], head + raw + b"\nQ EMC\n")
    gp.set_contents(xrefs[0])
    return gp.number


def apply_all(out: pymupdf.Document, specs, filename: str = "", now: datetime | None = None) -> int:
    """Draw `specs` onto every page of `out` (already in its final order and rotation). Returns the number
    of decorations placed."""
    specs = list(specs or [])
    if not specs:
        return 0
    n = out.page_count
    try:
        title = (out.metadata or {}).get("title") or ""
        has_labels = bool(out.get_page_labels())
    except Exception:  # noqa: BLE001
        title, has_labels = "", False
    ctx = Context(n, filename, title, now or datetime.now(), (lambda i: out[i].get_label()) if has_labels else None)
    gdoc = pymupdf.open()
    cache: dict = {}
    todo: list[tuple[int, int, bool]] = []  # (page number, layer page, behind)
    try:
        # all layer pages first: MuPDF's graft map can't take objects added to gdoc after the first show
        for i in range(n):
            r = out[i].rect
            w, h = r.width, r.height
            for spec in specs:
                content = layout(spec, i, ctx, w, h)
                if not content:
                    continue
                key = (spec, round(w, 2), round(h, 2), content)
                pno = cache.get(key)
                if pno is None:
                    pno = cache[key] = _layer(gdoc, w, h, content, spec)
                if pno >= 0:
                    todo.append((i, pno, spec.behind))
        page, rot = None, 0
        for i, pno, behind in todo:
            if page is None or page.number != i:
                if rot:
                    page.set_rotation(rot)
                page = out[i]
                rot = page.rotation
                target = page.rect * page.derotation_matrix
                # show_pdf_page misplaces content on rotated pages whose CropBox/MediaBox doesn't start at 0,0:
                # place it on the unrotated page (rotating the layer instead), then restore /Rotate
                if rot:
                    page.set_rotation(0)
            page.show_pdf_page(target, gdoc, pno, rotate=rot, overlay=not behind)
        if page is not None and rot:
            page.set_rotation(rot)
    finally:
        gdoc.close()
    return len(todo)


# ------------------------------------------------------------ undo/model
class SetDecorations(QUndoCommand):
    def __init__(self, canvas, new: list[Decoration], text: str):
        super().__init__(text)
        self.canvas = canvas
        self.old = decorations_of(canvas.doc)
        self.new = list(new)

    def _set(self, specs) -> None:
        self.canvas.doc.decorations = list(specs)
        preview_for(self.canvas).refresh()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def add(canvas, spec: Decoration) -> None:
    canvas.undo.push(SetDecorations(canvas, decorations_of(canvas.doc) + [spec], f"Add {KINDS[spec.kind].lower()}"))


def update(canvas, spec: Decoration) -> None:
    """Replace the decoration with the same uid."""
    cur = decorations_of(canvas.doc)
    new = [spec if s.uid == spec.uid else s for s in cur]
    if new != cur:
        canvas.undo.push(SetDecorations(canvas, new, f"Edit {KINDS[spec.kind].lower()}"))


def remove(canvas, uid: int) -> None:
    cur = decorations_of(canvas.doc)
    gone = [s for s in cur if s.uid == uid]
    if gone:
        canvas.undo.push(SetDecorations(canvas, [s for s in cur if s.uid != uid],
                                        f"Remove {KINDS[gone[0].kind].lower()}"))


def remove_all(canvas, kind: str | None = None) -> None:
    cur = decorations_of(canvas.doc)
    keep = [s for s in cur if kind is not None and s.kind != kind]
    if len(keep) != len(cur):
        canvas.undo.push(SetDecorations(canvas, keep, "Remove decorations"))


# ---------------------------------------------------------------- preview
_QFONTS = {"sans": ("Arial", QFont.StyleHint.SansSerif), "serif": ("Times New Roman", QFont.StyleHint.Serif),
           "mono": ("Courier New", QFont.StyleHint.TypeWriter)}
_REF_PX = 100  # fonts are laid out at 100 px and scaled, so small sizes keep exact proportions


@lru_cache(maxsize=12)
def _qfont(family: str, bold: bool):
    name, hint = _QFONTS.get(family, _QFONTS["sans"])
    f = QFont(name)
    f.setStyleHint(hint)
    f.setPixelSize(_REF_PX)
    f.setBold(bool(bold))
    return f, QFontMetricsF(f)


@lru_cache(maxsize=8)
def _qimage(data: bytes):
    img = QImage()
    img.loadFromData(data)
    return img


def paint_content(p, content) -> None:
    """Draw a `layout()` result with QPainter in display-space coordinates."""
    for it in content:
        if isinstance(it, ImageRun):
            img = _qimage(it.data)
            if not img.isNull():
                p.drawImage(QRectF(it.x, it.y, it.w, it.h), img)
            continue
        font, fm = _qfont(it.family, it.bold)
        k = it.size / _REF_PX
        p.save()
        p.translate(it.x, it.y)
        if it.angle:
            p.rotate(-it.angle)
        p.scale(k, k)
        p.setFont(font)
        p.setPen(QColor(it.color))
        p.drawText(QPointF(-fm.horizontalAdvance(it.text) * _ALIGN[it.align], it.dy / k), it.text)
        p.restore()


def preview_context(canvas) -> Context:
    doc = canvas.doc
    prim = doc.primary.doc
    try:
        title = (prim.metadata or {}).get("title") or ""
        has_labels = bool(prim.get_page_labels())
    except Exception:  # noqa: BLE001
        title, has_labels = "", False
    count = prim.page_count

    def label(i: int) -> str:
        return prim[i].get_label() if i < count else ""

    name = os.path.basename(doc.path) if doc.path else "Untitled.pdf"
    return Context(len(canvas.order), name, title, datetime.now(), label if has_labels else None)


class DecorationLayer(QGraphicsItem):
    """Non-interactive preview of the decorations on one page (child of the PageItem, display space).

    Not an overlay: `is_overlay` is False, so it is never saved, selected or hit (empty shape)."""

    def __init__(self, page_item, behind: bool):
        super().__init__(page_item)
        self.behind = behind
        self.setZValue(-0.5 if behind else 50)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)

    def boundingRect(self) -> QRectF:
        return self.parentItem().page_rect()

    def shape(self) -> QPainterPath:
        return QPainterPath()

    def paint(self, p, opt, widget=None) -> None:
        pg = self.parentItem()
        cv = pg.canvas
        specs = [s for s in decorations_of(cv.doc) if s.behind == self.behind]
        row = cv.index_of.get(pg)
        if not specs or row is None:
            return
        pv = preview_for(cv)
        ctx = pv.context()
        p.save()
        p.setClipRect(pg.page_rect())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.behind and not cv.night:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        for spec in specs:
            content = layout(spec, row, ctx, pg.w, pg.h)
            if content:
                p.setOpacity(max(0.0, min(1.0, spec.opacity)))
                paint_content(p, content)
        p.restore()


class Preview(QObject):
    """Keeps a pair of DecorationLayers on every page item that has been visible, refreshed on changes."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self._ctx: Context | None = None
        canvas.verticalScrollBar().valueChanged.connect(self.ensure)
        canvas.horizontalScrollBar().valueChanged.connect(self.ensure)
        canvas.layoutChanged.connect(self.ensure)
        canvas.pagesChanged.connect(self.refresh)
        canvas.documentChanged.connect(self.refresh)

    def context(self) -> Context:
        if self._ctx is None or self._ctx.n != len(self.canvas.order):
            self._ctx = preview_context(self.canvas)
        return self._ctx

    def ensure(self, *_) -> None:
        cv = self.canvas
        if cv.doc is None or not decorations_of(cv.doc):
            return
        for pg in cv.visible_pages():
            if pg.__dict__.get("_decor") is None:
                pg._decor = (DecorationLayer(pg, True), DecorationLayer(pg, False))

    def layers(self):
        for pg in self.canvas.order:
            for layer in pg.__dict__.get("_decor") or ():
                yield layer

    def refresh(self, *_) -> None:
        self._ctx = None
        if self.canvas.doc is None:
            return
        self.ensure()
        for layer in self.layers():
            layer.prepareGeometryChange()
            layer.update()


def preview_for(canvas) -> Preview:
    pv = canvas.__dict__.get("_decor_preview")
    if pv is None:
        pv = canvas._decor_preview = Preview(canvas)
    return pv


__all__ = ["Decoration", "watermark", "header_footer", "bates", "apply_all", "layout", "selection", "expand",
           "bates_text", "describe", "add", "update", "remove", "remove_all", "preview_for", "replace"]
