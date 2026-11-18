"""Per-window chrome: panel headers and tab groups with a close button, panels as one surface with rounded corners,
the page view as a rounded well with a small inset, a command search box in the toolbar, the status bar's page box
and elided hint. Everything here only dresses widgets window.py and the feature modules already built: no action,
dock or shortcut moves, so keyboard order, accessible names and saved layouts stay as they are."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QPainter, QPainterPath, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QLabel, QMainWindow, QTabBar, QTabWidget, QToolButton, QWidget,
)

from ..icons import icon
from . import theme
from .tokens import rgba


def T():
    return theme.T


# ------------------------------------------------------------------------------------------------- panels
class DockTitle(QWidget):
    """Panel header: the title (semibold) and a close button. Collapses while the panel sits in a tab group, whose
    tabs are the header then."""

    HEIGHT = 40

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self.dock = dock
        self.setObjectName("dockTitle")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(4)
        self.label = QLabel(dock.windowTitle())
        lay.addWidget(self.label, 1)
        self.close_btn = QToolButton()
        self.close_btn.setIcon(icon("close"))
        self.close_btn.setProperty("icon_name", "close")
        self.close_btn.setIconSize(QSize(14, 14))
        self.close_btn.setAutoRaise(True)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.clicked.connect(dock.close)
        lay.addWidget(self.close_btn)
        self._title(dock.windowTitle())
        dock.windowTitleChanged.connect(self._title)
        self.collapsed = False

    def _title(self, s: str) -> None:
        self.label.setText(s)
        self.close_btn.setToolTip(f"Close {s}")
        self.close_btn.setAccessibleName(f"Close {s} panel")

    def set_collapsed(self, on: bool) -> None:
        if on != self.collapsed:
            self.collapsed = on
            self.updateGeometry()
        self.setVisible(not on)

    def sizeHint(self) -> QSize:
        return QSize(100, 0 if self.collapsed else self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class DockManager(QObject):
    """Panel headers, tab groups with tabs on top (styled as pivots) and a close button at the end of each tab
    strip, the panel surface in the layer color, rounded panel corners."""

    def __init__(self, win: QMainWindow, radius: int):
        super().__init__(win)
        self.win = win
        self.radius = radius
        win.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        self._later = QTimer(self, singleShot=True, interval=0, timeout=self.sync)
        self._known: list[QDockWidget] = []
        win.installEventFilter(self)
        self.sync()

    def adopt(self) -> None:
        for d in self.win.findChildren(QDockWidget):
            if d in self._known:
                continue
            self._known.append(d)
            if d.titleBarWidget() is None:
                d.setTitleBarWidget(DockTitle(d))
            self.dress_content(d)
            d._corners = Corners(d, self.radius)
            d.visibilityChanged.connect(lambda *_: self._later.start())
            d.dockLocationChanged.connect(lambda *_: self._later.start())
            d.topLevelChanged.connect(lambda *_: self._later.start())

    def dress_content(self, d: QDockWidget) -> None:
        """The panel's own surface is the layer color (not the chrome), so the whole panel reads as one card."""
        w = d.widget()
        if w is None:
            return
        w.setAutoFillBackground(True)
        pal = w.palette()
        pal.setColor(QPalette.ColorRole.Window, T().layer)
        w.setPalette(pal)

    def restyle(self) -> None:
        for d in self._known:
            self.dress_content(d)

    def sync(self) -> None:
        self.adopt()
        for d in self._known:
            tb = d.titleBarWidget()
            if isinstance(tb, DockTitle):
                tabbed = not d.isFloating() and any(not o.isHidden() for o in self.win.tabifiedDockWidgets(d))
                tb.set_collapsed(tabbed)
        for bar in self.win.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly):
            self._dress_tabbar(bar)

    def _dress_tabbar(self, bar: QTabBar) -> None:
        bar.setDrawBase(False)
        bar.setExpanding(False)
        bar.setElideMode(Qt.TextElideMode.ElideRight)
        if bar.property("dressed"):
            self._place_close(bar)
            return
        bar.setProperty("dressed", True)
        btn = QToolButton(bar)
        btn.setObjectName("tabClose")
        btn.setIcon(icon("close"))
        btn.setProperty("icon_name", "close")
        btn.setIconSize(QSize(14, 14))
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip("Close panel")
        btn.setAccessibleName("Close panel")
        btn.clicked.connect(lambda: self._close_current(bar))
        bar.installEventFilter(self)
        bar.currentChanged.connect(lambda *_: self._place_close(bar))
        self._place_close(bar)

    def _close_current(self, bar: QTabBar) -> None:
        title = bar.tabText(bar.currentIndex()).replace("&", "")
        d = next((d for d in self._known if d.windowTitle().replace("&", "") == title and not d.isFloating()), None)
        if d is not None:
            d.close()

    def _place_close(self, bar: QTabBar) -> None:
        btn = bar.findChild(QToolButton, "tabClose")
        if btn is not None:
            s = 24
            btn.setGeometry(bar.width() - s - 6, (bar.height() - s) // 2, s, s)
            btn.raise_()
            btn.show()

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if obj is self.win and t in (QEvent.Type.ChildAdded, QEvent.Type.LayoutRequest, QEvent.Type.Show):
            self._later.start()
        elif isinstance(obj, QTabBar) and t in (QEvent.Type.Resize, QEvent.Type.Show):
            self._place_close(obj)
        return False


# ------------------------------------------------------------------------------------------------- rounded wells
class _Corner(QWidget):
    _cache: dict = {}

    def __init__(self, host: QWidget, which: str, radius: int):
        super().__init__(host)
        self.which, self.radius = which, radius
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resize(radius, radius)

    def paintEvent(self, e) -> None:
        t = T()
        if t is None:
            return
        dpr = self.devicePixelRatioF()
        key = (self.which, self.radius, t.chrome.rgba(), dpr)
        pm = _Corner._cache.get(key)
        if pm is None:
            r = float(self.radius)
            pm = QPixmap(round(r * dpr), round(r * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            sq = QPainterPath()
            sq.addRect(QRectF(0, 0, r, r))
            circ = QPainterPath()
            circ.addEllipse(QRectF(0 if self.which[1] == "l" else -r, 0 if self.which[0] == "t" else -r, 2 * r, 2 * r))
            q.fillPath(sq.subtracted(circ), t.chrome)
            q.end()
            _Corner._cache[key] = pm
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()


class Corners(QObject):
    """Rounds a rectangular surface (the page view, a panel) with four tiny mouse-transparent corner widgets that
    paint the chrome color outside an antialiased arc: no masks, no clipping, no effects. Only the four r x r
    squares ever repaint."""

    def __init__(self, host: QWidget, radius: int):
        super().__init__(host)
        self.host = host
        self.corners = [_Corner(host, c, radius) for c in ("tl", "tr", "bl", "br")]
        host.installEventFilter(self)
        self.place()

    def place(self) -> None:
        W, H = self.host.width(), self.host.height()
        for c in self.corners:
            r = c.radius
            c.move(0 if c.which[1] == "l" else W - r, 0 if c.which[0] == "t" else H - r)
            c.raise_()
            c.show()

    def eventFilter(self, obj, e) -> bool:
        if obj is self.host and e.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ChildAdded):
            self.place()
        return False


# ------------------------------------------------------------------------------------------------- toolbar
def _search_css() -> str:
    t = T()
    return (f"QToolButton#commandSearch {{ background: {rgba(t.input if t.dark else t.layer)}; border: 1px solid {rgba(t.stroke2)};"
            f" border-radius: {t.r_m}px; padding: 4px 10px; color: {rgba(t.text2)}; text-align: left; }}"
            f"QToolButton#commandSearch:hover {{ border-color: {rgba(t.text3)}; }}")


def command_search(win) -> QToolButton | None:
    """A "Search commands… Ctrl+K" box in the toolbar: opens the command palette (it is a button, not an input;
    the palette has the input)."""
    a = getattr(win, "a_command_palette", None)
    if a is None:
        return None
    b = QToolButton()
    b.setObjectName("commandSearch")
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setIcon(icon("find"))
    b.setProperty("icon_name", "find")
    b.setIconSize(QSize(16, 16))
    b.setAccessibleName("Search commands")
    b.setMinimumWidth(250)
    b.setCursor(Qt.CursorShape.IBeamCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # like the other toolbar buttons: Ctrl+K is the keyboard way in
    b.clicked.connect(a.trigger)

    def label() -> None:  # follows a customised shortcut
        from PySide6.QtGui import QKeySequence

        sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        b.setText(f" Search commands…{'      ' + sc if sc else ''}")
        b.setToolTip(a.toolTip())
    a.changed.connect(label)
    label()
    b.setStyleSheet(_search_css())
    return b


def tool_strip_css() -> str:
    t = T()
    return (f"#toolstrip {{ background: {rgba(t.layer2 if t.dark else t.layer)}; border: 1px solid {rgba(t.stroke)};"
            f" border-radius: {t.r_l}px; }}")


# ------------------------------------------------------------------------------------------------- status bar
def _page_spin_css() -> str:
    t = T()
    return (f"QSpinBox#pageSpin {{ padding: 1px 6px; border-radius: {t.r_s + 1}px; background: {rgba(t.input)}; }}"
            "QSpinBox#pageSpin::up-button, QSpinBox#pageSpin::down-button { width: 0px; border: none; }"
            "QSpinBox#pageSpin::up-arrow, QSpinBox#pageSpin::down-arrow { image: none; width: 0px; }")


class ElideHint(QObject):
    """Paints a status-bar label's text elided at the end instead of clipped (the label keeps its full text)."""

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Paint:
            p = QPainter(obj)
            p.setPen(T().text2)
            m = obj.contentsMargins()
            r = obj.rect().adjusted(m.left(), m.top(), -m.right(), -m.bottom())
            fm = obj.fontMetrics()
            p.drawText(r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(obj.text(), Qt.TextElideMode.ElideRight, r.width()))
            p.end()
            return True
        return False


def _status(win) -> None:
    sb = win.statusBar()
    sb.setContentsMargins(6, 0, 4, 0)
    spin = win.page_spin
    spin.setObjectName("pageSpin")
    spin.setButtonSymbols(spin.ButtonSymbols.UpDownArrows)  # zero-width buttons (see theme._PopupFilter)
    spin.setStyleSheet(_page_spin_css())
    spin.setFixedWidth(44)
    lab = win.hint_label
    lab.setContentsMargins(8, 0, 12, 0)
    lab._elide = ElideHint(lab)
    lab.installEventFilter(lab._elide)


# ------------------------------------------------------------------------------------------------- install
def install(win) -> None:
    t = T()
    win.canvas.setFrameShape(win.canvas.Shape.NoFrame)
    win.docks = DockManager(win, t.r_l)
    tb = win.toolbar
    tb.setIconSize(QSize(t.tool_icon, t.tool_icon))
    for b in getattr(win, "tool_buttons", []):
        b.setIconSize(QSize(t.tool_icon, t.tool_icon))
    lay = win.tool_strip.layout()
    lay.setContentsMargins(2, 2, 2, 2)
    lay.setSpacing(2)
    win._style_tool_strip()
    b = command_search(win)
    find = getattr(win, "a_find", None)
    if b is not None and find in tb.actions():  # between the tools and Find
        tb.insertWidget(find, b)
        gap = QWidget()
        gap.setFixedWidth(6)
        tb.insertWidget(find, gap)
        win.command_search = b
    _status(win)
    # panels as cards on the chrome: a small inset around everything, the page view rounded like the panels
    win.setContentsMargins(4, 0, 4, 0)
    win.canvas_corners = Corners(win.canvas, t.r_l + 2)


def restyle(win) -> None:
    """After a light/dark or accent switch: everything that copied token colors or icons."""
    docks = getattr(win, "docks", None)
    if docks is not None:
        docks.restyle()
    b = getattr(win, "command_search", None)
    if b is not None:
        b.setStyleSheet(_search_css())
    win.page_spin.setStyleSheet(_page_spin_css())
    for a in win.findChildren(QAction):
        n = a.property("icon_name")
        if n:
            a.setIcon(icon(n))
    for w in win.findChildren(QToolButton):
        n = w.property("icon_name")
        if n:
            w.setIcon(icon(n))
    focus = getattr(win, "focus_mode", None)
    if focus is not None:
        focus.restyle()
    win.update()


def title_font(widget: QWidget, size: float, weight=QFont.Weight.DemiBold) -> None:
    """The display face (Segoe UI Variable Display & co.) at `size` pt, for start-screen titles."""
    t = T()
    f = QFont(t.font_display) if t is not None and t.font_display else QFont(widget.font())
    f.setPointSizeF(size)
    f.setWeight(weight)
    widget.setFont(f)
