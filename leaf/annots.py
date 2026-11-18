"""Annotations already in the PDF as editable overlays, sticky notes, comments and replies.

Adoption
    When a page item is first painted (or when the Comments panel needs the whole document), each supported
    annotation of its source page becomes an overlay item that remembers its xref, its /NM and its original
    state (geometry, style, comment fields). Square, Circle, Line, Ink, Highlight/Underline/StrikeOut/Squiggly,
    FreeText, Text (sticky note) and Stamp are fully supported; Polygon, PolyLine, Caret and FileAttachment
    can be moved and deleted; annotations answering another one (/IRT) become invisible reply items shown in
    the Comments panel. Widgets, links, popups, hidden ones and media annotations are left alone.

Saving (fidelity)
    `Canvas.overlays_for()` only hands *changed* adopted items, plus a tombstone per deleted one, to the save
    pipeline. A page whose annotations were never touched is not even loaded at save time, so those objects
    are kept byte for byte (incremental save) or copied unchanged (full rewrite). A changed item finds its
    annotation by xref in the page it is given: `PdfDocument._apply` runs on each part opened from that
    source's own bytes, before pages are merged or moved, so the xref is valid there. Then
      - a pure move (or a Stamp resize) rewrites /Rect and the geometry keys (/L, /Vertices, /InkList,
        /QuadPoints, /CL) in place and keeps the appearance stream, so custom appearances survive;
      - a restyle, resize or endpoint edit updates the annotation and lets MuPDF regenerate its appearance;
      - a comment edit only rewrites /Contents, /T and /M;
      - a tombstone deletes the annotation (MuPDF also drops its popup and replies).

Display
    The page image is rendered with annotations. While an adopted item is untouched it paints only its
    selection outline: an invisible hit target over the rendered annotation. Once changed or deleted, its xref
    is hidden in its Source's *display copy* (a second document opened from the same bytes, never saved; see
    `Source.set_hidden_annots`) by setting the Hidden bit of /F. The in-process renderer and the render worker
    (message "flags") rasterize from that copy, and the item paints itself: a snapshot of the original
    appearance when only its geometry or comment changed, vector drawing once restyled. The hidden sets are
    recomputed from the items after every undo-stack change, so undo/redo keep the page image, the
    thumbnails and the saved file in step. `Source.data` stays pristine.
"""
from __future__ import annotations

import getpass
import os
import re
import time

import pymupdf
from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPainterPath, QPen, QShortcut, QTransform, QUndoCommand
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from . import commands as C
from .items import (
    ANNOT_KINDS, Z_OVERLAY, BoxItem, HighlightItem, InkItem, LineItem, NoteItem, ReplyItem, ShapeItem,
    draw_note_icon, pdf_color, to_pt, to_rect,
)

GHOST_SCALE = 3.0  # snapshot resolution of an annotation's own appearance (px per pt)
HIDDEN, NOVIEW = 2, 32
SKIP = {
    pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET, pymupdf.PDF_ANNOT_POPUP, pymupdf.PDF_ANNOT_REDACT,
    pymupdf.PDF_ANNOT_SCREEN, pymupdf.PDF_ANNOT_MOVIE, pymupdf.PDF_ANNOT_RICH_MEDIA, pymupdf.PDF_ANNOT_3D,
    pymupdf.PDF_ANNOT_TRAP_NET, pymupdf.PDF_ANNOT_PRINTER_MARK, pymupdf.PDF_ANNOT_WATERMARK,
}
MARKUP = {
    pymupdf.PDF_ANNOT_HIGHLIGHT: "highlight", pymupdf.PDF_ANNOT_UNDERLINE: "underline",
    pymupdf.PDF_ANNOT_STRIKE_OUT: "strikeout", pymupdf.PDF_ANNOT_SQUIGGLY: "squiggly",
}
TYPE_NAMES = {
    "note": "Note", "highlight": "Highlight", "underline": "Underline", "strikeout": "Strikeout",
    "squiggly": "Squiggly", "rect": "Rectangle", "ellipse": "Ellipse", "line": "Line", "arrow": "Arrow",
    "ink": "Drawing", "freetext": "Text box", "stamp": "Stamp", "polygon": "Polygon", "polyline": "Polyline",
    "caret": "Caret", "fileattachment": "Attachment", "sound": "Sound", "reply": "Reply",
}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")


# ------------------------------------------------------------------ helpers
def default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def hex_color(rgb, fallback: str = "#000000") -> str:
    if not rgb or len(rgb) < 3:
        if rgb and len(rgb) == 1:  # gray
            v = round(rgb[0] * 255)
            return QColor(v, v, v).name()
        if rgb and len(rgb) == 4:  # cmyk
            c, m, y, k = rgb
            return QColor.fromCmykF(c, m, y, k).name()
        return fallback
    return QColor.fromRgbF(*(max(0.0, min(1.0, float(v))) for v in rgb[:3])).name()


def pdf_date_text(d: str) -> str:
    """'D:20261118130157+07'30'' -> '2026-11-18 13:01' (best effort)."""
    m = re.match(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?", d or "")
    if not m:
        return d or ""
    y, mo, da, h, mi = m.groups()
    out = f"{y}-{mo or '01'}-{da or '01'}"
    return f"{out} {h}:{mi or '00'}" if h else out


def is_annotation(it) -> bool:
    """Items that are (or become) PDF annotations: listed in the Comments panel, can carry a comment."""
    return isinstance(it, Adopted) or isinstance(it, ReplyItem) or getattr(it, "kind", "") in ANNOT_KINDS


def type_name(it) -> str:
    return TYPE_NAMES.get(getattr(it, "kind", ""), getattr(it, "kind", "").title() or "Annotation")


def author_of(it, default: str = "") -> str:
    """The comment's author; `default` (the user) only for annotations made in Leaf, not for the file's own."""
    got = (getattr(it, "meta", {}) or {}).get("author")
    return got or ("" if isinstance(it, Adopted) else default)


def snippet_of(it) -> str:
    meta = getattr(it, "meta", {}) or {}
    return meta.get("contents") or getattr(it, "marked_text", "") or ""


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def shift_keys(doc: pymupdf.Document, xref: int, dx: float, dy: float) -> None:
    """Translate an annotation by (dx, dy) in PDF user space, keeping its appearance stream."""
    for key in ("Rect", "L", "Vertices", "InkList", "QuadPoints", "CL"):
        t, v = doc.xref_get_key(xref, key)
        if t != "array":
            continue
        k = 0

        def rep(mo):
            nonlocal k
            val = float(mo.group()) + (dx if k % 2 == 0 else dy)
            k += 1
            return _fmt(val)

        doc.xref_set_key(xref, key, _NUM.sub(rep, v))


def raw_delta(page: pymupdf.Page, m: pymupdf.Matrix, d: QPointF) -> tuple[float, float]:
    """A display-space (page.rect) displacement in PDF user space (y up, MediaBox origin)."""
    mm = m * ~page.transformation_matrix
    a = pymupdf.Point(0, 0) * mm
    b = pymupdf.Point(d.x(), d.y()) * mm
    return b.x - a.x, b.y - a.y


def raw_rect(page: pymupdf.Page, m: pymupdf.Matrix, r: QRectF) -> str:
    rr = (pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom()) * m * ~page.transformation_matrix).normalize()
    return "[" + " ".join(_fmt(v) for v in (rr.x0, rr.y0, rr.x1, rr.y1)) + "]"


def raw_point(page: pymupdf.Page, m: pymupdf.Matrix, p: QPointF) -> pymupdf.Point:
    return pymupdf.Point(p.x(), p.y()) * m * ~page.transformation_matrix


def _qrect(r: pymupdf.Rect) -> QRectF:
    r = pymupdf.Rect(r).normalize()
    return QRectF(r.x0, r.y0, r.width, r.height)


# ------------------------------------------------------------- adopted items
class Adopted:
    """Mixed into an overlay class (first in the MRO). See the module docstring."""

    adopted = True

    def _adopt(self, src, index: int, xref: int, subtype: str, nm: str, meta: dict, grect: QRectF) -> None:
        self.src, self.src_index, self.xref, self.subtype, self.nm = src, index, xref, subtype, nm
        self.meta = dict(meta)
        self._grect = QRectF(grect)  # the annotation's whole Rect (display space) when adopted
        self._ghost: QImage | None = None
        self._ghost_failed = False
        self._orig = None

    def freeze(self) -> None:
        self._orig = self._snapshot()

    def _snapshot(self):
        return (self.geometry(), dict(self.props), dict(self.meta))

    def is_dirty(self) -> bool:
        return self._orig is not None and self._snapshot() != self._orig

    def geometry_changed(self) -> bool:
        return self.geometry() != self._orig[0]

    def style_changed(self) -> bool:
        return self.props != self._orig[1]

    def meta_changed(self) -> bool:
        return self.meta != self._orig[2]

    def draws_self(self) -> bool:
        return self.xref in self.src.hidden_annots or self.is_dirty()

    def clone_overlay(self):
        return None  # copying a page copies the original annotation with it

    # -- appearance snapshot ("ghost")
    def uses_ghost(self) -> bool:
        return not self.style_changed()

    def ghost(self) -> QImage | None:
        if self._ghost is None and not self._ghost_failed:
            try:
                page = self.src.page(self.src_index)
                a = page.load_annot(self.xref)
                pix = a.get_pixmap(matrix=pymupdf.Matrix(GHOST_SCALE, GHOST_SCALE), alpha=True)
                fmt = QImage.Format.Format_RGBA8888 if pix.alpha else QImage.Format.Format_RGB888
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()
                if page.rotation:
                    img = img.transformed(QTransform().rotate(page.rotation))
                self._ghost = img
            except Exception:  # noqa: BLE001
                self._ghost_failed = True
        return self._ghost

    def ghost_rect(self) -> QRectF:
        """Where the snapshot goes, in item coordinates (follows moves; boxes also follow resizes)."""
        pos0 = self._orig[0][0] if isinstance(self._orig[0], tuple) else self._orig[0]
        return self._grect.translated(-pos0)

    def paint_ghost(self, p: QPainter) -> bool:
        img = self.ghost()
        if img is None:
            return False
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(self.ghost_rect(), img)
        p.restore()
        return True

    def ghost_bounds(self) -> QRectF:
        return self.ghost_rect() if self._orig is not None else QRectF()

    # -- saving helpers
    def _load(self, page: pymupdf.Page):
        try:
            a = page.load_annot(self.xref)
        except Exception:  # noqa: BLE001
            return None
        if a is not None and self.nm:
            got = a.info.get("id") or ""
            if got and got != self.nm:
                return None
        return a

    def _shift(self, page: pymupdf.Page, m: pymupdf.Matrix, d: QPointF) -> None:
        if abs(d.x()) > 1e-6 or abs(d.y()) > 1e-6:
            dx, dy = raw_delta(page, m, d)
            shift_keys(page.parent, self.xref, dx, dy)

    def _write_meta(self, a: pymupdf.Annot) -> None:
        if not self.meta_changed():
            return
        info = {"content": self.meta.get("contents") or "", "modDate": self.meta.get("modified") or pymupdf.get_pdf_now()}
        if self.meta.get("author") != self._orig[2].get("author"):
            info["title"] = self.meta.get("author") or ""
        a.set_info(**info)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        if not self.is_dirty():
            return
        a = self._load(page)
        if a is None:
            return
        self.apply_changes(page, a, m)
        self._write_meta(a)

    def apply_changes(self, page, a, m) -> None:
        pass


class AdoptedShape(Adopted, ShapeItem):
    def __init__(self, kind: str, rect: QRectF, props: dict, fill_rgb):
        ShapeItem.__init__(self, kind, rect, props)
        self.fill_rgb = fill_rgb

    def ghost_rect(self) -> QRectF:
        (pos0, r0), cur = self._orig[0], self.scene_rect_in_parent()
        ob = r0.translated(pos0)
        sx, sy = cur.width() / max(ob.width(), 1e-6), cur.height() / max(ob.height(), 1e-6)
        g = self._grect
        r = QRectF(cur.left() + (g.left() - ob.left()) * sx, cur.top() + (g.top() - ob.top()) * sy,
                   g.width() * sx, g.height() * sy)
        return r.translated(-self.pos())

    def boundingRect(self) -> QRectF:
        return ShapeItem.boundingRect(self).united(self.ghost_bounds())

    def _fill(self) -> QColor | None:
        if not self.props.get("fill"):
            return None
        if self.fill_rgb and self.props.get("color") == self._orig[1].get("color"):
            c = QColor(hex_color(self.fill_rgb))
        else:
            c = QColor(*(round(255 * v) for v in self._blend()))
        c.setAlphaF(float(self.props.get("opacity", 1.0)))
        return c

    def _blend(self):
        return tuple(1 - 0.25 * (1 - v) for v in pdf_color(self.props["color"]))

    def paint_content(self, p: QPainter) -> None:
        if not self.draws_self():
            return
        if self.uses_ghost() and self.paint_ghost(p):
            return
        pen = self.pen()
        w = pen.widthF()
        r = self._r.adjusted(w / 2, w / 2, -w / 2, -w / 2)
        p.setPen(pen if w > 0 else Qt.PenStyle.NoPen)
        fill = self._fill()
        p.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
        if self.kind == "ellipse":
            p.drawEllipse(r)
        else:
            p.drawRect(r)

    def clone(self):
        c = ShapeItem(self.kind, QRectF(self._r), dict(self.props))
        c.set_geometry(self.geometry())
        return c

    def apply_changes(self, page, a, m) -> None:
        (pos0, r0) = self._orig[0]
        if not self.style_changed() and self._r.size() == r0.size():
            self._shift(page, m, self.scene_rect_in_parent().topLeft() - r0.translated(pos0).topLeft())
            return
        target = to_rect(self.scene_rect_in_parent(), m)
        if self.geometry_changed():
            a.set_rect(target)
        if self.style_changed():
            a.set_border(width=float(self.props.get("width", 1.0)))
            fill = []
            if self.props.get("fill"):
                fill = self.fill_rgb if (self.fill_rgb and self.props.get("color") == self._orig[1].get("color")) else self._blend()
            a.set_colors(stroke=pdf_color(self.props["color"]), fill=fill)
            a.set_opacity(float(self.props.get("opacity", 1.0)))
        a.update()
        if self.geometry_changed():  # MuPDF pads /Rect for the border: aim so the result is where the box is shown
            got = a.rect
            if max(abs(got.x0 - target.x0), abs(got.y0 - target.y0), abs(got.x1 - target.x1), abs(got.y1 - target.y1)) > 0.25:
                a.set_rect(pymupdf.Rect(2 * target.x0 - got.x0, 2 * target.y0 - got.y0,
                                        2 * target.x1 - got.x1, 2 * target.y1 - got.y1))
                a.update()


class AdoptedLine(Adopted, LineItem):
    def __init__(self, kind: str, p1: QPointF, p2: QPointF, props: dict, ends):
        LineItem.__init__(self, kind, p1, p2, props)
        self.ends = tuple(ends or (0, 0))

    def _head(self, tip: QPointF, frm: QPointF, path: QPainterPath) -> None:
        import math

        d = tip - frm
        ang = math.atan2(d.y(), d.x())
        size = 4 + 3 * float(self.props.get("width", 2))
        for s in (-1, 1):
            a = ang + math.pi - s * math.radians(28)
            path.moveTo(tip)
            path.lineTo(tip + QPointF(math.cos(a) * size, math.sin(a) * size))

    def _path(self) -> QPainterPath:
        path = QPainterPath(self.p1)
        path.lineTo(self.p2)
        if self.ends[0]:
            self._head(self.p1, self.p2, path)
        if self.ends[1]:
            self._head(self.p2, self.p1, path)
        return path

    def uses_ghost(self) -> bool:
        g = self._orig[0]
        return not self.style_changed() and self.p1 == g[1] and self.p2 == g[2]

    def boundingRect(self) -> QRectF:
        return LineItem.boundingRect(self).united(self.ghost_bounds())

    def paint(self, p: QPainter, opt, widget=None) -> None:
        if self.draws_self():
            if not (self.uses_ghost() and self.paint_ghost(p)):
                p.setPen(self.pen())
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(self._path())
        if self.isSelected():
            from .items import _handle, _zoom

            pen = self._sel_pen()
            p.setPen(pen)
            p.drawLine(self.p1, self.p2)
            h = 8.0 / _zoom(self)
            _handle(p, self.p1, h)
            _handle(p, self.p2, h)

    def clone(self):
        c = LineItem("arrow" if self.ends[1] else "line", QPointF(0, 0), QPointF(1, 1), dict(self.props))
        c.set_geometry(self.geometry())
        return c

    def apply_changes(self, page, a, m) -> None:
        if self.uses_ghost():
            self._shift(page, m, self.pos() - self._orig[0][0])
            return
        q1 = raw_point(page, m, self.mapToParent(self.p1))
        q2 = raw_point(page, m, self.mapToParent(self.p2))
        page.parent.xref_set_key(self.xref, "L", "[" + " ".join(_fmt(v) for v in (q1.x, q1.y, q2.x, q2.y)) + "]")
        a.set_border(width=float(self.props.get("width", 1.0)))
        a.set_colors(stroke=pdf_color(self.props["color"]))
        a.set_opacity(float(self.props.get("opacity", 1.0)))
        a.update()


class AdoptedInk(Adopted, InkItem):
    def __init__(self, strokes: list[list[QPointF]], props: dict):
        InkItem.__init__(self, QPointF(0, 0), props)
        self.strokes = [[QPointF(p) for p in s] for s in strokes if s]
        self.pts = list(self.strokes[0]) if self.strokes else [QPointF(0, 0)]

    def _path(self) -> QPainterPath:
        if self._cache is None:
            path = QPainterPath()
            for s in self.strokes:
                path.moveTo(s[0])
                if len(s) == 1:
                    path.lineTo(s[0] + QPointF(0.01, 0.01))
                for pt in s[1:]:
                    path.lineTo(pt)
            self._cache = path
        return self._cache

    def boundingRect(self) -> QRectF:
        return InkItem.boundingRect(self).united(self.ghost_bounds())

    def paint(self, p: QPainter, opt, widget=None) -> None:
        if self.draws_self() and not (self.uses_ghost() and self.paint_ghost(p)):
            p.setPen(self.pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._path())
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._path().boundingRect())

    def clone(self):
        c = InkItem(QPointF(0, 0), dict(self.props))
        c.pts = [QPointF(p) for p in self.strokes[0]] if self.strokes else [QPointF(0, 0)]
        c._cache = None
        c.setPos(self.pos())
        return c

    def apply_changes(self, page, a, m) -> None:
        self._shift(page, m, self.pos() - self._orig[0])
        if self.style_changed():
            a.set_border(width=float(self.props.get("width", 1.0)))
            a.set_colors(stroke=pdf_color(self.props["color"]))
            a.set_opacity(float(self.props.get("opacity", 1.0)))
            a.update()


class AdoptedMarkup(Adopted, HighlightItem):
    def __init__(self, kind: str, rects: list[QRectF], props: dict):
        HighlightItem.__init__(self, rects, props)
        self.kind = kind

    def boundingRect(self) -> QRectF:
        return HighlightItem.boundingRect(self).united(self.ghost_bounds())

    def paint(self, p: QPainter, opt, widget=None) -> None:
        if self.draws_self() and not (self.uses_ghost() and self.paint_ghost(p)):
            c = QColor(self.props.get("color", "#ffd83d"))
            c.setAlphaF(float(self.props.get("opacity", 1.0)))
            if self.kind == "highlight":
                p.save()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                for r in self.rects:
                    p.drawRect(r)
                p.restore()
            else:
                for r in self.rects:
                    lw = max(0.6, r.height() / 14)
                    p.setPen(QPen(c, lw))
                    if self.kind == "strikeout":
                        y = r.center().y()
                        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
                    elif self.kind == "underline":
                        y = r.bottom() - lw
                        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
                    else:
                        amp, step = r.height() / 12, r.height() / 6
                        path = QPainterPath(QPointF(r.left(), r.bottom() - amp))
                        x, k = r.left(), 0
                        while x < r.right():
                            x = min(r.right(), x + step)
                            k += 1
                            path.lineTo(QPointF(x, r.bottom() - amp - (amp * 2 if k % 2 else 0)))
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawPath(path)
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            for r in self.rects:
                p.drawRect(r)

    def apply_changes(self, page, a, m) -> None:
        if self.style_changed():
            a.set_colors(stroke=pdf_color(self.props["color"]))
            a.set_opacity(float(self.props.get("opacity", 1.0)))
            a.update()


class AdoptedNote(Adopted, NoteItem):
    def __init__(self, rect: QRectF, props: dict, icon: str):
        NoteItem.__init__(self, rect.topLeft(), props)
        self.set_geometry((rect.topLeft(), QRectF(0, 0, rect.width(), rect.height())))
        self.icon = icon

    def boundingRect(self) -> QRectF:
        return NoteItem.boundingRect(self).united(self.ghost_bounds())

    def paint_content(self, p: QPainter) -> None:
        if self.draws_self() and not (self.uses_ghost() and self.paint_ghost(p)):
            draw_note_icon(p, self._r, QColor(self.props.get("color", "#ffd83d")))

    def clone(self):
        c = NoteItem(QPointF(self.scene_rect_in_parent().topLeft()), dict(self.props),
                     {k: v for k, v in self.meta.items() if k in ("contents", "author")})
        c.meta["icon"] = self.icon
        return c

    def apply_changes(self, page, a, m) -> None:
        self._shift(page, m, self.pos() - self._orig[0][0])
        if self.style_changed():
            a.set_colors(stroke=pdf_color(self.props["color"]))
            a.update()


class AdoptedBox(Adopted, BoxItem):
    """FreeText, Stamp and the move/delete-only kinds: drawn from the annotation's own appearance."""

    def __init__(self, kind: str, rect: QRectF, resizable: bool):
        BoxItem.__init__(self, kind, rect, {})
        self.resizable = resizable

    def corner_at(self, pos: QPointF):
        return BoxItem.corner_at(self, pos) if self.resizable else None

    ghost_rect = AdoptedShape.ghost_rect

    def boundingRect(self) -> QRectF:
        return BoxItem.boundingRect(self).united(self.ghost_bounds())

    def uses_ghost(self) -> bool:
        return not (self.kind == "freetext" and self.meta.get("contents") != self._orig[2].get("contents"))

    def paint_content(self, p: QPainter) -> None:
        if not self.draws_self() or (self.uses_ghost() and self.paint_ghost(p)):
            return
        p.setPen(QPen(QColor(0, 0, 0, 120), 0.8))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self._r)
        if self.kind == "freetext":
            f = p.font()
            f.setPixelSize(10)
            p.setFont(f)
            p.setPen(Qt.GlobalColor.black)
            p.drawText(self._r.adjusted(2, 2, -2, -2), Qt.TextFlag.TextWordWrap, self.meta.get("contents") or "")

    def apply_changes(self, page, a, m) -> None:
        (pos0, r0) = self._orig[0]
        cur = self.scene_rect_in_parent()
        if self._r.size() == r0.size():
            self._shift(page, m, cur.topLeft() - r0.translated(pos0).topLeft())
        elif self.kind == "freetext":
            a.set_rect(to_rect(cur, m))
            a.update()
        else:  # the appearance is mapped into /Rect: rewriting it scales the stamp
            ob = r0.translated(pos0)
            g = self._grect
            sx, sy = cur.width() / max(ob.width(), 1e-6), cur.height() / max(ob.height(), 1e-6)
            new = QRectF(cur.left() + (g.left() - ob.left()) * sx, cur.top() + (g.top() - ob.top()) * sy,
                         g.width() * sx, g.height() * sy)
            page.parent.xref_set_key(self.xref, "Rect", raw_rect(page, m, new))
        if self.kind == "freetext" and self.meta.get("contents") != self._orig[2].get("contents"):
            a.set_info(content=self.meta.get("contents") or "")
            a.update()


class AdoptedReply(Adopted, ReplyItem):
    def __init__(self, target, meta: dict):
        ReplyItem.__init__(self, target, meta)

    def draws_self(self) -> bool:
        return False

    def ghost_bounds(self) -> QRectF:
        return QRectF()

    def apply_changes(self, page, a, m) -> None:
        pass  # comment fields only (written by Adopted.apply)


class Deleted:
    """Stand-in handed to the save pipeline for a deleted adopted annotation."""

    kind = "deleted"
    props: dict = {}
    meta: dict = {}

    def __init__(self, item):
        self.item = item
        self.xref = item.xref

    def clone_overlay(self):
        return None

    def zValue(self) -> float:
        return 1e9

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        a = Adopted._load(self.item, page)
        if a is not None:
            page.delete_annot(a)


# ------------------------------------------------------------------ adoption
def _meta_of(a: pymupdf.Annot) -> tuple[dict, str]:
    info = a.info
    meta = {"contents": info.get("content") or "", "author": info.get("title") or "",
            "created": info.get("creationDate") or "", "modified": info.get("modDate") or "",
            "subject": info.get("subject") or ""}
    return meta, info.get("id") or ""


def _width(a: pymupdf.Annot) -> float:
    w = (a.border or {}).get("width", 1.0)
    return float(w) if w is not None and w >= 0 else 1.0


def _opacity(a: pymupdf.Annot) -> float:
    o = a.opacity
    return float(o) if o is not None and 0 <= o <= 1 else 1.0


def adopt_page(pg) -> list:
    """Overlay items for the annotations of `pg`'s source page (added to pg.content, not on the undo stack)."""
    src, index = pg.ref.src, pg.ref.index
    try:
        page = src.page(index)
    except Exception:  # noqa: BLE001
        return []
    rm = page.rotation_matrix
    made: dict[int, object] = {}
    replies = []
    out = []
    for n, a in enumerate(page.annots() or []):
        try:
            t = a.type[0]
            if t in SKIP or a.flags & (HIDDEN | NOVIEW):
                continue
            xref = a.xref
            meta, nm = _meta_of(a)
            disp = a.rect * rm
            grect = _qrect(disp)
            irt = a.irt_xref
            if irt:
                replies.append((n, xref, irt, meta, nm, grect))
                continue
            colors = a.colors or {}
            stroke = hex_color(colors.get("stroke"), "#000000")
            it = None
            if t in (pymupdf.PDF_ANNOT_SQUARE, pymupdf.PDF_ANNOT_CIRCLE):
                fill = colors.get("fill") or None
                props = {"color": stroke, "width": _width(a), "fill": bool(fill), "opacity": _opacity(a)}
                it = AdoptedShape("ellipse" if t == pymupdf.PDF_ANNOT_CIRCLE else "rect", grect, props, fill)
            elif t == pymupdf.PDF_ANNOT_LINE and a.vertices and len(a.vertices) >= 2:
                p1, p2 = (pymupdf.Point(v) * rm for v in a.vertices[:2])
                ends = a.line_ends or (0, 0)
                props = {"color": stroke, "width": _width(a), "opacity": _opacity(a)}
                it = AdoptedLine("arrow" if any(ends) else "line", QPointF(p1.x, p1.y), QPointF(p2.x, p2.y), props, ends)
            elif t == pymupdf.PDF_ANNOT_INK and a.vertices:
                strokes = [[QPointF(*(pymupdf.Point(v) * rm)) for v in s] for s in a.vertices]
                props = {"color": stroke, "width": _width(a), "opacity": _opacity(a)}
                it = AdoptedInk(strokes, props)
            elif t in MARKUP and a.vertices:
                v = a.vertices
                rects, words = [], []
                for k in range(0, len(v) - 3, 4):
                    q = pymupdf.Quad(v[k], v[k + 1], v[k + 2], v[k + 3])
                    rects.append(_qrect(q.rect * rm))
                    try:
                        words.append(page.get_textbox(q.rect).strip())
                    except Exception:  # noqa: BLE001
                        pass
                props = {"color": hex_color(colors.get("stroke"), "#ffd83d"), "opacity": _opacity(a)}
                it = AdoptedMarkup(MARKUP[t], rects, props)
                it.marked_text = " ".join(w for w in words if w)
            elif t == pymupdf.PDF_ANNOT_TEXT:
                props = {"color": hex_color(colors.get("stroke"), "#ffd83d")}
                it = AdoptedNote(grect, props, a.info.get("name") or "Note")
            elif t in (pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_STAMP):
                it = AdoptedBox("freetext" if t == pymupdf.PDF_ANNOT_FREE_TEXT else "stamp", grect, True)
            else:
                it = AdoptedBox(a.type[1].lower(), grect, False)
            it._adopt(src, index, xref, a.type[1], nm, meta, grect)
        except Exception:  # noqa: BLE001  (one odd annotation must not stop the page)
            continue
        it.setZValue(Z_OVERLAY - 3 + n * 1e-4)
        it.setParentItem(pg.content)
        it.freeze()
        made[xref] = it
        out.append(it)
    for n, xref, irt, meta, nm, grect in replies:  # answers may answer answers: resolve in file order
        target = made.get(irt)
        if target is None:
            continue
        it = AdoptedReply(target, meta)
        it._adopt(src, index, xref, "Text", nm, meta, grect)
        it.setParentItem(pg.content)
        it.freeze()
        made[xref] = it
        out.append(it)
    return out


def thread_root(it):
    while isinstance(it, ReplyItem):
        it = it.target
    return it


# ------------------------------------------------------------------- commands
class SetMeta(QUndoCommand):
    """Edit an annotation's comment (contents/author)."""

    def __init__(self, item, old: dict, new: dict, text: str = "Edit comment"):
        super().__init__(text)
        self.item, self.old, self.new = item, dict(old), dict(new)

    def redo(self) -> None:
        self.item.meta = dict(self.new)
        self.item.update()

    def undo(self) -> None:
        self.item.meta = dict(self.old)
        self.item.update()


# ---------------------------------------------------------------- note editor
class NoteEditor(QDialog):
    """Small popup editor for a note's (or any annotation's) comment."""

    def __init__(self, parent, title: str, author: str, when: str, text: str, can_delete: bool):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.deleted = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        self.author = QLineEdit(author)
        self.author.setPlaceholderText("Author")
        self.author.setToolTip("Author (remembered for new comments)")
        head.addWidget(self.author, 1)
        self.when = QLabel(when)
        self.when.setEnabled(False)
        head.addWidget(self.when)
        lay.addLayout(head)
        self.edit = QPlainTextEdit(text)
        self.edit.setPlaceholderText("Write a comment…")
        self.edit.setMinimumSize(280, 120)
        lay.addWidget(self.edit, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setToolTip("Ctrl+Enter")
        if can_delete:
            d = QPushButton("Delete")
            bb.addButton(d, QDialogButtonBox.ButtonRole.DestructiveRole)
            d.clicked.connect(self._delete)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for k in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(k), self, self.accept)
        self.edit.setFocus()
        c = self.edit.textCursor()
        c.movePosition(c.MoveOperation.End)
        self.edit.setTextCursor(c)

    def _delete(self) -> None:
        self.deleted = True
        self.accept()


# -------------------------------------------------------------------- manager
class AnnotManager(QObject):
    """Per canvas: lazy adoption, display hiding, notes, comments and replies."""

    changed = Signal()  # the set of annotations or their comments changed (Comments panel)
    thumbsStale = Signal(list)  # page uids whose thumbnails show annotations that are now hidden/shown
    authorChanged = Signal(str)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.author = default_author()
        # (item, title, author, when, text, can_delete) -> (text, author) | "delete" | None; replaced in tests
        self.prompt = self._prompt
        self._sync = QTimer(self, singleShot=True, interval=0, timeout=self.sync)
        self._emit = QTimer(self, singleShot=True, interval=60, timeout=self.changed.emit)
        self._bg = QTimer(self, singleShot=True, interval=0, timeout=self._adopt_some)
        self._todo: list = []
        canvas.undo.indexChanged.connect(lambda _=0: self._sync.start())
        canvas.pagesChanged.connect(self._emit.start)

    # ---------------------------------------------------------- adoption
    def reset(self) -> None:
        self._todo = []
        self._bg.stop()
        self._emit.start()

    def ensure(self, pg) -> list:
        if pg.adopted is None:
            pg.adopted = adopt_page(pg) if self.canvas.doc is not None else []
            if pg.adopted:
                self._emit.start()
        return pg.adopted

    def adopt_all(self) -> None:
        """Adopt every page in the background (time-sliced); the Comments panel wants the whole document."""
        self._todo = [pg for pg in self.canvas.order if pg.adopted is None]
        if self._todo:
            self._bg.start()

    def busy(self) -> bool:
        return bool(self._todo)

    def _adopt_some(self) -> None:
        t0 = time.perf_counter()
        while self._todo and time.perf_counter() - t0 < 0.012:
            pg = self._todo.pop(0)
            if pg.scene() is not None:
                self.ensure(pg)
        if self._todo:
            self._bg.start()
        else:
            self._emit.start()

    def items(self) -> list[tuple[int, object]]:
        """(page position, item) for every annotation item on the pages in the current order."""
        out = []
        for i, pg in enumerate(self.canvas.order):
            for it in pg.overlays():
                if is_annotation(it):
                    out.append((i, it))
        return out

    # ------------------------------------------------------------- saving
    def for_save(self, pg) -> list:
        """What the save pipeline gets for a page: new items, changed adopted items, tombstones (last)."""
        out = [o for o in pg.overlays() if not isinstance(o, Adopted) or o.is_dirty()]
        out += [Deleted(it) for it in (pg.adopted or []) if it.parentItem() is None]
        return out

    # ------------------------------------------------------------ display
    def sync(self) -> None:
        cv = self.canvas
        doc = cv.doc
        if doc is None:
            return
        want: dict = {}
        now = None
        for pg in list(cv.items_by_uid.values()):
            for it in pg.adopted or []:
                if it.parentItem() is None or it.is_dirty():
                    want.setdefault(it.src, set()).add(it.xref)
            for it in pg.overlays():  # stamp the author on new annotations
                if not isinstance(it, Adopted) and is_annotation(it) and not it.meta.get("author"):
                    now = now or pymupdf.get_pdf_now()
                    it.meta.setdefault("author", self.author)
                    it.meta.setdefault("created", now)
        stale = []
        for src in doc.sources:
            ch = src.set_hidden_annots(want.get(src, ()))
            if not ch:
                continue
            spots = {(it.src, it.src_index) for pg in cv.items_by_uid.values() for it in (pg.adopted or []) if it.xref in ch}
            for pg in cv.items_by_uid.values():
                if (pg.ref.src, pg.ref.index) in spots:
                    pg.invalidate()
                    stale.append(pg.ref.uid)
                    for it in pg.adopted or []:
                        it.update()
        if stale:
            self.thumbsStale.emit(stale)
        self._emit.start()

    # ------------------------------------------------------- notes, comments
    def set_author(self, name: str) -> None:
        name = name.strip()
        if name and name != self.author:
            self.author = name
            self.authorChanged.emit(name)

    def _prompt(self, item, title, author, when, text, can_delete):
        cv = self.canvas
        dlg = NoteEditor(cv.window(), title, author, when, text, can_delete)
        try:
            r = item.mapRectToScene(item.boundingRect())
            g = cv.viewport().mapToGlobal(cv.mapFromScene(r.topRight()))
            dlg.adjustSize()
            dlg.move(g + QPoint(12, 0))
        except RuntimeError:
            pass
        if not dlg.exec():
            return None
        if dlg.deleted:
            return "delete"
        return dlg.edit.toPlainText(), dlg.author.text()

    def add_note(self, pg, lp: QPointF):
        """Note tool click: ask for the comment, then add the note (one undo step). Returns the item or None."""
        s = NoteItem.SIZE
        r = pg.content.rect()
        pos = QPointF(min(max(lp.x() - s / 2, r.left()), r.right() - s), min(max(lp.y() - s / 2, r.top()), r.bottom() - s))
        item = NoteItem(pos, self.canvas.defaults.get("note"), {"author": self.author})
        item.setParentItem(pg.content)
        self.canvas.scene().clearSelection()
        item.setSelected(True)
        got = self.prompt(item, "Note", self.author, "", "", False)
        if not isinstance(got, tuple):
            item.setSelected(False)
            if item.scene() is not None:
                item.scene().removeItem(item)
            return None
        text, author = got
        self.set_author(author)
        now = pymupdf.get_pdf_now()
        item.meta = {"contents": text, "author": author or self.author, "created": now, "modified": now}
        self.canvas.undo.push(C.AddItems([item], "Add note"))
        item.setSelected(True)
        return item

    def open_editor(self, item) -> None:
        """Edit the comment of a note or any annotation (double-click / Enter / Comments panel)."""
        if not is_annotation(item):
            return
        meta = item.meta
        when = pdf_date_text(meta.get("modified") or meta.get("created") or "")
        got = self.prompt(item, type_name(item), meta.get("author") or self.author, when, meta.get("contents") or "", True)
        if got == "delete":
            self.delete([item])
        elif isinstance(got, tuple):
            text, author = got
            self.set_author(author)
            self.set_comment(item, text, author or meta.get("author") or self.author)

    def set_comment(self, item, text: str, author: str | None = None) -> None:
        old = dict(item.meta)
        new = dict(old)
        new["contents"] = text
        if author is not None:
            new["author"] = author
        if new == old:
            return
        new["modified"] = pymupdf.get_pdf_now()
        self.canvas.undo.push(SetMeta(item, old, new))

    def add_reply(self, target, text: str):
        if target is None or not text.strip() or target.parentItem() is None:
            return None
        now = pymupdf.get_pdf_now()
        r = ReplyItem(target, {"contents": text, "author": self.author, "created": now, "modified": now})
        r.setParentItem(target.parentItem())
        self.canvas.undo.push(C.AddItems([r], "Reply"))
        return r

    def replies_of(self, item) -> list:
        """All replies answering `item`, directly or through other replies."""
        content = item.parentItem()
        if content is None:
            return []
        out, frontier = [], {id(item)}
        changed = True
        kids = [c for c in content.childItems() if isinstance(c, ReplyItem)]
        while changed:
            changed = False
            for c in kids:
                if id(c) not in frontier and id(c.target) in frontier:
                    frontier.add(id(c))
                    out.append(c)
                    changed = True
        return out

    def with_replies(self, items) -> list:
        out, seen = [], set()
        for it in items:
            for x in [it, *self.replies_of(it)]:
                if id(x) not in seen:
                    seen.add(id(x))
                    out.append(x)
        return out

    def delete(self, items) -> None:
        items = [i for i in self.with_replies(items) if i.parentItem() is not None]
        if items:
            self.canvas.undo.push(C.RemoveItems(items, "Delete"))

    def select(self, item) -> None:
        """Select an annotation on the page and scroll it into view (replies select what they answer)."""
        item = thread_root(item)
        cv = self.canvas
        if item.parentItem() is None:
            return
        if cv.tool not in ("select", "hand"):
            cv.set_tool("select")
        cv.scene().clearSelection()
        item.setSelected(True)
        r = item.boundingRect()
        cv.ensure_rect_visible(r, item)

