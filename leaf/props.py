"""Compact style panel for the active tool or the selected items."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QMenu, QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from . import fonts
from .icons import icon

SWATCHES = ["#1a1a1a", "#d43a2a", "#e8862a", "#ffd83d", "#2f9e44", "#1f4fd1", "#7b3fd1", "#ffffff"]
HL_SWATCHES = ["#ffd83d", "#8ce99a", "#74c0fc", "#ffa8d8", "#ffc078"]
FILL_SWATCHES = ["#ffffff", "#fff3bf", "#d3f9d8", "#d0ebff", "#ffe3e3", "#f1f3f5"]
BORDER_SWATCHES = ["#1a1a1a", "#868e96", "#d43a2a", "#1f4fd1"]
SIZE_PRESETS = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72]
SPACINGS = [1.0, 1.15, 1.5, 2.0]
STYLES = [("bold", "B", "Bold (Ctrl+B)"), ("italic", "I", "Italic (Ctrl+I)"), ("underline", "U", "Underline (Ctrl+U)"),
          ("strike", "S", "Strikethrough (Ctrl+Shift+X)")]
ALIGNS = [("left", "align_left", "Align left"), ("center", "align_center", "Center"), ("right", "align_right", "Align right"),
          ("justify", "align_justify", "Justify")]
KEEP_EDITING = "leafKeepsTextEditing"  # see items.TextItem: focus moving here keeps the text box in edit mode
TITLES = {
    "select": "Selection", "text": "Text", "highlight": "Highlight", "ink": "Draw", "rect": "Rectangle",
    "ellipse": "Ellipse", "line": "Line", "arrow": "Arrow", "whiteout": "Whiteout", "image": "Image",
    "signature": "Signature", "hand": "Hand",
}
ICONS = {"hand": "select"}


class Swatch(QAbstractButton):
    """Round color chip; shows an accent ring when it is the current color. An empty color draws a 'none' chip."""

    def __init__(self, color: str = "#000000", size: int = 22, parent=None):
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(color or "None")
        self._name()

    def _name(self) -> None:
        from .a11y import color_name  # a11y: screen readers say "Color: red" instead of nothing

        self.setAccessibleName(f"Color: {color_name(self.color)}" if self.color else "No color")

    def set_color(self, color: str) -> None:
        self.color = color
        self.setToolTip(color or "None")
        self._name()
        self.update()

    def sizeHint(self) -> QSize:
        return self.size()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked():
            p.setPen(QPen(pal.color(QPalette.ColorRole.Accent), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.hasFocus():  # a11y: keyboard focus looks different from "current color" (dashed, in the text color)
            p.setPen(QPen(pal.color(QPalette.ColorRole.WindowText), 1.2, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        inner = r.adjusted(3.5, 3.5, -3.5, -3.5)
        if self.underMouse() and not self.isChecked():
            inner = r.adjusted(2.5, 2.5, -2.5, -2.5)
        edge = QColor(pal.color(QPalette.ColorRole.WindowText))
        edge.setAlpha(70)
        p.setPen(QPen(edge, 1))
        if not self.color:
            p.setBrush(pal.color(QPalette.ColorRole.Base))
            p.drawEllipse(inner)
            p.setPen(QPen(QColor("#d43a2a"), 1.4))
            d = inner.width() * 0.2
            p.drawLine(inner.bottomLeft() + QPointF(d, -d), inner.topRight() + QPointF(-d, d))
            return
        p.setBrush(QColor(self.color))
        p.drawEllipse(inner)

    def enterEvent(self, e) -> None:
        self.update()

    def leaveEvent(self, e) -> None:
        self.update()


class ColorChips(QWidget):
    """None + a few color chips + custom; emits the picked color ('' for none)."""

    picked = Signal(str)

    def __init__(self, colors: list[str], allow_none: bool = True, size: int = 22, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.chips: list[Swatch] = []
        for c in ([""] if allow_none else []) + colors:
            b = Swatch(c, size)
            b.clicked.connect(lambda _=False, btn=b: self.picked.emit(btn.color))
            lay.addWidget(b)
            self.chips.append(b)
        self.more = QToolButton()
        self.more.setIcon(icon("plus"))
        self.more.setAutoRaise(True)
        self.more.setToolTip("Custom color…")
        self.more.setFixedSize(size + 2, size + 2)
        self.more.clicked.connect(self._pick)
        lay.addWidget(self.more)
        lay.addStretch()
        self._cur = ""

    def set_current(self, color: str) -> None:
        self._cur = QColor(color).name() if color else ""
        for b in self.chips:
            b.setChecked((QColor(b.color).name() if b.color else "") == self._cur)

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._cur or "#ffffff"), self, "Color")
        if c.isValid():
            self.picked.emit(c.name())

    def refresh_icons(self) -> None:
        self.more.setIcon(icon("plus"))


class FontBox(QComboBox):
    """Searchable font family picker: the PDF base fonts first, then the installed families. Emits the family key."""

    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(18)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(10)
        self.setToolTip("Font (type to search)")
        self._filled = False
        self._key = fonts.DEFAULT
        for key in fonts.BASE:
            self.addItem(fonts.label(key), key)
        comp = QCompleter(self.model(), self)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.setCompleter(comp)
        self.activated.connect(self._activated)
        self.lineEdit().editingFinished.connect(self._revert_text)

    def _fill(self) -> None:
        if self._filled:
            return
        self._filled = True
        self.blockSignals(True)
        self.insertSeparator(self.count())
        for fam in QFontDatabase.families():
            if not QFontDatabase.isPrivateFamily(fam) and self.findData(fam) < 0:
                self.addItem(fam, fam)
        self.blockSignals(False)
        self.set_key(self._key)

    def showPopup(self) -> None:
        self._fill()
        super().showPopup()

    def focusInEvent(self, e) -> None:
        self._fill()  # so typing can search every family
        super().focusInEvent(e)

    def set_key(self, key: str) -> None:
        self._key = key or fonts.DEFAULT
        i = self.findData(self._key)
        self.blockSignals(True)
        if i < 0:
            self.addItem(fonts.label(self._key), self._key)
            i = self.count() - 1
        self.setCurrentIndex(i)
        self.blockSignals(False)

    def _activated(self, i: int) -> None:
        key = self.itemData(i)
        if key:
            self._key = key
            self.picked.emit(key)

    def _revert_text(self) -> None:
        if self.currentText() != fonts.label(self._key):
            self.set_key(self._key)


def style_button(text: str, tip: str, font_attr: str | None = None) -> QToolButton:
    """A checkable B / I / U / S button drawn with its own font style."""
    b = QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setAccessibleName(tip.split(" (", 1)[0])  # a11y: "Bold", not "B"
    b.setCheckable(True)
    b.setAutoRaise(True)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    f = QFont(b.font())
    if font_attr == "bold":
        f.setBold(True)
    elif font_attr == "italic":
        f.setItalic(True)
        f.setFamily("Times New Roman")
        f.setStyleHint(QFont.StyleHint.Serif)
    elif font_attr == "underline":
        f.setUnderline(True)
    elif font_attr == "strike":
        f.setStrikeOut(True)
    b.setFont(f)
    b.setFixedSize(26, 26)
    return b


def _secondary(w: QWidget) -> None:
    pal = w.palette()
    pal.setColor(QPalette.ColorRole.WindowText, pal.color(QPalette.ColorRole.PlaceholderText))
    w.setPalette(pal)


class PropsPanel(QWidget):
    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty(KEEP_EDITING, True)
        self._block = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_icon = QLabel()
        self.title = QLabel()
        f = self.title.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSizeF(f.pointSizeF() * 1.12)
        self.title.setFont(f)
        head.addWidget(self.title_icon)
        head.addWidget(self.title, 1)
        lay.addLayout(head)

        self.form_box = QWidget()
        self.form = QFormLayout(self.form_box)
        self.form.setContentsMargins(0, 2, 0, 0)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(10)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        lay.addWidget(self.form_box)

        # text: font family
        self.font_box = FontBox()
        self.font_box.picked.connect(lambda k: self._emit({"font": k}))
        self.form.addRow("Font", self.font_box)

        self.size_row = QWidget()
        zl = QHBoxLayout(self.size_row)
        zl.setContentsMargins(0, 0, 0, 0)
        zl.setSpacing(4)
        self.size = QSpinBox()
        self.size.setRange(4, 144)
        self.size.setSuffix(" pt")
        self.size.valueChanged.connect(lambda v: self._emit({"size": v}))
        self.size_presets = QToolButton()
        self.size_presets.setIcon(icon("chevron_down"))
        self.size_presets.setToolTip("Font size presets")
        self.size_presets.setAutoRaise(True)
        self.size_presets.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.size_presets.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        m = QMenu(self.size_presets)
        for s in SIZE_PRESETS:
            m.addAction(f"{s} pt", lambda s=s: self.size.setValue(s))
        self.size_presets.setMenu(m)
        zl.addWidget(self.size, 1)
        zl.addWidget(self.size_presets)
        self.form.addRow("Size", self.size_row)

        # text: B I U S + alignment
        self.style_row = QWidget()
        stl = QHBoxLayout(self.style_row)
        stl.setContentsMargins(0, 0, 0, 0)
        stl.setSpacing(1)
        self.style_btns: dict[str, QToolButton] = {}
        for key, text, tip in STYLES:
            b = style_button(text, tip, key)
            b.toggled.connect(lambda on, k=key: self._emit({k: on}))
            stl.addWidget(b)
            self.style_btns[key] = b
        stl.addSpacing(8)
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        self.align_btns: dict[str, QToolButton] = {}
        for key, ic, tip in ALIGNS:
            b = QToolButton()
            b.setIcon(icon(ic))
            b.setProperty("icon_name", ic)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFixedSize(26, 26)
            b.clicked.connect(lambda _=False, k=key: self._emit({"align": k}))
            self.align_group.addButton(b)
            stl.addWidget(b)
            self.align_btns[key] = b
        stl.addStretch()
        self.form.addRow("Style", self.style_row)

        self.swatch_row = QWidget()
        sl = QHBoxLayout(self.swatch_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(2)
        self.swatches: list[Swatch] = []
        for _ in range(len(SWATCHES)):
            b = Swatch()
            b.clicked.connect(lambda _=False, btn=b: self._emit({"color": btn.color}))
            sl.addWidget(b)
            self.swatches.append(b)
        self.more = QToolButton()
        self.more.setIcon(icon("plus"))
        self.more.setAutoRaise(True)
        self.more.setToolTip("Custom color…")
        self.more.setFixedSize(24, 24)
        self.more.clicked.connect(self._pick)
        sl.addWidget(self.more)
        sl.addStretch()
        self.form.addRow("Color", self.swatch_row)

        self.width = QDoubleSpinBox()
        self.width.setRange(0.25, 30)
        self.width.setSingleStep(0.5)
        self.width.setDecimals(2)
        self.width.setSuffix(" pt")
        self.width.valueChanged.connect(lambda v: self._emit({"width": v}))
        self.form.addRow("Stroke", self.width)

        self.opacity_row = QWidget()
        ol = QHBoxLayout(self.opacity_row)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(8)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 100)
        self.opacity.setPageStep(10)
        self.opacity_pct = QLabel("100%")
        self.opacity_pct.setMinimumWidth(self.opacity_pct.fontMetrics().horizontalAdvance("100%") + 2)
        self.opacity_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.opacity.valueChanged.connect(lambda v: self.opacity_pct.setText(f"{v}%"))
        self.opacity.valueChanged.connect(lambda v: self._emit({"opacity": v / 100}))
        ol.addWidget(self.opacity, 1)
        ol.addWidget(self.opacity_pct)
        self.form.addRow("Opacity", self.opacity_row)

        self.fill = QCheckBox("Tinted fill")
        self.fill.toggled.connect(lambda v: self._emit({"fill": v}))
        self.form.addRow("", self.fill)

        # text: paragraph + box
        self.spacing = QComboBox()
        for s in SPACINGS:
            self.spacing.addItem(f"{s:.2f}".rstrip("0") if s != int(s) else f"{s:.1f}", s)
        self.spacing.setToolTip("Line spacing")
        self.spacing.activated.connect(lambda i: self._emit({"spacing": self.spacing.itemData(i)}))
        self.form.addRow("Spacing", self.spacing)

        self.bg = ColorChips(FILL_SWATCHES)
        self.bg.picked.connect(lambda c: self._emit({"bg": c}))
        self.form.addRow("Fill", self.bg)

        self.border = ColorChips(BORDER_SWATCHES)
        self.border.picked.connect(lambda c: self._emit({"border": c}))
        self.form.addRow("Border", self.border)

        self.box_row = QWidget()
        bl = QHBoxLayout(self.box_row)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self.pad = QDoubleSpinBox()
        self.pad.setRange(0, 36)
        self.pad.setSingleStep(1)
        self.pad.setDecimals(1)
        self.pad.setSuffix(" pt")
        self.pad.setToolTip("Padding inside the box")
        self.pad.valueChanged.connect(lambda v: self._emit({"pad": v}))
        self.box_w = QDoubleSpinBox()
        self.box_w.setRange(0, 2000)
        self.box_w.setDecimals(0)
        self.box_w.setSingleStep(10)
        self.box_w.setSuffix(" pt")
        self.box_w.setSpecialValueText("Auto")
        self.box_w.setToolTip("Box width: Auto grows with the text; a fixed width wraps it (or drag the handle)")
        self.box_w.valueChanged.connect(lambda v: self._emit({"box_w": float(v)}))
        self.pad_label = QLabel("Padding")
        bl.addWidget(self.box_w, 1)
        bl.addWidget(self.pad_label)
        bl.addWidget(self.pad, 1)
        self.form.addRow("Width", self.box_row)
        self.pad_label.setVisible(False)

        self.empty = QLabel("Select an item or pick a tool")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setContentsMargins(0, 16, 0, 0)
        _secondary(self.empty)
        lay.addWidget(self.empty)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        _secondary(self.hint)
        lay.addWidget(self.hint)
        lay.addStretch()
        self._color = "#000000"
        self._kind = "select"
        # reserve the widest layout (text tool) up front: otherwise the dock widens on the first tool pick
        # and fit-width pages re-zoom
        from .items import DEFAULTS

        self.set_context("text", dict(DEFAULTS.get("text", {})), "")
        self.setMinimumWidth(self.minimumSizeHint().width())
        self.set_context("select", {}, "")

    def _emit(self, delta: dict) -> None:
        if self._block:
            return
        if "color" in delta:
            self._color = delta["color"]
            self._paint_swatches()
        if "bg" in delta:
            self.bg.set_current(delta["bg"])
        if "border" in delta:
            self.border.set_current(delta["border"])
        self.changed.emit(delta)

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "Color")
        if c.isValid():
            self._emit({"color": c.name()})

    def _paint_swatches(self) -> None:
        cur = QColor(self._color).name()
        for b in self.swatches:
            b.setChecked(QColor(b.color).name() == cur)

    def _row(self, w, visible: bool) -> None:
        self.form.setRowVisible(w, visible)

    def set_context(self, kind: str, props: dict, hint: str) -> None:
        self._block = True
        self._kind = kind
        self.title.setText(TITLES.get(kind, kind.replace("_", " ").title()))
        dpr = self.devicePixelRatioF()
        self.title_icon.setPixmap(icon(ICONS.get(kind, kind)).pixmap(QSize(18, 18), dpr))
        sw = HL_SWATCHES if kind == "highlight" else SWATCHES
        for i, b in enumerate(self.swatches):
            if i < len(sw):
                b.set_color(sw[i])
            b.setVisible(i < len(sw))
        self._row(self.font_box, "font" in props)
        self._row(self.style_row, "bold" in props or "align" in props)
        self._row(self.swatch_row, "color" in props)
        self._row(self.width, "width" in props)
        self._row(self.size_row, "size" in props)
        self._row(self.opacity_row, "opacity" in props)
        self._row(self.fill, "fill" in props)
        self._row(self.spacing, "spacing" in props)
        self._row(self.bg, "bg" in props)
        self._row(self.border, "border" in props)
        self._row(self.box_row, "pad" in props or "box_w" in props)
        self.form_box.setVisible(bool(props))
        self.empty.setVisible(not props and kind in ("select", "hand"))
        if "font" in props:
            self.font_box.set_key(props["font"])
        for k, b in self.style_btns.items():
            b.setVisible(k in props)
            b.setChecked(bool(props.get(k)))
        for k, b in self.align_btns.items():
            b.setVisible("align" in props)
            b.setChecked(props.get("align") == k)
        if "color" in props:
            self._color = props["color"]
            self._paint_swatches()
        if "width" in props:
            self.width.setValue(float(props["width"]))
        if "size" in props:
            self.size.setValue(int(round(float(props["size"]))))
        if "opacity" in props:
            self.opacity.setValue(int(round(float(props["opacity"]) * 100)))
            self.opacity_pct.setText(f"{self.opacity.value()}%")
        if "fill" in props:
            self.fill.setChecked(bool(props["fill"]))
        if "spacing" in props:
            sp = float(props["spacing"] or 1.0)
            self.spacing.setCurrentIndex(min(range(len(SPACINGS)), key=lambda i: abs(SPACINGS[i] - sp)))
        if "bg" in props:
            self.bg.set_current(props["bg"])
        if "border" in props:
            self.border.set_current(props["border"])
        self.box_w.setVisible("box_w" in props)
        self.pad_label.setVisible("box_w" in props and "pad" in props)
        self.pad.setVisible("pad" in props)
        self.form.labelForField(self.box_row).setText("Width" if "box_w" in props else "Padding")
        if "pad" in props:
            self.pad.setValue(float(props["pad"]))
        if "box_w" in props:
            self.box_w.setValue(float(props["box_w"] or 0))
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self._block = False

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            for w in (self.empty, self.hint):
                w.setPalette(QPalette())
                _secondary(w)
            self.more.setIcon(icon("plus"))
            self.size_presets.setIcon(icon("chevron_down"))
            for w in (self.bg, self.border):
                w.refresh_icons()
            for b in self.align_btns.values():
                b.setIcon(icon(b.property("icon_name")))
        super().changeEvent(e)
