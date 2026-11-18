"""Shared driver for the end-to-end workflow tests (tests/test_e2e_*.py).

They drive a real MainWindow the way a person does: mouse events on the canvas viewport at page coordinates, key
presses that go through the shortcut map, QActions, and dialogs found by their widgets. Modal dialogs, message boxes,
file dialogs and popup menus never block: `Driver` patches their `exec()` / static helpers and runs the handler the
test queued for them; anything unexpected is rejected and fails the test.

Offscreen by default. To watch the same scripts on the real platform with screenshots:
    QT_QPA_PLATFORM=windows LEAF_E2E_SHOTS=<dir> pytest tests/test_e2e_workflows.py
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QSettings, Qt, QTimer
from PySide6.QtGui import QKeySequence, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QInputDialog, QMenu, QMessageBox, QWidget,
)

SHOTS = os.environ.get("LEAF_E2E_SHOTS", "")
L = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


def pump(secs: float = 0.2) -> None:
    app = QApplication.instance()
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(cond, secs: float = 10.0, msg: str = "condition") -> None:
    end = time.time() + secs
    while time.time() < end:
        QApplication.instance().processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {msg}")


def shot(widget: QWidget, name: str) -> None:
    """Screenshot for visual review (only when LEAF_E2E_SHOTS names a directory)."""
    if not SHOTS:
        return
    pump(0.3)
    os.makedirs(SHOTS, exist_ok=True)
    widget.grab().save(os.path.join(SHOTS, f"{name}.jpg"), "JPG", 85)


# ------------------------------------------------------------------ mouse
def send_mouse(target: QWidget, kind: QEvent.Type, pos: QPoint, button=Qt.MouseButton.NoButton,
               buttons=Qt.MouseButton.NoButton, mods=NOMOD) -> None:
    lp = QPointF(pos)
    gp = QPointF(target.mapToGlobal(pos))
    ev = QMouseEvent(kind, lp, lp, gp, button, buttons, mods)
    QApplication.sendEvent(target, ev)


def press(target, pos, mods=NOMOD):
    send_mouse(target, QEvent.Type.MouseButtonPress, pos, L, L, mods)


def move(target, pos, held=True, mods=NOMOD):
    send_mouse(target, QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, L if held else Qt.MouseButton.NoButton, mods)


def release(target, pos, mods=NOMOD):
    send_mouse(target, QEvent.Type.MouseButtonRelease, pos, L, Qt.MouseButton.NoButton, mods)


def click(target, pos, mods=NOMOD) -> None:
    move(target, pos, held=False)
    press(target, pos, mods)
    release(target, pos, mods)
    pump(0.05)


def double_click(target, pos) -> None:
    click(target, pos)
    send_mouse(target, QEvent.Type.MouseButtonDblClick, pos, L, L)
    release(target, pos)
    pump(0.05)


def drag(target, a: QPoint, b: QPoint, steps: int = 8, mods=NOMOD) -> None:
    move(target, a, held=False)
    press(target, a, mods)
    for k in range(1, steps + 1):
        p = QPoint(round(a.x() + (b.x() - a.x()) * k / steps), round(a.y() + (b.y() - a.y()) * k / steps))
        move(target, p, mods=mods)
        pump(0.01)
    release(target, b, mods)
    pump(0.05)


# ------------------------------------------------------------------ canvas geometry
def page_point(win, page: int, x: float, y: float, scroll: bool = True) -> QPoint:
    """Viewport position of point (x, y) in page `page`'s display space (page.rect, like overlays)."""
    cv = win.canvas
    pg = cv.order[page]
    if scroll:
        from PySide6.QtCore import QRectF

        cv.ensure_rect_visible(QRectF(x - 20, y - 20, 40, 40), pg.content)
        pump(0.05)
    return cv.mapFromScene(pg.content.mapToScene(QPointF(x, y)))


def word_rect(win, page: int, word: str):
    """(x0, y0, x1, y1) of the first word on display page `page` equal to `word` (page.rect space)."""
    ref = win.canvas.order[page].ref
    for w in ref.src.words(ref.index, ref.uid):
        if w[4].strip(".,") == word:
            return w[:4]
    raise AssertionError(f"word {word!r} not on page {page}")


# ------------------------------------------------------------------ keyboard
def _reset_mods(widget) -> None:
    """QTest leaves QApplication.keyboardModifiers() at the last modifier it released (a real keyboard doesn't):
    a release with no modifiers clears it, so later clicks aren't read as Ctrl/Shift+click."""
    try:
        QTest.keyRelease(widget, Qt.Key.Key_Control, NOMOD)
    except RuntimeError:
        pass


def key(widget, k, mods=NOMOD, text: str | None = None) -> None:
    QTest.keyClick(widget, k, mods)
    if mods != NOMOD:
        _reset_mods(widget)
    pump(0.03)


def shortcut(win, seq: str) -> None:
    """Press a key sequence the way a person does: into the focus widget, through the window's shortcut map."""
    ks = QKeySequence(seq)
    combo = ks[0]
    target = QApplication.focusWidget()
    if target is None or not target.isVisible() or target.window() is not QApplication.activeWindow():
        target = win
    QTest.keyClick(target, combo.key(), combo.keyboardModifiers())
    if combo.keyboardModifiers() != NOMOD:
        _reset_mods(target if target is not None and QApplication.focusWidget() is None else (QApplication.focusWidget() or win))
    pump(0.05)


def type_text(widget, text: str) -> None:
    QTest.keyClicks(widget, text)
    pump(0.03)


def activate(win) -> None:
    win.show()
    win.raise_()
    win.activateWindow()
    QTest.qWaitForWindowActive(win, 2000)
    pump(0.05)


# ------------------------------------------------------------------ dialogs
class _MenuWatch(QObject):
    """QMenu.exec can't be patched from Python (shiboken binds it per instance): watch for popups instead and
    drive them inside their own event loop."""

    def __init__(self, driver):
        super().__init__()
        self.driver = driver

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Show and isinstance(obj, QMenu) and obj.isWindow():
            QTimer.singleShot(0, lambda m=obj: self.driver._menu_shown(m))
        return False


class Driver:
    """Runs queued handlers for modal dialogs, message boxes, file dialogs and menus; rejects unexpected ones."""

    def __init__(self, monkeypatch):
        self.queue: list[tuple[str, object]] = []  # (kind, handler)
        self.unexpected: list[str] = []
        self.seen: list[str] = []
        mp = monkeypatch
        drv = self

        def dlg_exec(dlg, *a, **k):
            return drv._run("dialog", dlg, f"dialog {type(dlg).__name__} {dlg.windowTitle()!r}", QDialog.DialogCode.Rejected)

        mp.setattr(QDialog, "exec", dlg_exec)
        mp.setattr(QMessageBox, "exec", dlg_exec)
        mp.setattr(QFileDialog, "exec", dlg_exec)
        mp.setattr(QInputDialog, "exec", dlg_exec)
        for name in ("question", "information", "warning", "critical"):
            mp.setattr(QMessageBox, name, staticmethod(
                lambda *a, _n=name, **k: drv._run("msg", (_n, a, k), f"QMessageBox.{_n} {a[1:3] if len(a) > 2 else a}",
                                                  QMessageBox.StandardButton.Cancel if _n == "question" else QMessageBox.StandardButton.Ok)))
        mp.setattr(QMessageBox, "about", staticmethod(lambda *a, **k: drv._run("msg", ("about", a, k), "about", None)))
        for name in ("getSaveFileName", "getOpenFileName", "getOpenFileNames", "getExistingDirectory"):
            empty = ([], "") if name == "getOpenFileNames" else "" if name == "getExistingDirectory" else ("", "")
            mp.setattr(QFileDialog, name, staticmethod(
                lambda *a, _n=name, _e=empty, **k: drv._run("file", (_n, a, k), f"QFileDialog.{_n} {a[1:2]}", _e)))
        for name in ("getText", "getItem", "getInt", "getDouble", "getMultiLineText"):
            mp.setattr(QInputDialog, name, staticmethod(
                lambda *a, _n=name, **k: drv._run("input", (_n, a, k), f"QInputDialog.{_n} {a[1:3]}", ("", False))))
        self._watch = _MenuWatch(self)
        QApplication.instance().installEventFilter(self._watch)

    def close(self) -> None:
        QApplication.instance().removeEventFilter(self._watch)

    def on(self, kind: str, handler) -> "Driver":
        """Queue a handler: kind is 'dialog' (handler(dlg) -> exec result), 'msg' (handler((name, args, kw)) ->
        button), 'file' (handler(...) -> what the static function returns), 'input', or 'menu' (handler(menu) ->
        the QAction to pick, or None to dismiss it)."""
        self.queue.append((kind, handler))
        return self

    def _run(self, kind, obj, desc, default):
        self.seen.append(desc)
        i = next((i for i, (k, _) in enumerate(self.queue) if k == kind), -1)  # first queued of this kind
        if i >= 0:
            _, h = self.queue.pop(i)
            return h(obj)
        import traceback

        where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_stack()[-8:-2][::-1])
        self.unexpected.append(f"{desc} [{where}]")
        if kind in ("dialog",) and isinstance(obj, QDialog):
            obj.reject()
        return default

    def _menu_shown(self, menu: QMenu) -> None:
        try:
            if not menu.isVisible():
                return
        except RuntimeError:
            return
        a = self._run("menu", menu, f"menu {[x.text() for x in menu.actions()]}", None)
        p = menu.parentWidget()
        back = p.window() if p is not None else None
        if a is None:
            menu.close()
        else:
            menu.setActiveAction(a)
            QTest.keyClick(menu, Qt.Key.Key_Return)
        if back is not None and back.isVisible():  # offscreen doesn't hand activation back after a popup
            back.activateWindow()

    def check(self) -> None:
        assert not self.unexpected, f"unexpected popups: {self.unexpected}"
        assert not self.queue, f"expected popups never appeared: {[k for k, _ in self.queue]}"


def ok_button(dlg):
    """The dialog's accept button (default button of its QDialogButtonBox, else the first AcceptRole/YesRole)."""
    from PySide6.QtWidgets import QDialogButtonBox

    for bb in dlg.findChildren(QDialogButtonBox):
        btns = [b for b in bb.buttons() if bb.buttonRole(b) in (QDialogButtonBox.ButtonRole.AcceptRole,
                                                                 QDialogButtonBox.ButtonRole.YesRole)]
        for b in btns:
            if getattr(b, "isDefault", lambda: False)():
                return b
        if btns:
            return btns[0]
    return None


def accept_with(fn=None, button: str | None = None):
    """Dialog handler: show the dialog, let the test fill it (fn(dlg); a non-None return is the exec result), then
    click its OK button (or the button whose text contains `button`) like a person; returns the dialog result."""
    def h(dlg):
        dlg.show()
        pump(0.1)
        r = fn(dlg) if fn else None
        if r is None:
            b = find_button(dlg, button) if button else ok_button(dlg)
            if b is None:
                dlg.accept()
            else:
                assert b.isEnabled(), f"{b.text()!r} is disabled in {dlg.windowTitle()!r}"
                b.click()
            pump(0.05)
            r = dlg.result()
        dlg.hide()
        parent = dlg.parentWidget()
        if parent is not None:  # like the end of a real modal: the window behind it is active again
            activate(parent.window())
        return r
    return h


def choose(text: str):
    """Menu handler: pick the action whose text contains `text`."""
    def h(menu):
        for a in menu.actions():
            if text in a.text().replace("&", ""):
                return a
        raise AssertionError(f"no {text!r} in menu {[a.text() for a in menu.actions()]}")
    return h


def find_button(w: QWidget, text: str):
    """The visible button labelled `text`: an exact label first, then one that starts with it, then contains it."""
    from PySide6.QtWidgets import QAbstractButton

    btns = [b for b in w.findChildren(QAbstractButton) if b.isVisibleTo(w)]
    norm = lambda b: b.text().replace("&", "").rstrip("…. ").lower()  # noqa: E731
    t = text.lower().rstrip("…. ")
    for test in (lambda n: n == t, lambda n: n.startswith(t), lambda n: t in n):
        for b in btns:
            if test(norm(b)):
                return b
    raise AssertionError(f"no button {text!r} in {w}: {[b.text() for b in btns]}")


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def sandbox(app, tmp_path, monkeypatch):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafE2E")
    QSettings().clear()
    from leaf import recovery

    # crash-recovery copies from earlier (killed) runs would pop a "Restore unsaved changes" box mid-test
    monkeypatch.setattr(recovery, "OVERRIDE_DIR", str(tmp_path / "recovery"))
    yield
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


@pytest.fixture
def driver(monkeypatch):
    d = Driver(monkeypatch)
    yield d
    d.close()


@pytest.fixture
def windows(sandbox, driver):
    """new_window() -> a shown MainWindow; all are closed (discarding changes) at the end."""
    from leaf import window as W

    made = []

    def new_window(w=1200, h=860):
        win = W.MainWindow()
        win.resize(w, h)
        activate(win)
        made.append(win)
        return win

    yield new_window
    for win in list(W._windows) + made:
        try:
            if win.canvas.doc is not None:
                win._commit_edits()  # closeEvent would commit text still being typed and ask to save
                win.canvas.undo.setClean()
            win.close()
        except RuntimeError:
            pass
    pump(0.1)


def open_in(win, path: str) -> None:
    win.open_path(path)
    wait_until(lambda: win.canvas.doc is not None and win.canvas.order, msg="document open")
    pump(0.3)
    win.canvas.setFocus()


# ------------------------------------------------------------------ drag and drop
def drag_and_drop(monkeypatch, source_vp: QWidget, start: QPoint, target_vp: QWidget, target: QPoint) -> None:
    """A real mouse drag from `start` on `source_vp` (the widget starts its QDrag itself); the platform's
    drag loop is replaced by delivering DragEnter/DragMove/Drop to `target_vp` at `target`."""
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDrag, QDropEvent

    done = []

    def fake_exec(drag, *a, **k):
        src = drag.source()
        monkeypatch.setattr(QDropEvent, "source", lambda self: src)
        md = drag.mimeData()
        acts = Qt.DropAction.MoveAction | Qt.DropAction.CopyAction
        pos = QPointF(target)
        for ev in (QDragEnterEvent(pos, acts, md, L, NOMOD), QDragMoveEvent(pos, acts, md, L, NOMOD)):
            QApplication.sendEvent(target_vp, ev)
        drop = QDropEvent(pos, acts, md, L, NOMOD)
        QApplication.sendEvent(target_vp, drop)
        done.append(drop.isAccepted())
        return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction

    monkeypatch.setattr(QDrag, "exec", fake_exec)
    move(source_vp, start, held=False)
    press(source_vp, start)
    dist = QApplication.startDragDistance() + 4
    for k in range(1, 6):
        move(source_vp, start + QPoint(0, dist * k))
        pump(0.01)
        if done:
            break
    release(source_vp, start + QPoint(0, dist * 5))
    pump(0.1)
    assert done, "the drag never started"
    assert done[0], "the drop was refused"


def thumb_point(sidebar, row: int) -> QPoint:
    it = sidebar.item(row)
    sidebar.scrollToItem(it)
    pump(0.02)
    return sidebar.visualItemRect(it).center()


def diag(win, driver=None) -> str:
    fw = QApplication.focusWidget()
    aw = QApplication.activeWindow()
    tops = [f"{type(w).__name__}:{w.windowTitle()!r}" for w in QApplication.topLevelWidgets() if w.isVisible()]
    out = f"focus={type(fw).__name__ if fw else None} active_is_win={aw is win} active={type(aw).__name__ if aw else None} visible_tops={tops}"
    if driver is not None:
        out += f" popups={driver.seen} unexpected={driver.unexpected}"
    return out


def save_keys(win, driver=None) -> None:
    """Ctrl+S, then wait until the document is clean."""
    shortcut(win, "Ctrl+S")
    wait_until(lambda: win.canvas.undo.isClean(), msg="save; " + diag(win, driver))


# ------------------------------------------------------------------ documents and checks
def labelled_pdf(path: str, labels: list[str]) -> str:
    """One A4 page per label, the label as large text at the top left."""
    import pymupdf

    d = pymupdf.open()
    for t in labels:
        d.new_page(width=595, height=842).insert_text((72, 100), t, fontsize=24)
    d.save(path)
    d.close()
    return path


def page_labels(win) -> list[str]:
    """First word of each page's own text, in the current page order ("" for a blank page)."""
    out = []
    for r in win.canvas.doc.pages:
        t = r.src.doc[r.index].get_text().strip()
        out.append(t.split()[0] if t else "")
    return out


def find_count(win, q: str) -> int:
    """Ctrl+F, type `q`, wait for the search to finish, Esc; returns the number of hits."""
    shortcut(win, "Ctrl+F")
    assert win.find.isVisible() and win.find.edit.hasFocus()
    win.find.edit.selectAll()
    type_text(win.find.edit, q)
    wait_until(lambda: win.find.count.text() not in ("", "Searching…") and not win.find._todo
               and not win.find._debounce.isActive(), msg="find")
    n = len(win.find.hits)
    key(win.find.edit, Qt.Key.Key_Escape)
    return n
