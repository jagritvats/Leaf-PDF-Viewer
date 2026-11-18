"""Edit existing text, UI side: tool wiring, hover outline, inline editor (commit/cancel/refusal), paragraph mode,
geometry on Leaf-rotated pages. A small fake engine stands in for leaf/textedit.py so these tests pin down the
UI contract on their own; test_real_engine_* run against the real engine when it can edit."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QUndoCommand
from PySide6.QtWidgets import QApplication

from leaf import textedit_ui as U
from tests.test_editor import app, canvas_for, pump  # noqa: F401


# ------------------------------------------------------------------------------------------------ fake engine
class FakeRefuse(Exception):
    pass


def _unit(ref, kind, lines, words):
    from leaf.textedit import TextUnit

    boxes = []
    for k in lines:
        ws = [w for w in words if (w[5], w[6]) == k]
        boxes.append((min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws)))
    text = "\n".join(" ".join(w[4] for w in words if (w[5], w[6]) == k) for k in lines)
    bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    b0 = boxes[0]
    return TextUnit(page_uid=ref.uid, kind=kind, key=(ref.uid, tuple(lines)), text=text, bbox=bbox,
                    line_boxes=tuple(boxes), baseline=(b0[0], b0[3] - 12 * 0.22), angle=0.0, size=12.0,
                    color="#000000", family="Helvetica", bold=False, italic=False, align="left")


class FakeEngine:
    Refuse = FakeRefuse

    def __init__(self):
        self.unit_calls = 0
        self.checks = []

    def unit_at(self, doc, ref, x, y, para=False):
        self.unit_calls += 1
        words = ref.src.words(ref.index)
        keys = list(dict.fromkeys((w[5], w[6]) for w in words))
        for k in keys:
            ws = [w for w in words if (w[5], w[6]) == k]
            if min(w[0] for w in ws) - 2 <= x <= max(w[2] for w in ws) + 2 \
                    and min(w[1] for w in ws) - 2 <= y <= max(w[3] for w in ws) + 2:
                return _unit(ref, "para", keys[:2], words) if para else _unit(ref, "line", [k], words)
        return None

    def check(self, doc, ref, unit, new_text=None):
        self.checks.append(new_text)
        if unit.text.startswith("Second"):
            raise FakeRefuse("the text has no reliable Unicode mapping")
        if new_text and "BAD" in new_text:
            raise FakeRefuse("a character is not in the font")
        if new_text and "€" in new_text:
            return ["Uses a fallback font for 1 character"]
        return []


class FakeEdit(QUndoCommand):
    def __init__(self, canvas, ref, unit, new_text):
        super().__init__(f"Edit text “{new_text[:20]}”")
        self.canvas, self.ref, self.unit, self.new_text = canvas, ref, unit, new_text
        self.canvas.fake_edits = getattr(self.canvas, "fake_edits", {})

    def redo(self):
        self.canvas.fake_edits[self.unit.key] = self.new_text

    def undo(self):
        self.canvas.fake_edits.pop(self.unit.key, None)


FakeEdit.__name__ = "EditPageText"


@pytest.fixture
def fake(monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(U, "engine", lambda: eng)
    monkeypatch.setattr(U, "command_class", lambda: FakeEdit)
    return eng


def make_text_pdf(path):
    d = pymupdf.open()
    for i in range(2):
        p = d.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGE{i + 1} The quick brown fox jumps over the lazy dog.", fontsize=12)
        p.insert_text((72, 130), "Second line of text for selection tests.", fontsize=12)
    d.save(path)
    d.close()


@pytest.fixture
def setup(app, tmp_path, fake):
    src = str(tmp_path / "t.pdf")
    make_text_pdf(src)
    c = canvas_for(src)
    toasts = []
    hidden = []
    c.preview_text_hidden = lambda ref, unit, sync=True: hidden.append((ref.uid, unit))
    tool = U.EditTextTool(c, None, lambda text, *a, **k: toasts.append(text))
    QApplication.sendEvent(c.scene(), QEvent(QEvent.Type.WindowActivate))  # offscreen: let items take focus
    c.set_tool(U.TOOL)
    return types.SimpleNamespace(c=c, tool=tool, toasts=toasts, hidden=hidden, eng=fake)


def vp_point(c, page_idx, x, y) -> QPointF:
    pg = c.order[page_idx]
    return QPointF(c.mapFromScene(pg.content.mapToScene(QPointF(x, y))))


def mouse(c, kind, pos: QPointF, button=Qt.MouseButton.LeftButton):
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button
    btn = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button
    ev = QMouseEvent(kind, pos, c.viewport().mapToGlobal(pos), btn, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(c.viewport(), ev)


def click(c, pos: QPointF):
    mouse(c, QEvent.Type.MouseButtonPress, pos)
    mouse(c, QEvent.Type.MouseButtonRelease, pos)


def key(target, k, mods=Qt.KeyboardModifier.NoModifier, text=""):
    ev = QKeyEvent(QEvent.Type.KeyPress, k, mods, text)
    if hasattr(target, "keyPressEvent") and not hasattr(target, "viewport"):
        target.keyPressEvent(ev)
    else:
        QApplication.sendEvent(target, ev)
    return ev


def type_text(ed, text: str):
    cur = ed.textCursor()
    cur.insertText(text)
    ed.setTextCursor(cur)


def replace_all(ed, text: str):
    from PySide6.QtGui import QTextCursor

    cur = ed.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    cur.insertText(text)
    ed.setTextCursor(cur)


# ---------------------------------------------------------------------------------------------------- tests
def test_hover_outline_follows_line_and_is_cached(setup):
    s = setup
    pos = vp_point(s.c, 0, 120, 96)
    mouse(s.c, QEvent.Type.MouseMove, pos)
    s.tool._do_hover()
    h = s.tool.hover
    assert h is not None and h.isVisible() and h.parentItem() is s.c.order[0].content
    assert h.unit.text.startswith("PAGE1") and h.unit.kind == "line"
    assert h.pen().isCosmetic()
    r = h.path().boundingRect()
    bb = QRectF(QPointF(*h.unit.bbox[:2]), QPointF(*h.unit.bbox[2:]))
    assert r.contains(bb) and r.height() < bb.height() + 6
    assert s.c.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    # moving along the same line doesn't hit the engine again
    n = s.eng.unit_calls
    for x in (130, 140, 150, 160):
        mouse(s.c, QEvent.Type.MouseMove, vp_point(s.c, 0, x, 96))
        s.tool._do_hover()
    assert s.eng.unit_calls == n
    # nothing under the pointer: hidden
    mouse(s.c, QEvent.Type.MouseMove, vp_point(s.c, 0, 300, 500))
    s.tool._do_hover()
    assert not h.isVisible()
    # another tool: no outline
    mouse(s.c, QEvent.Type.MouseMove, pos)
    s.tool._do_hover()
    s.c.set_tool("select")
    assert not h.isVisible()


def test_refusal_shows_toast_and_does_not_open(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 100, 126))  # "Second line..." is refused by the fake engine
    assert s.tool.editor is None and not s.hidden
    assert s.toasts and s.toasts[-1] == "The text has no reliable Unicode mapping."
    assert s.c.undo.count() == 0


def test_edit_enter_pushes_one_command(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 120, 96))
    ed = s.tool.editor
    assert ed is not None and ed.parentItem() is s.c.order[0].content
    unit = s.tool.unit
    assert s.hidden == [(s.c.doc.pages[0].uid, unit)]
    assert ed.toPlainText() == unit.text and ed.defaultTextColor().name() == "#000000"
    assert ed.font().bold() is False and ed.font().pixelSize() == 12
    assert s.c._editing_text() or ed.textInteractionFlags() & Qt.TextInteractionFlag.TextEditable
    # inside the editor, Ctrl+Z undoes typing (the document's own stack), not the canvas stack
    type_text(ed, "X")
    ed.document().undo()
    assert ed.toPlainText() == unit.text and s.c.undo.count() == 0
    replace_all(ed, "PAGE1 The quick red fox")
    key(ed, Qt.Key.Key_Return)
    assert s.tool.editor is None
    assert s.c.undo.count() == 1
    cmd = s.c.undo.command(0)
    assert type(cmd).__name__ == "EditPageText" and cmd.new_text == "PAGE1 The quick red fox" and cmd.unit is unit
    assert s.c.fake_edits[unit.key] == "PAGE1 The quick red fox"
    assert s.hidden[-1] == (s.c.doc.pages[0].uid, None)
    # canvas undo reverts the edit
    s.c.undo.undo()
    assert unit.key not in s.c.fake_edits
    s.c.undo.redo()
    assert s.c.fake_edits[unit.key] == "PAGE1 The quick red fox"


def test_escape_cancels_and_restores(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 120, 96))
    ed = s.tool.editor
    replace_all(ed, "something else")
    key(ed, Qt.Key.Key_Escape)
    assert s.tool.editor is None and ed.scene() is None
    assert s.c.undo.count() == 0
    assert s.hidden[-1] == (s.c.doc.pages[0].uid, None)


def test_unchanged_text_pushes_nothing_and_click_outside_commits(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 120, 96))
    key(s.tool.editor, Qt.Key.Key_Return)
    assert s.tool.editor is None and s.c.undo.count() == 0
    click(s.c, vp_point(s.c, 0, 120, 96))
    replace_all(s.tool.editor, "Changed")
    click(s.c, vp_point(s.c, 0, 300, 600))  # empty spot: commits, opens nothing
    assert s.tool.editor is None and s.c.undo.count() == 1


def test_refused_new_text_keeps_editor_and_warnings_bar(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 120, 96))
    ed = s.tool.editor
    replace_all(ed, "costs 5 €")
    s.tool._live_check()
    bar = s.tool.bar
    assert bar.isVisible() and not bar.error and "fallback" in bar.messages[0]
    assert bar.flags() & bar.GraphicsItemFlag.ItemIgnoresTransformations
    replace_all(ed, "BAD text")
    key(ed, Qt.Key.Key_Return)
    assert s.tool.editor is ed and s.c.undo.count() == 0  # stays open
    assert bar.error and "font" in bar.messages[0] and s.toasts
    key(ed, Qt.Key.Key_Escape)
    assert s.tool.editor is None and bar.scene() is None


def test_tab_stays_in_editor_and_paragraph_mode(setup, tmp_path):
    from PySide6.QtCore import QSettings

    s = setup
    st = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    tool = s.tool
    tool.settings = st
    # Tab while hovering toggles paragraph mode and remembers it
    assert tool.para is False
    key(s.c, Qt.Key.Key_Tab)
    assert tool.para is True and st.value(U.SETTING) in (True, "true")
    again = U.EditTextTool(s.c, st)
    assert again.para is True
    s.c.removeEventFilter(again)
    s.c.viewport().removeEventFilter(again)
    # paragraph units: wrapped editor, Shift+Enter is a new line, Enter commits
    mouse(s.c, QEvent.Type.MouseMove, vp_point(s.c, 0, 120, 96))
    tool._do_hover()
    assert tool.hover.unit.kind == "para"
    click(s.c, vp_point(s.c, 0, 120, 96))
    ed = tool.editor
    assert ed is not None and ed.para and ed.textWidth() > 0
    ev = key(s.c, Qt.Key.Key_Tab)  # swallowed by the tool: focus never leaves the editor
    assert tool.editor is ed and "\t" not in ed.toPlainText()
    from PySide6.QtGui import QTextCursor

    cur = ed.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(cur)
    key(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    type_text(ed, "Third")
    assert tool.editor is ed and ed.plain_text().endswith("\nThird")
    key(ed, Qt.Key.Key_Return)
    assert tool.editor is None and s.c.undo.command(0).new_text.endswith("\nThird")
    del ev


def test_paragraph_editor_keeps_line_pitch(app):
    from leaf.textedit import TextUnit

    u = TextUnit(1, "para", (1,), "First line here\nsecond line", (72, 90, 400, 133),
                 ((72, 90, 400, 103), (72, 120, 300, 133)), (72, 100.5), 0.0, 10.5, "#1a2b3c", "Arial", True, True,
                 "left")
    ed = U.InlineEditor(types.SimpleNamespace(closing=False), u, True)
    assert ed.font().bold() and ed.font().italic() and ed.defaultTextColor().name() == "#1a2b3c"
    b0 = ed.mapToParent(QPointF(0, ed.first_baseline()))
    lay = ed.document().firstBlock().next().layout()
    b1 = ed.mapToParent(QPointF(0, lay.position().y() + lay.lineAt(0).y() + lay.lineAt(0).ascent()))
    assert abs(b0.x() - 72) < 0.01 and abs(b0.y() - 100.5) < 0.01
    assert abs((b1.y() - b0.y()) - 30.0) < 0.2  # the original leading, at a fractional font size
    assert abs(ed.textWidth() * ed.scale_f - 328) < 0.5  # wraps at the paragraph's width


def test_line_mode_flattens_newlines(setup):
    s = setup
    click(s.c, vp_point(s.c, 0, 120, 96))
    ed = s.tool.editor
    key(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)  # line mode: Shift+Enter commits too
    assert s.tool.editor is None
    click(s.c, vp_point(s.c, 0, 120, 96))
    replace_all(s.tool.editor, "a\nb")
    key(s.tool.editor, Qt.Key.Key_Return)
    assert s.c.undo.command(0).new_text == "a b"


def _baseline_in_content(ed) -> QPointF:
    return ed.mapToParent(QPointF(0, ed.first_baseline()))


def test_editor_on_baseline_and_rotated_page(setup):
    s = setup
    c = s.c
    click(c, vp_point(c, 0, 120, 96))
    ed, unit = s.tool.editor, s.tool.unit
    b = _baseline_in_content(ed)
    assert abs(b.x() - unit.baseline[0]) < 0.05 and abs(b.y() - unit.baseline[1]) < 0.05
    key(ed, Qt.Key.Key_Escape)
    # Leaf's extra rotation: the editor rides on the page content transform
    ref = c.doc.pages[0]
    ref.rotation = 90
    c.sync_pages()
    pg = c.order[0]
    click(c, vp_point(c, 0, 120, 96))
    ed, unit = s.tool.editor, s.tool.unit
    assert ed is not None and ed.parentItem() is pg.content
    want = pg.content.mapToScene(QPointF(*unit.baseline))
    got = ed.mapToScene(QPointF(0, ed.first_baseline()))
    assert abs(want.x() - got.x()) < 0.05 and abs(want.y() - got.y()) < 0.05
    # the text runs down the screen on a page turned 90°, inside the unit's box
    box = pg.content.mapRectToScene(QRectF(QPointF(*unit.bbox[:2]), QPointF(*unit.bbox[2:])))
    er = ed.mapRectToScene(ed.text_rect())
    assert er.height() > er.width()
    assert abs(er.left() - box.left()) < 3 and abs(er.right() - box.right()) < 3 and abs(er.top() - box.top()) < 3
    key(ed, Qt.Key.Key_Escape)


def test_rotated_text_outline_polygons(setup):
    from dataclasses import replace

    s = setup
    unit = s.eng.unit_at(s.c.doc, s.c.doc.pages[0], 120, 96)
    # a line turned 30° (y down: clockwise on screen); its axis-aligned box from the true rectangle
    import math

    L, h, a = 200.0, 13.8, 30.0
    cx, cy = 300.0, 400.0
    co, si = math.cos(math.radians(a)), math.sin(math.radians(a))
    W, H = L * co + h * si, L * si + h * co
    box = (cx - W / 2, cy - H / 2, cx + W / 2, cy + H / 2)
    ru = replace(unit, angle=a, bbox=box, line_boxes=(box,))
    poly = U.line_polygons(ru)[0]
    side = math.dist((poly[0].x(), poly[0].y()), (poly[1].x(), poly[1].y()))
    assert abs(side - L) < 1.0
    ang = math.degrees(math.atan2(poly[1].y() - poly[0].y(), poly[1].x() - poly[0].x()))
    assert abs(ang - a) < 0.5
    assert U._contains(ru, QPointF(cx, cy)) and not U._contains(ru, QPointF(box[0] + 1, box[1] + 1))


def test_select_tool_double_click_opens_editor(setup):
    s = setup
    c = s.c
    c.set_tool("select")
    pos = vp_point(c, 0, 120, 96)
    mouse(c, QEvent.Type.MouseButtonPress, pos)
    mouse(c, QEvent.Type.MouseButtonRelease, pos)
    mouse(c, QEvent.Type.MouseButtonDblClick, pos)
    ed = s.tool.editor
    assert ed is not None and ed.hasFocus() and c._editing_text()
    # keys typed on the canvas go to the editor: single-key tools don't fire
    key(c, Qt.Key.Key_V, text="v")
    assert c.tool == "select" and "v" in ed.toPlainText()
    key(c, Qt.Key.Key_Tab)
    assert ed.hasFocus() and "	" not in ed.toPlainText()
    # a click elsewhere with the Select tool ends editing and applies the change (one undo step)
    click(c, vp_point(c, 0, 300, 600))
    QApplication.processEvents()
    assert s.tool.editor is None and c.undo.count() == 1 and "v" in c.undo.command(0).new_text


def test_window_wiring(app, tmp_path, fake):
    from PySide6.QtCore import QCoreApplication, QSettings

    from leaf.window import HINTS, MainWindow

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafTextEditTest")
    QSettings().clear()
    w = MainWindow()
    try:
        src = str(tmp_path / "t.pdf")
        make_text_pdf(src)
        w.open_path(src)
        cv = w.canvas
        a = w.a_tool_edittext
        assert a.isEnabled() and a in w.tool_group.actions() and w.tool_actions[U.TOOL] is a
        assert cv.tool_keys.get(Qt.Key.Key_I) == U.TOOL
        assert "Ctrl" not in a.toolTip() and "(I)" in a.toolTip()
        assert a in w.menus["edit"].actions() and w.a_textedit_paragraphs in w.menus["edit"].actions()
        # toolbar: right after the Text tool
        lay = w.tool_strip.layout()
        acts = [lay.itemAt(i).widget().defaultAction() for i in range(lay.count()) if lay.itemAt(i).widget()]
        assert acts.index(a) == acts.index(w.tool_actions["text"]) + 1
        # single key, status hint
        cv.setFocus()
        key(cv, Qt.Key.Key_I, text="i")
        assert cv.tool == U.TOOL and a.isChecked()
        assert w.hint_label.text() == HINTS[U.TOOL] == U.hint(False)
        # paragraph toggle: menu action <-> Tab, persisted, hint follows
        key(cv, Qt.Key.Key_Tab)
        assert w.a_textedit_paragraphs.isChecked() and w.hint_label.text() == U.hint(True)
        assert QSettings().value(U.SETTING) in (True, "true")
        w.a_textedit_paragraphs.setChecked(False)
        assert w.textedit_tool.para is False
        # palette finds it
        from leaf import shortcuts

        names = [" › ".join(p + [shortcuts.clean(x.text())]) for p, x in shortcuts.all_entries(w)]
        assert any("Edit text" in n for n in names)
        # an edit re-runs an open search (the engine re-reads the page's words)
        cv.preview_text_hidden = lambda ref, unit, sync=True: None
        w.find.show()
        w.find.edit.setText("quick")
        w.find._debounce.stop()
        w.textedit_tool.open_at(cv.order[0], QPointF(120, 96))
        replace_all(w.textedit_tool.editor, "PAGE1 The slow brown fox")
        w.textedit_tool.commit()
        assert cv.undo.count() == 1 and w.find._debounce.isActive()
        w.find.close_bar()
        # while editing, single-key tools don't fire
        w.textedit_tool.open_at(cv.order[0], QPointF(120, 96))
        assert w.textedit_tool.editing()
        QApplication.sendEvent(cv.scene(), QEvent(QEvent.Type.WindowActivate))
        cv.scene().setFocusItem(w.textedit_tool.editor)
        assert cv._editing_text()
        key(cv, Qt.Key.Key_V, text="v")
        assert cv.tool == U.TOOL
        # Ctrl+Z (window Undo) while editing undoes typing, not the canvas history
        w.undo()
        assert w.textedit_tool.editor.toPlainText() == w.textedit_tool.unit.text and cv.undo.count() == 1
        w.textedit_tool.cancel()
    finally:
        if w.canvas.doc is not None:
            w.canvas.undo.setClean()
        w.close()
        QSettings().clear()
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])


# ------------------------------------------------------------------------------------------- real engine
def _real_ready() -> bool:
    from leaf import commands as C
    from leaf.canvas import Canvas

    return hasattr(C, "EditPageText") and hasattr(Canvas, "preview_text_hidden")


def make_real_pdf(path):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 100), "Hello quick world line", fontsize=12)
    p.insert_text((300, 400), "Rotated thirty", fontsize=14, morph=(pymupdf.Point(300, 400), pymupdf.Matrix(30)))
    p.insert_text((72, 200), "Para line one is here", fontsize=11)
    p.insert_text((72, 214), "and para line two here", fontsize=11)
    d.save(path)
    d.close()


@pytest.fixture
def real(app, tmp_path):
    src = str(tmp_path / "r.pdf")
    make_real_pdf(src)
    c = canvas_for(src)
    toasts = []
    tool = U.EditTextTool(c, None, lambda text, *a, **k: toasts.append(text))
    QApplication.sendEvent(c.scene(), QEvent(QEvent.Type.WindowActivate))
    c.set_tool(U.TOOL)
    return types.SimpleNamespace(c=c, tool=tool, toasts=toasts)


def test_real_engine_units_drive_hover_and_editor(real):
    """The real engine's units (no commit needed): outline and editor sit on the PDF's own text."""
    c, tool = real.c, real.tool
    hidden = []
    c.preview_text_hidden = lambda ref, unit, sync=True: hidden.append(unit)  # display side may not exist yet
    mouse(c, QEvent.Type.MouseMove, vp_point(c, 0, 100, 96))
    tool._do_hover()
    u = tool.hover.unit
    assert u is not None and u.text == "Hello quick world line" and u.kind == "line"
    click(c, vp_point(c, 0, 100, 96))
    ed = tool.editor
    assert ed is not None and hidden == [u]
    b = _baseline_in_content(ed)
    assert abs(b.x() - u.baseline[0]) < 0.05 and abs(b.y() - u.baseline[1]) < 0.05
    er = ed.mapRectToParent(ed.text_rect())
    box = QRectF(QPointF(*u.bbox[:2]), QPointF(*u.bbox[2:]))
    assert abs(er.left() - box.left()) < 1
    from PySide6.QtGui import QFontDatabase

    if QFontDatabase.families():  # the offscreen platform on Windows has no fonts at all
        assert abs(er.right() - box.right()) < 4  # same font, same width
    key(ed, Qt.Key.Key_Escape)
    # text turned 30° counter-clockwise on the page: the editor turns with it
    click(c, vp_point(c, 0, 330, 382))
    ed, u = tool.editor, tool.unit
    assert ed is not None and u.text == "Rotated thirty"
    d = ed.mapToParent(QPointF(100, 0)) - ed.mapToParent(QPointF(0, 0))
    import math

    assert abs(math.degrees(math.atan2(d.y(), d.x())) - U.qt_angle(u)) < 0.1 and abs(U.qt_angle(u) + 30) < 0.5
    key(ed, Qt.Key.Key_Escape)
    # paragraph mode: the engine's paragraph, wrapped at its width; the live check reports growth
    tool.set_para(True)
    click(c, vp_point(c, 0, 80, 210))
    ed, u = tool.editor, tool.unit
    assert u.kind == "para" and len(u.line_boxes) == 2 and ed.para
    replace_all(ed, u.text + " and a good deal more words to make it grow")
    tool._live_check()
    assert tool.bar.isVisible() and "lines" in " ".join(tool.bar.messages)
    key(ed, Qt.Key.Key_Escape)


@pytest.mark.skipif(not _real_ready(), reason="engine display/commit hooks not in place yet")
def test_real_engine_commit_undo_and_find(real):
    c, tool = real.c, real.tool
    ref = c.doc.pages[0]
    click(c, vp_point(c, 0, 100, 96))
    ed = tool.editor
    replace_all(ed, "Hello brave new world")
    key(ed, Qt.Key.Key_Return)
    assert tool.editor is None and c.undo.count() == 1
    assert c.doc.text_edits[ref.uid]
    u = U.engine().unit_at(c.doc, ref, 100, 96)
    assert u.text == "Hello brave new world"
    assert "brave" in " ".join(w[4] for w in ref.src.words(ref.index, ref.uid))  # selection/find see the new text
    assert c.doc.text_page(ref).search_for("brave")
    c.undo.undo()
    assert not (c.doc.text_edits.get(ref.uid) or {})
    assert U.engine().unit_at(c.doc, ref, 100, 96).text == "Hello quick world line"
