"""Small monochrome vector icons, drawn at runtime so they follow the palette (light/dark).

Icons are painted on demand at the exact device pixel size (crisp at 125/150/175% scaling).
Names: open save undo redo select text highlight ink rect ellipse line arrow whiteout image signature
rotate_left rotate_right delete find zoom_in zoom_out sidebar props page_add fit_width fit_page chevron_up
chevron_down chevron_right close shapes history outline palette form_field night day fullscreen
fullscreen_exit organizer duplicate copy more plus minus check leaf recent opacity stroke text_size
pages info warning bookmark extract merge print lock.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QIcon, QIconEngine, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import QApplication

STROKE = 1.6  # in the 24-unit grid; ~1.3 px at the 20 px toolbar size, scales with DPI
STROKE_PX: float | None = None  # set by the style layer: the same line width in logical px at every icon size
CHECKED: QColor | None = None  # set by the style layer: palette-colored icons of checked buttons (QIcon.On)


def configure(stroke_px: float | None, checked: QColor | None) -> None:
    """Style layer: a constant stroke in logical pixels (16, 20 and 24 px icons keep the same optical weight) and
    a color for checked tools, so the selected one reads at a glance. Icons are made again on the next icon()."""
    global STROKE_PX, CHECKED
    STROKE_PX, CHECKED = stroke_px, QColor(checked) if checked is not None else None
    _cache.clear()
    for e in _engines:
        e._pm.clear()


def _p(*pts) -> QPainterPath:
    path = QPainterPath(QPointF(*pts[0]))
    for pt in pts[1:]:
        path.lineTo(QPointF(*pt))
    return path


def _leaf_path() -> QPainterPath:
    path = QPainterPath(QPointF(4.5, 19.5))
    path.cubicTo(3, 10, 9, 3.5, 20, 4)
    path.cubicTo(20.5, 15, 14, 21, 4.5, 19.5)
    return path


def _draw(name: str, p: QPainter) -> None:  # 24x24 canvas
    L = lambda x1, y1, x2, y2: p.drawLine(QPointF(x1, y1), QPointF(x2, y2))  # noqa: E731
    if name == "open":
        p.drawPath(_p((3, 18.5), (3, 6), (9, 6), (11, 8.5), (19, 8.5), (19, 11)))
        p.drawPath(_p((3, 18.5), (6, 11), (21.5, 11), (18.5, 18.5), (3, 18.5)))
    elif name == "save":
        p.drawPath(_p((4, 5.5), (4, 18.5), (5.5, 20), (18.5, 20), (20, 18.5), (20, 8), (16, 4), (5.5, 4), (4, 5.5)))
        p.drawPath(_p((8, 4), (8, 8.5), (15, 8.5), (15, 4)))
        p.drawPath(_p((7.5, 20), (7.5, 14), (16.5, 14), (16.5, 20)))
    elif name in ("undo", "redo"):
        if name == "redo":
            p.translate(24, 0)
            p.scale(-1, 1)
        path = QPainterPath(QPointF(5, 10))
        path.lineTo(15, 10)
        path.cubicTo(21.5, 10, 21.5, 19.5, 15, 19.5)
        path.lineTo(10, 19.5)
        p.drawPath(path)
        p.drawPath(_p((9, 6), (5, 10), (9, 14)))
    elif name == "select":
        p.drawPath(_p((6, 3.5), (6, 19), (10, 15), (12.8, 20.6), (15.2, 19.5), (12.5, 14), (18, 14), (6, 3.5)))
    elif name == "text":
        p.drawPath(_p((5, 7), (5, 4.5), (19, 4.5), (19, 7)))
        L(12, 4.5, 12, 19.5)
        L(9, 19.5, 15, 19.5)
    elif name == "text_size":
        p.drawPath(_p((3, 7), (3, 5), (13, 5), (13, 7)))
        L(8, 5, 8, 19)
        p.drawPath(_p((14, 12), (14, 11), (21, 11), (21, 12)))
        L(17.5, 11, 17.5, 19)
    elif name == "highlight":
        p.drawPath(_p((14, 3.5), (20, 9.5), (11.5, 18), (6.5, 18), (5.5, 17), (5.5, 12), (14, 3.5)))
        L(9, 9, 15, 15)
        L(3, 21, 21, 21)
    elif name == "ink":
        path = QPainterPath(QPointF(3, 17))
        path.cubicTo(6, 9, 9, 9, 10, 14)
        path.cubicTo(11, 19, 14, 19, 16, 12)
        path.cubicTo(17, 8, 19, 7, 21, 9)
        p.drawPath(path)
    elif name == "rect":
        p.drawRoundedRect(QRectF(4, 6, 16, 12), 1.5, 1.5)
    elif name == "ellipse":
        p.drawEllipse(QRectF(3.5, 6, 17, 12))
    elif name == "line":
        L(4.5, 19.5, 19.5, 4.5)
    elif name == "arrow":
        L(4.5, 19.5, 19.5, 4.5)
        p.drawPath(_p((11, 4.5), (19.5, 4.5), (19.5, 13)))
    elif name == "shapes":
        p.drawRoundedRect(QRectF(3, 3, 11, 11), 1.5, 1.5)
        path = QPainterPath()
        path.addEllipse(QRectF(9, 9, 12, 12))
        clip = QPainterPath()
        clip.addRect(QRectF(0, 0, 24, 24))
        sq = QPainterPath()
        sq.addRect(QRectF(3, 3, 11, 11))
        p.save()
        p.setClipPath(clip.subtracted(sq))
        p.drawPath(path)
        p.restore()
    elif name == "whiteout":
        p.drawPath(_p((8, 20), (3, 15), (13, 5), (21, 13), (14, 20), (8, 20)))
        L(8, 10, 16, 18)
        L(14, 20, 21, 20)
    elif name == "redact":  # text lines with one blacked-out bar (redaction)
        L(4, 6, 20, 6)
        p.fillRect(QRectF(4, 10, 11, 4), p.pen().color())
        L(17.5, 12, 20, 12)
        L(4, 18, 14, 18)
    elif name == "image":
        p.drawRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        p.drawPath(_p((3, 17), (9, 11), (14, 16), (16, 14), (21, 18)))
        p.drawEllipse(QRectF(14, 7.5, 3, 3))
    elif name == "signature":
        path = QPainterPath(QPointF(3, 15))
        path.cubicTo(5, 5, 9, 5, 8, 12)
        path.cubicTo(7, 18, 11, 16, 13, 12)
        path.cubicTo(14, 15, 16, 15, 18, 12)
        p.drawPath(path)
        L(3, 20, 21, 20)
    elif name in ("rotate_left", "rotate_right"):
        if name == "rotate_right":
            p.translate(24, 0)
            p.scale(-1, 1)
        path = QPainterPath()
        path.arcMoveTo(QRectF(5, 5, 14, 14), 140)
        path.arcTo(QRectF(5, 5, 14, 14), 140, -280)
        p.drawPath(path)
        p.drawPath(_p((5, 4), (6, 9), (11, 8)))
    elif name == "delete":
        L(4, 6.5, 20, 6.5)
        p.drawPath(_p((9, 6.5), (9, 3.5), (15, 3.5), (15, 6.5)))
        p.drawPath(_p((6, 6.5), (7, 20.5), (17, 20.5), (18, 6.5)))
        L(10, 10.5, 10, 16.5)
        L(14, 10.5, 14, 16.5)
    elif name in ("find", "zoom_in", "zoom_out"):
        p.drawEllipse(QRectF(4, 4, 12, 12))
        L(14.5, 14.5, 20, 20)
        if name != "find":
            L(7, 10, 13, 10)
        if name == "zoom_in":
            L(10, 7, 10, 13)
    elif name == "sidebar":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2, 2)
        L(9, 4.5, 9, 19.5)
    elif name == "props":
        for y, x in ((7, 15), (12, 8), (17, 13)):
            L(3.5, y, x - 2, y)
            L(x + 2, y, 20.5, y)
            p.drawEllipse(QPointF(x, y), 2, 2)
    elif name == "pages":
        p.drawRoundedRect(QRectF(7, 3, 12, 15), 1.5, 1.5)
        p.drawPath(_p((5, 6.5), (5, 19.5), (6.5, 21), (15, 21)))
    elif name == "page_add":
        p.drawPath(_p((5, 3), (14, 3), (19, 8), (19, 21), (5, 21), (5, 3)))
        L(9, 14, 15, 14)
        L(12, 11, 12, 17)
    elif name == "extract":
        p.drawPath(_p((13, 3), (5, 3), (5, 21), (19, 21), (19, 13)))
        L(11, 13, 20, 4)
        p.drawPath(_p((14, 4), (20, 4), (20, 10)))
    elif name == "merge":
        p.drawPath(_p((6, 3), (6, 9), (12, 14), (12, 21)))
        p.drawPath(_p((18, 3), (18, 9), (12, 14)))
        p.drawPath(_p((9, 18), (12, 21), (15, 18)))
    elif name == "fit_width":
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2, 2)
        p.drawPath(_p((8, 9.5), (5.5, 12), (8, 14.5)))
        p.drawPath(_p((16, 9.5), (18.5, 12), (16, 14.5)))
        L(5.5, 12, 18.5, 12)
    elif name == "fit_page":
        p.drawRoundedRect(QRectF(5, 3, 14, 18), 2, 2)
        p.drawPath(_p((9.5, 8), (12, 5.5), (14.5, 8)))
        p.drawPath(_p((9.5, 16), (12, 18.5), (14.5, 16)))
        L(12, 5.5, 12, 18.5)
    elif name == "chevron_up":
        p.drawPath(_p((6, 15), (12, 9), (18, 15)))
    elif name == "chevron_down":
        p.drawPath(_p((6, 9), (12, 15), (18, 9)))
    elif name == "chevron_right":
        p.drawPath(_p((9, 6), (15, 12), (9, 18)))
    elif name == "close":
        L(6, 6, 18, 18)
        L(18, 6, 6, 18)
    elif name in ("history", "recent"):
        path = QPainterPath()
        path.arcMoveTo(QRectF(4, 4, 16, 16), 200)
        path.arcTo(QRectF(4, 4, 16, 16), 200, -330)
        p.drawPath(path)
        p.drawPath(_p((3, 8.5), (4.4, 12.3), (8, 10.8)))
        p.drawPath(_p((12, 8), (12, 12), (15, 14)))
    elif name == "outline":
        for y, x in ((6, 4), (12, 8), (18, 8)):
            p.drawEllipse(QPointF(x, y), 0.6, 0.6)
            L(x + 3.5, y, 20.5, y)
    elif name == "bookmark":
        p.drawPath(_p((6.5, 3.5), (17.5, 3.5), (17.5, 20.5), (12, 16), (6.5, 20.5), (6.5, 3.5)))
    elif name == "palette":  # command palette
        p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2.5, 2.5)
        p.drawPath(_p((7, 9.5), (10, 12), (7, 14.5)))
        L(12, 15, 17, 15)
    elif name == "form_field":
        p.drawRoundedRect(QRectF(2.5, 7, 19, 10), 2, 2)
        L(7, 9.5, 7, 14.5)
        L(5.8, 9.5, 8.2, 9.5)
        L(5.8, 14.5, 8.2, 14.5)
    elif name == "night":
        path = QPainterPath(QPointF(19.5, 14.5))
        path.cubicTo(16, 20, 6.5, 18.5, 5, 12)
        path.cubicTo(4, 7.5, 7, 4.5, 10, 4)
        path.cubicTo(8, 9, 12, 16, 19.5, 14.5)
        p.drawPath(path)
    elif name == "day":
        p.drawEllipse(QPointF(12, 12), 4, 4)

        for k in range(8):
            a = k * math.pi / 4
            L(12 + 6.5 * math.cos(a), 12 + 6.5 * math.sin(a), 12 + 8.5 * math.cos(a), 12 + 8.5 * math.sin(a))
    elif name in ("fullscreen", "fullscreen_exit"):
        corners = [((3.5, 9), (3.5, 3.5), (9, 3.5)), ((15, 3.5), (20.5, 3.5), (20.5, 9)),
                   ((20.5, 15), (20.5, 20.5), (15, 20.5)), ((9, 20.5), (3.5, 20.5), (3.5, 15))]
        if name == "fullscreen_exit":
            corners = [((3.5, 9), (9, 9), (9, 3.5)), ((15, 3.5), (15, 9), (20.5, 9)),
                       ((20.5, 15), (15, 15), (15, 20.5)), ((9, 20.5), (9, 15), (3.5, 15))]
        for c in corners:
            p.drawPath(_p(*c))
    elif name == "organizer":
        for x, y in ((3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)):
            p.drawRoundedRect(QRectF(x, y, 7, 7), 1.2, 1.2)
    elif name in ("duplicate", "copy"):
        p.drawRoundedRect(QRectF(8, 8, 12, 12), 2, 2)
        p.drawPath(_p((16, 5.5), (16, 5), (15, 4), (5, 4), (4, 5), (4, 15), (5, 16), (5.5, 16)))
        if name == "duplicate":
            L(14, 11, 14, 17)
            L(11, 14, 17, 14)
    elif name == "more":
        b = p.brush()
        p.setBrush(p.pen().color())
        for x in (6, 12, 18):
            p.drawEllipse(QPointF(x, 12), 0.9, 0.9)
        p.setBrush(b)
    elif name == "plus":
        L(12, 5, 12, 19)
        L(5, 12, 19, 12)
    elif name == "minus":
        L(5, 12, 19, 12)
    elif name == "check":
        p.drawPath(_p((5, 12.5), (10, 17.5), (19, 7)))
    elif name == "note":  # sticky note: page with a folded corner and text lines
        p.drawPath(_p((5, 4), (19, 4), (19, 14.5), (13.5, 20), (5, 20), (5, 4)))
        p.drawPath(_p((19, 14.5), (13.5, 14.5), (13.5, 20)))
        L(8, 8.5, 16, 8.5)
        L(8, 11.5, 14, 11.5)
    elif name == "comment":  # speech bubble
        path = QPainterPath()
        path.addRoundedRect(QRectF(3.5, 4.5, 17, 11.5), 3, 3)
        p.drawPath(path.united(_p((7.5, 15.5), (6.5, 20), (12, 15.5), (7.5, 15.5))))
        L(7.5, 8.5, 16.5, 8.5)
        L(7.5, 12, 13.5, 12)
    elif name == "leaf":
        p.drawPath(_leaf_path())
        L(4.5, 19.5, 14, 10)
    elif name == "opacity":
        p.drawEllipse(QRectF(4, 4, 16, 16))
        half = QPainterPath()
        half.moveTo(12, 4)
        half.arcTo(QRectF(4, 4, 16, 16), 90, -180)
        half.closeSubpath()
        p.fillPath(half, p.pen().color())
    elif name == "stroke":
        c = p.pen()
        for y, w in ((6, 1.0), (12, 2.2), (18, 3.6)):
            pen = QPen(c)
            pen.setWidthF(w)
            p.setPen(pen)
            L(4, y, 20, y)
        p.setPen(c)
    elif name == "lock":
        p.drawRoundedRect(QRectF(5, 10.5, 14, 10), 1.5, 1.5)
        L(8, 10.5, 8, 7.5)
        L(16, 7.5, 16, 10.5)
        p.drawArc(QRectF(8, 3.5, 8, 8), 0, 180 * 16)
        L(12, 14.5, 12, 16.5)
    elif name == "print":
        p.drawPath(_p((7, 9), (7, 3.5), (17, 3.5), (17, 9)))
        p.drawPath(_p((7, 17), (4, 17), (4, 9), (20, 9), (20, 17), (17, 17)))
        p.drawRect(QRectF(7, 13.5, 10, 7))
    elif name.startswith("align_"):  # align_left / align_center / align_right / align_justify
        for i, w in enumerate((16, 11, 16, 9)):
            if name == "align_justify":
                w = 16 if i < 3 else 9
            x = {"align_center": 12 - w / 2, "align_right": 20 - w}.get(name, 4)
            L(x, 6 + 4 * i, x + w, 6 + 4 * i)
    elif name == "fill":  # paint bucket
        p.drawPath(_p((11, 3.5), (19, 11.5), (12, 18.5), (4, 10.5), (11, 3.5)))
        L(4.5, 11, 18.5, 11)
        path = QPainterPath(QPointF(20, 14))
        path.cubicTo(18.5, 16.5, 18.5, 18.5, 20, 18.5)
        path.cubicTo(21.5, 18.5, 21.5, 16.5, 20, 14)
        p.drawPath(path)
    elif name in ("info", "warning"):
        if name == "info":
            p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
            L(12, 11, 12, 16.5)
            p.drawEllipse(QPointF(12, 7.8), 0.4, 0.4)
        else:
            p.drawPath(_p((12, 3.5), (21, 19.5), (3, 19.5), (12, 3.5)))
            L(12, 9.5, 12, 14)
            p.drawEllipse(QPointF(12, 16.8), 0.4, 0.4)


class _Engine(QIconEngine):
    """Paints the vector icon at whatever size/DPR Qt asks for and caches the result."""

    def __init__(self, name: str, color: QColor, disabled: QColor, auto: bool = False):
        super().__init__()
        self.name, self.color, self.disabled = name, QColor(color), QColor(disabled)
        self.auto = auto  # palette-colored (no explicit color): checked buttons may show it in CHECKED
        self._pm: dict[tuple, QPixmap] = {}
        self._dpr = 1.0

    def paint(self, p: QPainter, rect: QRect, mode, state) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(rect.width(), rect.height()) / 24
        p.translate(rect.x() + (rect.width() - 24 * s) / 2, rect.y() + (rect.height() - 24 * s) / 2)
        p.scale(s, s)
        col = self.disabled if mode == QIcon.Mode.Disabled else self.color
        if self.auto and CHECKED is not None and state == QIcon.State.On and mode != QIcon.Mode.Disabled:
            col = CHECKED
        width = STROKE
        if STROKE_PX is not None:  # rect is in device pixels when rendering a pixmap (_render), logical otherwise
            width = max(1.1, min(STROKE_PX * self._dpr / max(s, 1e-3), 2.6))
        p.setPen(QPen(col, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        _draw(self.name, p)
        p.restore()

    def _render(self, size: QSize, mode, state, scale: float) -> QPixmap:
        w, h = max(1, round(size.width() * scale)), max(1, round(size.height() * scale))
        on = self.auto and CHECKED is not None and state == QIcon.State.On
        key = (w, h, int(mode == QIcon.Mode.Disabled), on, scale)
        pm = self._pm.get(key)
        if pm is None:
            pm = QPixmap(w, h)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            self._dpr = scale
            self.paint(p, QRect(QPoint(0, 0), QSize(w, h)), mode, state)
            self._dpr = 1.0
            p.end()
            pm.setDevicePixelRatio(scale)
            if len(self._pm) > 32:
                self._pm.clear()
            self._pm[key] = pm
        return pm

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        return self._render(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode, state, scale: float) -> QPixmap:
        return self._render(size, mode, state, scale)

    def actualSize(self, size: QSize, mode, state) -> QSize:
        return size

    def clone(self) -> "_Engine":
        e = _Engine(self.name, self.color, self.disabled, self.auto)
        _engines.append(e)
        return e

    def iconName(self) -> str:
        return self.name


_cache: dict[tuple, QIcon] = {}
_engines: list[_Engine] = []  # the QIcons don't own the Python wrappers; keep them alive


def icon(name: str, color: QColor | None = None) -> QIcon:
    pal = QApplication.palette()
    col = QColor(color) if color is not None else pal.color(QPalette.ColorRole.WindowText)
    disabled = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    key = (name, col.rgba(), disabled.rgba(), color is None)
    ico = _cache.get(key)
    if ico is None:
        eng = _Engine(name, col, disabled, color is None)
        _engines.append(eng)
        ico = _cache[key] = QIcon(eng)
    return ico


def app_icon() -> QIcon:
    """The Leaf app icon: a white leaf on a green rounded square (fixed colors, any size)."""
    ico = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(size / 24, size / 24)
        g = QLinearGradient(0, 0, 0, 24)
        g.setColorAt(0, QColor("#43c463"))
        g.setColorAt(1, QColor("#1f8a3b"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(QRectF(1, 1, 22, 22), 5.5, 5.5)
        p.translate(12, 12)
        p.scale(0.82, 0.82)
        p.translate(-12, -12.5)
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawPath(_leaf_path())
        p.setPen(QPen(QColor("#2a9a48"), 1.3 if size >= 32 else 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(5, 19), QPointF(14.5, 9.5))
        p.end()
        ico.addPixmap(pm)
    return ico


# ---- edit existing text (leaf/features/textedit_ui.py): drawn here so _Engine picks them up by name
_draw_base = _draw


def _draw(name: str, p: QPainter) -> None:  # noqa: F811
    L = lambda x1, y1, x2, y2: p.drawLine(QPointF(x1, y1), QPointF(x2, y2))  # noqa: E731
    if name == "edittext":  # lines of text with a text cursor in them
        L(3.5, 7, 12, 7)
        L(3.5, 12, 9.5, 12)
        L(3.5, 17, 12, 17)
        L(16.5, 4.5, 16.5, 19.5)
        L(14.5, 4.5, 18.5, 4.5)
        L(14.5, 19.5, 18.5, 19.5)
    elif name == "pilcrow":  # paragraph mode
        L(11, 4.5, 11, 19.5)
        L(15.5, 4.5, 15.5, 19.5)
        path = QPainterPath(QPointF(18.5, 4.5))
        path.lineTo(9.5, 4.5)
        path.cubicTo(5.5, 4.5, 5.5, 12, 9.5, 12)
        path.lineTo(11, 12)
        p.drawPath(path)
    else:
        _draw_base(name, p)
