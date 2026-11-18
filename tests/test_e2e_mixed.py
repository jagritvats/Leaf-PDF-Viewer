"""End-to-end: features combined the way people mix them (one feature's edit, another's view, then save)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from tests.e2e_kit import (  # noqa: F401
    CTRL, diag, find_count, labelled_pdf, page_labels, SHIFT, accept_with, activate, click, drag, driver, key, open_in, page_point, pump, sandbox, save_keys,
    shortcut, shot, thumb_point, type_text, wait_until, windows, word_rect,
)
from tests.test_editor import app, make_pdf  # noqa: F401


def test_undo_survives_save(app, windows, driver, tmp_path):
    from leaf.items import ShapeItem

    src = labelled_pdf(str(tmp_path / "u.pdf"), ["U1"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 0, 100, 300), page_point(win, 0, 200, 380))
    save_keys(win, driver)
    assert not win.isWindowModified()
    with pymupdf.open(src) as d:
        assert [a.type[1] for a in d[0].annots()] == ["Square"]
    cv.setFocus()
    shortcut(win, "Ctrl+Z")
    assert not [i for i in cv.order[0].overlays() if isinstance(i, ShapeItem)]
    assert win.isWindowModified()
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert not list(d[0].annots())
    shortcut(win, "Ctrl+Y")
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert [a.type[1] for a in d[0].annots()] == ["Square"]
    driver.check()


def test_edit_text_then_reorder_find_save(app, windows, driver, tmp_path):
    from PySide6.QtGui import QTextCursor

    from tests.e2e_kit import move

    src = labelled_pdf(str(tmp_path / "t.pdf"), ["First", "Second", "Third"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    tool = win.textedit_tool
    key(cv, Qt.Key.Key_I)
    r = word_rect(win, 1, "Second")
    at = page_point(win, 1, r[0] + 5, (r[1] + r[3]) / 2)
    move(vp, at, held=False)
    click(vp, at)
    assert tool.editing()
    c = tool.editor.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    tool.editor.setTextCursor(c)
    type_text(cv, "Changed")
    key(cv, Qt.Key.Key_Return)
    wait_until(lambda: not tool.editing(), msg="editor closed")

    # move that page to the start from the page list (Ctrl+Home there)
    sb = win.sidebar
    click(sb.viewport(), thumb_point(sb, 1))
    key(sb, Qt.Key.Key_Home, CTRL)
    assert page_labels(win)[0] == "Second", page_labels(win)
    assert find_count(win, "SecondChanged") == 1
    hits = win.find.hits
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert d[0].get_text().split() == ["SecondChanged"], d[0].get_text()
        assert [p.get_text().split()[0] for p in d] == ["SecondChanged", "First", "Third"]
    # a second save replays the edit again from the untouched source
    cv.setFocus()
    key(cv, Qt.Key.Key_V)
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 2, 100, 300), page_point(win, 2, 200, 380))
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert d[0].get_text().split() == ["SecondChanged"], d[0].get_text()
        assert len(list(d[2].annots())) == 1
    driver.check()
    assert hits is not None


def test_select_copy_highlight_and_redact_tool(app, windows, driver, tmp_path):
    from leaf.items import HighlightItem

    src = str(tmp_path / "s.pdf")
    make_pdf(src, rotate_second=True, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    # select text on the rotated page 2 by dragging, copy it, highlight it with Ctrl+Shift+H
    cv.go_to(1)
    pump(0.1)
    q, f = word_rect(win, 1, "quick"), word_rect(win, 1, "fox")  # the text runs down the screen here
    drag(vp, page_point(win, 1, (q[0] + q[2]) / 2, q[1] + 1), page_point(win, 1, (f[0] + f[2]) / 2, f[3] - 1, scroll=False))
    assert cv.selected_text() == "quick brown fox", cv.selected_text()
    cb = QGuiApplication.clipboard()
    cb.setText("probe")
    usable = cb.text() == "probe"  # a sandboxed desktop session may not hand the clipboard to this process
    cb.clear()
    shortcut(win, "Ctrl+C")
    if usable:
        wait_until(lambda: cb.text() == "quick brown fox", secs=3, msg=f"clipboard {cb.text()!r} {diag(win, driver)}")
    shortcut(win, "Ctrl+Shift+H")
    assert [i.marked_text for i in cv.order[1].overlays() if isinstance(i, HighlightItem)] == ["quick brown fox"]

    # redact tool: drag across "lazy dog" on page 1
    cv.setFocus()
    key(cv, Qt.Key.Key_X)
    assert cv.tool == "redact"
    a, b = word_rect(win, 0, "lazy"), word_rect(win, 0, "dog")
    my = (a[1] + a[3]) / 2
    drag(vp, page_point(win, 0, a[0] + 1, my), page_point(win, 0, b[2] - 1, my, scroll=False))
    driver.on("dialog", accept_with(button="Save and remove"))
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        t0 = d[0].get_text()
        assert "lazy" not in t0 and "dog" not in t0 and "quick" in t0, t0
        p1 = d[1]
        assert [x.type[1] for x in p1.annots()] == ["Highlight"], "highlight lost"
    driver.check()


def test_organizer_keyboard_and_presentation(app, windows, driver, tmp_path):
    src = labelled_pdf(str(tmp_path / "o.pdf"), ["O1", "O2", "O3"])
    win = windows()
    open_in(win, src)
    shortcut(win, "Ctrl+Shift+O")
    assert win.current_view() == "organizer"
    org = win.organizer
    wait_until(lambda: org.count() == 3, msg="organizer grid")
    assert not win.side_dock.isVisible() and not win.props_dock.isVisible(), "the organizer isn't full-window"
    shot(win, "m1_organizer")
    click(org.viewport(), thumb_point(org, 2))
    key(org, Qt.Key.Key_Home, CTRL)
    assert page_labels(win) == ["O3", "O1", "O2"], page_labels(win)
    key(org, Qt.Key.Key_Escape)
    assert win.current_view() == "document"
    assert win.sidebar.uids() == [r.uid for r in win.canvas.doc.pages]

    # presentation: F5, arrow keys page, Esc back
    cv = win.canvas
    cv.go_to(0)
    shortcut(win, "F5")
    assert cv.presenting
    pump(0.3)
    shot(win, "m2_presentation")
    i = cv.current_index()
    key(cv, Qt.Key.Key_Right)
    assert cv.current_index() == i + 1
    key(cv, Qt.Key.Key_Escape)
    pump(0.3)
    assert not cv.presenting and not win.isFullScreen()
    assert win.statusBar().isVisible() and win.side_dock.isVisible()
    driver.check()


def test_image_opened_then_saved_as(app, windows, driver, tmp_path):
    from PySide6.QtGui import QColor, QImage

    png = str(tmp_path / "photo.png")
    img = QImage(600, 400, QImage.Format.Format_RGB32)
    img.fill(QColor("#3a7"))
    img.save(png)
    win = windows()
    open_in(win, png)
    assert win.canvas.doc is not None and len(win.canvas.doc.pages) == 1
    out = str(tmp_path / "photo-out.pdf")
    driver.on("file", lambda call: (out, ""))
    shortcut(win, "Ctrl+S")  # a converted image has no PDF yet: Save asks where
    wait_until(lambda: os.path.exists(out), msg="saved as")
    assert win.canvas.doc.path == os.path.abspath(out)
    assert os.path.abspath(out) in (win.settings.value("recent") or [])
    with pymupdf.open(out) as d:
        assert d.page_count == 1 and d[0].get_images()
    # Ctrl+S again writes to the new file without asking
    key(win.canvas, Qt.Key.Key_R)
    drag(win.canvas.viewport(), page_point(win, 0, 50, 50), page_point(win, 0, 120, 120))
    save_keys(win, driver)
    with pymupdf.open(out) as d:
        assert len(list(d[0].annots())) == 1
    driver.check()


def test_same_file_twice_raises_existing_window(app, windows, driver, tmp_path):
    from leaf import window as W

    src = labelled_pdf(str(tmp_path / "same.pdf"), ["S1"])
    win = windows()
    open_in(win, src)
    n = len(W._windows)
    win.open_path(src)
    pump(0.1)
    assert len(W._windows) == n
    other = labelled_pdf(str(tmp_path / "other.pdf"), ["X1"])
    win.open_path(other)  # a second file opens in a new window
    wait_until(lambda: len(W._windows) == n + 1, msg="second window")
    w2 = W._windows[-1]
    wait_until(lambda: w2.canvas.doc is not None, msg="second doc")
    assert w2.canvas.doc.path == os.path.abspath(other)
    driver.check()


def test_flat_shapes_still_save(app, windows, driver, tmp_path):
    """A rectangle/ellipse dragged straight across (zero height) used to make every save fail with
    "rect is infinite or empty" (and the crash-recovery copy too)."""
    src = labelled_pdf(str(tmp_path / "flat.pdf"), ["F1"])
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    for k, y in ((Qt.Key.Key_R, 300), (Qt.Key.Key_E, 400), (Qt.Key.Key_W, 500)):
        key(cv, k)
        a, b = page_point(win, 0, 100, y), page_point(win, 0, 300, y, scroll=False)
        drag(vp, a, QPoint(b.x(), a.y()))
    assert len(cv.order[0].overlays()) == 3
    save_keys(win, driver)
    with pymupdf.open(src) as d:
        assert sorted(a.type[1] for a in d[0].annots()) == ["Circle", "Square"]
    driver.check()
