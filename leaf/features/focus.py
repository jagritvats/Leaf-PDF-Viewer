"""Focus mode (View › Focus mode, Ctrl+Shift+F): just the page.

Hides the menu bar, toolbars, status bar and open panels; a floating toolbar over the top of the page (menus,
tools, image, signature, panel toggles, command palette) and a page/zoom bar at the bottom take their place. Both
slide out of the way while you read (wheel, Page Down/Up, Space, arrows) and come back when the mouse moves.
Alt alone or F10 brings the menu bar back for a moment (a menu command or Esc puts it away). Esc, the shortcut or
the menu entry again leave Focus mode and restore exactly what was showing. Every shortcut keeps working, and the
command palette (Ctrl+K) reaches every command.

The state is remembered ("view/focus_mode"): a window that opens a document while it was on goes straight back
into Focus mode. Closing the window leaves Focus mode first, so the saved toolbar and panel layout is the normal one.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QFrame, QHBoxLayout, QLabel, QMenu, QTabBar, QToolBar, QToolButton, QWidget,
)

from .. import style
from ..icons import icon
from ..style.tokens import rgba

KEY = "view/focus_mode"
SHORTCUT = "Ctrl+Shift+F"
SLIDE_MS = 170
SHADOW = 8  # px around a bar for its painted shadow
READING_KEYS = (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp, Qt.Key.Key_Space, Qt.Key.Key_Down, Qt.Key.Key_Up)


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True")


def reduce_motion() -> bool:
    """Windows "Show animations in Windows" off (SPI_GETCLIENTAREAANIMATION): the bars move without sliding."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        on = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0):
            return not on.value
    except Exception:  # noqa: BLE001
        pass
    return False


# ------------------------------------------------------------------------------------------------- bars
class Bar(QWidget):
    """A rounded bar floating over the page view, centered at the top or bottom. Paints its own body and a soft
    shadow (no graphics effect: the page view behind it scrolls at full speed). Slides out of view and back."""

    def __init__(self, canvas, name: str, edge: str):
        super().__init__(canvas)
        self.canvas = canvas
        self.edge = edge
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.shown = True
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(SHADOW + 6, SHADOW + 4, SHADOW + 6, SHADOW + 4)
        self.lay.setSpacing(2)
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        canvas.installEventFilter(self)
        self.restyle()

    def restyle(self) -> None:
        t = style.current()
        ss = ("QToolButton { border: none; border-radius: 14px; padding: 6px; background: transparent; }"
              f"QToolButton:hover {{ background: {rgba(t.hover)}; }}"
              f"QToolButton:checked {{ background: {rgba(t.accent_soft)}; }}"
              "QToolButton::menu-indicator { image: none; width: 0; }"
              f"QFrame#barSep {{ background: {rgba(t.stroke2)}; border: none; }}"
              f"QLabel {{ color: {rgba(t.text2)}; padding: 0 2px; }}"
              f"QLabel#pageNow {{ color: {rgba(t.text)}; font-weight: 600; }}")
        if ss != self.styleSheet():
            self.setStyleSheet(ss)
        self.update()

    def paintEvent(self, e) -> None:
        t = style.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(SHADOW, SHADOW - 2, -SHADOW, -SHADOW - 2)
        p.setPen(Qt.PenStyle.NoPen)
        for i, a in ((6, 6), (4, 10), (2, 16)):
            k = round(a * (2.4 if t.dark else 1))
            p.setBrush(QColor(0, 0, 0, k))
            g = body.adjusted(-i, -i + 3, i, i + 3)
            p.drawRoundedRect(g, g.height() / 2, g.height() / 2)
        p.setPen(QPen(t.stroke2, 1))
        p.setBrush(t.popup)
        b = body.adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(b, b.height() / 2, b.height() / 2)
        p.end()

    def _target(self, shown: bool) -> QPoint:
        vp = self.canvas.viewport().geometry()
        x = vp.x() + (vp.width() - self.width()) // 2
        if self.edge == "top":
            y = vp.y() + 6 if shown else -self.height()
        else:
            y = vp.bottom() - self.height() - 6 if shown else self.canvas.height() + 2
        return QPoint(x, y)

    def place(self) -> None:
        self.adjustSize()
        self._anim.stop()
        self.move(self._target(self.shown))
        self.raise_()

    def set_shown(self, on: bool) -> None:
        if on == self.shown and self._anim.state() != QPropertyAnimation.State.Running:
            return
        self.shown = on
        self._anim.stop()
        if reduce_motion() or not self.isVisible():
            self.move(self._target(on))
            return
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(self._target(on))
        self._anim.start()

    def eventFilter(self, obj, e) -> bool:
        if obj is self.canvas and e.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            if self.isVisible():
                self.place()
        return False

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self.restyle()
        super().changeEvent(e)


def _sep() -> QFrame:
    f = QFrame()
    f.setObjectName("barSep")
    f.setFixedSize(1, 22)
    return f


def _button(a: QAction, size: int = 20) -> QToolButton:
    b = QToolButton()
    b.setDefaultAction(a)
    b.setAutoRaise(True)
    b.setIconSize(QSize(size, size))
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # like the toolbar's tool strip: keys reach these actions directly
    return b


def _ensure_icon(a: QAction, name: str) -> None:
    if a.icon().isNull():
        a.setIcon(icon(name))
        a.setProperty("icon_name", name)


def tool_bar(win) -> Bar:
    bar = Bar(win.canvas, "focusTools", "top")
    bar.setAccessibleName("Focus mode toolbar")
    lay = bar.lay
    menu_btn = QToolButton()
    menu_btn.setIcon(icon("more"))
    menu_btn.setProperty("icon_name", "more")
    menu_btn.setIconSize(QSize(20, 20))
    menu_btn.setToolTip("Menu  (Alt or F10 shows the menu bar)")
    menu_btn.setAccessibleName("Menu")
    menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    m = QMenu(menu_btn)
    for a in win.menuBar().actions():
        if a.menu() is not None:
            m.addMenu(a.menu())
    menu_btn.setMenu(m)
    lay.addWidget(menu_btn)
    lay.addSpacing(4)
    lay.addWidget(_sep())
    lay.addSpacing(4)
    shapes = getattr(win, "shapes_button", None)
    for b in getattr(win, "tool_buttons", []):
        a = b.defaultAction()
        if a is None:
            continue
        nb = _button(a)
        if b.menu() is not None:  # the shapes: same menu, follows the last-used shape
            nb.setMenu(b.menu())
            nb.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        if b is shapes:
            win.canvas.toolChanged.connect(lambda _t, nb=nb, sb=shapes: nb.setDefaultAction(sb.defaultAction())
                                           if sb.defaultAction() is not nb.defaultAction() else None)
        lay.addWidget(nb)
    lay.addSpacing(4)
    lay.addWidget(_sep())
    lay.addSpacing(4)
    for a in (win.a_image, win.a_sign):
        lay.addWidget(_button(a))
    lay.addSpacing(4)
    lay.addWidget(_sep())
    lay.addSpacing(4)
    comments = getattr(win, "a_comments_panel", None)
    palette = getattr(win, "a_command_palette", None)
    if comments is not None:
        _ensure_icon(comments, "comment")
    if palette is not None:
        _ensure_icon(palette, "palette")
    for a in (win.a_sidebar, comments, win.a_props, palette):
        if a is not None:
            lay.addWidget(_button(a))
    exit_btn = _button(win.a_focus_mode)
    lay.addWidget(exit_btn)
    return bar


def page_bar(win) -> Bar:
    cv = win.canvas
    bar = Bar(cv, "focusPage", "bottom")
    bar.setAccessibleName("Page and zoom")
    lay = bar.lay
    lay.setContentsMargins(SHADOW + 6, SHADOW + 2, SHADOW + 10, SHADOW + 2)
    _ensure_icon(win.a_prev, "chevron_up")
    _ensure_icon(win.a_next, "chevron_down")
    bar.now = QLabel("1")
    bar.now.setObjectName("pageNow")
    bar.total = QLabel("of 1")
    lay.addWidget(_button(win.a_prev, 16))
    lay.addWidget(bar.now)
    lay.addWidget(bar.total)
    lay.addWidget(_button(win.a_next, 16))
    lay.addSpacing(6)
    lay.addWidget(_sep())
    lay.addSpacing(6)
    lay.addWidget(_button(win.a_zoom_out, 16))
    bar.zoom = QLabel("100%")
    bar.zoom.setMinimumWidth(bar.zoom.fontMetrics().horizontalAdvance("1600%") + 6)
    bar.zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(bar.zoom)
    lay.addWidget(_button(win.a_zoom_in, 16))
    lay.addWidget(_button(win.a_fit_w, 16))

    def page(i: int) -> None:
        n = len(cv.order)
        fm = bar.now.fontMetrics()
        bar.now.setMinimumWidth(fm.horizontalAdvance("8" * len(str(n))) + 6)  # steady width while paging
        bar.now.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.now.setText(str(i + 1 if n else 0))
        bar.total.setText(f"of {n}")
        bar.now.setAccessibleName(f"Page {i + 1 if n else 0} of {n}")

    def zoom(z: float) -> None:
        bar.zoom.setText(f"{round(z * 100)}%")

    cv.pageChanged.connect(page)
    cv.pagesChanged.connect(lambda: (page(cv.current_index()), bar.place() if bar.isVisible() else None))
    cv.zoomChanged.connect(zoom)
    page(max(0, cv.current_index()))
    zoom(cv.zoom)
    return bar


# ------------------------------------------------------------------------------------------------- mode
class FocusMode(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.saved: list[QWidget] | None = None  # what was showing before (restored on exit)
        self.bars: list[Bar] = []
        self._alt_alone = False
        a = self.action = QAction("&Focus mode", win)
        a.setObjectName("a_focus_mode")
        a.setCheckable(True)
        a.setShortcut(QKeySequence(SHORTCUT))
        a.setIcon(icon("fullscreen_exit"))
        a.setProperty("icon_name", "fullscreen_exit")
        a.setToolTip(f"Focus mode: just the page, tools in a floating bar  ({QKeySequence(SHORTCUT).toString(QKeySequence.SequenceFormat.NativeText)})")
        a.toggled.connect(lambda on: self.set_on(on, remember=True))
        win.a_focus_mode = a
        win.addAction(a)  # the shortcut works with the menu bar hidden
        view = getattr(win, "menus", {}).get("view")
        if view is not None:
            acts = view.actions()
            after = getattr(win, "a_full_screen", None)
            i = acts.index(after) + 1 if after in acts else len(acts)
            view.insertAction(acts[i], a) if i < len(acts) else view.addAction(a)
        cv = win.canvas
        cv.installEventFilter(self)  # Esc, reading keys
        cv.viewport().installEventFilter(self)  # mouse movement, wheel
        win.installEventFilter(self)  # closing
        cv.documentChanged.connect(self._document_changed)
        cv.presentingChanged.connect(lambda _on: self._sync_bars())
        win.menuBar().triggered.connect(lambda _a: self._peek_menu_bar(False))
        a.setEnabled(cv.doc is not None)

    @property
    def on(self) -> bool:
        return self.saved is not None

    # --------------------------------------------------------------- on / off
    def set_on(self, on: bool, remember: bool = False) -> None:
        win = self.win
        if remember:
            win.settings.setValue(KEY, on)
        if on and self.saved is None and win.canvas.doc is not None:
            self.saved = [w for w in self._parts() if w.isVisible()]
            for w in self.saved:
                w.hide()
            for tb in win.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly):
                tb.hide()  # Qt leaves the tab strip of hidden tabbed panels on screen
            if not self.bars:
                self.bars = [tool_bar(win), page_bar(win)]
            QApplication.instance().installEventFilter(self)  # Alt / F10 for the menu bar
            for b in self.bars:
                b.shown = True
            self._sync_bars()
            win.canvas.setFocus()
        elif not on and self.saved is not None:
            QApplication.instance().removeEventFilter(self)
            saved, self.saved = self.saved, None
            for b in self.bars:
                b.hide()
            viewing = getattr(win, "viewing", None)
            show = viewing.hold(saved) if viewing is not None else saved  # full screen / presenting keep theirs
            for w in show:
                try:
                    w.show()
                except RuntimeError:  # deleted meanwhile
                    pass
            if win.canvas.doc is None:
                win.statusBar().hide()
            if win.canvas.doc is not None:
                win.canvas.setFocus()
        self.action.blockSignals(True)
        self.action.setChecked(self.on)
        self.action.blockSignals(False)

    def toggle(self) -> None:
        self.set_on(not self.on, remember=True)

    def _parts(self) -> list[QWidget]:
        """What Focus mode hides: menu bar, toolbars, status bar, panels."""
        win, direct = self.win, Qt.FindChildOption.FindDirectChildrenOnly
        return [win.menuBar(), *win.findChildren(QToolBar, options=direct), win.statusBar(),
                *win.findChildren(QDockWidget, options=direct)]

    def hand_over(self, state: dict) -> dict:
        """Full screen or a presentation ends while Focus mode is on: of what they would show again ({widget:
        visible}), Focus mode's part stays hidden and comes back when Focus mode ends. Returns the rest."""
        parts = self._parts()
        rest = {}
        for w, vis in state.items():
            if w in parts:
                if vis and w not in self.saved:
                    self.saved.append(w)
            else:
                rest[w] = vis
        return rest

    def _sync_bars(self) -> None:
        vis = self.on and not getattr(self.win.canvas, "presenting", False)
        for b in self.bars:
            b.setVisible(vis)
            if vis:
                b.place()

    def _show_bars(self, on: bool) -> None:
        for b in self.bars:
            if b.isVisible():
                b.set_shown(on)

    def _peek_menu_bar(self, show: bool | None = None) -> None:
        if not self.on:
            return
        mb = self.win.menuBar()
        mb.setVisible(not mb.isVisible() if show is None else show)

    def _document_changed(self) -> None:
        cv = self.win.canvas
        self.action.setEnabled(cv.doc is not None)
        if cv.doc is None and self.on:
            self.set_on(False)
        elif cv.doc is not None and not self.on and _truthy(self.win.settings.value(KEY)):
            QTimer.singleShot(0, lambda: self.set_on(True) if self.win.canvas.doc is not None else None)

    def restyle(self) -> None:
        for b in self.bars:
            b.restyle()

    # ----------------------------------------------------------------- events
    def _esc_exits(self) -> bool:
        """Esc first deselects, ends a text selection and goes back to the Select tool; then it leaves."""
        cv = self.win.canvas
        return not (cv.presenting or cv._editing_text() or cv.selected_overlays() or cv._sel_rects
                    or cv.tool != "select" or cv.scene().focusItem() is not None)

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        win, cv = self.win, self.win.canvas
        if obj is win and t == QEvent.Type.Close:
            if self.on:  # the saved layout must be the normal one
                self.set_on(False)
                QTimer.singleShot(0, self._close_cancelled)
            return False
        if not self.on:
            return False
        # (also an application-wide filter while on: Alt alone or F10 anywhere in the window)
        if t == QEvent.Type.KeyPress:
            k = e.key()
            if k == Qt.Key.Key_Alt:
                if not e.isAutoRepeat():
                    self._alt_alone = True
                return False
            self._alt_alone = False
            if k == Qt.Key.Key_F10 and not e.modifiers() and win.isActiveWindow():
                self._peek_menu_bar()
                return True
            if obj is cv:
                if k == Qt.Key.Key_Escape and not e.modifiers():
                    if win.menuBar().isVisible():
                        self._peek_menu_bar(False)
                        return True
                    if self._esc_exits():
                        self.toggle()
                        return True
                elif k in READING_KEYS and not e.modifiers() and cv.scene().focusItem() is None:
                    self._show_bars(False)
            return False
        if t == QEvent.Type.KeyRelease:
            if e.key() == Qt.Key.Key_Alt and self._alt_alone:
                self._alt_alone = False
                if win.isActiveWindow():
                    self._peek_menu_bar()
            return False
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.ApplicationDeactivate):
            self._alt_alone = False
        if obj is cv.viewport():
            if t == QEvent.Type.MouseMove:
                p = e.position().toPoint()
                last = getattr(self, "_last", None)
                if last is None or (p - last).manhattanLength() > 6:
                    self._show_bars(True)
                self._last = p
            elif t == QEvent.Type.Wheel and not (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._show_bars(False)
        return False

    def _close_cancelled(self) -> None:
        try:
            if self.win.isVisible() and _truthy(self.win.settings.value(KEY)):
                self.set_on(True)
        except RuntimeError:  # the window is gone
            pass


def install(win) -> None:
    win.focus_mode = FocusMode(win)
