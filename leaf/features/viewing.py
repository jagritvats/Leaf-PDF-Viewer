"""Viewing feature: link history, outline dock, page layouts, night mode, full screen / presentation,
per-file position memory, background (out-of-process) rendering."""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QDockWidget, QMenu, QToolBar

from ..outline import OutlinePanel

POS_PREFIX = "viewpos/"
POS_ORDER = "viewpos/_order"
POS_CAP = 200


def _truthy(v, default: bool) -> bool:
    if v is None:
        return default
    return v in (True, 1, "1", "true", "True")


def _action(win, name: str, text: str, slot=None, keys=(), checkable: bool = False) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    a.setCheckable(checkable)
    if slot is not None:
        (a.toggled if checkable else a.triggered).connect(slot)
    sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    a.setToolTip(text.replace("&", "").rstrip("…") + (f"  ({sc})" if sc else ""))
    setattr(win, f"a_{name}", a)
    win.addAction(a)  # shortcuts keep working with the menu bar hidden (full screen)
    return a


def _insert_after(menu, after: QAction | None, items: list) -> None:
    acts = menu.actions()
    before = acts[acts.index(after) + 1] if after in acts and acts.index(after) + 1 < len(acts) else None
    for it in items:
        if isinstance(it, QMenu):
            menu.insertMenu(before, it) if before else menu.addMenu(it)
        elif it is None:
            menu.insertSeparator(before) if before else menu.addSeparator()
        else:
            menu.insertAction(before, it) if before else menu.addAction(it)


# ------------------------------------------------------------ position memory
def position_key(path: str) -> str:
    return POS_PREFIX + hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:20]


def _order(settings) -> list[str]:
    v = settings.value(POS_ORDER) or []
    return [v] if isinstance(v, str) else list(v)


def save_position(settings, canvas) -> None:
    doc = canvas.doc
    if doc is None or not doc.path or not canvas.order or canvas.presenting:
        return
    st = canvas._nav_state()
    i = canvas.current_index()
    frac = st[1] if st else 0.0
    key = position_key(doc.path)
    settings.setValue(key, f"{i};{frac:.4f};{canvas.zoom:.4f};{canvas.fit_mode or ''}")
    order = [k for k in _order(settings) if k != key]
    order.insert(0, key)
    for old in order[POS_CAP:]:
        settings.remove(old)
    settings.setValue(POS_ORDER, order[:POS_CAP])


def restore_position(settings, canvas) -> bool:
    doc = canvas.doc
    if doc is None or not doc.path or not canvas.order:
        return False
    v = settings.value(position_key(doc.path))
    if not v:
        return False
    try:
        i, frac, zoom, fit = str(v).split(";")
        i, frac, zoom = int(i), float(frac), float(zoom)
    except ValueError:
        return False
    if fit == "width":
        canvas.fit_width()
    elif fit == "page":
        canvas.fit_page()
    else:
        canvas.zoom_to(zoom)
    i = max(0, min(i, len(canvas.order) - 1))
    canvas._restore_nav((canvas.order[i].ref.uid, frac))
    return True


# ------------------------------------------------------------ outline dock
class OutlineDock(QDockWidget):
    def __init__(self, win):
        super().__init__("Outline", win)
        self.win = win
        self.setObjectName("outline")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.panel = OutlinePanel(win.canvas)
        self.setWidget(self.panel)

    def closeEvent(self, e) -> None:  # the user closed it: don't pop it up for the next document
        self.win.settings.setValue("view/outline", False)
        super().closeEvent(e)


class _CloseWatcher(QObject):
    def __init__(self, win, fn):
        super().__init__(win)
        self.fn = fn
        win.installEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Close:
            self.fn()
        return False


# ------------------------------------------------------------------ install
class Viewing:
    def __init__(self, win):
        self.win = win
        cv = self.cv = win.canvas
        s = win.settings
        self._screen_state = None
        self._present_state = None
        self._restoring = False

        mode = s.value("view/mode")
        cv.view_mode = mode if mode in ("continuous", "single", "spread") else "continuous"
        cv.cover_alone = _truthy(s.value("view/cover_alone"), True)
        cv.night = _truthy(s.value("view/night"), False)
        # headless (offscreen) runs, i.e. tests, render in-process unless asked otherwise
        if _truthy(s.value("view/worker"), QGuiApplication.platformName() != "offscreen"):
            cv.renderer.enable_worker(True)

        # outline dock, tabbed with the pages panel
        self.dock = OutlineDock(win)
        side = getattr(win, "side_dock", None)
        win.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)
        if side is not None:
            win.tabifyDockWidget(side, self.dock)
            side.raise_()
        self.dock.hide()
        # the window restores saved dock state after install(); with no document there's no outline to show
        QTimer.singleShot(0, lambda: self.dock.setVisible(False) if not self.dock.panel.has_outline() else None)

        A = _action
        self.a_back = A(win, "nav_back", "&Back", cv.nav_back, ["Alt+Left"])
        self.a_forward = A(win, "nav_forward", "&Forward", cv.nav_forward, ["Alt+Right"])
        self.a_outline = self.dock.toggleViewAction()
        self.a_outline.setText("&Outline panel")
        self.a_outline.setObjectName("a_outline")
        self.a_outline.setShortcut(QKeySequence("F8"))
        self.a_outline.setToolTip("Outline panel  (F8)")
        self.a_outline.triggered.connect(lambda on: s.setValue("view/outline", on))
        win.a_outline = self.a_outline
        win.addAction(self.a_outline)

        self.layout_group = QActionGroup(win)
        self.a_modes = {}
        for mode, text, key in [("continuous", "&Continuous", "Ctrl+4"), ("single", "&Single page", "Ctrl+5"),
                                ("spread", "&Two-page spread", "Ctrl+6")]:
            a = A(win, f"view_{mode}", text, None, [key], checkable=True)
            a.triggered.connect(lambda _=False, m=mode: self.set_mode(m))
            self.layout_group.addAction(a)
            self.a_modes[mode] = a
        self.a_modes[cv.view_mode].setChecked(True)
        self.a_cover = A(win, "view_cover_alone", "Cover page &alone", None, checkable=True)
        self.a_cover.setChecked(cv.cover_alone)
        self.a_cover.toggled.connect(self.set_cover_alone)
        self.layout_menu = QMenu("Page &layout", win)
        self.layout_menu.addActions(list(self.a_modes.values()))
        self.layout_menu.addSeparator()
        self.layout_menu.addAction(self.a_cover)

        self.a_night = A(win, "night_mode", "&Night mode (invert pages)", self.set_night, ["Ctrl+Shift+N"], checkable=True)
        self.a_night.setChecked(cv.night)
        self.a_fullscreen = A(win, "full_screen", "&Full screen", self.toggle_full_screen, ["F11"], checkable=True)
        self.a_present = A(win, "presentation", "&Presentation", self.toggle_presentation, ["F5"])
        self.a_bg_render = A(win, "background_render", "Render in &background process", self.set_worker, checkable=True)
        self.a_bg_render.setChecked(cv.renderer.worker is not None)

        view = win.menus["view"]
        _insert_after(view, getattr(win, "a_next", None), [self.a_back, self.a_forward])
        _insert_after(view, getattr(win, "a_props", None), [self.a_outline])
        view.addSeparator()
        view.addMenu(self.layout_menu)
        view.addActions([self.a_night, self.a_fullscreen, self.a_present])
        view.addSeparator()
        view.addAction(self.a_bg_render)

        # spread-aware previous/next page
        for a, d in ((getattr(win, "a_prev", None), -1), (getattr(win, "a_next", None), 1)):
            if a is not None:
                try:
                    a.triggered.disconnect()
                except (RuntimeError, TypeError):
                    pass
                a.triggered.connect(lambda _=False, d=d: cv.next_page(d))

        self._save_timer = QTimer(win, singleShot=True, interval=700, timeout=self.save_position)
        cv.documentChanged.connect(self._document_changed)
        cv.historyChanged.connect(self._update_history)
        cv.pageChanged.connect(lambda _: self._position_changed())
        cv.zoomChanged.connect(lambda _: self._position_changed())
        cv.message.connect(self.message)
        cv.presentationExitRequested.connect(self.exit_presentation)
        _CloseWatcher(win, self._closing)
        self._doc_actions = [self.a_outline, self.a_present, *self.a_modes.values(), self.a_cover, self.a_night,
                             self.a_fullscreen]
        self._update_enabled()
        self._update_history()

    # -------------------------------------------------------------- state
    def _update_enabled(self) -> None:
        has = self.cv.doc is not None
        for a in self._doc_actions:
            a.setEnabled(has)
        self.a_outline.setEnabled(has and self.dock.panel.has_outline())
        self.a_fullscreen.setEnabled(True)

    def _update_history(self) -> None:
        self.a_back.setEnabled(self.cv.can_go_back())
        self.a_forward.setEnabled(self.cv.can_go_forward())

    def _document_changed(self) -> None:
        self.dock.panel.rebuild()
        has = self.dock.panel.has_outline()
        self.dock.setVisible(has and _truthy(self.win.settings.value("view/outline"), True))
        self._update_enabled()
        self._restoring = True
        QTimer.singleShot(0, self._restore)

    def _restore(self) -> None:
        try:
            if self.cv.doc is not None and _truthy(self.win.settings.value("view/restore_position"), True):  # Settings › General
                restore_position(self.win.settings, self.cv)
        finally:
            self._restoring = False

    def _position_changed(self) -> None:
        if not self._restoring and self.cv.doc is not None:
            self._save_timer.start()

    def save_position(self) -> None:
        if not self._restoring:
            save_position(self.win.settings, self.cv)

    def _closing(self) -> None:
        if self._present_state is not None:
            self.exit_presentation()
        self.save_position()

    def message(self, text: str) -> None:
        toast = getattr(self.win, "toast", None)
        if callable(toast):
            toast(text)
        else:
            self.win.statusBar().showMessage(text, 4000)

    # ------------------------------------------------------------- layout
    def set_mode(self, mode: str) -> None:
        self.cv.set_view_mode(mode)
        self.a_modes[mode].setChecked(True)
        self.win.settings.setValue("view/mode", mode)

    def set_cover_alone(self, on: bool) -> None:
        self.cv.set_view_mode(None, cover_alone=on)
        self.win.settings.setValue("view/cover_alone", on)

    def set_night(self, on: bool) -> None:
        self.cv.set_night(on)
        self.win.settings.setValue("view/night", on)

    def set_worker(self, on: bool) -> None:
        running = self.cv.renderer.enable_worker(on)
        self.win.settings.setValue("view/worker", on)
        if on and not running:
            self.a_bg_render.blockSignals(True)
            self.a_bg_render.setChecked(False)
            self.a_bg_render.blockSignals(False)
            self.message("Background rendering isn't available; rendering in the window instead.")

    # -------------------------------------------------------- full screen
    def _chrome(self) -> list:
        w = self.win
        out = [w.menuBar(), w.statusBar(), *w.findChildren(QToolBar)]
        mini = getattr(w, "minibar", None)
        if mini is not None and hasattr(mini, "setVisible"):
            out.append(mini)
        return [x for x in out if x is not None]

    def _hide(self, widgets) -> dict:
        state = {x: x.isVisible() for x in widgets}
        for x in widgets:
            x.hide()
        return state

    @staticmethod
    def _unhide(state: dict) -> None:
        for x, vis in state.items():
            try:
                x.setVisible(vis)
            except RuntimeError:  # widget deleted meanwhile
                pass

    def toggle_full_screen(self, on: bool) -> None:
        w = self.win
        if on and self._screen_state is None:
            self._screen_state = (w.windowState(), self._hide(self._chrome()))
            w.showFullScreen()
            self.a_fullscreen.setShortcuts([QKeySequence("F11"), QKeySequence("Esc")])
            self.cv.setFocus()
        elif not on and self._screen_state is not None:
            ws, vis = self._screen_state
            self._screen_state = None
            self.a_fullscreen.setShortcuts([QKeySequence("F11")])
            self._unhide(vis)
            w.setWindowState(ws & ~Qt.WindowState.WindowFullScreen)
        self.a_fullscreen.blockSignals(True)
        self.a_fullscreen.setChecked(self._screen_state is not None)
        self.a_fullscreen.blockSignals(False)

    def toggle_presentation(self) -> None:
        if self._present_state is None:
            self.enter_presentation()
        else:
            self.exit_presentation()

    def enter_presentation(self) -> None:
        w, cv = self.win, self.cv
        if cv.doc is None or self._present_state is not None:
            return
        if self._screen_state is not None:
            self.toggle_full_screen(False)
        save_position(w.settings, cv)
        docks = [d for d in w.findChildren(QDockWidget)]
        find = getattr(w, "find", None)
        self._present_state = (w.windowState(), self._hide(self._chrome() + docks + ([find] if find else [])))
        w.showFullScreen()
        cv.set_presenting(True)
        cv.setFocus()
        # fit again once the window really is full screen
        QTimer.singleShot(60, lambda: cv.fit_page() if cv.presenting else None)

    def exit_presentation(self) -> None:
        if self._present_state is None:
            return
        ws, vis = self._present_state
        self._present_state = None
        self.cv.set_presenting(False)
        self._unhide(vis)
        self.win.setWindowState(ws)
        self.cv.setFocus()


def install(win) -> None:
    win.viewing = Viewing(win)
    win.outline = win.viewing.dock.panel
