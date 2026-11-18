"""History area: command palette, shortcuts, History panel, undo quality, duplicate, crash recovery."""
from __future__ import annotations

import gc
import json
import os
import weakref
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QUndoView

from leaf import commands as C
from leaf import palette, recovery, shortcuts
from leaf.features import history
from leaf.items import LineItem, ShapeItem, TextItem
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path)
    return path


@pytest.fixture
def isolated(tmp_path):
    """Fresh ini-based QSettings and recovery dir per test; no startup recovery prompt."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    recovery.OVERRIDE_DIR = str(tmp_path / "recovery")
    recovery._offered = True
    yield tmp_path
    recovery.OVERRIDE_DIR = None
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


@pytest.fixture
def win(app, isolated, pdf):
    from leaf.window import MainWindow

    w = MainWindow()
    w.show()
    w.open_path(pdf)
    pump(app, 0.2)
    yield w
    w.canvas.undo.setClean()
    w.close()
    pump(app, 0.05)


def key(c, k, text=""):
    c.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier, text))


# ------------------------------------------------------------------- palette
def test_fuzzy_ranking():
    E = palette.Entry
    entries = [E("Rotate left", "Pages › "), E("Rotate right", "Pages › "), E("Zoom in", "View › "),
               E("Fit width", "View › "), E("Delete pages", "Pages › "), E("Disabled rotate", "Pages › ", enabled=False)]
    assert palette.match("rot r", entries)[0].title == "Rotate right"
    assert palette.match("zi", entries)[0].title == "Zoom in"
    assert palette.match("fw", entries)[0].title == "Fit width"
    assert palette.match("pages del", entries)[0].title == "Delete pages"  # words can hit the menu path
    assert palette.match("rotate", entries)[-1].title == "Disabled rotate"  # disabled last
    assert palette.match("xyzzy", entries) == []
    assert len(palette.match("", entries)) == len(entries)


def test_fuzzy_is_fast():
    import time

    entries = [palette.Entry(f"Command number {i} does things", f"Menu{i % 7} › ") for i in range(300)]
    t0 = time.perf_counter()
    for q in ("c", "co", "com", "cmd n", "number 12", "zz"):
        palette.match(q, entries)
    assert (time.perf_counter() - t0) / 6 < 0.01  # well under a frame per keystroke


def test_page_jump(app, pdf):
    c = canvas_for(pdf)
    w = SimpleNamespace(canvas=c)
    e = palette.page_entry(w, "p3")
    assert e.title == "Go to page 3" and e.enabled
    assert palette.page_entry(w, "page 2").title == "Go to page 2"
    assert not palette.page_entry(w, "12").enabled
    assert palette.page_entry(w, "rotate") is None
    c.show()
    pump(app, 0.2)
    e.run()
    pump(app, 0.1)
    assert c.current_index() == 2


def test_palette_in_window(app, win):
    pal = palette.show_palette(win)
    assert pal.isVisible()
    titles = [e.title for e in pal.entries]
    assert "Rotate right" in titles and "Duplicate" in titles
    pal.edit.setText("rotate right")
    e = pal.current_entry()
    assert e.title == "Rotate right" and e.group.startswith("Pages")
    assert e.shortcut  # shortcut shown on the right
    pal.edit.setText("2")
    assert pal.current_entry().title == "Go to page 2"
    pal.edit.setText("rotate right")
    pal.accept_current()
    pump(app, 0.1)
    assert not pal.isVisible()
    assert win.canvas.doc.pages[win.canvas.current_index()].rotation == 90
    assert pal.recent_keys()[0] == win.a_rot_r.objectName()
    pal = palette.show_palette(win)
    assert pal.shown[0].title == "Rotate right"  # recently used first
    pal.hide()


# ----------------------------------------------------------------- shortcuts
def test_shortcut_persistence(app, win, isolated):
    from leaf.window import MainWindow

    a = win.a_open
    assert a.objectName() == "a_open"
    shortcuts.save(win, a, [QKeySequence("Ctrl+Alt+O")])
    shortcuts.apply_saved(win)
    assert a.shortcut() == QKeySequence("Ctrl+Alt+O")
    assert QKeySequence("Ctrl+Alt+O").toString(QKeySequence.SequenceFormat.NativeText) in a.toolTip()
    w2 = MainWindow()  # saved shortcuts apply at install in every window
    try:
        assert w2.a_open.shortcut() == QKeySequence("Ctrl+Alt+O")
    finally:
        w2.close()
    shortcuts.save(win, a, shortcuts.defaults(a))  # back to default -> key removed
    assert not win.settings.contains("shortcuts/a_open")


def test_conflicts_and_tool_keys(app, win):
    dlg = shortcuts.ShortcutsDialog(win)
    find, pal = win.a_find, win.a_command_palette
    dlg.ask_reassign = lambda *a: False
    assert not dlg.assign(find, QKeySequence("Ctrl+K"))
    assert dlg.pending[find][0] == QKeySequence(QKeySequence.StandardKey.Find)
    dlg.ask_reassign = lambda *a: True
    assert dlg.assign(find, QKeySequence("Ctrl+K"))
    assert QKeySequence("Ctrl+K") not in dlg.pending[pal]
    assert QKeySequence("Ctrl+Shift+P") in dlg.pending[pal]  # alternate kept
    rect = win.tool_actions["rect"]
    assert dlg.conflicts(rect, QKeySequence("V")) == [win.tool_actions["select"]]
    assert dlg.assign(rect, QKeySequence("Q"))
    dlg.accept()
    assert find.shortcut() == QKeySequence("Ctrl+K")
    assert QKeySequence("Ctrl+K") not in pal.shortcuts()
    cv = win.canvas
    assert cv.tool_keys.get(Qt.Key.Key_Q) == "rect" and Qt.Key.Key_R not in cv.tool_keys
    assert rect.toolTip().endswith("(Q)")
    key(cv, Qt.Key.Key_Q, "q")
    assert cv.tool == "rect"
    key(cv, Qt.Key.Key_Escape)
    assert cv.tool == "select"
    dlg2 = shortcuts.ShortcutsDialog(win)
    dlg2.reset_all()
    dlg2.accept()
    assert cv.tool_keys.get(Qt.Key.Key_R) == "rect"
    assert pal.shortcuts()[0] == QKeySequence("Ctrl+K")
    assert not [k for k in win.settings.allKeys() if k.startswith("shortcuts/")]


def test_cheat_sheet(app, win):
    win.show_shortcuts()
    sheet = win.findChild(shortcuts.CheatSheet)
    assert sheet.isVisible()
    groups = [sheet.tree.topLevelItem(i).text(0) for i in range(sheet.tree.topLevelItemCount())]
    assert "Edit" in groups and "Mouse and keys" in groups
    sheet.search.setText("rotate")
    visible = [sheet.tree.topLevelItem(i) for i in range(sheet.tree.topLevelItemCount()) if not sheet.tree.topLevelItem(i).isHidden()]
    assert [g.text(0) for g in visible] == ["Pages"]
    sheet.close()
    win.canvas.setFocus()
    app.sendEvent(win.canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Question, Qt.KeyboardModifier.ShiftModifier, "?"))
    assert sheet.isVisible()
    sheet.close()


# ---------------------------------------------------------------- history/undo
def test_history_view(app, win):
    view = win.history_dock.widget()
    assert isinstance(view, QUndoView) and view.emptyLabel() == "Opened file"
    cv = win.canvas
    add(cv, 0, ShapeItem("rect", QRectF(100, 100, 50, 50)))
    add(cv, 0, ShapeItem("ellipse", QRectF(200, 100, 50, 50)))
    assert view.model().rowCount() == 3
    assert win.a_history_panel in win.menus["view"].actions()
    cv.undo.setIndex(0)  # what clicking the first row does
    assert not cv.order[0].overlays()


def test_duplicate_and_select_all(app, pdf):
    c = canvas_for(pdf)
    r = add(c, 0, ShapeItem("rect", QRectF(100, 100, 50, 40), {"color": "#00ff00"}))
    t = add(c, 0, TextItem(QPointF(300, 300), None, "hello"))
    ln = add(c, 0, LineItem("arrow", QPointF(50, 400), QPointF(150, 450)))
    assert history.select_all_on_page(c) == 3
    copies = history.duplicate(c)
    assert len(copies) == 3 and len(c.order[0].overlays()) == 6
    assert c.undo.undoText() == "Duplicate 3 items"
    rc = next(x for x in copies if isinstance(x, ShapeItem))
    assert rc.props["color"] == "#00ff00" and rc.pos() == r.pos() + QPointF(12, 12) and rc.geometry()[1] == r.geometry()[1]
    assert next(x for x in copies if isinstance(x, TextItem)).toPlainText() == "hello"
    assert set(c.selected_overlays()) == set(copies)
    c.undo.undo()
    assert len(c.order[0].overlays()) == 3
    c.undo.redo()
    assert len(c.order[0].overlays()) == 6
    assert t and ln


def test_command_texts_and_merging(app, pdf):
    c = canvas_for(pdf)
    r = add(c, 0, ShapeItem("rect", QRectF(100, 100, 50, 40)))
    assert c.undo.undoText() == "Add rectangle"
    g0 = r.geometry()
    r.set_geometry((g0[0], QRectF(0, 0, 60, 40)))
    c.undo.push(C.SetGeometry(r, g0, r.geometry()))
    g1 = r.geometry()
    r.set_geometry((g1[0], QRectF(0, 0, 80, 40)))
    c.undo.push(C.SetGeometry(r, g1, r.geometry()))
    assert c.undo.count() == 2 and c.undo.undoText() == "Resize rectangle"  # merged
    p0 = QPointF(r.pos())
    for dx in (5, 10):
        old = QPointF(r.pos())
        r.setPos(p0 + QPointF(dx, 0))
        c.undo.push(C.MoveItems({r: (old, QPointF(r.pos()))}))
    assert c.undo.count() == 3 and c.undo.undoText() == "Move rectangle"
    c.undo.push(C.SetProps({r: (dict(r.props), {**r.props, "color": "#111111"})}))
    c.undo.push(C.SetProps({r: (dict(r.props), {**r.props, "color": "#222222"})}))
    assert c.undo.count() == 4 and c.undo.undoText() == "Change color"
    c.undo.undo()
    assert r.props["color"] == "#d43a2a"
    c.undo.undo()
    assert r.pos() == p0
    c.undo.undo()
    assert r.geometry()[1] == g0[1]
    e = add(c, 0, ShapeItem("ellipse", QRectF(10, 10, 5, 5)))
    c.undo.push(C.RemoveItems([r, e]))
    assert c.undo.undoText() == "Delete 2 items"
    assert C.SetFormValues(c, {(1, "name", ()): ("", "x")}).text() == "Fill “name”"


def test_undone_items_are_freed(app, pdf):
    c = canvas_for(pdf)
    ref = weakref.ref(add(c, 0, ShapeItem("rect", QRectF(100, 100, 50, 40))))
    c.undo.undo()
    add(c, 0, ShapeItem("ellipse", QRectF(10, 10, 5, 5)))  # discards the undone command
    gc.collect()
    assert ref() is None


# ------------------------------------------------------------------ recovery
def test_recovery_written_and_removed(app, pdf, isolated):
    c = canvas_for(pdf)
    host = QMainWindow()
    host.canvas = c
    rec = recovery.Recovery(host)
    assert not rec.autosave()  # clean: nothing to do
    add(c, 0, ShapeItem("rect", QRectF(100, 100, 50, 40)))
    assert rec.autosave()
    pdf_path, meta, _ = rec.paths(rec.key)
    assert os.path.exists(pdf_path)
    info = json.load(open(meta, encoding="utf-8"))
    assert info["path"] == c.doc.path and info["pages"] == 3
    assert not rec.autosave()  # unchanged since last autosave
    assert recovery.orphans() == []  # owned by this (live) process
    c.undo.setClean()  # what a successful save does
    assert not os.path.exists(pdf_path) and not os.path.exists(meta)


def test_recovery_restore(app, win, isolated):
    cv = win.canvas
    add(cv, 0, ShapeItem("rect", QRectF(100, 100, 50, 40)))
    rec = win.recovery
    assert rec.autosave()
    pdf_path, meta, _ = rec.paths(rec.key)
    rec.lock.unlock()  # simulate the owning process having crashed
    rec.lock, rec.key = None, None
    found = recovery.orphans()
    assert len(found) == 1 and found[0]["pages"] == 3
    target = recovery.restore(win, found[0])
    pump(app, 0.1)
    assert target is not None and target is not win
    doc = target.canvas.doc
    assert doc.path.endswith("in (recovered).pdf") and doc.converted
    assert not target.canvas.undo.isClean()
    assert not os.path.exists(pdf_path) and not os.path.exists(meta)
    assert target.recovery.key is not None  # protected again under its new name
    assert len(list(target.canvas.doc.page(doc.pages[0]).annots())) == 1  # the rect survived
    target.canvas.undo.setClean()
    target.close()
