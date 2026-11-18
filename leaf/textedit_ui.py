"""Edit existing text (phase 3), the user-facing part: the "Edit text" canvas tool, the hover outline and the
inline editor.

The engine (leaf/textedit.py) finds lines and paragraphs and decides what can be edited; this module only shows
them and turns typing into one undoable `commands.EditPageText`. Mouse and keys come through event filters on the
canvas viewport and the canvas, so canvas.py has no tool-specific code (like the crop tool).

Geometry: units are in the source page.rect space, the space of `PageItem.content`'s children. The outline, the
editor and the warning bar are children of the page, so zoom, scrolling, Leaf's extra rotation and crops just work.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPolygonF,
    QTextBlockFormat, QTextCursor, QTextOption, QTransform,
)
from PySide6.QtWidgets import QApplication, QGraphicsItem, QGraphicsPathItem, QGraphicsTextItem, QGraphicsView, QStyle
from shiboken6 import isValid

TOOL = "edittext"
SETTING = "textedit/paragraph"
CELL = 3.0  # hover cache grid, pt
HOVER_MS = 16  # mouse moves are coalesced to one hit test per frame
CHECK_MS = 250  # live check while typing
WRAP = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
HINT_LINE = "Click a line to edit it (Tab: whole paragraphs). Enter applies, Esc cancels."
HINT_PARA = "Click a paragraph to edit it (Tab: single lines). Shift+Enter starts a new line, Enter applies, Esc cancels."


def hint(para: bool) -> str:
    return HINT_PARA if para else HINT_LINE


# ------------------------------------------------------------------ engine access (tests may replace these)
def engine():
    from . import textedit

    return textedit


def command_class():
    from . import commands

    return commands.EditPageText


def is_edit_command(cmd) -> bool:
    return cmd is not None and type(cmd).__name__ == "EditPageText"


# ------------------------------------------------------------------------------------------------- geometry
def _rect(r) -> QRectF:
    if isinstance(r, QRectF):
        return QRectF(r)
    if hasattr(r, "x0"):
        return QRectF(QPointF(r.x0, r.y0), QPointF(r.x1, r.y1)).normalized()
    x0, y0, x1, y1 = r
    return QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()


def qt_angle(unit) -> float:
    """The unit's text direction as a Qt rotation (degrees, y down, so positive turns clockwise on screen); the
    engine reports it in that same page.rect space."""
    a = (float(getattr(unit, "angle", 0.0) or 0.0) + 180.0) % 360.0 - 180.0
    return 0.0 if abs(a) < 0.5 else a


def line_polygons(unit) -> list[QPolygonF]:
    """One outline per line, page.rect space, turned with the text."""
    boxes = [_rect(b) for b in (unit.line_boxes or (unit.bbox,))]
    a = qt_angle(unit)
    if not a:
        return [QPolygonF(b) for b in boxes]
    c, s = abs(math.cos(math.radians(a))), abs(math.sin(math.radians(a)))
    out = []
    for b in boxes:
        # an axis-aligned box around a rotated L x h line: W = L c + h s, H = L s + h c
        h = max(1.0, float(unit.size) * 1.15)
        h = min(h, b.width() / s if s > 1e-3 else h, b.height() / c if c > 1e-3 else h)
        L = (b.width() - h * s) / c if c >= s else (b.height() - h * c) / s
        L = max(L, h)
        t = QTransform().translate(b.center().x(), b.center().y()).rotate(a)
        out.append(t.map(QPolygonF(QRectF(-L / 2, -h / 2, L, h))))
    return out


def _reason(ex: Exception, default: str) -> str:
    """The engine's refusal as a sentence for the user."""
    t = str(ex).strip()
    if not t:
        return default
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".!?…" else t + "."


def _accent() -> QColor:
    pal = QGuiApplication.palette()
    role = getattr(QPalette.ColorRole, "Accent", QPalette.ColorRole.Highlight)
    c = pal.color(role)
    return c if c.isValid() and c.alpha() else pal.color(QPalette.ColorRole.Highlight)


# --------------------------------------------------------------------------------------------- hover outline
class HoverOutline(QGraphicsPathItem):
    """Accent outline around the line/paragraph under the pointer (cosmetic pen: crisp at any zoom and DPI)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.unit = None
        self.setZValue(60)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.restyle()

    def restyle(self) -> None:
        acc = _accent()
        pen = QPen(acc, 1.25)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)
        fill = QColor(acc)
        fill.setAlpha(26)
        self.setBrush(fill)

    def show_unit(self, unit) -> None:
        self.unit = unit
        pad = max(0.6, float(unit.size) * 0.08)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        if qt_angle(unit):
            for poly in line_polygons(unit):
                path.addPolygon(poly)
                path.closeSubpath()
        else:
            gap = pad * 2 if unit.kind == "para" else pad  # close the leading between lines of a paragraph
            for b in (unit.line_boxes or (unit.bbox,)):
                path.addRoundedRect(_rect(b).adjusted(-pad, -gap, pad, gap), 1.5, 1.5)
        self.setPath(path.simplified())
        self.show()


def _contains(unit, p: QPointF) -> bool:
    if unit.kind == "para" and not qt_angle(unit):
        return _rect(unit.bbox).contains(p)
    return any(poly.containsPoint(p, Qt.FillRule.OddEvenFill) for poly in line_polygons(unit))


# ------------------------------------------------------------------------------------------------ warning bar
class WarnBar(QGraphicsItem):
    """Small note just above the editor ("Uses a fallback font for 2 characters"). It ignores zoom and page
    rotation, so it stays upright and readable; it is a child of the page item (not the clipped content)."""

    PAD_X, PAD_Y, GAP, MAX_W = 8.0, 4.0, 6.0, 420.0

    def __init__(self, parent):
        super().__init__(parent)
        self.messages: list[str] = []
        self.error = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(200)
        self._font = QFont(QApplication.font())
        self._font.setPointSizeF(max(7.5, self._font.pointSizeF() * 0.92))
        self.hide()

    def set_messages(self, messages, error: bool = False) -> None:
        msgs = [str(m) for m in messages if str(m).strip()][:3]
        self.prepareGeometryChange()
        self.messages, self.error = msgs, error
        self.setVisible(bool(msgs))
        self.update()

    def _rects(self) -> list[QRectF]:
        """Each message's wrapped text rectangle, stacked, inside the padding."""
        fm = QFontMetricsF(self._font)
        out, y = [], self.PAD_Y
        for m in self.messages:
            r = fm.boundingRect(QRectF(0, 0, self.MAX_W, 1e5), WRAP, m)
            out.append(QRectF(self.PAD_X, y, r.width(), r.height()))
            y += r.height() + 2
        return out

    def _size(self) -> tuple[float, float]:
        rs = self._rects()
        if not rs:
            return 0.0, 0.0
        return max(r.right() for r in rs) + self.PAD_X, rs[-1].bottom() + self.PAD_Y

    def boundingRect(self) -> QRectF:
        w, h = self._size()
        return QRectF(-1, -1, w + 2, h + 2)

    def place(self, view, editor_rect: QRectF) -> None:
        """Above the editor (below it when there's no room), kept inside the visible part of the view.
        `editor_rect` is in scene coordinates; the bar's own size is in screen pixels."""
        if not self.messages:
            return
        z = max(abs(view.transform().m11()), abs(view.transform().m12()), 1e-3)
        w, h = (v / z for v in self._size())
        gap = self.GAP / z
        vis = view.mapToScene(view.viewport().rect()).boundingRect().adjusted(4 / z, 4 / z, -4 / z, -4 / z)
        x = min(max(editor_rect.left(), vis.left()), max(vis.left(), vis.right() - w))
        y = editor_rect.top() - gap - h
        if y < vis.top():
            y = editor_rect.bottom() + gap
        self.setPos(self.parentItem().mapFromScene(QPointF(x, y)))

    def paint(self, p: QPainter, opt, widget=None) -> None:
        if not self.messages:
            return
        pal = QGuiApplication.palette()
        w, h = self._size()
        r = QRectF(0, 0, w, h)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QColor("#d13438") if self.error else _accent()
        p.setPen(QPen(border, 1.0))
        p.setBrush(pal.color(QPalette.ColorRole.ToolTipBase))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        p.setFont(self._font)
        p.setPen(pal.color(QPalette.ColorRole.ToolTipText))
        for m, tr in zip(self.messages, self._rects()):
            p.drawText(tr.adjusted(0, 0, 1, 0), WRAP, m)


# --------------------------------------------------------------------------------------------- inline editor
class InlineEditor(QGraphicsTextItem):
    """Plain-text editor placed on the unit's baseline, in its font, size, colour and direction."""

    PAD = 1.5  # outline distance, in item units

    def __init__(self, tool: "EditTextTool", unit, para: bool, night: bool = False):
        super().__init__()
        self.tool = tool
        self.unit = unit
        self.para = para
        self.setAcceptDrops(False)
        self.setZValue(70)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        size = max(1.0, float(unit.size or 12.0))
        px = max(1, round(size))
        self.scale_f = size / px  # QFont pixel sizes are whole numbers; the rest is a transform
        f = QFont(unit.family or QApplication.font().family())
        f.setPixelSize(px)
        f.setBold(bool(unit.bold))
        f.setItalic(bool(unit.italic))
        f.setKerning(True)
        self.setFont(f)
        col = QColor(unit.color or "#000000")
        if night:  # the page is shown inverted
            col = QColor(255 - col.red(), 255 - col.green(), 255 - col.blue())
        self.setDefaultTextColor(col)
        doc = self.document()
        doc.setDocumentMargin(0)
        opt = QTextOption(doc.defaultTextOption())
        align = {"right": Qt.AlignmentFlag.AlignRight, "center": Qt.AlignmentFlag.AlignHCenter,
                 "justify": Qt.AlignmentFlag.AlignJustify}.get(unit.align, Qt.AlignmentFlag.AlignLeft)
        opt.setAlignment(align if para else Qt.AlignmentFlag.AlignLeft)
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if para else QTextOption.WrapMode.NoWrap)
        doc.setDefaultTextOption(opt)
        boxes = [_rect(b) for b in (unit.line_boxes or (unit.bbox,))]
        self.box = _rect(unit.bbox)
        self.pitch = 0.0
        if para and len(boxes) > 1:
            ys = sorted(b.bottom() for b in boxes)
            steps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.1]
            self.pitch = sorted(steps)[len(steps) // 2] if steps else 0.0
        self.ghost = False  # committed: shown without outline until the page re-renders with the new text
        self.setPlainText(unit.text if para else unit.text.replace("\n", " "))
        self._format_blocks()
        doc.clearUndoRedoStacks()
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.min_w = 0.0
        self.place()

    def _format_blocks(self) -> None:
        if self.pitch <= 0:
            return
        c = QTextCursor(self.document())
        c.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        # the original line pitch; added below each line, so the first baseline stays where it is
        extra = self.pitch / self.scale_f - self._natural_line_height()
        fmt.setLineHeight(extra, QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value)
        c.mergeBlockFormat(fmt)

    def _natural_line_height(self) -> float:
        if not hasattr(self, "_natural"):
            self.document().size()  # lays the document out
            lay = self.document().firstBlock().layout()
            self._natural = lay.lineAt(0).height() if lay is not None and lay.lineCount()                 else QFontMetricsF(self.font()).height()
        return self._natural

    def first_baseline(self) -> float:
        """Distance from the item's top to the first line's baseline, in item units."""
        block = self.document().firstBlock()
        lay = block.layout()
        if lay is not None and lay.lineCount():
            ln = lay.lineAt(0)
            return lay.position().y() + ln.y() + ln.ascent()
        return QFontMetricsF(self.font()).ascent()

    def place(self) -> None:
        """Put the first baseline on the unit's baseline, turned with the text; width = the unit's box."""
        u, s, a = self.unit, self.scale_f, qt_angle(self.unit)
        bx, by = float(u.baseline[0]), float(u.baseline[1])
        x0 = 0.0
        if self.para:
            width = self.box.width() if not a else max(b.width() for b in map(_rect, u.line_boxes or (u.bbox,)))
            if not a:
                x0 = self.box.left() - bx
            self.setTextWidth(max(width, s * 4) / s)
            self.min_w = 0.0
        else:
            self.setTextWidth(-1)
            if a:  # the outline's long side
                poly = line_polygons(u)[0]
                length = math.dist((poly[0].x(), poly[0].y()), (poly[1].x(), poly[1].y()))
            else:
                length = self.box.right() - bx
            self.min_w = max(0.0, length) / s
        asc = self.first_baseline()
        self.setPos(bx, by)
        self.setTransform(QTransform().scale(s, s) * QTransform().translate(x0, -asc * s) * QTransform().rotate(a))

    def text_rect(self) -> QRectF:
        r = super().boundingRect()
        if self.min_w > r.width():
            r.setWidth(self.min_w)
        return r

    def boundingRect(self) -> QRectF:
        p = self.PAD + 1
        return self.text_rect().adjusted(-p, -p, p, p)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def paint(self, p: QPainter, opt, widget=None) -> None:
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        if not self.ghost:
            pen = QPen(_accent(), 1.25)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.text_rect().adjusted(-self.PAD, -self.PAD, self.PAD, self.PAD), 2, 2)
        super().paint(p, opt, widget)

    def plain_text(self) -> str:
        t = self.toPlainText().replace(" ", "\n").replace(" ", "\n")
        return t if self.para else " ".join(t.split("\n"))

    # ---------------------------------------------------------------------------------------- interaction
    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.para and e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                c = self.textCursor()
                c.insertText("\n")
                self.setTextCursor(c)
                self._format_blocks()
            else:
                self.tool.commit()
            e.accept()
            return
        if k == Qt.Key.Key_Escape:
            self.tool.cancel()
            e.accept()
            return
        if k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):  # never a tab character, never out of the editor
            e.accept()
            return
        if e.matches(QKeySequence.StandardKey.Paste):  # plain text only
            t = QGuiApplication.clipboard().text().replace("\r\n", "\n").replace("\t", " ")
            if not self.para:
                t = " ".join(t.split("\n"))
            if t:
                c = self.textCursor()
                c.insertText(t)
                self.setTextCursor(c)
                self._format_blocks()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        if e.reason() in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
            return  # context menu, another window: keep editing
        if not self.tool.closing:
            QTimer.singleShot(0, self.tool._focus_lost)  # never remove an item inside its own focus event


# --------------------------------------------------------------------------------------------------- the tool
class EditTextTool(QObject):
    """Hover, open, commit and cancel. One per canvas."""

    modeChanged = Signal(bool)
    editingChanged = Signal(bool)

    def __init__(self, canvas, settings=None, toast=None):
        super().__init__(canvas)
        self.cv = canvas
        self.settings = settings
        self.toast = toast or (lambda *a, **k: None)
        self.para = False
        if settings is not None:
            self.para = str(settings.value(SETTING, False)).lower() in ("true", "1")
        self.hover: HoverOutline | None = None
        self._grid: dict = {}
        self._last_vp: QPointF | None = None
        self._press_eaten = False
        self.editor: InlineEditor | None = None
        self.bar: WarnBar | None = None
        self.page = None  # PageItem being edited
        self.ref = None
        self.unit = None
        self.closing = False
        self._hover_timer = QTimer(self, singleShot=True, interval=HOVER_MS, timeout=self._do_hover)
        self._check_timer = QTimer(self, singleShot=True, interval=CHECK_MS, timeout=self._live_check)
        self._ghost = None  # (item, page item, image) kept until the page shows the new text
        self._ghost_timer = QTimer(self, interval=30, timeout=self._ghost_tick)
        self._ghost_left = 0
        canvas.viewport().installEventFilter(self)
        canvas.installEventFilter(self)
        canvas.toolChanged.connect(self._tool_changed)
        canvas.documentChanged.connect(self._doc_changed)
        canvas.pagesChanged.connect(self._pages_changed)
        canvas.undo.indexChanged.connect(self._undo_changed)

    # ------------------------------------------------------------------------------------------ mode
    def set_para(self, on: bool) -> None:
        on = bool(on)
        if on == self.para:
            return
        self.para = on
        if self.settings is not None:
            self.settings.setValue(SETTING, on)
        self._grid.clear()
        self.modeChanged.emit(on)
        if self.editor is None:
            self._clear_hover()
            self._hover_timer.start()

    # ----------------------------------------------------------------------------------------- hover
    def unit_at(self, pg, lp: QPointF):
        """The unit under `lp` (page.rect space) in the current mode, cached per page and position."""
        if self.hover is not None and self.hover.isVisible() and self.hover.unit is not None \
                and self.hover.parentItem() is pg.content and _contains(self.hover.unit, lp):
            return self.hover.unit
        key = (pg.ref.uid, self.para, int(lp.x() // CELL), int(lp.y() // CELL))
        if key in self._grid:
            return self._grid[key]
        eng = engine()
        try:
            u = eng.unit_at(self.cv.doc, pg.ref, lp.x(), lp.y(), self.para)
        except Exception:  # noqa: BLE001  a refusal or an engine problem: nothing to edit here
            u = None
        if len(self._grid) > 4096:
            self._grid.clear()
        self._grid[key] = u
        return u

    def _page_point(self, vp: QPointF):
        sp = self.cv.mapToScene(vp.toPoint())
        pg = self.cv.page_at(sp)
        return (pg, pg.content.mapFromScene(sp)) if pg is not None else (None, None)

    def _do_hover(self) -> None:
        cv = self.cv
        if cv.tool != TOOL or self.editor is not None or cv.doc is None or self._last_vp is None:
            self._clear_hover()
            return
        pg, lp = self._page_point(self._last_vp)
        unit = self.unit_at(pg, lp) if pg is not None else None
        if unit is None:
            self._clear_hover()
            return
        if self.hover is None or not isValid(self.hover) or self.hover.parentItem() is not pg.content:
            self._clear_hover(drop=True)
            self.hover = HoverOutline(pg.content)
        if self.hover.unit != unit or not self.hover.isVisible():
            self.hover.show_unit(unit)

    def _clear_hover(self, drop: bool = False) -> None:
        h = self.hover
        if h is None:
            return
        if not isValid(h):
            self.hover = None
            return
        if drop:
            if h.scene() is not None:
                h.scene().removeItem(h)
            self.hover = None
        else:
            h.hide()
            h.unit = None

    # -------------------------------------------------------------------------------------- open/close
    def open_at(self, pg, lp: QPointF) -> bool:
        unit = self.unit_at(pg, lp)
        return unit is not None and self.open(pg, unit, lp)

    def open(self, pg, unit, click: QPointF | None = None) -> bool:
        cv = self.cv
        eng = engine()
        try:
            warnings = list(eng.check(cv.doc, pg.ref, unit) or [])
        except eng.Refuse as ex:
            self.toast(_reason(ex, "This text can’t be edited."), ms=5000)
            return False
        if self.editor is not None:
            self.cancel()
        self._clear_hover()
        cv._end_text_selection()
        cv.scene().clearSelection()
        self._drop_ghost()
        self.page, self.ref, self.unit = pg, pg.ref, unit
        hide = getattr(cv, "preview_text_hidden", None)
        if hide is not None:
            hide(pg.ref, unit)
        para = unit.kind == "para"
        ed = self.editor = InlineEditor(self, unit, para, night=bool(getattr(cv, "night", False)))
        ed.setParentItem(pg.content)
        ed.document().contentsChanged.connect(self._typed)
        self.bar = WarnBar(pg)
        self._set_messages(warnings)
        cv.setFocus(Qt.FocusReason.MouseFocusReason)
        ed.setFocus(Qt.FocusReason.MouseFocusReason)
        c = ed.textCursor()
        pos = -1
        if click is not None:
            pos = ed.document().documentLayout().hitTest(ed.mapFromParent(click), Qt.HitTestAccuracy.FuzzyHit)
        if pos < 0:
            c.movePosition(QTextCursor.MoveOperation.End)
        else:
            c.setPosition(pos)
        ed.setTextCursor(c)
        cv.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.editingChanged.emit(True)
        return True

    def _set_messages(self, msgs, error: bool = False) -> None:
        if self.bar is None or not isValid(self.bar) or self.editor is None:
            return
        self.bar.set_messages(msgs, error)
        self.bar.place(self.cv, self.editor.sceneBoundingRect())

    def _typed(self) -> None:
        if self.editor is not None:
            self._check_timer.start()
            self._set_messages(self.bar.messages if self.bar is not None else [],
                               self.bar.error if self.bar is not None else False)  # follow the editor's size

    def _live_check(self) -> None:
        if self.editor is None or not isValid(self.editor):
            return
        eng = engine()
        try:
            msgs = list(eng.check(self.cv.doc, self.ref, self.unit, self.editor.plain_text()) or [])
        except eng.Refuse as ex:
            self._set_messages([_reason(ex, "This change can’t be applied.")], error=True)
            return
        except Exception as ex:  # noqa: BLE001
            self._set_messages([f"This change can’t be checked: {ex}"], error=True)
            return
        self._set_messages(msgs)

    def editing(self) -> bool:
        return self.editor is not None

    def commit(self, focus_lost: bool = False) -> bool:
        """Apply the typed text as one undo step. On a refusal the editor stays open (Enter) or the edit is
        dropped with a note (focus went elsewhere). True when the editor is closed."""
        ed = self.editor
        if ed is None:
            return True
        if not isValid(ed):
            self._forget()
            return True
        text = ed.plain_text()
        ref, unit = self.ref, self.unit
        if text == unit.text:
            self.cancel()
            return True
        eng = engine()
        try:
            eng.check(self.cv.doc, ref, unit, text)
        except eng.Refuse as ex:
            reason = _reason(ex, "This change can’t be applied.")
            if focus_lost:
                self.cancel()
                self.toast(f"Text not changed. {reason}", ms=5000)
                return True
            self._check_timer.stop()
            self._set_messages([reason], error=True)
            self.toast(reason, ms=5000)
            return False
        self._close(ghost=True)
        hide = getattr(self.cv, "preview_text_hidden", None)
        if hide is not None:
            hide(ref, None, sync=False)  # the command below re-syncs the page once
        self.cv.undo.push(command_class()(self.cv, ref, unit, text))
        self._after_close()
        return True

    def cancel(self) -> None:
        if self.editor is None:
            return
        ref = self.ref
        self._close()
        hide = getattr(self.cv, "preview_text_hidden", None)
        if hide is not None and ref is not None:
            hide(ref, None)
        self._after_close()

    def _after_close(self) -> None:
        self.editingChanged.emit(False)
        if self.cv.tool == TOOL:
            self._hover_timer.start()

    def _focus_lost(self) -> None:
        ed = self.editor
        if ed is None or not isValid(ed) or ed.hasFocus():
            return
        self.commit(focus_lost=True)

    def _close(self, ghost: bool = False) -> None:
        ed, bar, pg = self.editor, self.bar, self.page
        self._forget()
        self._check_timer.stop()
        self.closing = True
        try:
            if bar is not None and isValid(bar) and bar.scene() is not None:
                bar.scene().removeItem(bar)
            if ed is not None and isValid(ed):
                ed.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                if ed.hasFocus():
                    ed.clearFocus()
                if ghost and pg is not None and isValid(pg) and ed.scene() is not None:
                    # keep the typed text on screen until the page has re-rendered with it (no blank flash)
                    ed.ghost = True
                    ed.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                    ed.update()
                    self._ghost = (ed, pg, pg.img)
                    self._ghost_left = 25
                    self._ghost_timer.start()
                elif ed.scene() is not None:
                    ed.scene().removeItem(ed)
        finally:
            self.closing = False

    def _forget(self) -> None:
        self.editor = self.bar = None
        self.page = self.ref = self.unit = None

    def _ghost_tick(self) -> None:
        g = self._ghost
        if g is None:
            self._ghost_timer.stop()
            return
        _, pg, img = g
        self._ghost_left -= 1
        if self._ghost_left <= 0 or not isValid(pg) or pg.img is not img:
            self._drop_ghost()

    def _drop_ghost(self) -> None:
        g, self._ghost = self._ghost, None
        self._ghost_timer.stop()
        if g is not None and isValid(g[0]) and g[0].scene() is not None:
            g[0].scene().removeItem(g[0])

    # ---------------------------------------------------------------------------------------- events
    def eventFilter(self, obj, e) -> bool:
        cv = self.cv
        t = e.type()
        if obj is cv:
            return t == QEvent.Type.KeyPress and self._key(e)
        if cv.doc is None or getattr(cv, "presenting", False):
            return False
        if t == QEvent.Type.MouseMove:
            self._last_vp = e.position()
            if cv.tool == TOOL and not e.buttons() and not cv._space:
                if self.editor is None and not self._hover_timer.isActive():
                    self._hover_timer.start()
                if cv.viewport().cursor().shape() != Qt.CursorShape.IBeamCursor:
                    cv.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            return False
        if t == QEvent.Type.Leave:
            self._last_vp = None
            self._hover_timer.stop()
            self._clear_hover()
            return False
        left = getattr(e, "button", lambda: None)() == Qt.MouseButton.LeftButton
        if t == QEvent.Type.MouseButtonPress and left and not cv._space:
            if self._in_editor(e):
                QGraphicsView.mousePressEvent(cv, e)  # straight to the editor: no tool logic (select would end it)
                return True
            if cv.tool == TOOL:
                self._press(e)
                self._press_eaten = True
                return True
            return False
        if t == QEvent.Type.MouseButtonRelease and left and self._press_eaten:
            self._press_eaten = False
            return True
        if t == QEvent.Type.MouseButtonDblClick and left:
            if self._in_editor(e):
                return False
            if cv.tool == TOOL:
                self._press_eaten = True
                return True
            if cv.tool == "select" and self.editor is None:
                return self._select_double_click(e)
        return False

    def _in_editor(self, e) -> bool:
        ed = self.editor
        if ed is None or not isValid(ed):
            return False
        sp = self.cv.mapToScene(e.position().toPoint())
        return ed.contains(ed.mapFromScene(sp))

    def _top_item(self, e):
        from .canvas import PageItem
        from .forms import FieldItem
        from .items import is_overlay

        top = self.cv.itemAt(e.position().toPoint())
        while top is not None and not (is_overlay(top) or isinstance(top, (FieldItem, PageItem))):
            top = top.parentItem()
        return top

    def _press(self, e) -> None:
        cv = self.cv
        self._last_vp = e.position()
        if self.editor is not None and not self.commit():
            return  # refused: keep editing
        top = self._top_item(e)
        from .items import TextItem

        if isinstance(top, TextItem):  # Leaf's own text boxes edit in place, as with the Text tool
            cv.scene().clearSelection()
            top.setSelected(True)
            top.start_edit()
            return
        if top is not None and not hasattr(top, "content"):  # another overlay or a form field: not page text
            return
        pg, lp = self._page_point(e.position())
        if pg is not None:
            self.open_at(pg, lp)

    def _select_double_click(self, e) -> bool:
        top = self._top_item(e)
        if top is None or not hasattr(top, "content"):
            return False
        pg, lp = self._page_point(e.position())
        if pg is None:
            return False
        unit = self.unit_at(pg, lp)
        if unit is None:
            return False
        self.open(pg, unit, lp)  # a refusal shows its toast; either way the double-click is handled
        return True

    def _key(self, e) -> bool:
        cv = self.cv
        k = e.key()
        plain = not (e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                                      | Qt.KeyboardModifier.MetaModifier))
        if self.editor is not None:
            # focus stays in the editor (the scene would move it to a form field); no mode switch mid-edit
            return k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and plain
        if k == Qt.Key.Key_Tab and plain and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) \
                and cv.tool == TOOL and cv.doc is not None and not cv._editing_text():
            self.set_para(not self.para)
            return True
        return False

    # ---------------------------------------------------------------------------------------- canvas state
    def _tool_changed(self, tool: str) -> None:
        if tool == TOOL:
            self.cv.viewport().setCursor(Qt.CursorShape.IBeamCursor)
            if self.editor is None:
                self._hover_timer.start()
            return
        self._hover_timer.stop()
        self._clear_hover()
        if self.editor is not None and tool != "select":
            self.commit(focus_lost=True)

    def _doc_changed(self) -> None:
        # set_document cleared the scene: our items are gone with it
        self._forget()
        self.hover = None
        self._ghost = None
        self._ghost_timer.stop()
        self._check_timer.stop()
        self._grid.clear()

    def _pages_changed(self) -> None:
        self._grid.clear()
        pg = self.page
        if self.editor is not None and (pg is None or not isValid(pg) or pg.scene() is None):
            self.cancel()
        elif self.editor is not None and isValid(self.editor):
            self._set_messages(self.bar.messages if self.bar else [], self.bar.error if self.bar else False)

    def _undo_changed(self, _idx: int) -> None:
        self._grid.clear()
        if self.hover is not None and isValid(self.hover):
            self._clear_hover()
            if self.cv.tool == TOOL and self.editor is None:
                self._hover_timer.start()

    def restyle(self) -> None:
        if self.hover is not None and isValid(self.hover):
            self.hover.restyle()
        if self.bar is not None and isValid(self.bar):
            self.bar.update()
        if self.editor is not None and isValid(self.editor):
            self.editor.update()
