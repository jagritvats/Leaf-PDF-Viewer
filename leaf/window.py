"""Main window: menus, toolbar, docks, find bar, file handling."""
from __future__ import annotations

import math
import os

import pymupdf
from PySide6.QtCore import QLocale, QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QImage, QKeySequence, QPainterPath, QPalette, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDockWidget, QFileDialog, QGraphicsPathItem, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTabBar, QToolBar, QToolButton,
    QVBoxLayout, QWidget,
)

from . import commands as C
from . import pageops
from .canvas import Canvas
from .document import IMAGE_EXTS, SEARCH_FLAGS, PasswordRequired, PdfDocument
from . import i18n
from .i18n import tr
from .icons import app_icon, icon
from .items import DEFAULTS
from .minibar import MiniToolbar
from .props import PropsPanel
from .sidebar import UID_ROLE, Sidebar
from .signature import SignatureDialog
from .toast import show_toast
from .welcome import WelcomeView, recent_paths

APP = "Leaf"
PDF_FILTER = "PDF files (*.pdf)"
OPEN_FILTER = "Documents (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;PDF files (*.pdf);;All files (*)"
TOOLS = [
    ("select", "Select", "V", "select"),
    ("text", "Add text", "T", "text"),
    ("highlight", "Highlight text", "H", "highlight"),
    ("ink", "Draw", "D", "ink"),
    ("rect", "Rectangle", "R", "rect"),
    ("ellipse", "Ellipse", "E", "ellipse"),
    ("line", "Line", "L", "line"),
    ("arrow", "Arrow", "A", "arrow"),
    ("whiteout", "Whiteout (cover content)", "W", "whiteout"),
]
TOOL_KEYS = {getattr(Qt.Key, f"Key_{k}"): t for t, _, k, _ in TOOLS}
SHAPES = ("rect", "ellipse", "line", "arrow")
ZOOM_LO, ZOOM_HI = 0.1, 8.0  # zoom slider range (log scale)
HINTS = {
    "select": "Drag across text to select it. Click an item to move it, drag corners to resize. Space+drag pans.",
    "text": "Click on the page to type. Esc or click away to finish.",
    "highlight": "Drag across text to highlight it.",
    "ink": "Draw freehand.",
    "rect": "Drag to draw. Shift keeps it square. Click for a default size.",
    "ellipse": "Drag to draw. Shift keeps it round.",
    "line": "Drag to draw. Shift snaps to 45°.",
    "arrow": "Drag to draw. Shift snaps to 45°.",
    "whiteout": "Covers what's underneath with white. Not a redaction: the hidden text stays in the file.",
}
_windows: list["MainWindow"] = []


class FindBar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find in document")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMaximumWidth(320)
        self.count = QLabel()
        self.prev = QToolButton()
        self.prev.setIcon(icon("chevron_up"))
        self.prev.setToolTip("Previous (Shift+Enter)")
        self.next = QToolButton()
        self.next.setIcon(icon("chevron_down"))
        self.next.setToolTip("Next (Enter)")
        close = QToolButton()
        close.setIcon(icon("close"))
        close.setAutoRaise(True)
        close.setToolTip("Close (Esc)")
        for w in (self.edit, self.prev, self.next, self.count):
            lay.addWidget(w)
        lay.addStretch()
        lay.addWidget(close)
        self.hits: list = []  # (page_item, QRectF)
        self.cur = -1
        self._marks: list[QGraphicsPathItem] = []
        self._todo: list = []
        self._query = ""
        self._debounce = QTimer(self, singleShot=True, interval=220, timeout=self._start)
        self._worker = QTimer(self, singleShot=True, interval=0, timeout=self._chunk)
        self.edit.textChanged.connect(lambda _: self._debounce.start())
        self.edit.returnPressed.connect(lambda: self.step(-1 if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier else 1))
        self.prev.clicked.connect(lambda: self.step(-1))
        self.next.clicked.connect(lambda: self.step(1))
        close.clicked.connect(self.close_bar)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(e)

    def open_bar(self) -> None:
        self.show()
        sel = self.win.canvas.selected_text()
        if sel and "\n" not in sel:
            self.edit.setText(sel)
        self.edit.setFocus()
        self.edit.selectAll()

    def close_bar(self) -> None:
        self.clear()
        self.hide()
        self.win.canvas.setFocus()

    def clear(self) -> None:
        self._worker.stop()
        self._todo = []
        for m in self._marks:
            if m.scene() is not None:
                m.scene().removeItem(m)
        self._marks = []
        self.hits = []
        self.cur = -1
        self.count.setText("")

    def _start(self) -> None:
        self.clear()
        self._query = self.edit.text().strip()
        if not self._query or self.win.canvas.doc is None:
            return
        self._todo = list(self.win.canvas.order)
        self.count.setText("Searching…")
        self._worker.start()

    def _chunk(self) -> None:
        doc = self.win.canvas.doc
        from time import perf_counter
        t0 = perf_counter()
        for _ in range(30):
            if not self._todo or perf_counter() - t0 > 0.05:
                break
            pg = self._todo.pop(0)
            provider = getattr(self, "provider", None)  # Find & replace: its own matching (case, word, regex)
            if provider is not None:
                rects = provider(pg, self._query)
            else:
                page = doc.text_page(pg.ref)  # text edits: the page as shown
                m = page.rotation_matrix
                rects = [QRectF(r.x0, r.y0, r.width, r.height) for r in (q * m for q in page.search_for(self._query, flags=SEARCH_FLAGS))]
            if provider is None and pg.ref.src.ocr.get(pg.ref.index):  # ocr: recognized (not yet saved) text, page.rect space
                from .ocr import search_words
                rects += [QRectF(r.x0, r.y0, r.width, r.height) for r in search_words(pg.ref.src.ocr[pg.ref.index], self._query)]
            if rects:
                mark = QGraphicsPathItem(pg.content)
                path = QPainterPath()
                for r in rects:
                    path.addRect(r)
                    self.hits.append((pg, r))
                mark.setPath(path)
                mark.setBrush(QColor(255, 200, 0, 90))
                mark.setPen(Qt.PenStyle.NoPen)
                mark.setZValue(40)
                self._marks.append(mark)
                if self.cur < 0:
                    self.cur = 0
                    self._show_current()
        self._update_count()
        if self._todo:
            self._worker.start()

    def _update_count(self) -> None:
        n = len(self.hits)
        more = "…" if self._todo else ""
        self.count.setText(f"{self.cur + 1 if n else 0} of {n}{more}" if n or not self._todo else "Searching…")
        if not n and not self._todo:
            self.count.setText("No matches")

    def _show_current(self) -> None:
        if not self.hits:
            return
        pg, r = self.hits[self.cur]
        if not hasattr(self, "_curmark") or self._curmark.scene() is None:
            self._curmark = QGraphicsPathItem()
            self._curmark.setBrush(QColor(255, 120, 0, 120))
            self._curmark.setPen(Qt.PenStyle.NoPen)
            self._curmark.setZValue(41)
            self._marks.append(self._curmark)
        self._curmark.setParentItem(pg.content)
        path = QPainterPath()
        path.addRect(r.adjusted(-1, -1, 1, 1))
        self._curmark.setPath(path)
        self.win.canvas.ensure_rect_visible(r, pg.content)

    def step(self, d: int) -> None:
        if not self.hits:
            self._start()
            return
        self.cur = (self.cur + d) % len(self.hits)
        self._show_current()
        self._update_count()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        i18n.install()  # Qt's own + Leaf's translations, pseudo-locale (once per process)
        _windows.append(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.settings = QSettings()
        self.canvas = Canvas(self)
        self.find = FindBar(self)
        self.find.hide()
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.find)
        cl.addWidget(self.canvas)
        self.views = QStackedWidget()
        self._views: dict[str, QWidget] = {}
        self._parked_docks: list[QDockWidget] = []
        self.setCentralWidget(self.views)
        self.add_view("document", central)

        self.sidebar = Sidebar(self.canvas)
        self.side_dock = QDockWidget("Pages", self)
        self.side_dock.setObjectName("pages")
        self.side_dock.setWidget(self.sidebar)
        self.side_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.side_dock)

        self.props = PropsPanel()
        self.props_dock = QDockWidget("Style", self)
        self.props_dock.setObjectName("style")
        self.props_dock.setWidget(self.props)
        self.props_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.props_dock)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_status()
        self._wire()
        self.welcome = WelcomeView(self)
        self.add_view("welcome", self.welcome)
        self.minibar = MiniToolbar(self)
        self.setWindowIcon(app_icon())
        from .features import install_all
        install_all(self)
        self._adopt_page_info()
        self._load_defaults()
        self._update_enabled()
        self._update_title()
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 860)
        if self.settings.value("ui/maximized") in (True, "true", "1", 1):  # restoreGeometry alone loses it on some platforms
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        state = self.settings.value("state")
        if state is not None:
            self.restoreState(state)
        self.setAcceptDrops(True)
        self._sync_view()

    # ----------------------------------------------------------- view stack
    def add_view(self, key: str, widget: QWidget) -> None:
        """Register a central view (e.g. "document", "welcome"); replaces an existing one with the same key."""
        old = self._views.pop(key, None)
        if old is not None and old is not widget:
            self.views.removeWidget(old)
        self._views[key] = widget
        if self.views.indexOf(widget) < 0:
            self.views.addWidget(widget)

    def show_view(self, key: str) -> None:
        w = self._views.get(key)
        if w is not None and self.views.currentWidget() is not w:
            self.views.setCurrentWidget(w)
            if key == "document":
                self.canvas.setFocus()

    def current_view(self) -> str:
        cur = self.views.currentWidget()
        return next((k for k, w in self._views.items() if w is cur), "")

    def _sync_view(self) -> None:
        """Welcome screen without a document, the document otherwise (other views are left alone)."""
        if self.canvas.doc is None:
            if self.current_view() in ("document", ""):
                self.show_view("welcome")
        elif self.current_view() == "welcome":
            self.show_view("document")
        self._doc_chrome(self.canvas.doc is not None)

    def _doc_chrome(self, on: bool) -> None:
        """Panels and the status bar only make sense with a document: tuck them away on the welcome screen."""
        if not on:
            # also catches docks that feature modules added after the first park
            late = [d for d in self.findChildren(QDockWidget) if not d.isHidden()]
            self._parked_docks += late
            for d in late:
                d.hide()
            # Qt leaves the tab strip of tabified docks on screen after hiding them
            for tb in self.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly):
                tb.hide()
        elif on and self._parked_docks:
            for d in self._parked_docks:
                d.show()
            self._parked_docks = []
        self.statusBar().setVisible(on)

    def _adopt_page_info(self) -> None:
        """A feature may add its own page-size label; show that one in place of ours."""
        info = getattr(self, "_page_info_label", None)
        if info is None:
            return
        lay = self.page_size.parentWidget().layout()
        self.statusBar().removeWidget(info)
        lay.insertWidget(lay.indexOf(self.page_size), info)
        info.setContentsMargins(0, 0, 0, 0)
        info.show()
        self.page_size.hide()

    # -------------------------------------------------------------- actions
    def _act(self, text, slot=None, shortcut=None, ic=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if ic:
            a.setIcon(icon(ic))
            a.setProperty("icon_name", ic)
        if shortcut:
            a.setShortcuts(shortcut if isinstance(shortcut, list) else [QKeySequence(shortcut)])
        if checkable:
            a.setCheckable(True)
        if slot:
            a.triggered.connect(slot)
        sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        a.setToolTip(f"{tip or text.replace('&', '').rstrip('…')}" + (f"  ({sc})" if sc else ""))
        return a

    def _build_actions(self) -> None:
        K = QKeySequence.StandardKey
        self.a_open = self._act("&Open…", self.open_dialog, QKeySequence(K.Open), "open")
        self.a_save = self._act("&Save", self.save, QKeySequence(K.Save), "save")
        self.a_save_as = self._act("Save &As…", self.save_as, QKeySequence(K.SaveAs))
        self.a_close = self._act("&Close", self.close, QKeySequence(K.Close))
        self.a_quit = self._act("&Quit", QApplication.closeAllWindows, QKeySequence(K.Quit))
        self.a_undo = self._act("&Undo", self.undo, QKeySequence(K.Undo), "undo")
        self.a_redo = self._act("&Redo", self.redo, [QKeySequence(K.Redo), QKeySequence("Ctrl+Y")], "redo")
        self.a_copy = self._act("&Copy text", self.copy_text, QKeySequence(K.Copy))
        self.a_delete = self._act("&Delete", self.canvas.delete_selected, None, "delete")
        self.a_find = self._act("&Find…", lambda: self.find.open_bar(), QKeySequence(K.Find), "find")
        self.a_hl_sel = self._act("Highlight selected text", self.canvas.highlight_selection, "Ctrl+Shift+H", "highlight")
        self.a_zoom_in = self._act("Zoom &in", lambda: self.canvas.zoom_step(1), [QKeySequence(K.ZoomIn), QKeySequence("Ctrl+=")], "zoom_in")
        self.a_zoom_out = self._act("Zoom &out", lambda: self.canvas.zoom_step(-1), QKeySequence(K.ZoomOut), "zoom_out")
        self.a_zoom_100 = self._act("&Actual size", lambda: self.canvas.zoom_to(1.0), "Ctrl+0")
        self.a_fit_w = self._act("Fit &width", self.canvas.fit_width, "Ctrl+1", "fit_width")
        self.a_fit_p = self._act("Fit &page", self.canvas.fit_page, "Ctrl+2")
        self.a_sidebar = self.side_dock.toggleViewAction()
        self.a_sidebar.setText("&Pages panel")
        self.a_sidebar.setShortcut("F4")
        self.a_sidebar.setIcon(icon("sidebar"))
        self.a_sidebar.setProperty("icon_name", "sidebar")
        self.a_props = self.props_dock.toggleViewAction()
        self.a_props.setText("&Style panel")
        self.a_props.setShortcut("F6")
        self.a_props.setIcon(icon("props"))
        self.a_props.setProperty("icon_name", "props")
        self.a_rot_l = self._act("Rotate &left", lambda: self.rotate_pages(-90), "Ctrl+Shift+L", "rotate_left")
        self.a_rot_r = self._act("Rotate &right", lambda: self.rotate_pages(90), "Ctrl+Shift+R", "rotate_right")
        self.a_del_pages = self._act("&Delete pages", self.delete_pages, "Ctrl+Shift+Del")
        self.a_blank = self._act("Insert &blank page", self.insert_blank, None, "page_add")
        self.a_insert_file = self._act("&Insert pages from file…", self.insert_file_dialog)
        self.a_extract = self._act("&Extract pages…", self.extract_pages)
        self.a_image = self._act("&Image…", self.insert_image_dialog, "Ctrl+Shift+I", "image", tip="Insert image")
        self.a_sign = self._act("&Signature…", self.insert_signature, "Ctrl+Shift+G", "signature", tip="Add signature")
        self.a_prev = self._act("Previous page", lambda: self.canvas.go_to(self.canvas.current_index() - 1), "Ctrl+PgUp")
        self.a_next = self._act("Next page", lambda: self.canvas.go_to(self.canvas.current_index() + 1), "Ctrl+PgDown")

        self.tool_group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for name, text, key, ic in TOOLS:
            a = self._act(text, lambda _=False, n=name: self.canvas.set_tool(n), None, ic, checkable=True)
            a.setToolTip(f"{text}  ({key})")
            self.tool_group.addAction(a)
            self.tool_actions[name] = a
        self.tool_actions["select"].setChecked(True)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        self.menus: dict[str, QMenu] = {}
        m = self.menus["file"] = mb.addMenu(tr("&File"))
        m.addAction(self.a_open)
        self.recent_menu = m.addMenu(tr("Open &Recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        m.addActions([self.a_save, self.a_save_as])
        m.addSeparator()
        m.addAction(self.a_close)
        m.addAction(self.a_quit)
        m = self.menus["edit"] = mb.addMenu(tr("&Edit"))
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_copy, self.a_hl_sel, self.a_delete])
        m.addSeparator()
        m.addAction(self.a_find)
        m = self.menus["view"] = mb.addMenu(tr("&View"))
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_zoom_100, self.a_fit_w, self.a_fit_p])
        m.addSeparator()
        m.addActions([self.a_prev, self.a_next])
        m.addSeparator()
        m.addActions([self.a_sidebar, self.a_props])
        m = self.menus["insert"] = mb.addMenu(tr("&Insert"))
        m.addActions([self.a_image, self.a_sign])
        m.addSeparator()
        m.addActions(list(self.tool_actions.values()))
        self.page_menu = self.menus["pages"] = mb.addMenu(tr("&Pages"))
        self.page_menu.addActions([self.a_rot_l, self.a_rot_r])
        self.page_menu.addSeparator()
        self.page_menu.addActions([self.a_blank, self.a_insert_file, self.a_extract])
        self.page_menu.addSeparator()
        self.page_menu.addAction(self.a_del_pages)
        m = self.menus["help"] = mb.addMenu(tr("&Help"))
        # explicit names: the shortcut settings key must not change with the language (shortcuts.ensure_names)
        m.addAction(tr("&Keyboard shortcuts"), self.show_shortcuts).setObjectName("a_help_keyboard_shortcuts")
        m.addAction(tr("&About Leaf"), lambda: QMessageBox.about(
            self, tr("About Leaf"), f"<b>Leaf</b> 0.1 - a fast, local PDF editor.<br>Engine: MuPDF {pymupdf.mupdf_version}<br>"
            "Nothing leaves your computer.")).setObjectName("a_help_about_leaf")

    def _build_toolbar(self) -> None:
        tb = QToolBar("Tools", self)
        tb.setObjectName("tools")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tb.customContextMenuRequested.connect(self._toolbar_menu)
        self.addToolBar(tb)
        for a, t in ((self.a_open, "Open"), (self.a_save, "Save"), (self.a_undo, "Undo"), (self.a_redo, "Redo"),
                     (self.a_image, "Image"), (self.a_sign, "Sign"), (self.a_find, "Find"), (self.a_sidebar, "Pages"),
                     (self.a_props, "Style")):
            a.setIconText(t)
        tb.addActions([self.a_sidebar])
        tb.addSeparator()
        tb.addActions([self.a_open, self.a_save])
        tb.addSeparator()
        tb.addActions([self.a_undo, self.a_redo])
        tb.addSeparator()

        # tools as one segmented group; the four shapes share a dropdown that shows the last-used one
        self.tool_strip = QWidget()
        self.tool_strip.setObjectName("toolstrip")
        tl = QHBoxLayout(self.tool_strip)
        tl.setContentsMargins(3, 2, 3, 2)
        tl.setSpacing(1)
        self.tool_buttons: list[QToolButton] = []
        for name in ("select", "text", "highlight", "ink", "shapes", "whiteout"):
            b = QToolButton()
            b.setAutoRaise(True)
            b.setIconSize(QSize(20, 20))
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if name == "shapes":
                menu = QMenu(b)
                menu.addActions([self.tool_actions[s] for s in SHAPES])
                b.setMenu(menu)
                b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                last = self.settings.value("ui/last_shape", "rect")
                b.setDefaultAction(self.tool_actions[last if last in SHAPES else "rect"])
                self.shapes_button = b
            else:
                b.setDefaultAction(self.tool_actions[name])
            tl.addWidget(b)
            self.tool_buttons.append(b)
        tb.addWidget(self.tool_strip)
        tb.addSeparator()
        tb.addActions([self.a_image, self.a_sign])
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addActions([self.a_find, self.a_props])
        self.toolbar = tb
        self._set_toolbar_labels(self.settings.value("ui/toolbar_labels", False) in (True, "true", "1", 1))
        self._style_tool_strip()

    def _style_tool_strip(self) -> None:
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        bg = "rgba(255,255,255,13)" if dark else "rgba(0,0,0,10)"
        from .a11y import high_contrast

        if high_contrast():  # a11y: a faint tint vanishes in high-contrast themes; outline the group instead
            self.tool_strip.setStyleSheet("#toolstrip { border: 1px solid palette(window-text); border-radius: 7px; }")
            return
        self.tool_strip.setStyleSheet(f"#toolstrip {{ background: {bg}; border-radius: 7px; }}")

    def _set_toolbar_labels(self, on: bool) -> None:
        style = Qt.ToolButtonStyle.ToolButtonTextUnderIcon if on else Qt.ToolButtonStyle.ToolButtonIconOnly
        self.toolbar.setToolButtonStyle(style)
        for b in self.tool_buttons:
            b.setToolButtonStyle(style)
        self.settings.setValue("ui/toolbar_labels", on)

    def _toolbar_menu(self, pos) -> None:
        m = QMenu(self)
        a = m.addAction("Show text labels")
        a.setCheckable(True)
        a.setChecked(self.toolbar.toolButtonStyle() != Qt.ToolButtonStyle.ToolButtonIconOnly)
        a.toggled.connect(self._set_toolbar_labels)
        m.addSeparator()
        m.addActions([self.a_sidebar, self.a_props])
        m.exec(self.toolbar.mapToGlobal(pos))

    def _tool_changed(self, t: str) -> None:
        a = self.tool_actions.get(t)
        if a is not None:
            a.setChecked(True)
        if t in SHAPES:
            self.shapes_button.setDefaultAction(self.tool_actions[t])
            self.settings.setValue("ui/last_shape", t)
        self.hint_label.setText(HINTS.get(t, "") if self.canvas.doc is not None else "")

    def _build_status(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.hint_label = QLabel()
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint_label.setContentsMargins(6, 0, 0, 0)
        sb.addWidget(self.hint_label, 1)

        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(6)
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.page_spin.setFixedWidth(48)
        self.page_spin.setToolTip("Go to page (Ctrl+PgUp / Ctrl+PgDown)")
        self.page_spin.valueChanged.connect(lambda v: self.canvas.go_to(v - 1))
        self.page_total = QLabel("/ 0")
        self.page_size = QLabel()
        self.page_size.setToolTip("Page size")
        lay.addWidget(self.page_spin)
        lay.addWidget(self.page_total)
        lay.addSpacing(10)
        lay.addWidget(self.page_size)
        lay.addSpacing(10)
        for a in (self.a_zoom_out, None, self.a_zoom_in):
            if a is None:
                self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
                self.zoom_slider.setRange(0, 1000)
                self.zoom_slider.setFixedWidth(120)
                self.zoom_slider.setToolTip("Zoom")
                self.zoom_slider.valueChanged.connect(self._zoom_slider_moved)
                lay.addWidget(self.zoom_slider)
                continue
            b = QToolButton()
            b.setDefaultAction(a)
            b.setAutoRaise(True)
            b.setIconSize(QSize(16, 16))
            lay.addWidget(b)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_box.setMinimumContentsLength(5)
        self.zoom_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.zoom_box.setToolTip("Zoom (Ctrl+wheel, Ctrl+1 fit width, Ctrl+2 fit page)")
        for z in (50, 75, 100, 125, 150, 200, 300, 400):
            self.zoom_box.addItem(f"{z}%", z / 100)
        self.zoom_box.addItem("Fit width", "w")
        self.zoom_box.addItem("Fit page", "p")
        self.zoom_box.activated.connect(self._zoom_box_activated)
        self.zoom_box.lineEdit().editingFinished.connect(self._zoom_box_typed)
        lay.addWidget(self.zoom_box)
        sb.addPermanentWidget(w)

    def _zoom_slider_moved(self, v: int) -> None:
        if self.canvas.doc is not None:
            self.canvas.zoom_to(ZOOM_LO * (ZOOM_HI / ZOOM_LO) ** (v / 1000))

    def _wire(self) -> None:
        cv = self.canvas
        cv.pageChanged.connect(self._page_changed)
        cv.zoomChanged.connect(self._zoom_changed)
        cv.toolChanged.connect(self._tool_changed)
        cv.selectionInfo.connect(self._refresh_props)
        cv.pagesChanged.connect(self._pages_changed)
        cv.filesDropped.connect(self._files_dropped)
        cv.undo.cleanChanged.connect(lambda _: self._update_title())
        cv.undo.canUndoChanged.connect(self.a_undo.setEnabled)
        cv.undo.canRedoChanged.connect(self.a_redo.setEnabled)
        cv.undo.undoTextChanged.connect(lambda t: self.a_undo.setText(f"&Undo {t}".strip()))
        cv.undo.redoTextChanged.connect(lambda t: self.a_redo.setText(f"&Redo {t}".strip()))
        self.props.changed.connect(self._props_changed)
        self.sidebar.pageActivated.connect(cv.go_to)
        self.sidebar.orderChanged.connect(self._reorder)
        self.sidebar.filesDropped.connect(self.insert_files)
        self.sidebar.contextRequested.connect(lambda pos: self.page_menu.exec(pos))
        cv.tool_keys = TOOL_KEYS

    # ---------------------------------------------------------- persistence
    def _load_defaults(self) -> None:
        for kind in DEFAULTS:
            v = self.settings.value(f"defaults/{kind}")
            if isinstance(v, dict):
                self.canvas.defaults[kind].update({k: type(DEFAULTS[kind][k])(x) if not isinstance(DEFAULTS[kind][k], bool)
                                                   else x in (True, "true", "1", 1) for k, x in v.items() if k in DEFAULTS[kind]})

    def _save_defaults(self) -> None:
        for kind, d in self.canvas.defaults.items():
            self.settings.setValue(f"defaults/{kind}", d)

    def _add_recent(self, path: str) -> None:
        rec = [p for p in recent_paths(self.settings) if p != path]
        rec.insert(0, path)
        from .settings import recent_max

        self.settings.setValue("recent", rec[:recent_max(self.settings)])

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        rec = [p for p in recent_paths(self.settings) if os.path.exists(p)]
        for p in rec:
            self.recent_menu.addAction(os.path.basename(p), lambda p=p: self.open_path(p)).setToolTip(p)
        if not rec:
            self.recent_menu.addAction("No recent files").setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list", self.welcome.clear_recent)

    # ------------------------------------------------------------ open/save
    def open_dialog(self) -> None:
        start = os.path.dirname(self.canvas.doc.path) if self.canvas.doc and self.canvas.doc.path else self.settings.value("lastdir", "")
        paths, _ = QFileDialog.getOpenFileNames(self, "Open", start, OPEN_FILTER)
        for p in paths:
            self.open_path(p)

    def open_path(self, path: str) -> None:
        path = os.path.abspath(path)
        if self.canvas.doc is not None:
            other = next((w for w in _windows if w.canvas.doc and w.canvas.doc.path == path), None)
            if other:
                other.raise_()
                other.activateWindow()
                return
            w = MainWindow()
            w.show()
            w.open_path(path)
            return
        password = None
        while True:
            try:
                doc = PdfDocument.open(path, password)
                break
            except PasswordRequired:
                password, ok = QInputDialog.getText(self, "Password", f"{os.path.basename(path)} is protected. Password:",
                                                    QLineEdit.EchoMode.Password)
                if not ok:
                    return
            except Exception as ex:  # noqa: BLE001
                QMessageBox.critical(self, APP, f"Couldn't open {os.path.basename(path)}:\n{ex}")
                return
        if os.path.splitext(path)[1].lower() != ".pdf":
            doc.path = os.path.splitext(path)[0] + ".pdf"
            doc.converted = True
        self.canvas.set_document(doc)
        self.settings.setValue("lastdir", os.path.dirname(path))
        self._add_recent(path)
        self._update_enabled()
        self._update_title()
        self._sync_view()
        self._tool_changed(self.canvas.tool)
        if doc.primary.doc.is_form_pdf:
            self.toast("This document has form fields. Click a field to fill it in.", ms=4500)

    def _commit_edits(self) -> None:
        tool = getattr(self, "textedit_tool", None)  # Edit text: its focus-out commit is deferred, too late for a save
        if tool is not None and tool.editing():
            tool.commit()  # a refused edit stays open with the reason
        self.canvas._clear_focus()

    def save(self) -> bool:
        doc = self.canvas.doc
        if doc is None:
            return False
        if not doc.path or getattr(doc, "converted", False):
            return self.save_as()
        return self._save_to(doc.path)

    def save_as(self) -> bool:
        doc = self.canvas.doc
        if doc is None:
            return False
        path, _ = QFileDialog.getSaveFileName(self, "Save As", doc.path or "", PDF_FILTER)
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if self._save_to(path):
            doc.path = os.path.abspath(path)
            doc.converted = False
            self._add_recent(doc.path)
            self._update_title()
            return True
        return False

    def _save_to(self, path: str, pages=None) -> bool:
        self._commit_edits()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            inc = self.canvas.doc.save(path, self.canvas.overlays_for, pages)
        except Exception as ex:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP, f"Couldn't save:\n{ex}\n\nThe original file was not changed.")
            return False
        QApplication.restoreOverrideCursor()
        if pages is None:
            self.canvas.undo.setClean()
        how = "original bytes and signatures kept" if inc else "full rewrite"
        self.toast(f"Saved {os.path.basename(path)}  ·  {how}")
        return True

    def closeEvent(self, e) -> None:
        if self.canvas.doc is not None:
            self._commit_edits()  # text still being typed counts as a change (else closing loses it unasked)
        if self.canvas.doc is not None and not self.canvas.undo.isClean():
            name = os.path.basename(self.canvas.doc.path or "Untitled")
            r = QMessageBox.question(self, APP, f"Save changes to {name}?",
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel or (r == QMessageBox.StandardButton.Save and not self.save()):
                e.ignore()
                return
        self.settings.setValue("ui/maximized", self._maximized_for_next_time())
        if not self.isFullScreen():  # full screen / presentation: keep the geometry of the normal window
            self.settings.setValue("geometry", self.saveGeometry())
        if not self._parked_docks:  # on the welcome screen the panels are only hidden for the moment
            self.settings.setValue("state", self.saveState())
        self._save_defaults()
        if self in _windows:
            _windows.remove(self)
        self.canvas.undo.blockSignals(True)  # its teardown would otherwise poke already-deleted actions
        e.accept()

    def _maximized_for_next_time(self) -> bool:
        """Maximized now, or maximized before entering full screen / presentation."""
        if self.isFullScreen():
            v = getattr(self, "viewing", None)
            st = getattr(v, "_screen_state", None) or getattr(v, "_present_state", None)
            return bool(st and st[0] & Qt.WindowState.WindowMaximized)
        return self.isMaximized()

    # ---------------------------------------------------------------- pages
    def _page_view(self):
        """The page list the user works in: the organizer when it's showing, else the Pages panel."""
        org = getattr(self, "organizer", None)
        return org if org is not None and org.isVisible() else self.sidebar

    def _target_rows(self) -> list[int]:
        view = self._page_view()
        rows = view.selected_rows() if view is not self.sidebar or view.hasFocus() or len(view.selectedItems()) > 1 else []
        return rows or ([self.canvas.current_index()] if self.canvas.order else [])

    def _state(self):
        return [(r, r.rotation) for r in self.canvas.doc.pages]

    def _select_pages(self, uids: list[int]) -> None:
        """Select pages (by uid) in the page lists and show the first one."""
        if not uids:
            return
        for view in (self.sidebar, getattr(self, "organizer", None)):
            if view is not None:
                view.select_uids(uids)
        row = next((i for i, r in enumerate(self.canvas.doc.pages) if r.uid == uids[0]), -1)
        if row >= 0:
            self.canvas.go_to(row)

    def rotate_pages(self, delta: int) -> None:
        if self.canvas.doc:
            pageops.rotate(self.canvas, self._target_rows(), delta)

    def delete_pages(self) -> None:
        doc = self.canvas.doc
        if not doc:
            return
        rows = self._target_rows()
        if len(set(rows)) >= len(doc.pages):
            QMessageBox.information(self, APP, "A PDF needs at least one page.")
            return
        from .settings import ask_delete_pages

        if not ask_delete_pages(self, len(set(rows))):
            return
        pageops.delete(self.canvas, rows)

    def insert_blank(self, _checked: bool = False, before: bool = False) -> None:
        if not self.canvas.doc:
            return
        rows = self._target_rows() or [0]
        row = min(rows) if before else max(rows) + 1
        self._select_pages(pageops.insert_blank(self.canvas, row, like=min(rows) if before else max(rows)))

    def insert_file_dialog(self) -> None:
        if self.canvas.doc:
            from .features import pages as pages_feature

            pages_feature.insert_file_dialog(self)

    def _insert_source(self, path: str):
        """Load `path` as a new source of the document, asking for a password if needed. None on failure."""
        doc = self.canvas.doc
        try:
            return doc.add_source(path)
        except PasswordRequired:
            pw, ok = QInputDialog.getText(self, "Password", f"{os.path.basename(path)} is protected. Password:", QLineEdit.EchoMode.Password)
            if not ok:
                return None
            try:
                return doc.add_source(path, pw)
            except Exception as ex:  # noqa: BLE001
                QMessageBox.warning(self, APP, f"Couldn't open {os.path.basename(path)}: {ex}")
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, APP, f"Couldn't open {os.path.basename(path)}: {ex}")
        return None

    def insert_files(self, paths: list[str], row: int) -> None:
        doc = self.canvas.doc
        if not doc:
            if paths:
                self.open_path(paths[0])
            return
        from .document import PageRef

        added = []
        for p in paths:
            src = self._insert_source(p)
            if src is not None:
                added += [(PageRef(src, i), 0) for i in range(src.doc.page_count)]
        if added:
            self._select_pages(pageops.insert(self.canvas, added, row, f"Insert {len(added)} page(s)"))

    def extract_pages(self) -> None:
        if self.canvas.doc:
            from .features import pages as pages_feature

            pages_feature.extract_dialog(self)

    def _reorder(self, uids: list[int]) -> None:
        pageops.reorder(self.canvas, uids)

    # --------------------------------------------------------------- insert
    def insert_image_dialog(self) -> None:
        if not self.canvas.doc:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Insert image", self.settings.value("lastdir", ""),
                                              "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if path:
            self._insert_image_file(path)

    def _insert_image_file(self, path: str) -> None:
        img = QImage(path)
        if img.isNull():
            self.toast(f"Couldn't read {os.path.basename(path)} as an image")
            return
        if max(img.width(), img.height()) > 2400:  # keeps the PDF small and rendering fast
            img = img.scaled(2400, 2400, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.canvas.insert_image(img, "image")

    def insert_signature(self) -> None:
        if not self.canvas.doc:
            return
        dlg = SignatureDialog(self)
        if dlg.exec() and dlg.result_image is not None:
            self.canvas.insert_image(dlg.result_image, "signature")
            self.toast("Signature added. Drag to place it, corners to resize.", "Undo", self.canvas.undo.undo)

    # ----------------------------------------------------------------- misc
    def undo(self) -> None:
        fi = self.canvas.scene().focusItem()
        if self.canvas._editing_text() and fi is not None:
            fi.document().undo()
        else:
            self.canvas.undo.undo()

    def redo(self) -> None:
        fi = self.canvas.scene().focusItem()
        if self.canvas._editing_text() and fi is not None:
            fi.document().redo()
        else:
            self.canvas.undo.redo()

    def copy_text(self) -> None:
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit):
            fw.copy()
            return
        if self.canvas._editing_text():
            fi = self.canvas.scene().focusItem()
            QGuiApplication.clipboard().setText(fi.textCursor().selectedText().replace(" ", "\n"))
            return
        t = self.canvas.selected_text()
        if t:
            QGuiApplication.clipboard().setText(t)
            self.toast(f"Copied {len(t)} character{'s' if len(t) != 1 else ''}", ms=1600)

    def toast(self, text: str, action_text: str | None = None, callback=None, ms: int = 2500) -> None:
        show_toast(self, text, action_text, callback, ms)

    def show_shortcuts(self) -> None:
        from .shortcuts import show_cheat_sheet

        show_cheat_sheet(self)  # generated from the live actions, so it includes feature modules and custom keys

    def _files_dropped(self, paths: list[str]) -> None:
        imgs = [p for p in paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        if self.canvas.doc is not None and imgs and not pdfs:
            for p in imgs:
                self._insert_image_file(p)
            return
        for p in pdfs or imgs:
            self.open_path(p)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        self._files_dropped([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def _page_changed(self, i: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(i + 1)
        self.page_spin.blockSignals(False)
        self.sidebar.set_current(i)
        self._update_page_size(i)

    def _update_page_size(self, i: int) -> None:
        order = self.canvas.order
        if not 0 <= i < len(order):
            self.page_size.setText("")
            return
        w, h = order[i].w, order[i].h
        if QLocale().measurementSystem() == QLocale.MeasurementSystem.ImperialUSSystem:
            self.page_size.setText(f"{w / 72:.2f} × {h / 72:.2f} in")
        else:
            self.page_size.setText(f"{w / 72 * 25.4:.0f} × {h / 72 * 25.4:.0f} mm")

    def _pages_changed(self) -> None:
        n = len(self.canvas.doc.pages) if self.canvas.doc else 0
        self.page_spin.setMaximum(max(1, n))
        self.page_total.setText(f"/ {n}")
        self.sidebar.rebuild()
        self._page_changed(self.canvas.current_index())

    def _zoom_changed(self, z: float) -> None:
        self.zoom_box.lineEdit().setText(f"{round(z * 100)}%")
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(round(1000 * math.log(max(ZOOM_LO, min(ZOOM_HI, z)) / ZOOM_LO) / math.log(ZOOM_HI / ZOOM_LO)))
        self.zoom_slider.blockSignals(False)

    def _zoom_box_activated(self, i: int) -> None:
        d = self.zoom_box.itemData(i)
        if d == "w":
            self.canvas.fit_width()
        elif d == "p":
            self.canvas.fit_page()
        else:
            self.canvas.zoom_to(float(d))
        self._zoom_changed(self.canvas.zoom)
        self.canvas.setFocus()

    def _zoom_box_typed(self) -> None:
        t = self.zoom_box.lineEdit().text().strip().rstrip("%")
        try:
            self.canvas.zoom_to(float(t) / 100)
        except ValueError:
            pass
        self._zoom_changed(self.canvas.zoom)

    def _refresh_props(self) -> None:
        sel = self.canvas.selected_overlays()
        if sel:
            kinds = {i.kind for i in sel}
            kind = sel[0].kind if len(kinds) == 1 else "select"
            props = dict(sel[0].props)
            for it in sel[1:]:
                props = {k: v for k, v in props.items() if k in it.props}
            self.props.set_context(kind, props, f"{len(sel)} items selected" if len(sel) > 1 else "")
        else:
            t = self.canvas.tool
            self.props.set_context(t, self.canvas.defaults.get(t, {}), HINTS.get(t, ""))

    def _props_changed(self, delta: dict) -> None:
        self.canvas.apply_props(delta)

    def _update_enabled(self) -> None:
        has = self.canvas.doc is not None
        for a in [self.a_save, self.a_save_as, self.a_find, self.a_zoom_in, self.a_zoom_out, self.a_zoom_100, self.a_fit_w,
                  self.a_fit_p, self.a_rot_l, self.a_rot_r, self.a_del_pages, self.a_blank, self.a_insert_file, self.a_extract,
                  self.a_image, self.a_sign, self.a_hl_sel, self.a_delete, self.a_prev, self.a_next, *self.tool_actions.values()]:
            a.setEnabled(has)
        self.a_undo.setEnabled(self.canvas.undo.canUndo())
        self.a_redo.setEnabled(self.canvas.undo.canRedo())
        for w in (self.zoom_box, self.zoom_slider, self.page_spin, self.shapes_button):
            w.setEnabled(has)
        self.page_spin.parentWidget().setVisible(has)

    def _update_title(self) -> None:
        doc = self.canvas.doc
        if doc is None:
            self.setWindowTitle(APP)
            self.setWindowModified(False)
            return
        self.setWindowTitle(f"{os.path.basename(doc.path or 'Untitled')}[*] — {APP}")
        self.setWindowModified(not self.canvas.undo.isClean())

    def changeEvent(self, e) -> None:
        from PySide6.QtCore import QEvent

        if e.type() == QEvent.Type.PaletteChange:
            for a in self.findChildren(QAction):
                n = a.property("icon_name")
                if n:
                    a.setIcon(icon(n))
            if hasattr(self, "tool_strip"):
                self._style_tool_strip()
        super().changeEvent(e)
