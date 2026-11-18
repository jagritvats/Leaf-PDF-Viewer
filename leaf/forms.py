"""Interactive AcroForm fields drawn as lightweight scene items (crisp at any zoom), plus
form-level operations: tab order, reset, export/import of values, flattened copies."""
from __future__ import annotations

import json
import os
import tempfile
import xml.etree.ElementTree as ET

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsTextItem, QLineEdit, QMenu, QWidgetAction

from . import commands as C
from .document import PdfDocument, choice_pairs, inherited_key, listbox_value, rect_key

FIELD_BG = QColor(222, 233, 255)
FIELD_BG_HOVER = QColor(205, 222, 255)
FIELD_BG_RO = QColor(232, 232, 232)
FIELD_BORDER = QColor(70, 120, 220, 160)
FIELD_BORDER_RO = QColor(150, 150, 150, 160)
REQUIRED = QColor(214, 40, 40)
FOCUS = QColor(37, 99, 235)
SELECTED_ROW = QColor(150, 190, 245)
SIGNED_BG = QColor(222, 244, 226)
SIGNED_FG = QColor(30, 120, 50)
Z_FIELD = 5
MASK = "•"
OFF_VALUES = (None, False, "", "Off", "off", "False", "false", "No", "no", "0")

T = pymupdf
TEXT, CHECK, RADIO = T.PDF_WIDGET_TYPE_TEXT, T.PDF_WIDGET_TYPE_CHECKBOX, T.PDF_WIDGET_TYPE_RADIOBUTTON
COMBO, LIST, SIG = T.PDF_WIDGET_TYPE_COMBOBOX, T.PDF_WIDGET_TYPE_LISTBOX, T.PDF_WIDGET_TYPE_SIGNATURE

_highlight = True


def highlight_enabled() -> bool:
    return _highlight


def set_highlight(canvas, on: bool) -> None:
    global _highlight
    _highlight = bool(on)
    for f in canvas._all_fields():
        f.refresh()


def _truthy(v) -> bool:
    return v not in OFF_VALUES


def widget_value(doc: pymupdf.Document, w: pymupdf.Widget):
    """A widget's current value in Leaf's representation (str, bool, or list for multi-select)."""
    t = w.field_type
    if t in (CHECK, RADIO):
        return _truthy(w.field_value)
    if t == LIST:
        sel = listbox_value(doc, w)
        return sel if w.field_flags & T.PDF_CH_FIELD_IS_MULTI_SELECT else (sel[0] if sel else "")
    if t == SIG:
        return None
    v = w.field_value
    return "" if v in (None, False) else str(v)


def _font(size: float) -> QFont:
    f = QFont("Arial")
    f.setStyleHint(QFont.StyleHint.SansSerif)
    f.setPixelSize(max(1, round(size)))
    return f


class FieldItem(QGraphicsObject):

    def __init__(self, canvas, key: tuple, rect: pymupdf.Rect, widget: pymupdf.Widget, doc: pymupdf.Document):
        super().__init__()
        self.canvas = canvas
        self.key = key
        self.name = widget.field_name
        self.ftype = widget.field_type
        self.flags = widget.field_flags or 0
        self.readonly = bool(self.flags & T.PDF_FIELD_IS_READ_ONLY)
        self.required = bool(self.flags & T.PDF_FIELD_IS_REQUIRED)
        self.initial = widget_value(doc, widget)
        self.r = QRectF(rect.x0, rect.y0, rect.width, rect.height)
        self.font_size = widget.text_fontsize or 0
        tc = widget.text_color
        self.color = QColor.fromRgbF(*[min(1.0, max(0.0, float(c))) for c in tc[:3]]) \
            if tc and len(tc) >= 3 else QColor(10, 10, 10)
        qt, q = inherited_key(doc, widget.xref, "Q")
        self.align = int(q) if qt == "int" and q in ("0", "1", "2") else 0
        self.setZValue(Z_FIELD)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.proxy: _KeyProxy | None = None
        if not self.readonly:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        tip = widget.field_label or widget.field_name or ""
        if self.readonly:
            tip += " (read-only)"
        elif self.required:
            tip += " (required)"
        self.setToolTip(tip.strip())

    # state ---------------------------------------------------------------
    def interactive(self) -> bool:
        return not self.readonly

    def value(self):
        return self.canvas.doc.form_values.get(self.key, self.initial)

    def empty_value(self):
        return ""

    def is_empty(self) -> bool:
        v = self.value()
        return not v

    def changed(self) -> bool:
        return self.key in self.canvas.doc.form_values

    def has_focus(self) -> bool:
        return self.proxy is not None and self.proxy.hasFocus()

    def show_own(self) -> bool:
        """With highlighting off, the PDF's own appearance shows until the value changes."""
        return _highlight or self.changed() or self.has_focus()

    # painting -------------------------------------------------------------
    def boundingRect(self) -> QRectF:
        return self.r.adjusted(-2, -2, 2, 2)

    def paint_bg(self, p: QPainter) -> None:
        if _highlight:
            pen = QPen(FIELD_BORDER_RO if self.readonly else FIELD_BORDER, 1)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(FIELD_BG_RO if self.readonly else FIELD_BG_HOVER if self._hover and self.interactive() else FIELD_BG)
            p.drawRect(self.r)
        elif self.show_own():
            p.fillRect(self.r, Qt.GlobalColor.white)
        if self.required and not self.readonly and self.is_empty():
            pen = QPen(REQUIRED, 1.5)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.r)
        if self.has_focus():
            pen = QPen(FOCUS, 1.5, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.r.adjusted(-1, -1, 1, 1))

    def hoverEnterEvent(self, e) -> None:
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, e) -> None:
        self._hover = False
        self.update()

    def refresh(self) -> None:
        self.update()

    # editing --------------------------------------------------------------
    def commit(self, value, text: str = "Fill form") -> None:
        same = [f for f in self.canvas._all_fields() if f.name == self.name and type(f) is type(self)] or [self]
        self.canvas.set_form_values({f.key: value for f in same}, text)

    def clear(self) -> None:
        self.commit(self.empty_value(), "Clear field")

    def focus_in(self, tab: bool = True) -> None:
        if self.proxy is None:
            self.proxy = _KeyProxy(self)
        self.proxy.setFocus(Qt.FocusReason.TabFocusReason if tab else Qt.FocusReason.MouseFocusReason)
        self.update()

    def activate(self, screen_pos=None) -> None:
        """Keyboard (Space/Enter) or click activation."""

    def _menu_pos(self, screen_pos):
        if screen_pos is not None:
            return screen_pos
        v = self.canvas
        return v.viewport().mapToGlobal(v.mapFromScene(self.mapToScene(self.r.bottomLeft())))

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton or not self.interactive():
            e.ignore()
            return
        e.accept()
        self.activate(e.screenPos())

    def contextMenuEvent(self, e) -> None:
        menu = QMenu(self.canvas)
        self.fill_context_menu(menu)
        if self.interactive() and not self.is_empty():
            menu.addAction("Clear field", self.clear)
        if menu.actions():
            menu.addSeparator()
        menu.addAction("Reset form", lambda: reset_form(self.canvas))
        e.accept()
        menu.exec(e.screenPos())

    def fill_context_menu(self, menu: QMenu) -> None:
        pass


class _UndoRelay:
    def __init__(self, stack):
        self.stack = stack

    def undo(self) -> None:
        self.stack.undo()

    def redo(self) -> None:
        self.stack.redo()


def _tab_to_key_press(item: QGraphicsTextItem, e) -> bool:
    """QGraphicsTextItem.sceneEvent hands Tab/Backtab straight to its text control (a tab character), never to
    keyPressEvent: route them to keyPressEvent so Tab moves to the next field in document order."""
    if e.type() == QEvent.Type.KeyPress and e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        item.keyPressEvent(e)
        return True
    if e.type() == QEvent.Type.KeyRelease and e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        return True
    return False


class _KeyProxy(QGraphicsTextItem):
    """Invisible keyboard focus target for non-text fields. It reports text interaction so the
    canvas passes keys (Space, Tab...) through to it instead of treating them as tool keys."""

    def sceneEvent(self, e) -> bool:
        return _tab_to_key_press(self, e) or super().sceneEvent(e)

    def __init__(self, field: FieldItem):
        super().__init__(field)
        self.field = field
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def document(self):  # Edit > Undo while a checkbox has focus undoes the form edit
        return _UndoRelay(self.field.canvas.undo)

    def boundingRect(self) -> QRectF:
        return QRectF()

    def shape(self) -> QPainterPath:
        return QPainterPath()

    def paint(self, p, opt, widget=None) -> None:
        pass

    def keyPressEvent(self, e) -> None:
        k = e.key()
        if k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            back = k == Qt.Key.Key_Backtab or bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.clearFocus()
            self.field.canvas.focus_next_field(self.field, back=back)
        elif k in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.field.activate()
        elif k == Qt.Key.Key_Escape:
            self.clearFocus()
        elif k in (Qt.Key.Key_Up, Qt.Key.Key_Down) and isinstance(self.field, ListField):
            self.field.step(-1 if k == Qt.Key.Key_Up else 1)
        e.accept()

    def inputMethodEvent(self, e) -> None:
        e.accept()

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        self.field.update()


# ----------------------------------------------------------------- text
class TextField(FieldItem):
    def __init__(self, canvas, key, rect, widget, doc):
        super().__init__(canvas, key, rect, widget, doc)
        f = self.flags
        self.multiline = bool(f & T.PDF_TX_FIELD_IS_MULTILINE)
        self.password = bool(f & T.PDF_TX_FIELD_IS_PASSWORD)
        self.maxlen = int(widget.text_maxlen or 0)
        self.comb = bool(f & T.PDF_TX_FIELD_IS_COMB) and self.maxlen > 0 and not self.multiline and not self.password
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape)
        self.editor = _FieldEditor(self)
        self.refresh()

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.r)
        return path

    def has_focus(self) -> bool:
        return self.editor.editing()

    def display(self, text: str) -> str:
        return MASK * len(text) if self.password else text

    def _auto_size(self, text: str) -> float:
        h, w = self.r.height(), self.r.width() - 4
        if self.multiline:
            doc = self.editor.document()
            for size in range(12, 3, -1):
                self.editor.setFont(_font(size))
                if doc.size().height() <= h + 0.5:
                    return size
            return 4
        size = min(12.0, max(4.0, h * 0.62))
        if self.comb:
            return min(size, max(4.0, w / self.maxlen))
        tw = QFontMetricsF(_font(size)).horizontalAdvance(text) if text else 0
        if tw > w > 0:
            size = max(4.0, size * w / tw)
        return size

    def refresh(self) -> None:
        ed = self.editor
        if not ed.editing():
            ed.set_value(str(self.value() or ""))
        h = self.r.height()
        opt = ed.document().defaultTextOption()
        opt.setAlignment([Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignRight][self.align])
        ed.document().setDefaultTextOption(opt)
        ed.document().setDocumentMargin(1)
        ed.setTextWidth(self.r.width() - (0 if self.multiline else 2))
        size = self.font_size or self._auto_size(ed.toPlainText())
        ed.setFont(_font(size))
        if self.multiline:
            ed.setPos(self.r.left(), self.r.top())
        else:
            eh = ed.boundingRect().height()
            ed.setPos(self.r.left() + 1, self.r.top() + (h - eh) / 2)
        ed.setVisible(ed.editing() or (self.show_own() and not self.comb))
        self.update()

    def paint(self, p, opt, widget=None) -> None:
        self.paint_bg(p)
        if not self.comb or self.editor.editing() or not self.show_own():
            return
        n = self.maxlen
        cw = self.r.width() / n
        if _highlight:
            pen = QPen(QColor(FIELD_BORDER.red(), FIELD_BORDER.green(), FIELD_BORDER.blue(), 70), 1)
            pen.setCosmetic(True)
            p.setPen(pen)
            for i in range(1, n):
                x = self.r.left() + i * cw
                p.drawLine(QPointF(x, self.r.top() + 2), QPointF(x, self.r.bottom() - 2))
        p.setFont(self.editor.font())
        p.setPen(self.color)
        for i, ch in enumerate(str(self.value() or "")[:n]):
            p.drawText(QRectF(self.r.left() + i * cw, self.r.top(), cw, self.r.height()),
                       int(Qt.AlignmentFlag.AlignCenter), ch)

    def focus_in(self, tab: bool = True) -> None:
        self.editor.start_edit(select_all=tab)

    def activate(self, screen_pos=None) -> None:
        self.editor.start_edit()

    def commit(self, value, text: str = "Fill form") -> None:
        if not self.multiline:
            value = value.replace("\n", " ")
        if self.maxlen:
            value = value[: self.maxlen]
        super().commit(value, text)


class _FieldEditor(QGraphicsTextItem):
    def __init__(self, field: TextField):
        super().__init__(field)
        self.field = field
        self.secret = ""  # the real text of a password field (the editor only holds bullets)
        self._guard = False
        self.setDefaultTextColor(field.color)
        opt = self.document().defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere if field.multiline else QTextOption.WrapMode.NoWrap)
        self.document().setDefaultTextOption(opt)
        if field.password:
            self.document().setUndoRedoEnabled(False)
        self.document().contentsChange.connect(self._changed)

    def editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def sceneEvent(self, e) -> bool:
        return _tab_to_key_press(self, e) or super().sceneEvent(e)

    def text(self) -> str:
        return self.secret if self.field.password else self.toPlainText()

    def set_value(self, text: str) -> None:
        self.secret = text
        self._guard = True
        self.setPlainText(self.field.display(text))
        self._guard = False

    def start_edit(self, select_all: bool = False) -> None:
        if self.field.readonly:
            return
        self.setVisible(True)
        if not self.editing():
            self.set_value(str(self.field.value() or ""))
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.TabFocusReason if select_all else Qt.FocusReason.MouseFocusReason)
        c = self.textCursor()
        if select_all:
            c.select(QTextCursor.SelectionType.Document)
        else:
            c.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(c)
        self.field.canvas.ensure_rect_visible(self.field.r, self.field)
        self.field.update()

    def _changed(self, pos: int, removed: int, added: int) -> None:
        f = self.field
        if self._guard:
            return
        if f.maxlen and not f.password:
            n = len(self.toPlainText())
            if n > f.maxlen:
                end = pos + added
                c = QTextCursor(self.document())
                c.setPosition(max(pos, end - (n - f.maxlen)))
                c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                self._guard = True
                c.removeSelectedText()
                self._guard = False
        if not f.font_size and self.editing():
            self._guard = True
            self.setFont(_font(f._auto_size(self.toPlainText())))
            self._guard = False

    def _password_key(self, e) -> bool:
        c = self.textCursor()
        s, t = c.selectionStart(), c.selectionEnd()
        k, mods = e.key(), e.modifiers()
        if e.matches(QKeySequence.StandardKey.Paste):
            ins = QGuiApplication.clipboard().text().replace("\n", "")
        elif e.matches(QKeySequence.StandardKey.Copy) or e.matches(QKeySequence.StandardKey.Cut):
            return True  # never put the secret on the clipboard
        elif k == Qt.Key.Key_Backspace:
            s = max(0, s - 1) if s == t else s
            ins = ""
        elif k == Qt.Key.Key_Delete:
            t = min(len(self.secret), t + 1) if s == t else t
            ins = ""
        elif e.text() and e.text().isprintable() and not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            ins = e.text()
        else:
            return False
        ml = self.field.maxlen
        if ml:
            ins = ins[: max(0, ml - (len(self.secret) - (t - s)))]
        self.set_value(self.secret[:s] + ins + self.secret[t:])
        c = self.textCursor()
        c.setPosition(s + len(ins))
        self.setTextCursor(c)
        return True

    def keyPressEvent(self, e) -> None:
        k, mods = e.key(), e.modifiers()
        if k == Qt.Key.Key_Escape:
            self.set_value(str(self.field.value() or ""))
            self.clearFocus()
            return
        if k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            back = k == Qt.Key.Key_Backtab or bool(mods & Qt.KeyboardModifier.ShiftModifier)
            self.clearFocus()
            self.field.canvas.focus_next_field(self.field, back=back)
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not self.field.multiline or mods & Qt.KeyboardModifier.ControlModifier:
                self.clearFocus()
                return
        if self.field.password and self._password_key(e):
            return
        super().keyPressEvent(e)

    def inputMethodEvent(self, e) -> None:
        if self.field.password:
            if e.commitString():
                s = e.commitString()
                c = self.textCursor()
                a, b = c.selectionStart(), c.selectionEnd()
                self.set_value(self.secret[:a] + s + self.secret[b:])
            e.accept()
            return
        super().inputMethodEvent(e)

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        if not self.editing():
            return
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        c = self.textCursor()
        c.clearSelection()
        self.setTextCursor(c)
        text = self.text()
        if text != str(self.field.value() or ""):
            self.field.commit(text)
        self.field.refresh()


# --------------------------------------------------------- check / radio
class CheckField(FieldItem):
    def __init__(self, canvas, key, rect, widget, doc):
        super().__init__(canvas, key, rect, widget, doc)
        try:
            self.on_state = str(widget.on_state())
        except Exception:  # noqa: BLE001 - malformed appearance dicts
            self.on_state = "Yes"

    def empty_value(self):
        return False

    def is_empty(self) -> bool:
        if self.ftype == RADIO:
            return not any(f.value() for f in self.group())
        return not self.value()

    def group(self) -> list[FieldItem]:
        return [f for f in self.canvas.fields_named(self.key[0], self.name) if isinstance(f, CheckField)] or [self]

    def paint(self, p, opt, widget=None) -> None:
        self.paint_bg(p)
        if not self.value() or not self.show_own():
            return
        r = self.r
        s = min(r.width(), r.height()) * 0.7
        c = r.center()
        pen = QPen(self.color, max(0.8, s * 0.14))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.ftype == RADIO:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.color)
            p.drawEllipse(c, s * 0.33, s * 0.33)
        else:
            path = QPainterPath(c + QPointF(-s * 0.4, 0))
            path.lineTo(c + QPointF(-s * 0.1, s * 0.32))
            path.lineTo(c + QPointF(s * 0.42, -s * 0.35))
            p.drawPath(path)

    def set_checked(self, on: bool, text: str = "Fill form") -> None:
        """Kids sharing this on-state follow it; with `on`, kids with other on-states go off (radio)."""
        vals = {}
        for f in self.group():
            if f.on_state == self.on_state:
                vals[f.key] = on
            elif on:
                vals[f.key] = False
        self.canvas.set_form_values(vals, text)

    def activate(self, screen_pos=None) -> None:
        if self.ftype == RADIO and (self.flags & T.PDF_BTN_FIELD_IS_NO_TOGGLE_TO_OFF or not self.value()):
            self.set_checked(True)
        else:
            self.set_checked(not self.value())

    def clear(self) -> None:
        self.canvas.set_form_values({f.key: False for f in self.group()}, "Clear field")


# --------------------------------------------------------------- choices
class ChoiceField(FieldItem):
    def __init__(self, canvas, key, rect, widget, doc):
        super().__init__(canvas, key, rect, widget, doc)
        self.choices = choice_pairs(widget.choice_values)
        self.editable = bool(self.flags & T.PDF_CH_FIELD_IS_EDIT)

    def interactive(self) -> bool:
        return not self.readonly and (bool(self.choices) or self.editable)

    def label(self) -> str:
        v = str(self.value() or "")
        return next((d for e, d in self.choices if e == v), v)

    def paint(self, p, opt, widget=None) -> None:
        self.paint_bg(p)
        if not self.show_own():
            return
        h = self.r.height()
        p.setFont(_font(self.font_size or min(12.0, max(5.0, h * 0.62))))
        p.setPen(self.color)
        align = [Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignRight][self.align]
        p.drawText(self.r.adjusted(2, 0, -h, 0), int(Qt.AlignmentFlag.AlignVCenter | align), self.label())
        if not _highlight or self.readonly:
            return
        a = h * 0.18
        c = QPointF(self.r.right() - h / 2, self.r.center().y())
        path = QPainterPath(c + QPointF(-a, -a / 2))
        path.lineTo(c + QPointF(0, a / 2))
        path.lineTo(c + QPointF(a, -a / 2))
        pen = QPen(QColor(60, 60, 60), 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def activate(self, screen_pos=None) -> None:
        if not self.interactive():
            return
        menu = QMenu(self.canvas)
        cur = str(self.value() or "")
        edit = None
        typed = []
        if self.editable:
            edit = QLineEdit(cur)
            edit.setPlaceholderText("Type a value")
            edit.setMinimumWidth(180)
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(edit)
            menu.addAction(wa)
            edit.returnPressed.connect(lambda: (typed.append(edit.text()), menu.close()))
            if self.choices:
                menu.addSeparator()
        for export, display in self.choices:
            act = menu.addAction(display)
            act.setCheckable(True)
            act.setChecked(export == cur)
            act.setData(export)
        if edit is not None:
            edit.setFocus()
            edit.selectAll()
        chosen = menu.exec(self._menu_pos(screen_pos))
        new = chosen.data() if chosen is not None and chosen.data() is not None else typed[-1] if typed else None
        if new is not None and new != cur:
            self.commit(new)


class ListField(FieldItem):
    def __init__(self, canvas, key, rect, widget, doc):
        super().__init__(canvas, key, rect, widget, doc)
        self.choices = choice_pairs(widget.choice_values)
        self.multi = bool(self.flags & T.PDF_CH_FIELD_IS_MULTI_SELECT)

    def interactive(self) -> bool:
        return not self.readonly and bool(self.choices)

    def empty_value(self):
        return [] if self.multi else ""

    def selected(self) -> list[str]:
        v = self.value()
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else ([str(v)] if v else [])

    def _metrics(self) -> tuple[float, float, int]:
        h = self.r.height()
        fs = self.font_size or min(12.0, max(5.0, (h - 2) / max(1, len(self.choices)) / 1.15))
        lh = fs * 1.15
        rows = max(1, int((h - 2) // lh))
        exports = [e for e, _ in self.choices]
        first = min((exports.index(s) for s in self.selected() if s in exports), default=0)
        return fs, lh, first - rows + 1 if first >= rows else 0

    def overflows(self) -> bool:
        fs, lh, _ = self._metrics()
        return len(self.choices) * lh > self.r.height() - 2

    def paint(self, p, opt, widget=None) -> None:
        self.paint_bg(p)
        if not self.show_own():
            return
        fs, lh, top = self._metrics()
        sel = set(self.selected())
        p.save()
        p.setClipRect(self.r.adjusted(1, 1, -1, -1))
        p.setFont(_font(fs))
        for row, (export, display) in enumerate(self.choices[top:]):
            rr = QRectF(self.r.left() + 1, self.r.top() + 1 + row * lh, self.r.width() - 2, lh)
            if rr.top() > self.r.bottom():
                break
            if export in sel:
                p.fillRect(rr, SELECTED_ROW)
            p.setPen(self.color)
            p.drawText(rr.adjusted(2, 0, -2, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), display)
        p.restore()

    def toggle(self, export: str) -> None:
        if self.multi:
            sel = self.selected()
            new = [e for e, _ in self.choices if (e in sel) != (e == export)]
            self.commit(new)
        elif export != str(self.value() or ""):
            self.commit(export)

    def step(self, d: int) -> None:
        if self.multi or not self.choices:
            return
        exports = [e for e, _ in self.choices]
        cur = str(self.value() or "")
        i = exports.index(cur) + d if cur in exports else 0
        self.toggle(exports[max(0, min(len(exports) - 1, i))])

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton or not self.interactive():
            e.ignore()
            return
        e.accept()
        if self.overflows():
            self.activate(e.screenPos())
            return
        fs, lh, top = self._metrics()
        row = int((e.pos().y() - self.r.top() - 1) // lh) + top
        if 0 <= row < len(self.choices):
            self.toggle(self.choices[row][0])

    def activate(self, screen_pos=None) -> None:
        if not self.interactive():
            return
        menu = QMenu(self.canvas)
        sel = set(self.selected())
        for export, display in self.choices:
            act = menu.addAction(display)
            act.setCheckable(True)
            act.setChecked(export in sel)
            act.setData(export)
        chosen = menu.exec(self._menu_pos(screen_pos))
        if chosen is not None:
            self.toggle(chosen.data())


# ------------------------------------------------------------- signature
class SignatureField(FieldItem):
    def __init__(self, canvas, key, rect, widget, doc):
        super().__init__(canvas, key, rect, widget, doc)
        self.signed = bool(widget.is_signed)
        if self.signed:
            self.setToolTip("Digitally signed. This field can't be changed; Leaf saves incrementally so the signature stays valid.")
            self.unsetCursor()
        else:
            self.setToolTip((widget.field_label or "Signature") + " - click to sign")

    def interactive(self) -> bool:
        return not self.readonly and not self.signed

    def has_signature_image(self) -> bool:
        """A handwritten signature image (Sign…, or one moved onto the field) is placed over this field."""
        return signature_overlay_over(self, self.mapRectToParent(self.r))

    def is_empty(self) -> bool:
        return not self.signed and not self.has_signature_image()

    def show_own(self) -> bool:
        return _highlight

    def paint(self, p, opt, widget=None) -> None:
        r = self.r
        if not self.signed and self.has_signature_image():
            return  # filled: the placed signature shows; no "Sign here" or required outline under it
        if self.signed:
            p.fillRect(r, QColor(SIGNED_BG.red(), SIGNED_BG.green(), SIGNED_BG.blue(), 110))
            h = min(14.0, max(6.0, r.height() * 0.35))
            badge = QRectF(r.left() + 2, r.top() + 2, h * 4.6, h * 1.35)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(SIGNED_FG)
            p.drawRoundedRect(badge, h * 0.3, h * 0.3)
            p.setPen(QColor("white"))
            p.setFont(_font(h * 0.85))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "✓ Signed")
            return
        if not _highlight:
            return
        self.paint_bg(p)
        p.setPen(QColor(70, 90, 140))
        p.setFont(_font(min(11.0, max(5.0, r.height() * 0.3))))
        p.drawText(r.adjusted(4, 0, -4, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "✎ Sign here")

    def activate(self, screen_pos=None) -> None:
        if not self.interactive():
            return
        from .signature import SignatureDialog

        dlg = SignatureDialog(self.canvas.window())
        if dlg.exec() and dlg.result_image is not None:
            place_signature(self.canvas, self, dlg.result_image)

    def fill_context_menu(self, menu: QMenu) -> None:
        if self.interactive():
            menu.addAction("Sign…", self.activate)
            digital = getattr(self.canvas.window(), "sign_field_digitally", None)  # features/signing.py
            if digital is not None:
                menu.addAction("Sign digitally…", lambda: digital(self.name))

    def clear(self) -> None:
        pass


def fit_rect(box: QRectF, w: float, h: float, pad: float = 2.0) -> QRectF:
    """Largest rect with aspect w:h centered in `box` (minus `pad`)."""
    b = box.adjusted(pad, pad, -pad, -pad) if box.width() > 3 * pad and box.height() > 3 * pad else QRectF(box)
    aspect = w / max(h, 1e-6)
    fw = min(b.width(), b.height() * aspect)
    fh = fw / aspect
    return QRectF(b.center().x() - fw / 2, b.center().y() - fh / 2, fw, fh)


def signature_overlay_over(item, rect: QRectF) -> bool:
    """Whether a placed signature image (an ImageItem of kind "signature" on the same page) covers `rect` (in the
    coordinates of `item`'s parent, the page content): its center lies inside the rect. Such a field counts as
    signed/filled for display and for required-field checks."""
    from .items import ImageItem

    parent = item.parentItem()
    if parent is None:
        return False
    for c in parent.childItems():
        if (c is not item and isinstance(c, ImageItem) and c.kind == "signature" and c.scene() is not None
                and c.isVisible() and rect.contains(c.scene_rect_in_parent().center())):
            return True
    return False


def _watch_signature_overlays(canvas) -> None:
    """Fields read their "filled" state from the overlays next to them, so repaint after every undo-stack change
    (placing, moving, deleting or undoing a signature image)."""
    if getattr(canvas, "_sig_overlay_watch", False) or getattr(canvas, "undo", None) is None:
        return
    canvas._sig_overlay_watch = True

    def repaint(_i=0) -> None:
        try:
            canvas.viewport().update()
        except RuntimeError:  # the canvas is being destroyed (its undo stack clears last)
            pass

    canvas.undo.indexChanged.connect(repaint)


def place_signature(canvas, field: FieldItem, image):
    """Put `image` fitted (aspect kept, centered) into the field as a signature overlay (undoable)."""
    from .items import ImageItem

    rect = fit_rect(field.r, image.width(), image.height())
    item = ImageItem("signature", rect, image, canvas.defaults.get("signature"))
    item.setParentItem(field.parentItem())
    canvas.undo.push(C.AddItems([item], "Sign field"))
    canvas.scene().clearSelection()
    item.setSelected(True)
    return item


# --------------------------------------------------------------- building
KINDS = {TEXT: TextField, CHECK: CheckField, RADIO: CheckField, COMBO: ChoiceField, LIST: ListField, SIG: SignatureField}


def build_fields(canvas, page_item) -> list[FieldItem]:
    _watch_signature_overlays(canvas)
    existing = [c for c in page_item.content.childItems() if isinstance(c, FieldItem)]
    if existing:
        return existing
    doc = canvas.doc
    ref = page_item.ref
    page = doc.page(ref)
    if not page.first_widget:
        return []
    pdf = page.parent
    m = page.rotation_matrix  # widget rects are unrotated page coordinates
    entries = []
    for w in page.widgets():
        kind = KINDS.get(w.field_type)
        if kind is None or w.rect.is_empty or w.field_display in (1, 3):  # hidden / no-view
            continue
        entries.append((kind, w, (w.rect * m).normalize()))
    t, tabs = pdf.xref_get_key(page.xref, "Tabs")
    if tabs == "/R":
        entries.sort(key=lambda e: (round(e[2].y0), e[2].x0))
    elif tabs == "/C":
        entries.sort(key=lambda e: (round(e[2].x0), e[2].y0))
    out = []
    for kind, w, r in entries:
        item = kind(canvas, (ref.uid, w.field_name, rect_key(w.rect)), r, w, pdf)
        item.setParentItem(page_item.content)
        out.append(item)
    return out


def page_fields(canvas, page_item) -> list[FieldItem]:
    """Fields of a page, built now if the page hasn't been painted yet."""
    if not page_item.fields:
        page_item.fields = build_fields(canvas, page_item)
    return page_item.fields


def focusable(f: FieldItem) -> bool:
    return f.interactive() and f.isVisible()


def iter_fields(canvas, start: FieldItem | None = None, back: bool = False, page_index: int | None = None):
    """Fields in document tab order after `start` (or from page `page_index`), wrapping around once."""
    order = canvas.order
    if not order:
        return
    if start is not None:
        pg = start.parentItem().parentItem() if start.parentItem() else None
        i0 = order.index(pg) if pg in order else 0
    else:
        i0 = max(0, min(len(order) - 1, page_index if page_index is not None else canvas.current_index()))
    n = len(order)
    for step in range(n + 1):
        i = (i0 - step if back else i0 + step) % n
        fields = page_fields(canvas, order[i])
        seq = list(reversed(fields)) if back else list(fields)
        if step == 0 and start is not None and start in seq:
            seq = seq[seq.index(start) + 1:]
        elif step == n and start is not None and start in seq:
            seq = seq[: seq.index(start)]
        elif step == n:
            break
        yield from seq


def focus_field(canvas, f: FieldItem, tab: bool = True) -> None:
    canvas.ensure_rect_visible(f.r, f.parentItem())
    f.focus_in(tab)


def next_required_empty(canvas) -> FieldItem | None:
    fi = canvas.scene().focusItem()
    start = fi.parentItem() if fi is not None and isinstance(fi.parentItem(), FieldItem) else None
    for f in iter_fields(canvas, start):
        if f.required and focusable(f) and f.is_empty():
            focus_field(canvas, f)
            return f
    return None


def reset_form(canvas) -> None:
    """All fields back to the values the document was opened with (one undo step)."""
    if canvas.doc is not None and canvas.doc.form_values:
        canvas.set_form_values({k: C.UNSET for k in list(canvas.doc.form_values)}, "Reset form")


# ------------------------------------------------------------------ data
def _walk_widgets(doc: PdfDocument):
    for ref in doc.pages:
        page = doc.page(ref)
        if not page.first_widget:
            continue
        for w in page.widgets():
            if w.field_type in KINDS and w.field_type != SIG:
                yield ref, page, w


def collect_values(doc: PdfDocument, check_states: bool = False) -> dict[str, object]:
    """Field name -> current value. Radio groups give the chosen on-state (or "Off");
    checkboxes give a bool, or their on-state/"Off" with `check_states`."""
    out: dict[str, object] = {}
    for ref, page, w in _walk_widgets(doc):
        key = (ref.uid, w.field_name, rect_key(w.rect))
        v = doc.form_values.get(key, C.UNSET)
        if v is C.UNSET:
            v = widget_value(page.parent, w)
        name = w.field_name
        if w.field_type in (RADIO, CHECK) and (w.field_type == RADIO or check_states):
            if v:
                out[name] = str(w.on_state())
            else:
                out.setdefault(name, "Off")
        elif w.field_type == CHECK:
            out[name] = bool(v) or bool(out.get(name))
        else:
            out[name] = list(v) if isinstance(v, (list, tuple)) else v
    return out


def import_values(canvas, data: dict, text: str = "Import form data") -> int:
    """Apply name -> value pairs to every matching widget as one undo step. Returns fields changed."""
    vals = value_changes(canvas.doc, data)
    if vals:
        canvas.set_form_values(vals, text)
    return len(vals)


def value_changes(doc: PdfDocument, data: dict) -> dict:
    """The doc.form_values entries (key -> new value) that name -> value pairs change; read-only fields are
    skipped. Qt-free use: leaf/editset.py (headless edit sets) and import_values."""
    vals = {}
    for ref, page, w in _walk_widgets(doc):
        name = w.field_name
        if name not in data or w.field_flags & T.PDF_FIELD_IS_READ_ONLY:
            continue
        v = data[name]
        t = w.field_type
        key = (ref.uid, name, rect_key(w.rect))
        if t == RADIO:
            new = str(v) == str(w.on_state())
        elif t == CHECK:
            if isinstance(v, (bool, int, float)):
                new = bool(v)
            else:
                new = str(v) == str(w.on_state()) or str(v) not in OFF_VALUES
        elif t == LIST:
            sel = [str(x) for x in v] if isinstance(v, (list, tuple)) else ([str(v)] if v not in (None, "") else [])
            new = sel if w.field_flags & T.PDF_CH_FIELD_IS_MULTI_SELECT else (sel[0] if sel else "")
        else:
            new = "" if v is None else "\n".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        cur = doc.form_values.get(key, C.UNSET)
        if (cur if cur is not C.UNSET else widget_value(page.parent, w)) != new:
            vals[key] = new
    return vals


def export_json(doc: PdfDocument, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fields": collect_values(doc)}, f, ensure_ascii=False, indent=2)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("fields"), dict):
        data = data["fields"]
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object of field names and values.")
    return data


XFDF_NS = "http://ns.adobe.com/xfdf/"


def export_xfdf(doc: PdfDocument, path: str) -> None:
    root = ET.Element("xfdf", {"xmlns": XFDF_NS, "xml:space": "preserve"})
    fields = ET.SubElement(root, "fields")
    nodes: dict[str, ET.Element] = {}
    for name, v in collect_values(doc, check_states=True).items():
        parent, path_so_far = fields, ""
        for part in name.split("."):
            path_so_far = f"{path_so_far}.{part}" if path_so_far else part
            node = nodes.get(path_so_far)
            if node is None:
                node = nodes[path_so_far] = ET.SubElement(parent, "field", {"name": part})
            parent = node
        for item in (v if isinstance(v, list) else [v]):
            ET.SubElement(parent, "value").text = str(item)
    if doc.path:
        ET.SubElement(root, "f", {"href": os.path.basename(doc.path)})
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)


def read_xfdf(path: str) -> dict:
    root = ET.parse(path).getroot()
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    out: dict[str, object] = {}

    def walk(el, prefix: str) -> None:
        for fld in el.findall(f"{ns}field"):
            name = f"{prefix}.{fld.get('name')}" if prefix else fld.get("name", "")
            values = [v.text or "" for v in fld.findall(f"{ns}value")]
            if values:
                out[name] = values if len(values) > 1 else values[0]
            walk(fld, name)

    fields = root.find(f"{ns}fields")
    if fields is not None:
        walk(fields, "")
    return out


def save_flattened(canvas, target: str) -> None:
    """Save a copy where form fields are baked into page content. The open document is untouched."""
    doc = canvas.doc
    target = os.path.abspath(target)
    d = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(prefix=".leaf-", suffix=".pdf", dir=d)
    os.close(fd)
    fd, tmp2 = tempfile.mkstemp(prefix=".leaf-", suffix=".pdf", dir=d)
    os.close(fd)
    try:
        doc.save(tmp, canvas.overlays_for)
        out = pymupdf.open(tmp)
        PdfDocument._auth(out, doc.primary)
        out.bake(annots=False, widgets=True)
        out.save(tmp2, garbage=3, deflate=True)
        out.close()
        os.replace(tmp2, target)
    finally:
        for p in (tmp, tmp2):
            try:
                os.remove(p)
            except OSError:
                pass
