"""Overlay items the user adds on top of a page.

All items live inside a page's content item, whose coordinates are the source
page.rect space (PDF points, y down). `apply()` writes the item into a PDF page.
"""
from __future__ import annotations

import math

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QImage, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF,
    QTextCursor,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsTextItem

from . import fonts, richtext

DEFAULTS: dict[str, dict] = {
    "text": {"color": "#1a1a1a", "size": 12, "font": "Helvetica", "bold": False, "italic": False, "underline": False,
             "strike": False, "align": "left", "spacing": 1.0, "bg": "", "border": "", "pad": 2.0},
    "highlight": {"color": "#ffd83d"},
    "ink": {"color": "#1f4fd1", "width": 2.0, "opacity": 1.0},
    "rect": {"color": "#d43a2a", "width": 2.0, "fill": False, "opacity": 1.0},
    "ellipse": {"color": "#d43a2a", "width": 2.0, "fill": False, "opacity": 1.0},
    "line": {"color": "#d43a2a", "width": 2.0, "opacity": 1.0},
    "arrow": {"color": "#d43a2a", "width": 2.0, "opacity": 1.0},
    "whiteout": {},
    "redact": {"color": "#000000", "text": ""},  # fill color after redaction, optional overlay text (leaf/redact.py)
    "image": {"opacity": 1.0},
    "signature": {"opacity": 1.0},
    "note": {"color": "#ffd83d"},
}
# kinds that become real PDF annotations (they can carry a comment and show in the Comments panel)
ANNOT_KINDS = {"highlight", "ink", "rect", "ellipse", "line", "arrow", "note"}

ACCENT = QColor(37, 99, 235)
Z_OVERLAY = 10


def pdf_color(c: str) -> tuple[float, float, float]:
    q = QColor(c)
    return (q.redF(), q.greenF(), q.blueF())


def to_rect(r: QRectF, m: pymupdf.Matrix) -> pymupdf.Rect:
    return (pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom()) * m).normalize()


def to_pt(p: QPointF, m: pymupdf.Matrix) -> pymupdf.Point:
    return pymupdf.Point(p.x(), p.y()) * m


def _zoom(item: QGraphicsItem) -> float:
    s = item.scene()
    return getattr(s, "zoom", 1.0) if s else 1.0


def _push(item: QGraphicsItem, cmd) -> None:
    s = item.scene()
    if s is not None and getattr(s, "canvas", None) is not None:
        s.canvas.undo.push(cmd)


def write_meta(a: pymupdf.Annot, meta: dict, new: bool = True) -> None:
    """Comment fields of a new annotation: contents, author (/T), dates."""
    if not meta:
        return
    info = {}
    if meta.get("contents") is not None:
        info["content"] = meta["contents"]
    if meta.get("author"):
        info["title"] = meta["author"]
    if meta.get("subject"):
        info["subject"] = meta["subject"]
    now = pymupdf.get_pdf_now()
    info["modDate"] = meta.get("modified") or now
    if new:
        info["creationDate"] = meta.get("created") or now
    a.set_info(**info)


class OverlayMixin:
    kind = ""
    props: dict
    meta: dict  # comment fields for annotation kinds: contents, author, created, modified (PDF date strings)

    def _init_overlay(self, kind: str, props: dict) -> None:
        self.meta = {}
        self.kind = kind
        self.props = dict(DEFAULTS.get(kind, {}))
        self.props.update(props or {})
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(Z_OVERLAY)

    def set_props(self, props: dict) -> None:
        self.props = dict(props)
        self.prepareGeometryChange()
        self.props_changed()
        self.update()

    def props_changed(self) -> None:
        pass

    def _finish(self, a: pymupdf.Annot) -> None:
        """Last step of writing a new annotation: comment fields, appearance, and remember its xref (replies
        created in the same save point at it)."""
        meta = dict(self.meta)
        if self.kind == "highlight" and meta.get("contents") is None and getattr(self, "marked_text", ""):
            meta["contents"] = self.marked_text
        write_meta(a, meta)
        a.update()
        self.saved_xref = a.xref

    def pen(self) -> QPen:
        c = QColor(self.props.get("color", "#000"))
        c.setAlphaF(float(self.props.get("opacity", 1.0)))
        pen = QPen(c, float(self.props.get("width", 1.0)))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def _clamp(self, value: QPointF) -> QPointF:
        parent = self.parentItem()
        if parent is None:
            return value
        pr = parent.rect() if hasattr(parent, "rect") else parent.boundingRect()
        br = self.boundingRect()
        x, y = value.x(), value.y()
        if br.width() < pr.width():
            x = min(max(x, pr.left() - br.left()), pr.right() - br.right())
        if br.height() < pr.height():
            y = min(max(y, pr.top() - br.top()), pr.bottom() - br.bottom())
        return QPointF(x, y)

    def _sel_pen(self) -> QPen:
        pen = QPen(ACCENT, 1.25)
        pen.setCosmetic(True)
        pen.setStyle(Qt.PenStyle.DashLine)
        return pen


def _handle(p: QPainter, c: QPointF, h: float) -> None:
    pen = QPen(ACCENT, 1.25)
    pen.setCosmetic(True)
    p.setPen(pen)
    p.setBrush(QBrush(Qt.GlobalColor.white))
    p.drawRect(QRectF(c.x() - h / 2, c.y() - h / 2, h, h))


class BoxItem(OverlayMixin, QGraphicsObject):
    """Rect-shaped overlay with corner resize handles."""

    keep_aspect = False

    def __init__(self, kind: str, rect: QRectF, props: dict | None = None):
        QGraphicsObject.__init__(self)
        self._drag = None
        self._init_overlay(kind, props)
        r = rect.normalized()
        self._r = QRectF(0, 0, r.width(), r.height())
        self.setPos(r.topLeft())
        self.setAcceptHoverEvents(True)

    # geometry --------------------------------------------------------------
    def geometry(self):
        return (QPointF(self.pos()), QRectF(self._r))

    def set_geometry(self, geom) -> None:
        pos, r = geom
        self.prepareGeometryChange()
        self._r = QRectF(r)
        self.setPos(pos)
        self.update()

    def scene_rect_in_parent(self) -> QRectF:
        return self.mapRectToParent(self._r)

    def is_trivial(self) -> bool:
        return self._r.width() < 3 and self._r.height() < 3

    def drag_create(self, start: QPointF, cur: QPointF, shift: bool) -> None:
        r = QRectF(start, cur).normalized()
        if shift:
            s = max(r.width(), r.height())
            r = QRectF(start, QSizeF(math.copysign(s, cur.x() - start.x()), math.copysign(s, cur.y() - start.y()))).normalized()
        self.set_geometry((r.topLeft(), QRectF(0, 0, r.width(), r.height())))

    def handle_size(self) -> float:
        return 8.0 / _zoom(self)

    def boundingRect(self) -> QRectF:
        m = self.handle_size() + float(self.props.get("width", 0)) / 2 + 1
        return self._r.adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        h = self.handle_size() / 2 if self.isSelected() else 0
        path.addRect(self._r.adjusted(-h, -h, h, h))
        return path

    def corners(self) -> list[QPointF]:
        r = self._r
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def corner_at(self, pos: QPointF):
        if not self.isSelected():
            return None
        h = self.handle_size()
        for i, c in enumerate(self.corners()):
            if abs(c.x() - pos.x()) <= h and abs(c.y() - pos.y()) <= h:
                return i
        return None

    # painting --------------------------------------------------------------
    def paint(self, p: QPainter, opt, widget=None) -> None:
        self.paint_content(p)
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._r)
            h = self.handle_size()
            for c in self.corners():
                _handle(p, c, h)

    def paint_content(self, p: QPainter) -> None:
        pass

    # interaction -----------------------------------------------------------
    def hoverMoveEvent(self, e) -> None:
        c = self.corner_at(e.pos())
        if c is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if c % 2 == 0 else Qt.CursorShape.SizeBDiagCursor)
        super().hoverMoveEvent(e)

    def mousePressEvent(self, e) -> None:
        c = self.corner_at(e.pos())
        if c is not None and e.button() == Qt.MouseButton.LeftButton:
            pos0, r0 = self.geometry()
            fixed = pos0 + [r0.topLeft(), r0.topRight(), r0.bottomRight(), r0.bottomLeft()][(c + 2) % 4]
            self._drag = (fixed, (pos0, r0))
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag:
            fixed, (_, r0) = self._drag
            cur = self.mapToParent(e.pos())
            r = QRectF(fixed, cur).normalized()
            if self.keep_aspect and r0.height() > 0 and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                aspect = r0.width() / r0.height()
                w = max(r.width(), r.height() * aspect)
                h = w / aspect
                x = fixed.x() if cur.x() >= fixed.x() else fixed.x() - w
                y = fixed.y() if cur.y() >= fixed.y() else fixed.y() - h
                r = QRectF(x, y, w, h)
            if r.width() >= 4 and r.height() >= 4:
                self.set_geometry((r.topLeft(), QRectF(0, 0, r.width(), r.height())))
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag:
            from .commands import SetGeometry

            old = self._drag[1]
            self._drag = None
            new = self.geometry()
            if old != new:
                _push(self, SetGeometry(self, old, new))
            return
        super().mouseReleaseEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self._drag is None:
            return self._clamp(value)
        return super().itemChange(change, value)


class ShapeItem(BoxItem):
    """rect / ellipse / whiteout"""

    def paint_content(self, p: QPainter) -> None:
        if self.kind == "whiteout":
            p.fillRect(self._r, Qt.GlobalColor.white)
            if not self.isSelected():
                pen = QPen(QColor(0, 0, 0, 40), 1)
                pen.setCosmetic(True)
                pen.setStyle(Qt.PenStyle.DotLine)
                p.setPen(pen)
                p.drawRect(self._r)
            return
        pen = self.pen()
        w = pen.widthF()
        r = self._r.adjusted(w / 2, w / 2, -w / 2, -w / 2)
        p.setPen(pen)
        if self.props.get("fill"):
            c = QColor(self.props["color"])
            c.setAlphaF(0.25 * float(self.props.get("opacity", 1.0)))
            p.setBrush(c)
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "ellipse":
            p.drawEllipse(r)
        else:
            p.drawRect(r)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        r = to_rect(self.scene_rect_in_parent(), m)
        if r.width <= 0 or r.height <= 0:  # dragged flat (straight across): MuPDF refuses an empty annotation rect
            if self.kind == "whiteout":
                return  # covers nothing
            pad = max(0.5, float(self.props.get("width", 1)) / 2)  # keep the stroke the screen shows
            r = pymupdf.Rect(r.x0 - (pad if r.width <= 0 else 0), r.y0 - (pad if r.height <= 0 else 0),
                             r.x1 + (pad if r.width <= 0 else 0), r.y1 + (pad if r.height <= 0 else 0))
        if self.kind == "whiteout":
            page.draw_rect(r, color=None, fill=(1, 1, 1), overlay=True, width=0)
            return
        a = page.add_circle_annot(r) if self.kind == "ellipse" else page.add_rect_annot(r)
        a.set_border(width=float(self.props["width"]))
        fill = None
        if self.props.get("fill"):
            # annotation fill has no separate alpha; blend 25% toward white
            c = pdf_color(self.props["color"])
            fill = tuple(1 - 0.25 * (1 - v) for v in c)
        a.set_colors(stroke=pdf_color(self.props["color"]), fill=fill)
        a.set_opacity(float(self.props.get("opacity", 1.0)))
        self._finish(a)


class ImageItem(BoxItem):
    keep_aspect = True

    def __init__(self, kind: str, rect: QRectF, image: QImage, props: dict | None = None):
        super().__init__(kind, rect, props)
        self.image = image
        self._png: bytes | None = None

    def paint_content(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setOpacity(float(self.props.get("opacity", 1.0)))
        p.drawImage(self._r, self.image)
        p.setOpacity(1.0)

    def png(self) -> bytes:
        if self._png is None:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            self.image.save(buf, "PNG")
            self._png = bytes(ba)
        return self._png

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        r = to_rect(self.scene_rect_in_parent(), m)
        kwargs = dict(stream=self.png(), keep_proportion=False, rotate=rotate, overlay=True)
        op = float(self.props.get("opacity", 1.0))
        if op < 0.999:
            img = self.image.convertToFormat(QImage.Format.Format_ARGB32)
            faded = QImage(img.size(), QImage.Format.Format_ARGB32)
            faded.fill(Qt.GlobalColor.transparent)
            q = QPainter(faded)
            q.setOpacity(op)
            q.drawImage(0, 0, img)
            q.end()
            kwargs["stream"] = ImageItem("image", QRectF(), faded).png()
        page.insert_image(r, **kwargs)


class LineItem(OverlayMixin, QGraphicsObject):
    """line / arrow"""

    def __init__(self, kind: str, p1: QPointF, p2: QPointF, props: dict | None = None):
        QGraphicsObject.__init__(self)
        self._drag = None
        self._init_overlay(kind, props)
        self.setPos(p1)
        self.p1 = QPointF(0, 0)
        self.p2 = p2 - p1
        self._drag = None
        self.setAcceptHoverEvents(True)

    def geometry(self):
        return (QPointF(self.pos()), QPointF(self.p1), QPointF(self.p2))

    def set_geometry(self, geom) -> None:
        self.prepareGeometryChange()
        pos, self.p1, self.p2 = QPointF(geom[0]), QPointF(geom[1]), QPointF(geom[2])
        self.setPos(pos)
        self.update()

    def is_trivial(self) -> bool:
        d = self.p2 - self.p1
        return math.hypot(d.x(), d.y()) < 3

    def drag_create(self, start: QPointF, cur: QPointF, shift: bool) -> None:
        d = cur - start
        if shift:  # snap to 45 degrees
            ang = round(math.atan2(d.y(), d.x()) / (math.pi / 4)) * (math.pi / 4)
            ln = math.hypot(d.x(), d.y())
            d = QPointF(math.cos(ang) * ln, math.sin(ang) * ln)
        self.set_geometry((start, QPointF(0, 0), d))

    def _arrow_head(self) -> QPainterPath:
        d = self.p2 - self.p1
        ang = math.atan2(d.y(), d.x())
        size = 4 + 3 * float(self.props.get("width", 2))
        path = QPainterPath()
        for s in (-1, 1):
            a = ang + math.pi - s * math.radians(28)
            path.moveTo(self.p2)
            path.lineTo(self.p2 + QPointF(math.cos(a) * size, math.sin(a) * size))
        return path

    def _path(self) -> QPainterPath:
        path = QPainterPath(self.p1)
        path.lineTo(self.p2)
        if self.kind == "arrow":
            path.addPath(self._arrow_head())
        return path

    def boundingRect(self) -> QRectF:
        m = 8.0 / _zoom(self) + 4 + 3 * float(self.props.get("width", 2))
        return QRectF(self.p1, self.p2).normalized().adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        st = QPainterPathStroker()
        st.setWidth(max(float(self.props.get("width", 2)), 8.0 / _zoom(self)))
        return st.createStroke(self._path())

    def paint(self, p: QPainter, opt, widget=None) -> None:
        p.setPen(self.pen())
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(self._path())
        if self.isSelected():
            h = 8.0 / _zoom(self)
            _handle(p, self.p1, h)
            _handle(p, self.p2, h)

    def _end_at(self, pos: QPointF):
        if not self.isSelected():
            return None
        h = 8.0 / _zoom(self)
        for i, c in enumerate((self.p1, self.p2)):
            if abs(c.x() - pos.x()) <= h and abs(c.y() - pos.y()) <= h:
                return i
        return None

    def hoverMoveEvent(self, e) -> None:
        if self._end_at(e.pos()) is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        super().hoverMoveEvent(e)

    def mousePressEvent(self, e) -> None:
        i = self._end_at(e.pos())
        if i is not None and e.button() == Qt.MouseButton.LeftButton:
            self._drag = (i, self.geometry())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._drag:
            i = self._drag[0]
            self.prepareGeometryChange()
            if i == 0:
                self.p1 = e.pos()
            else:
                self.p2 = e.pos()
            self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag:
            from .commands import SetGeometry

            old = self._drag[1]
            self._drag = None
            if old != self.geometry():
                _push(self, SetGeometry(self, old, self.geometry()))
            return
        super().mouseReleaseEvent(e)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self._clamp(value)
        return super().itemChange(change, value)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        a = page.add_line_annot(to_pt(self.mapToParent(self.p1), m), to_pt(self.mapToParent(self.p2), m))
        if self.kind == "arrow":
            a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        a.set_border(width=float(self.props["width"]))
        a.set_colors(stroke=pdf_color(self.props["color"]))
        a.set_opacity(float(self.props.get("opacity", 1.0)))
        self._finish(a)


# ---------------------------------------------------------------- pen pressure (variable-width ink)
PRESSURE_MIN, PRESSURE_GAIN = 0.25, 1.25  # width = base * (MIN + GAIN * pressure): 0.6 ~ base, 1.0 = 1.5x base


def pressure_width(base: float, pressure: float) -> float:
    return base * (PRESSURE_MIN + PRESSURE_GAIN * min(max(float(pressure), 0.0), 1.0))


def ink_centerline(pts: list, ws: list) -> list[tuple[float, float, float]]:
    """The smoothed stroke InkItem draws (quadratic curves through the midpoints) sampled as (x, y, width).
    `pts` are (x, y) pairs, `ws` the width at each point."""
    n = len(pts)
    if n == 0:
        return []
    out = [(pts[0][0], pts[0][1], ws[0])]
    if n == 1:
        return out
    sx, sy, sw = pts[0][0], pts[0][1], ws[0]
    for i in range(1, n - 1):  # quad from the previous end, control pts[i], to the midpoint of i and i+1
        cx, cy = pts[i]
        ex, ey = (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2
        ew = (ws[i] + ws[i + 1]) / 2
        steps = max(1, min(8, int((math.hypot(cx - sx, cy - sy) + math.hypot(ex - cx, ey - cy)) / 1.5)))
        for k in range(1, steps + 1):
            t = k / steps
            u = 1 - t
            out.append((u * u * sx + 2 * u * t * cx + t * t * ex, u * u * sy + 2 * u * t * cy + t * t * ey,
                        sw + (ew - sw) * t))
        sx, sy, sw = ex, ey, ew
    out.append((pts[-1][0], pts[-1][1], ws[-1]))
    return out


def _ccw(poly: list) -> list:
    a = 0.0
    for k in range(len(poly)):
        x1, y1 = poly[k - 1]
        x2, y2 = poly[k]
        a += x1 * y2 - x2 * y1
    return poly if a >= 0 else poly[::-1]


def _circle(x: float, y: float, r: float, n: int = 16) -> list:
    return [(x + r * math.cos(2 * math.pi * k / n), y + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def pressure_outline(line: list[tuple[float, float, float]]) -> list[list[tuple[float, float]]]:
    """Variable-width stroke as polygons whose union (non-zero winding; all wound the same way, so overlaps
    never cancel) is the ink: one quad per segment, sharing its ends with the neighbours (the averaged normal)
    so there are no slivers between them, and round caps/joins at the ends, at sharp turns and wherever a quad
    would fold over itself (a tight turn for the width): there the segment's own trapezoid plus circles."""
    pts: list = []
    for x, y, w in line:  # repeated points have no direction
        if pts and abs(x - pts[-1][0]) + abs(y - pts[-1][1]) < 1e-6:
            pts[-1] = (x, y, max(w, pts[-1][2]))
            continue
        pts.append((x, y, w))
    if not pts:
        return []
    polys = [_ccw(_circle(pts[0][0], pts[0][1], pts[0][2] / 2))]
    if len(pts) == 1:
        return polys
    seg = []
    for k in range(1, len(pts)):
        dx, dy = pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]
        d = math.hypot(dx, dy)
        seg.append((-dy / d, dx / d))
    joint: list = [seg[0]]
    for i in range(1, len(pts) - 1):
        a, b = seg[i - 1], seg[i]
        if a[0] * b[0] + a[1] * b[1] >= 0.9:
            mx, my = a[0] + b[0], a[1] + b[1]
            m = math.hypot(mx, my)
            joint.append((mx / m, my / m))
        else:  # turned more than ~25 degrees: round join
            joint.append(None)
            polys.append(_ccw(_circle(pts[i][0], pts[i][1], pts[i][2] / 2)))
    joint.append(seg[-1])
    for k, n in enumerate(seg):
        (x0, y0, w0), (x1, y1, w1) = pts[k], pts[k + 1]
        r0, r1 = w0 / 2, w1 / 2
        na, nb = joint[k] or n, joint[k + 1] or n
        q = [(x0 + na[0] * r0, y0 + na[1] * r0), (x1 + nb[0] * r1, y1 + nb[1] * r1),
             (x1 - nb[0] * r1, y1 - nb[1] * r1), (x0 - na[0] * r0, y0 - na[1] * r0)]
        if not _convex(q):
            q = [(x0 + n[0] * r0, y0 + n[1] * r0), (x1 + n[0] * r1, y1 + n[1] * r1),
                 (x1 - n[0] * r1, y1 - n[1] * r1), (x0 - n[0] * r0, y0 - n[1] * r0)]
            polys.append(_ccw(_circle(x0, y0, r0)))
            polys.append(_ccw(_circle(x1, y1, r1)))
        polys.append(_ccw(q))
    polys.append(_ccw(_circle(pts[-1][0], pts[-1][1], pts[-1][2] / 2)))
    return polys


def _convex(q: list) -> bool:
    sign = 0
    for k in range(len(q)):
        (ax, ay), (bx, by), (cx, cy) = q[k - 2], q[k - 1], q[k]
        c = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if abs(c) < 1e-12:
            continue
        s = 1 if c > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def _pdf_num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def write_pressure_appearance(page: pymupdf.Page, a: pymupdf.Annot, stroke: list, widths: list,
                              color: str) -> None:
    """Replace a new ink annotation's appearance with the variable-width outline. The annotation itself keeps
    one /BS width (the average): apps that regenerate the appearance (e.g. after moving it) draw that width."""
    polys = pressure_outline(ink_centerline(stroke, widths))
    if not polys:
        return
    inv = ~page.transformation_matrix  # MuPDF page space -> PDF user space (the appearance stream's space)
    xs = [x for poly in polys for x, _ in poly]
    ys = [y for poly in polys for _, y in poly]
    r = pymupdf.Rect(min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1) | a.rect
    ops = []
    for poly in polys:
        for k, (x, y) in enumerate(poly):
            q = pymupdf.Point(x, y) * inv
            ops.append(f"{_pdf_num(q.x)} {_pdf_num(q.y)} {'m' if k == 0 else 'l'}")
        ops.append("h")
    try:
        old = bytes(a._getAP()).decode("latin-1")
    except Exception:  # noqa: BLE001
        old = ""
    gs = [ln.strip() for ln in old.splitlines() if ln.strip().endswith(" gs")][:1]  # opacity (/Resources stays)
    cr, cg, cb = pdf_color(color)
    body = "\n".join(["q", *gs, f"{_pdf_num(cr)} {_pdf_num(cg)} {_pdf_num(cb)} rg", *ops, "f", "Q", ""])
    pr = (r * inv).normalize()
    page.parent.xref_set_key(a.xref, "Rect", f"[{_pdf_num(pr.x0)} {_pdf_num(pr.y0)} {_pdf_num(pr.x1)} {_pdf_num(pr.y1)}]")
    a._setAP(body.encode("latin-1"), rect=1)  # BBox = the new /Rect


class InkItem(OverlayMixin, QGraphicsObject):
    def __init__(self, start: QPointF, props: dict | None = None):
        QGraphicsObject.__init__(self)
        self._init_overlay("ink", props)
        self.pts: list[QPointF] = [QPointF(start)]
        self.widths: list[float] = []  # pen pressure (0..1) per point; empty (or out of step) = uniform width
        self._cache: QPainterPath | None = None
        self._ocache: QPainterPath | None = None

    def geometry(self):
        return QPointF(self.pos())

    def set_geometry(self, pos) -> None:
        self.setPos(pos)

    def has_pressure(self) -> bool:
        return bool(self.widths) and len(self.widths) == len(self.pts)

    def add_point(self, p: QPointF, pressure: float | None = None) -> None:
        last = self.pts[-1]
        if abs(p.x() - last.x()) + abs(p.y() - last.y()) < 0.6:
            if pressure is not None and self.has_pressure() and pressure > self.widths[-1]:
                self.widths[-1] = float(pressure)  # the pen presses harder in place
                self._ocache = None
                self.prepareGeometryChange()
            return
        self.prepareGeometryChange()
        if pressure is not None:
            if len(self.widths) != len(self.pts):  # first pressure sample: earlier points get the same pressure
                self.widths = [float(pressure)] * len(self.pts)
            self.widths.append(float(pressure))
        self.pts.append(QPointF(p))
        self._cache = self._ocache = None

    def drag_create(self, start: QPointF, cur: QPointF, shift: bool) -> None:
        self.add_point(cur - self.pos())

    def is_trivial(self) -> bool:
        return False

    def props_changed(self) -> None:
        self._ocache = None

    def point_widths(self) -> list[float]:
        """Stroke width at each point (pen pressure), in page points."""
        base = float(self.props.get("width", 2))
        if not self.has_pressure():
            return [base] * len(self.pts)
        return [pressure_width(base, p) for p in self.widths]

    def _max_width(self) -> float:
        return max(self.point_widths()) if self.has_pressure() else float(self.props.get("width", 2))

    def _path(self) -> QPainterPath:
        if self._cache is None:
            pts = self.pts
            path = QPainterPath(pts[0])
            if len(pts) == 1:
                path.lineTo(pts[0] + QPointF(0.01, 0.01))
            for i in range(1, len(pts) - 1):  # smooth through midpoints
                mid = (pts[i] + pts[i + 1]) / 2
                path.quadTo(pts[i], mid)
            if len(pts) > 1:
                path.lineTo(pts[-1])
            self._cache = path
        return self._cache

    def _outline(self) -> QPainterPath:
        """Filled variable-width outline of a pressure stroke."""
        if self._ocache is None:
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.WindingFill)
            line = ink_centerline([(p.x(), p.y()) for p in self.pts], self.point_widths())
            for poly in pressure_outline(line):
                path.addPolygon(QPolygonF([QPointF(x, y) for x, y in poly]))
                path.closeSubpath()
            self._ocache = path
        return self._ocache

    def boundingRect(self) -> QRectF:
        m = self._max_width() + 2
        return self._path().boundingRect().adjusted(-m, -m, m, m)

    def shape(self) -> QPainterPath:
        st = QPainterPathStroker()
        st.setWidth(max(self._max_width(), 8.0 / _zoom(self)))
        return st.createStroke(self._path())

    def paint(self, p: QPainter, opt, widget=None) -> None:
        if self.has_pressure():
            c = QColor(self.props.get("color", "#000"))
            c.setAlphaF(float(self.props.get("opacity", 1.0)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawPath(self._outline())
        else:
            p.setPen(self.pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(self._path())
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._path().boundingRect())

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self._clamp(value)
        return super().itemChange(change, value)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        stroke = [tuple(to_pt(self.mapToParent(pt), m)) for pt in self.pts]
        widths = self.point_widths()
        if len(stroke) == 1:
            stroke.append((stroke[0][0] + 0.1, stroke[0][1] + 0.1))
            widths.append(widths[0])
        a = page.add_ink_annot([stroke])
        a.set_border(width=round(sum(widths) / len(widths), 2))  # with pen pressure: the average width
        a.set_colors(stroke=pdf_color(self.props["color"]))
        a.set_opacity(float(self.props.get("opacity", 1.0)))
        self._finish(a)
        if self.has_pressure():
            try:
                write_pressure_appearance(page, a, stroke, widths, self.props["color"])
            except Exception:  # noqa: BLE001 - the uniform-width appearance MuPDF made stays
                pass


class HighlightItem(OverlayMixin, QGraphicsObject):
    def __init__(self, rects: list[QRectF], props: dict | None = None):
        QGraphicsObject.__init__(self)
        self._init_overlay("highlight", props)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.rects = [QRectF(r) for r in rects]
        self.marked_text = ""  # the highlighted words (shown in the Comments panel, saved as contents)
        self.setZValue(Z_OVERLAY - 1)

    def geometry(self):
        return QPointF(self.pos())

    def set_geometry(self, pos) -> None:
        self.setPos(pos)

    def boundingRect(self) -> QRectF:
        br = QRectF()
        for r in self.rects:
            br = br.united(r)
        return br.adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        for r in self.rects:
            path.addRect(r)
        return path

    def paint(self, p: QPainter, opt, widget=None) -> None:
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.props["color"]))
        for r in self.rects:
            p.drawRect(r)
        p.restore()
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            for r in self.rects:
                p.drawRect(r)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        quads = [to_rect(self.mapRectToParent(r), m).quad for r in self.rects]
        a = page.add_highlight_annot(quads=quads)
        a.set_colors(stroke=pdf_color(self.props["color"]))
        self._finish(a)


def draw_note_icon(p: QPainter, r: QRectF, color: QColor) -> None:
    """A sticky-note glyph filling `r`: a speech bubble with text lines."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = r.width(), r.height()
    edge = QColor(color).darker(170)
    pen = QPen(edge, max(0.6, w / 22))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(color)
    path = QPainterPath()
    body = QRectF(r.left() + w * 0.06, r.top() + h * 0.08, w * 0.88, h * 0.66)
    path.addRoundedRect(body, w * 0.12, w * 0.12)
    tail = QPainterPath(QPointF(body.left() + w * 0.22, body.bottom() - 0.5))
    tail.lineTo(QPointF(body.left() + w * 0.16, r.bottom() - h * 0.06))
    tail.lineTo(QPointF(body.left() + w * 0.46, body.bottom() - 0.5))
    path = path.united(tail)
    p.drawPath(path)
    for k in range(3):
        y = body.top() + body.height() * (0.3 + 0.2 * k)
        p.drawLine(QPointF(body.left() + w * 0.16, y), QPointF(body.right() - w * (0.16 if k < 2 else 0.36), y))
    p.restore()


class NoteItem(BoxItem):
    """Sticky note (PDF Text annotation): a fixed-size icon carrying a comment. Moves, never resizes."""

    SIZE = 20.0

    def __init__(self, pos: QPointF, props: dict | None = None, meta: dict | None = None):
        super().__init__("note", QRectF(pos, QSizeF(self.SIZE, self.SIZE)), props)
        self.meta = dict(meta or {})

    def corner_at(self, pos: QPointF):
        return None

    def paint_content(self, p: QPainter) -> None:
        draw_note_icon(p, self._r, QColor(self.props.get("color", "#ffd83d")))

    def paint(self, p: QPainter, opt, widget=None) -> None:
        self.paint_content(p)
        if self.isSelected():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._r)

    def clone(self) -> "NoteItem":
        c = NoteItem(QPointF(self.scene_rect_in_parent().topLeft()), dict(self.props), dict(self.meta))
        c.meta.pop("created", None)
        c.setZValue(self.zValue())
        return c

    def clone_overlay(self) -> "NoteItem":
        return self.clone()

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        # Text annotations don't rotate with the page: pin the icon's top-left corner where it is shown
        pt = to_pt(self.scene_rect_in_parent().topLeft(), m)
        a = page.add_text_annot(pt, self.meta.get("contents") or "", icon=self.meta.get("icon") or "Note")
        a.set_colors(stroke=pdf_color(self.props.get("color", "#ffd83d")))
        self._finish(a)


class ReplyItem(OverlayMixin, QGraphicsObject):
    """A reply to an annotation (Text annotation with /IRT). Never drawn: it lives in the Comments panel."""

    def __init__(self, parent_annot, meta: dict | None = None):
        QGraphicsObject.__init__(self)
        self._init_overlay("reply", {})
        self.setFlags(QGraphicsItem.GraphicsItemFlag(0))
        self.setVisible(False)
        self.setZValue(Z_OVERLAY + 50)  # applied after the annotations it answers
        self.target = parent_annot
        self.meta = dict(meta or {})

    def geometry(self):
        return QPointF(self.pos())

    def set_geometry(self, pos) -> None:
        self.setPos(pos)

    def boundingRect(self) -> QRectF:
        return QRectF()

    def paint(self, p, opt, widget=None) -> None:
        pass

    def clone_overlay(self):
        return None

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        px = getattr(self.target, "xref", 0) or getattr(self.target, "saved_xref", 0)
        if not px or (self.target.parentItem() is None and not getattr(self.target, "xref", 0)):
            return  # the annotation it answers is gone
        try:
            parent = page.load_annot(px)
        except Exception:  # noqa: BLE001
            return
        if parent is None:
            return
        pr = parent.rect
        a = page.add_text_annot(pr.tl, self.meta.get("contents") or "", icon="Comment")
        a.set_irt_xref(px)
        write_meta(a, self.meta)
        a.update()
        hide_appearance(a)
        self.saved_xref = a.xref


def hide_appearance(a: pymupdf.Annot) -> None:
    """Give an annotation an empty appearance: still a comment in every viewer's list, never drawn on the page
    (replies sit on top of their parent otherwise)."""
    doc = a.parent.parent
    x = doc.get_new_xref()
    doc.update_object(x, "<</Type/XObject/Subtype/Form/BBox[0 0 1 1]>>")
    doc.update_stream(x, b" ", new=True)
    doc.xref_set_key(a.xref, "AP", f"<</N {x} 0 R>>")


class TextItem(OverlayMixin, QGraphicsTextItem):
    """A formatted text box. Box style is in props (font, size, color, bold/italic/underline/strike, align, spacing,
    bg, border, pad, box_w = fixed wrap width or 0 for auto); per-run formatting lives in the QTextDocument as
    overrides of it (see richtext). Saved with insert_htmlbox, line by line as laid out on screen."""
    font_layer = True  # saved through document._flush_font_layer (fonts subset)

    MARGIN = 2.0
    BORDER_W = 0.75
    KEEP_EDITING = "leafKeepsTextEditing"  # widget property: taking focus there doesn't end editing

    def __init__(self, pos: QPointF, props: dict | None = None, text: str = ""):
        QGraphicsTextItem.__init__(self)
        self._memo: dict = {}
        self._wdrag = None
        self._init_overlay("text", props)
        self._sanitize()
        self.setAcceptHoverEvents(True)
        self.document().setDocumentMargin(float(self.props["pad"]))
        self.document().contentsChanged.connect(self._contents_changed)
        richtext.deserialize(self.document(), text)
        self.setPos(pos)
        self.committed = bool(text)
        self._edit_start = self.content()
        self.props_changed()

    # ----------------------------------------------------------- props/content
    def _sanitize(self) -> None:
        p = self.props
        if not p.get("font") or p["font"] == "None":
            p["font"] = fonts.DEFAULT
        for k in ("bg", "border"):
            v = p.get(k)
            p[k] = QColor(v).name() if isinstance(v, str) and v not in ("", "None", "none") and QColor(v).isValid() else ""
        if p.get("align") not in richtext.ALIGNS:
            p["align"] = "left"
        for k, dflt, lo, hi in (("spacing", 1.0, 0.5, 4.0), ("pad", self.MARGIN, 0.0, 72.0), ("box_w", 0.0, 0.0, 5000.0)):
            try:
                p[k] = min(max(float(p.get(k, dflt)), lo), hi)
            except (TypeError, ValueError):
                p[k] = dflt
        for k in ("bold", "italic", "underline", "strike"):
            p[k] = p.get(k) in (True, "true", "1", 1)

    def set_props(self, props: dict) -> None:
        """Box style change. A character prop set on the whole box drops the runs' overrides of it; undoing restores
        them (memo of the content before, keyed by the props it had)."""
        old, new = self.props, dict(props)
        doc = self.document()
        key = lambda d: tuple(sorted((k, repr(v)) for k, v in d.items()))  # noqa: E731
        hit = self._memo.get(key(new))
        restore = hit is not None and hit[0] == self.content()
        changed = [k for k in richtext.CHAR_KEYS if old.get(k) != new.get(k)]
        prev = self.content() if not restore and changed and richtext.has_overrides(doc, changed) else None
        self.prepareGeometryChange()
        self.props = new
        self._sanitize()
        if restore:
            richtext.deserialize(doc, hit[1])
        elif prev is not None:
            richtext.clear_overrides(doc, changed)
            self._memo[key(old)] = (self.content(), prev)
        self.props_changed()
        self.update()

    def props_changed(self) -> None:
        p = self.props
        doc = self.document()
        self.setFont(richtext.base_font(p))
        self.setDefaultTextColor(QColor(p.get("color", "#000")))
        doc.setDocumentMargin(float(p["pad"]))
        opt = doc.defaultTextOption()
        opt.setAlignment(richtext.qt_alignment(p["align"]))
        doc.setDefaultTextOption(opt)
        richtext.set_spacing(doc, p["spacing"])
        self._fit()

    def content(self) -> str:
        """Text with per-run formatting (plain text when there is none); round-trips through set_content."""
        return richtext.serialize(self.document())

    def set_content(self, data: str) -> None:
        richtext.deserialize(self.document(), data)
        richtext.set_spacing(self.document(), self.props["spacing"])
        self._fit()

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        richtext.set_spacing(self.document(), self.props["spacing"])

    def clone(self) -> "TextItem":
        c = TextItem(QPointF(self.pos()), dict(self.props), self.content())
        c.setZValue(self.zValue())
        return c

    def _fit(self) -> None:
        """Fixed width wraps; auto width follows the text (sized to it when aligned, so centering has a frame)."""
        w = float(self.props.get("box_w") or 0)
        if w > 0:
            if abs(self.textWidth() - w) > 1e-6:
                self.setTextWidth(w)
        elif self.props.get("align", "left") == "left":
            if self.textWidth() != -1:
                self.setTextWidth(-1)
        else:
            self.setTextWidth(-1)
            self.setTextWidth(math.ceil(self.document().idealWidth() * 4) / 4 + 0.25)

    def _contents_changed(self) -> None:
        if not float(self.props.get("box_w") or 0) and self.props.get("align", "left") != "left":
            self._fit()

    # ------------------------------------------------------------- formatting
    def cursor_props(self) -> dict:
        """Box props with the character props at the text cursor (what the style UI shows while editing)."""
        d = dict(self.props)
        d.update(richtext.cursor_props(self.textCursor(), self.props))
        return d

    def format_selection(self, delta: dict) -> dict:
        """While editing: character props go to the selection (or the typing format), recorded in the document's own
        undo. Returns the remaining (box) props."""
        chars = {k: v for k, v in delta.items() if k in richtext.CHAR_KEYS}
        if chars:
            c = self.textCursor()
            richtext.format_cursor(c, chars, self.props)
            self.setTextCursor(c)
            self._refocus()
            self.update()
        return {k: v for k, v in delta.items() if k not in richtext.CHAR_KEYS}

    def _refocus(self) -> None:
        from PySide6.QtWidgets import QAbstractSpinBox, QApplication

        if self.hasFocus():
            return
        fw = QApplication.focusWidget()
        if fw is not None and (isinstance(fw, QAbstractSpinBox) or isinstance(fw.parentWidget(), QAbstractSpinBox)):
            return  # the user is typing a value; keep the field
        s = self.scene()
        if s is not None and s.views():
            s.views()[0].setFocus(Qt.FocusReason.OtherFocusReason)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _notify(self) -> None:
        s = self.scene()
        cv = getattr(s, "canvas", None) if s is not None else None
        if cv is not None:
            cv.selectionInfo.emit()

    # ---------------------------------------------------------------- geometry
    def geometry(self):
        return QPointF(self.pos())

    def set_geometry(self, pos) -> None:
        self.setPos(pos)

    def box_rect(self) -> QRectF:
        return QGraphicsTextItem.boundingRect(self)

    def _handle_rect(self) -> QRectF:
        r = self.box_rect()
        h = 8.0 / _zoom(self)
        return QRectF(r.right() - h * 0.35, r.center().y() - h * 0.9, h * 0.7, h * 1.8)

    def _on_handle(self, pos: QPointF) -> bool:
        if not self.isSelected():
            return False
        h = 4.0 / _zoom(self)
        return self._handle_rect().adjusted(-h, -h, h, h).contains(pos)

    def boundingRect(self) -> QRectF:
        r = self.box_rect()
        if self.isSelected():
            return r.united(self._handle_rect()).adjusted(-1, -1, 1, 1)
        return r

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    # ------------------------------------------------------------- interaction
    def editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def start_edit(self) -> None:
        self._edit_start = self.content()
        self.document().clearUndoRedoStacks()
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        c = self.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(c)

    def hoverMoveEvent(self, e) -> None:
        super().hoverMoveEvent(e)
        if self._on_handle(e.pos()):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif self.editing():
            self.setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._on_handle(e.pos()):
            self._wdrag = (dict(self.props), e.pos().x(), self.box_rect().width())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._wdrag:
            _, x0, w0 = self._wdrag
            lo = max(2 * float(self.props["pad"]) + 8, float(self.props.get("size", 12)) * 1.5)
            self.prepareGeometryChange()
            self.setTextWidth(max(lo, w0 + e.pos().x() - x0))
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._wdrag:
            old = self._wdrag[0]
            self._wdrag = None
            self._set_box_w(old, round(self.textWidth(), 2))
            return
        super().mouseReleaseEvent(e)
        if self.editing():
            self._notify()

    def _set_box_w(self, old: dict, w: float) -> None:
        from .commands import SetProps

        new = dict(old)
        new["box_w"] = w
        if new == old:
            self._fit()
            return
        s = self.scene()
        if s is not None and getattr(s, "canvas", None) is not None:
            s.canvas.undo.push(SetProps({self: (old, new)}, "Resize text box" if w else "Auto width"))
        else:
            self.set_props(new)

    def mouseDoubleClickEvent(self, e) -> None:
        if self._on_handle(e.pos()):  # double-click the width handle: back to auto width
            self._set_box_w(dict(self.props), 0.0)
            e.accept()
            return
        if not self.editing():
            self.start_edit()
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e) -> None:
        from PySide6.QtGui import QGuiApplication, QKeySequence

        if e.key() == Qt.Key.Key_Escape:
            self.clearFocus()
            return
        if e.matches(QKeySequence.StandardKey.Paste):  # plain text only: no foreign fonts/colors from the clipboard
            t = QGuiApplication.clipboard().text()
            if t:
                c = self.textCursor()
                c.insertText(t.replace("\r\n", "\n"))
                self.setTextCursor(c)
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e) -> None:
        super().keyReleaseEvent(e)
        if self.editing():
            self._notify()

    def _keeps_editing(self, reason) -> bool:
        from PySide6.QtWidgets import QApplication

        if reason == Qt.FocusReason.PopupFocusReason:
            return True
        w = QApplication.focusWidget()
        while w is not None:
            if w.property(self.KEEP_EDITING):
                return True
            w = w.parentWidget()
        return False

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        if not self.editing() or self._keeps_editing(e.reason()):
            return  # e.g. the font box in the style panel took focus: stay in edit mode, keep the selection
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        s = self.scene()
        if s is not None and getattr(s, "canvas", None):
            s.canvas.text_edit_finished(self, self._edit_start)

    def paint(self, p: QPainter, opt, widget=None) -> None:
        from PySide6.QtWidgets import QStyle

        r = self.box_rect()
        if self.props.get("bg"):
            p.fillRect(r, QColor(self.props["bg"]))
        if self.props.get("border"):
            bw = self.BORDER_W
            pen = QPen(QColor(self.props["border"]), bw)
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2))
        opt.state &= ~QStyle.StateFlag.State_Selected  # we draw our own selection
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(p, opt, widget)
        if self.isSelected() or self.editing():
            p.setPen(self._sel_pen())
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
        if self.isSelected():
            pen = QPen(ACCENT, 1.25)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(QBrush(Qt.GlobalColor.white))
            hr = self._handle_rect()
            p.drawRoundedRect(hr, hr.width() / 2, hr.width() / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self._clamp(value)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            self.prepareGeometryChange()  # the width handle is part of the bounds while selected
        return super().itemChange(change, value)

    # -------------------------------------------------------------------- save
    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        if not self.toPlainText().strip():
            return
        p = self.props
        box = self.mapRectToParent(self.box_rect())
        if p.get("bg"):
            page.draw_rect(to_rect(box, m), color=None, fill=pdf_color(p["bg"]), width=0, overlay=True)
        if p.get("border"):
            bw = self.BORDER_W
            page.draw_rect(to_rect(box.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2), m), color=pdf_color(p["border"]),
                           fill=None, width=bw, overlay=True)
        emb = fonts.Embedder()
        out = richtext.export(self.document(), p, emb)
        if out is None:
            return
        cx, cy = richtext.calibration()
        left, right = out["left"], out["right"]
        # generous slack on the side(s) alignment doesn't anchor, so MuPDF never wraps or shrinks a line
        slack = 3 * out["max_size"] + 0.1 * (right - left)
        if out["align"] == "right":
            x0, x1 = left - slack, right + cx
        elif out["align"] == "center":
            x0, x1 = left - slack / 2 - cx, right + slack / 2 + cx
        elif out["align"] == "justify":  # justified paragraphs need the exact width
            x0, x1 = left - cx, right + cx + 0.3 + 0.001 * (right - left)
        else:
            x0, x1 = left - cx, right + slack
        local = QRectF(QPointF(x0, out["top"] - cy), QPointF(x1, out["bottom"] + 2 * out["max_size"] + 10))
        page.insert_htmlbox(to_rect(self.mapRectToParent(local), m), out["html"], css=out["css"], rotate=rotate,
                            archive=emb.archive)


def editing_text_item(scene) -> TextItem | None:
    """The text box in edit mode: the focused one, or a selected one whose editing is parked while a style widget
    has focus."""
    if scene is None:
        return None
    fi = scene.focusItem()
    if isinstance(fi, TextItem) and fi.editing():
        return fi
    return next((i for i in scene.selectedItems() if isinstance(i, TextItem) and i.editing()), None)


OVERLAY_TYPES = (BoxItem, LineItem, InkItem, HighlightItem, TextItem, ReplyItem)


def is_overlay(item) -> bool:
    return isinstance(item, OVERLAY_TYPES)
