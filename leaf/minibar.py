"""Floating mini toolbar next to the selected overlays: colors, stroke, opacity, size, duplicate, delete; for text
boxes also font, bold/italic/underline/strike, alignment and box fill (shown while editing too, for the selection).

A child of the canvas viewport; follows the selection on scroll/zoom and hides while the mouse is down.
Changes go through `canvas.apply_props`, so they are undoable like the style panel's.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QMenu, QToolButton,
)

from . import fonts
from .icons import icon
from .items import editing_text_item
from .props import ALIGNS, BORDER_SWATCHES, FILL_SWATCHES, HL_SWATCHES, KEEP_EDITING, SIZE_PRESETS, STYLES, SWATCHES, \
    FontBox, Swatch, style_button

MINI_SWATCHES = 6
TEXT_SWATCHES = 4
WIDTHS = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0]
OPACITIES = [100, 75, 50, 25]


class MiniToolbar(QFrame):
    def __init__(self, win):
        self.win = win
        self.canvas = win.canvas
        super().__init__(self.canvas.viewport())
        self.setObjectName("leafMini")
        self.setAccessibleName("Selection toolbar")
        self.setProperty(KEEP_EDITING, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        from .a11y import high_contrast

        edge = "palette(window-text)" if high_contrast() else "rgba(128,128,128,90)"  # a11y: solid edge in high contrast
        self.setStyleSheet(f"#leafMini {{ background: palette(window); border: 1px solid {edge};"
                           " border-radius: 8px; }"
                           "#leafMini QToolButton { border: none; border-radius: 4px; padding: 2px; }"
                           "#leafMini QToolButton:hover { background: rgba(128,128,128,45); }"
                           "#leafMini QToolButton:checked { background: rgba(128,128,128,80); }"
                           "#leafMini QToolButton::menu-indicator { image: none; width: 0; }")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        # text: font, B I U S, alignment
        self.font_box = FontBox()
        self.font_box.setMinimumContentsLength(9)
        self.font_box.picked.connect(lambda k: self.apply({"font": k}))
        lay.addWidget(self.font_box)
        self.style_btns: dict[str, QToolButton] = {}
        for key, text, tip in STYLES:
            b = style_button(text, tip, key)
            b.clicked.connect(lambda on, k=key: self.apply({k: on}))
            lay.addWidget(b)
            self.style_btns[key] = b
        self.align_btn = self._button("align_left", "Alignment", lambda: None)
        self.align_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        am = QMenu(self.align_btn)
        for key, ic, tip in ALIGNS:
            a = am.addAction(icon(ic), tip, lambda k=key: self.apply({"align": k}))
            a.setCheckable(True)
            a.setData(key)
        am.aboutToShow.connect(lambda m=am: [a.setChecked(a.data() == self._props.get("align")) for a in m.actions()])
        self.align_btn.setMenu(am)
        lay.addWidget(self.align_btn)
        self.sep0 = self._sep()
        lay.addWidget(self.sep0)

        self.swatches: list[Swatch] = []
        for _ in range(MINI_SWATCHES):
            s = Swatch(size=22)
            s.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            s.clicked.connect(lambda _=False, b=s: self.apply({"color": b.color}))
            lay.addWidget(s)
            self.swatches.append(s)
        self.more = self._button("plus", "Custom color…", self._pick)
        lay.addWidget(self.more)
        self.sep1 = self._sep()
        lay.addWidget(self.sep1)
        self.width_btn = self._menu_button("stroke", "Stroke width", [(f"{w:g} pt", {"width": w}) for w in WIDTHS])
        self.size_btn = self._menu_button("text_size", "Font size", [(f"{s} pt", {"size": s}) for s in SIZE_PRESETS])
        self.opacity_btn = self._menu_button("opacity", "Opacity", [(f"{o}%", {"opacity": o / 100}) for o in OPACITIES])
        self.box_btn = self._button("fill", "Box fill and border", lambda: None)
        self.box_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.box_btn.setMenu(self._box_menu())
        for b in (self.width_btn, self.size_btn, self.opacity_btn, self.box_btn):
            lay.addWidget(b)
        self.sep2 = self._sep()
        lay.addWidget(self.sep2)
        self.dup_btn = self._button("duplicate", "Duplicate", self._duplicate)
        self.del_btn = self._button("delete", "Delete (Del)", self.canvas.delete_selected)
        lay.addWidget(self.dup_btn)
        lay.addWidget(self.del_btn)

        self._pressed = False
        self._kind = ""
        self._props: dict = {}
        self._later = QTimer(self, singleShot=True, interval=0, timeout=self.refresh)
        cv = self.canvas
        cv.selectionInfo.connect(self._schedule)
        cv.zoomChanged.connect(self._follow)
        cv.verticalScrollBar().valueChanged.connect(self._follow)
        cv.horizontalScrollBar().valueChanged.connect(self._follow)
        cv.undo.indexChanged.connect(self._schedule)
        cv.viewport().installEventFilter(self)
        self.hide()

    # ------------------------------------------------------------- widgets
    def _button(self, ic: str, tip: str, slot) -> QToolButton:
        b = QToolButton()
        b.setIcon(icon(ic))
        b.setProperty("icon_name", ic)
        b.setIconSize(QSize(18, 18))
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(slot)
        return b

    def _menu_button(self, ic: str, tip: str, entries) -> QToolButton:
        b = self._button(ic, tip, lambda: None)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(b)
        key = next(iter(entries[0][1]))
        for text, delta in entries:
            a = m.addAction(text, lambda d=delta: self.apply(d))
            a.setCheckable(True)
            a.setData(delta[key])
        m.aboutToShow.connect(lambda m=m, k=key: [a.setChecked(abs(float(a.data()) - float(self._props.get(k, -1))) < 1e-6)
                                                  for a in m.actions()])
        b.setMenu(m)
        return b

    def _box_menu(self) -> QMenu:
        m = QMenu(self)
        entries = []

        def section(title: str, key: str, colors: list[str]) -> None:
            m.addSection(title)
            for c in [""] + colors:
                a = m.addAction(_chip(c), c or "None", lambda c=c: self.apply({key: c}))
                a.setCheckable(True)
                entries.append((a, key, c))
            m.addAction("Custom…", lambda: self._pick_box(key))

        section("Fill", "bg", FILL_SWATCHES)
        section("Border", "border", BORDER_SWATCHES)

        def sync() -> None:
            for a, key, c in entries:
                cur = self._props.get(key) or ""
                a.setChecked((QColor(cur).name() if cur else "") == (QColor(c).name() if c else ""))

        m.aboutToShow.connect(sync)
        return m

    def _sep(self) -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setFixedWidth(9)
        f.setStyleSheet("color: rgba(128,128,128,90);")
        return f

    # --------------------------------------------------------------- state
    def _schedule(self, *_) -> None:
        self._later.start()

    def _follow(self, *_) -> None:
        if self.isVisible() or self.canvas.scene().selectedItems():
            self.reposition()

    def _editing(self):
        return editing_text_item(self.canvas.scene())

    def _targets(self) -> list:
        ed = self._editing()
        return [ed] if ed is not None else self.canvas.selected_overlays()

    def _common(self) -> tuple[str, dict, list]:
        ed = self._editing()
        if ed is not None:
            return "text", ed.cursor_props(), [ed]
        sel = self.canvas.selected_overlays()
        if not sel:
            return "", {}, sel
        kinds = {i.kind for i in sel}
        props = dict(sel[0].props)
        for it in sel[1:]:
            props = {k: v for k, v in props.items() if k in it.props}
        return (sel[0].kind if len(kinds) == 1 else "mixed"), props, sel

    def _wanted(self) -> bool:
        cv = self.canvas
        if cv.doc is None or self._pressed:
            return False
        if self._editing() is not None:
            return True
        return cv.tool == "select" and bool(cv.selected_overlays()) and not cv._editing_text()

    def refresh(self) -> None:
        if not self._wanted():
            self.hide()
            return
        kind, props, _ = self._common()
        self._kind, self._props = kind, props
        editing = self._editing() is not None
        has_color = "color" in props
        is_text = "font" in props
        sw = HL_SWATCHES if kind == "highlight" else SWATCHES
        n = TEXT_SWATCHES if is_text else MINI_SWATCHES
        cur = QColor(props.get("color", "#000")).name()
        for i, s in enumerate(self.swatches):
            s.setVisible(has_color and i < min(n, len(sw)))
            if i < len(sw):
                s.set_color(sw[i])
                s.setChecked(QColor(sw[i]).name() == cur)
        self.more.setVisible(has_color)
        self.font_box.setVisible(is_text)
        if is_text:
            self.font_box.set_key(props.get("font") or fonts.DEFAULT)
        for k, b in self.style_btns.items():
            b.setVisible(k in props)
            b.setChecked(bool(props.get(k)))
        self.align_btn.setVisible("align" in props)
        if "align" in props:
            ic = f"align_{props['align']}"
            self.align_btn.setIcon(icon(ic))
            self.align_btn.setProperty("icon_name", ic)
        self.sep0.setVisible(is_text)
        self.width_btn.setVisible("width" in props)
        self.size_btn.setVisible("size" in props)
        self.opacity_btn.setVisible("opacity" in props)
        self.box_btn.setVisible("bg" in props)
        self.sep1.setVisible(has_color and any(k in props for k in ("width", "size", "opacity", "bg")))
        dup = getattr(self.win, "a_duplicate", None)
        self.dup_btn.setVisible(not editing and dup is not None and dup.isEnabled())
        self.del_btn.setVisible(not editing)
        self.sep2.setVisible(not editing and any(k in props for k in ("color", "width", "size", "opacity")))
        self.adjustSize()
        self.reposition()

    def reposition(self) -> None:
        if not self._wanted():
            self.hide()
            return
        r = QRect()
        for it in self._targets():
            r = r.united(self.canvas.mapFromScene(it.sceneBoundingRect()).boundingRect())
        vp = self.canvas.viewport().rect()
        if not r.intersects(vp):
            self.hide()
            return
        gap = 10
        w, h = self.width(), self.height()
        x = r.center().x() - w // 2
        y = r.top() - h - gap
        if y < 6:
            y = r.bottom() + gap
            if y + h > vp.height() - 6:
                y = max(6, r.top() + 6)
        x = max(6, min(x, vp.width() - w - 6))
        self.move(QPoint(x, y))
        if not self.isVisible():
            self.show()
        self.raise_()

    # ------------------------------------------------------------- actions
    def apply(self, delta: dict) -> None:
        fonts.warn_unembeddable(self.win, delta)
        self.canvas.apply_props(delta)
        if hasattr(self.win, "_refresh_props"):
            self.win._refresh_props()
        self.refresh()

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._props.get("color", "#000000")), self.win, "Color")
        if c.isValid():
            self.apply({"color": c.name()})

    def _pick_box(self, key: str) -> None:
        c = QColorDialog.getColor(QColor(self._props.get(key) or "#ffffff"), self.win, "Fill" if key == "bg" else "Border")
        if c.isValid():
            self.apply({key: c.name()})

    def _duplicate(self) -> None:
        a = getattr(self.win, "a_duplicate", None)
        if a is not None:
            a.trigger()

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.Type.MouseButtonPress:
            self._pressed = True
            self.hide()
        elif t == QEvent.Type.MouseButtonRelease:
            self._pressed = False
            self._later.start()
        elif t == QEvent.Type.Resize:
            self._later.start()
        return False

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            for b in self.findChildren(QToolButton):
                n = b.property("icon_name")
                if n:
                    b.setIcon(icon(n))
        super().changeEvent(e)


def _chip(color: str):
    """A small round color icon for menus ('' = none)."""
    from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap

    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(128, 128, 128, 160), 2))
    p.setBrush(QColor(color) if color else QColor(255, 255, 255, 0))
    p.drawEllipse(4, 4, 24, 24)
    if not color:
        p.setPen(QPen(QColor("#d43a2a"), 2.5))
        p.drawLine(9, 23, 23, 9)
    p.end()
    return QIcon(pm)
