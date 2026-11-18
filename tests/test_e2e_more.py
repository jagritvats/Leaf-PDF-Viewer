"""End-to-end: crash recovery, extract with redactions, page deletion with overlays, authored form fields, a
flattened copy."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.e2e_kit import (  # noqa: F401
    CTRL, find_count, labelled_pdf, page_labels, accept_with, activate, click, drag, driver, find_button, key, open_in, page_point, pump, sandbox,
    save_keys, shortcut, shot, thumb_point, type_text, wait_until, windows, word_rect,
)
from tests.test_editor import app, make_pdf  # noqa: F401


def test_crash_recovery_restores_edits(app, windows, driver, tmp_path, monkeypatch):
    from leaf import recovery
    from leaf.items import TextItem

    src = labelled_pdf(str(tmp_path / "work.pdf"), ["W1", "W2"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 100, 400))
    type_text(cv, "Unsaved words")
    key(cv, Qt.Key.Key_Escape)
    assert [i for i in cv.order[0].overlays() if isinstance(i, TextItem)]
    driver.on("dialog", accept_with(lambda d: (d.text.selectAll(), type_text(d.text, "DRAFT"))[-1]))
    win.a_watermark.trigger()
    cv.setFocus()
    shortcut(win, "Ctrl+Shift+R")
    assert win.recovery.autosave(), "no recovery copy written"
    # "crash": the process dies without closing the window, so its lock is released by the OS
    win.recovery.lock.unlock()
    win.recovery.periodic.stop()
    win.recovery.idle.stop()
    win.recovery.key = None  # the dead process can't clean up
    cv.undo.setClean()  # (and must not discard the copy as the fixture closes this window)

    monkeypatch.setattr(recovery, "_offered", False)
    win2 = windows()
    box = None

    def found():
        nonlocal box
        box = next((w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()
                    and "Restore" in w.windowTitle()), None)
        return box is not None
    wait_until(found, secs=5, msg="restore offer")
    shot(box, "r1_restore_offer")
    find_button(box, "Restore").click()
    from leaf import window as W
    wait_until(lambda: any(w.canvas.doc is not None and "recovered" in (w.canvas.doc.path or "") for w in W._windows),
               msg="restored document")
    rw = next(w for w in W._windows if w.canvas.doc is not None and "recovered" in (w.canvas.doc.path or ""))
    doc = rw.canvas.doc
    assert len(doc.pages) == 2
    t = doc.primary.doc[0].get_text()
    assert "Unsaved words" in t and "DRAFT" in t and "W1" in t, t
    assert doc.primary.doc[0].rotation == 90
    assert rw.isWindowModified()
    out = str(tmp_path / "rescued.pdf")
    driver.on("file", lambda call: (out, ""))
    activate(rw)
    shortcut(rw, "Ctrl+S")  # a recovered copy asks where to save
    wait_until(lambda: os.path.exists(out), msg="rescued saved")
    driver.check()


def test_extract_applies_redactions_and_overlays(app, windows, driver, tmp_path):
    src = str(tmp_path / "x.pdf")
    make_pdf(src, rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_X)
    w = word_rect(win, 0, "lazy")
    my = (w[1] + w[3]) / 2
    drag(vp, page_point(win, 0, w[0] + 1, my), page_point(win, 0, w[2] - 1, my, scroll=False))
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 0, 100, 300), page_point(win, 0, 200, 380, scroll=False))
    out = str(tmp_path / "x-page1.pdf")
    driver.on("dialog", accept_with(lambda d: (d.range.setText("1"), None)[1]))
    driver.on("file", lambda call: (out, ""))
    driver.on("dialog", accept_with(button="Save and remove"))
    win.a_extract.trigger()
    wait_until(lambda: os.path.exists(out), msg="extracted")
    with pymupdf.open(out) as d:
        assert d.page_count == 1
        t = d[0].get_text()
        assert "lazy" not in t and "quick" in t, t
        assert [a.type[1] for a in d[0].annots()] == ["Square"]
    assert not cv.undo.isClean()  # extracting doesn't save the document
    driver.check()


def test_delete_page_with_overlays_then_undo(app, windows, driver, tmp_path):
    from leaf.items import ShapeItem

    src = labelled_pdf(str(tmp_path / "d.pdf"), ["D1", "D2", "D3"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 1, 100, 300), page_point(win, 1, 200, 380, scroll=False))
    sb = win.sidebar
    click(sb.viewport(), thumb_point(sb, 1))
    key(sb, Qt.Key.Key_Delete)
    assert page_labels(win) == ["D1", "D3"]
    cv.setFocus()
    shortcut(win, "Ctrl+Z")
    assert page_labels(win) == ["D1", "D2", "D3"]
    assert [i for i in cv.order[1].overlays() if isinstance(i, ShapeItem)], "the rectangle didn't come back"
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert [len(list(p.annots())) for p in d] == [0, 1, 0]
    driver.check()


def test_new_form_field_fill_and_flatten(app, windows, driver, tmp_path):
    src = labelled_pdf(str(tmp_path / "f.pdf"), ["Form"])
    win = windows()
    open_in(win, src)
    cv = win.canvas

    def props(d):
        d.name.selectAll()
        type_text(d.name, "email")
    driver.on("dialog", accept_with(props))
    win.a_add_text_field.trigger()
    sel = cv.selected_overlays()
    assert len(sel) == 1 and sel[0].props["name"] == "email"
    shot(win, "n1_new_field")
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert [w.field_name for w in d[0].widgets()] == ["email"]

    # reopen: the new field is a real one now; fill it in
    win.canvas.undo.setClean()
    win.close()
    win2 = windows()
    open_in(win2, src)
    f = [f for f in win2.canvas._all_fields() if f.name == "email"]
    assert f, "field not shown after reopening"
    r = f[0].r
    click(win2.canvas.viewport(), page_point(win2, 0, r.center().x(), r.center().y()))
    type_text(win2.canvas, "me@example.com")
    key(win2.canvas, Qt.Key.Key_Return)
    assert f[0].value() == "me@example.com"
    flat = str(tmp_path / "f-flat.pdf")
    driver.on("file", lambda call: (flat, ""))
    win2.a_save_flattened.trigger()
    wait_until(lambda: os.path.exists(flat), msg="flattened copy")
    with pymupdf.open(flat) as d:
        assert not list(d[0].widgets()) and "me@example.com" in d[0].get_text()
    assert not win2.canvas.undo.isClean()  # the open document keeps its (unsaved) value
    driver.check()


# ------------------------------------------------------------- save / close while still typing
def test_save_while_typing_keeps_what_was_typed(app, windows, driver, tmp_path):
    from PySide6.QtGui import QTextCursor

    from tests.e2e_kit import move

    src = str(tmp_path / "typing.pdf")
    make_pdf(src, rotate_second=False, form=True)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    # 1) a new text box, Ctrl+S while the cursor is still in it
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 100, 500))
    type_text(cv, "Typed then saved")
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert "Typed then saved" in d[0].get_text()
    # 2) a form field, Ctrl+S while typing in it
    cv.setFocus()
    key(cv, Qt.Key.Key_V)
    click(vp, page_point(win, 0, 150, 210))
    type_text(cv, "Field text")
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert {w.field_name: w.field_value for w in d[0].widgets()}["name"] == "Field text"
    # 3) the Edit text tool, Ctrl+S with the inline editor open
    cv.setFocus()
    key(cv, Qt.Key.Key_I)
    s = word_rect(win, 0, "Second")
    at = page_point(win, 0, s[0] + 4, (s[1] + s[3]) / 2)
    move(vp, at, held=False)
    click(vp, at)
    tool = win.textedit_tool
    assert tool.editing()
    c = tool.editor.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    tool.editor.setTextCursor(c)
    type_text(cv, " INLINE")
    shortcut(win, "Ctrl+S")
    pump(0.3)
    with pymupdf.open(src) as d:
        assert "INLINE" in d[0].get_text(), "Ctrl+S during an inline text edit didn't save the edit"
    assert cv.undo.isClean() and not win.isWindowModified(), "saved, but the window says there are unsaved changes"
    driver.check()


def test_close_while_typing_asks_to_save(app, windows, driver, tmp_path):
    from PySide6.QtGui import QTextCursor

    from tests.e2e_kit import move

    src = labelled_pdf(str(tmp_path / "close.pdf"), ["Close me"])
    asked = []

    def discard(call):
        asked.append(call[1][2] if len(call[1]) > 2 else "")
        return QMessageBox.StandardButton.Cancel

    # a new text box still being typed
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 100, 500))
    type_text(cv, "Not yet committed")
    driver.on("msg", discard)
    shortcut(win, "Ctrl+F4")
    assert asked, "closing with a text box being typed didn't ask to save"
    assert win.isVisible()
    # the Edit text tool with the inline editor open
    cv.setFocus()
    key(cv, Qt.Key.Key_Escape)
    win.canvas.undo.setClean()
    key(cv, Qt.Key.Key_I)
    s = word_rect(win, 0, "Close")
    at = page_point(win, 0, s[0] + 4, (s[1] + s[3]) / 2)
    move(vp, at, held=False)
    click(vp, at)
    assert win.textedit_tool.editing()
    c = win.textedit_tool.editor.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    win.textedit_tool.editor.setTextCursor(c)
    type_text(cv, "d")
    asked.clear()
    driver.on("msg", discard)
    shortcut(win, "Ctrl+F4")
    assert asked, "closing with an inline text edit open didn't ask to save"
    driver.check()


def test_page_numbers_on_a_cropped_page_stay_visible(app, windows, driver, tmp_path):
    from tests.e2e_kit import choose

    src = labelled_pdf(str(tmp_path / "cropnum.pdf"), ["C1", "C2"])
    win = windows()
    open_in(win, src)
    cv = win.canvas
    # crop page 1 to its top half with the crop tool
    key(cv, Qt.Key.Key_C)
    driver.on("menu", choose("Crop this page"))
    drag(cv.viewport(), page_point(win, 0, 20, 20), page_point(win, 0, 575, 420, scroll=False))
    assert cv.doc.pages[0].crop is not None
    driver.on("dialog", accept_with())  # header & footer: "Page {page} of {pages}" centred in the footer
    win.a_header_footer.trigger()
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        p = d[0]
        hits = p.search_for("Page 1 of 2")
        assert hits, p.get_text()
        assert p.cropbox.contains(hits[0]), f"page number at {hits[0]} is outside the visible (cropped) page {p.cropbox}"
        assert d[1].search_for("Page 2 of 2")
    driver.check()


def test_two_page_spread_draw_on_right_page(app, windows, driver, tmp_path):
    src = labelled_pdf(str(tmp_path / "spread.pdf"), ["L1", "R2", "L3"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    shortcut(win, "Ctrl+6")
    pump(0.2)
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 1, 100, 300), page_point(win, 1, 200, 380, scroll=False))
    shot(win, "s1_spread")
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert [len(list(p.annots())) for p in d] == [0, 1, 0]
    driver.check()


def test_reduce_size_copy_includes_unsaved_edits(app, windows, driver, tmp_path):
    src = labelled_pdf(str(tmp_path / "big.pdf"), ["B1", "B2"])
    out = str(tmp_path / "big-small.pdf")
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 0, 100, 300), page_point(win, 0, 200, 380, scroll=False))
    sb = win.sidebar
    click(sb.viewport(), thumb_point(sb, 0))
    shortcut(win, "Ctrl+Shift+R")

    def run(d):
        driver.on("file", lambda call: (out, ""))
        find_button(d, "Save optimized copy as").click()
        wait_until(lambda: d.stack.currentIndex() == 2, secs=60, msg=f"optimized ({d.status.text()})")
        shot(d, "o1_reduced")
        return 1
    driver.on("dialog", accept_with(run))
    win.a_reduce_size.trigger()
    with pymupdf.open(out) as d:
        assert d.page_count == 2 and d[0].rotation == 90 and len(list(d[0].annots())) == 1
    assert not cv.undo.isClean()  # the open document is still unsaved
    driver.check()


def test_edit_text_on_rotated_page(app, windows, driver, tmp_path):
    from PySide6.QtGui import QTextCursor

    from tests.e2e_kit import move

    src = str(tmp_path / "rot.pdf")
    make_pdf(src, rotate_second=True, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    tool = win.textedit_tool
    cv.go_to(1)
    pump(0.1)
    key(cv, Qt.Key.Key_I)
    w = word_rect(win, 1, "Second")  # the text runs down the screen on this /Rotate 90 page
    at = page_point(win, 1, (w[0] + w[2]) / 2, w[1] + 4)
    move(vp, at, held=False)
    click(vp, at)
    assert tool.editing(), "no editor on the rotated page"
    c = tool.editor.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    tool.editor.setTextCursor(c)
    type_text(cv, " ROT")
    shot(win, "t1_rotated_edit")
    key(cv, Qt.Key.Key_Return)
    wait_until(lambda: not tool.editing(), msg="editor closed")
    assert find_count(win, "tests. ROT") == 1
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        t = d[1].get_text()
        assert "selection tests. ROT" in t, t
    driver.check()
