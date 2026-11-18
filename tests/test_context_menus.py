"""Right-click menus, end to end: the page canvas (an empty spot acts on the page under the cursor, a text
selection, an annotation, a link; form fields keep their own menu), the Comments panel, the Attachments list and the
start screen's recent files. Shell calls (Show in folder) are replaced by recorders."""
from __future__ import annotations

import os

import pymupdf
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent, QGuiApplication
from PySide6.QtWidgets import QApplication

from leaf import shellint as S
from tests.e2e_kit import (  # noqa: F401
    choose, driver, drag, open_in, page_point, pump, sandbox, wait_until, windows, word_rect,
)
from tests.test_editor import app, make_pdf as _make_pdf  # noqa: F401


def make_pdf(path: str, **kw) -> str:
    _make_pdf(path, **kw)
    return path


def right_click(win, page: int, x: float, y: float) -> None:
    cv = win.canvas
    pos = page_point(win, page, x, y)
    ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, cv.viewport().mapToGlobal(pos))
    QApplication.sendEvent(cv.viewport(), ev)
    pump(0.05)


def record_menu(seen: list, pick: str | None = None):
    """Menu handler: remember the item texts; pick the one containing `pick` (None: dismiss)."""
    def h(menu):
        seen.append([a.text().replace("&", "") for a in menu.actions() if not a.isSeparator()])
        return choose(pick)(menu) if pick else None
    return h


@pytest.fixture
def calls(monkeypatch, tmp_path):
    rec: dict[str, list] = {}

    def recorder(name, result=True):
        def f(*a, **k):
            rec.setdefault(name, []).append(a)
            return result
        return f

    monkeypatch.setattr(S, "SHARE_DIR", str(tmp_path / "share"))
    for n in ("show_share_sheet", "reveal", "send_email", "open_default_apps_settings", "register",
              "notify_assoc_changed", "set_linux_default"):
        monkeypatch.setattr(S, n, recorder(n, (True, "Leaf is now the default app for PDF files.")
                                           if n == "set_linux_default" else True))
    rec["_recorder"] = recorder
    return rec


# ====================================================================== canvas menus
def test_canvas_page_menu_acts_on_the_page_under_the_cursor(app, windows, driver, tmp_path):  # noqa: F811
    src = make_pdf(str(tmp_path / "doc.pdf"), rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv = win.canvas
    cv.zoom_to(0.25)  # all three pages on screen; page 1 is the current one
    cv.go_to(0)
    pump(0.1)
    assert cv.current_index() == 0
    seen: list = []
    driver.on("menu", record_menu(seen, "Rotate right"))
    right_click(win, 2, 300, 600)  # page 3, an empty spot
    assert "Add sticky note here" in seen[0] and "Select all items on page" in seen[0]
    assert "Page" in seen[0] and "Find…" in seen[0]
    assert [r.rotation for r in cv.doc.pages] == [0, 0, 90]
    assert win.rows_override is None
    driver.check()


def test_canvas_text_selection_and_annotation_menus(app, windows, driver, tmp_path):  # noqa: F811
    from leaf.items import HighlightItem

    src = make_pdf(str(tmp_path / "doc.pdf"), rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv = win.canvas
    a, b = word_rect(win, 0, "quick"), word_rect(win, 0, "fox")
    my = (a[1] + a[3]) / 2
    drag(cv.viewport(), page_point(win, 0, a[0] + 1, my), page_point(win, 0, b[2] - 1, my, scroll=False))
    assert cv.selected_text() == "quick brown fox"
    seen: list = []
    driver.on("menu", record_menu(seen, "Highlight selected text"))
    right_click(win, 0, (a[0] + b[2]) / 2, my)
    assert seen[0][:2] == ["Copy text", "Highlight selected text"]
    assert "Redact selected text" in seen[0] and "Link…" in seen[0] and any(t.startswith("Find “quick") for t in seen[0])
    hl = [i for i in cv.order[0].overlays() if isinstance(i, HighlightItem)]
    assert len(hl) == 1 and hl[0].marked_text == "quick brown fox"

    # the new highlight: comment, reply, style, delete
    seen.clear()
    driver.on("menu", record_menu(seen, "Reply"))
    driver.on("input", lambda call: ("Looks right", True))
    right_click(win, 0, (a[0] + b[2]) / 2, my)
    assert seen[0][:3] == ["Edit comment…", "Reply…", "Style…"] and "Delete" in seen[0]
    assert cv.annots.replies_of(hl[0]) and hl[0].isSelected()
    driver.on("menu", choose("Delete"))
    right_click(win, 0, (a[0] + b[2]) / 2, my)
    assert not [i for i in cv.order[0].overlays() if isinstance(i, HighlightItem) and i.scene() is not None]
    driver.check()


def test_canvas_link_menu_and_form_fields_keep_their_menu(app, windows, driver, tmp_path):  # noqa: F811
    src = str(tmp_path / "link.pdf")
    make_pdf(src, rotate_second=False, form=True)
    with pymupdf.open(src) as d:
        d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 400, 250, 420), "uri": "https://example.com/x"})
        d.saveIncr()
    win = windows()
    open_in(win, src)
    seen: list = []
    driver.on("menu", record_menu(seen, "Copy link address"))
    right_click(win, 0, 150, 410)
    assert seen[0] == ["Open link", "Copy link address", "Edit link…", "Delete link"]
    assert QGuiApplication.clipboard().text() == "https://example.com/x"
    # a form field shows its own menu (forms.py), not the page's
    seen.clear()
    driver.on("menu", record_menu(seen))
    right_click(win, 0, 150, 210)
    assert seen and "Reset form" in seen[0] and "Add sticky note here" not in seen[0]
    driver.check()


# ====================================================================== panels and start screen
def test_comments_and_attachments_menus(app, windows, driver, tmp_path):  # noqa: F811
    src = make_pdf(str(tmp_path / "doc.pdf"), rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv = win.canvas
    a, b = word_rect(win, 0, "quick"), word_rect(win, 0, "fox")
    my = (a[1] + a[3]) / 2
    drag(cv.viewport(), page_point(win, 0, a[0] + 1, my), page_point(win, 0, b[2] - 1, my, scroll=False))
    cv.highlight_selection()
    win.comments_dock.show()
    panel = win.comments_dock.panel
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    def node():
        it = QTreeWidgetItemIterator(panel.tree)
        while it.value() is not None:
            if panel.item_of(it.value()) is not None:
                return it.value()
            it += 1
        return None

    wait_until(lambda: node() is not None, msg="comment listed")
    panel.tree.expandAll()
    pump(0.05)
    seen: list = []
    driver.on("menu", record_menu(seen, "Copy text"))
    pos = panel.tree.visualItemRect(node()).center()
    panel.tree.customContextMenuRequested.emit(pos)
    pump(0.05)
    assert seen[0] == ["Show on page", "Edit comment…", "Reply", "Copy text", "Delete"]
    assert QGuiApplication.clipboard().text() == "quick brown fox"

    att = win.attachments
    win.structure.dock.show()
    seen.clear()
    driver.on("menu", record_menu(seen))
    att.tree.customContextMenuRequested.emit(QPoint(5, 5))
    pump(0.05)
    assert seen[0] == ["Add files…"]
    driver.check()


def test_start_screen_recent_menu(app, windows, driver, tmp_path, calls):  # noqa: F811
    src = make_pdf(str(tmp_path / "doc.pdf"))
    win = windows()
    win.settings.setValue("recent", [src])
    win.welcome.refresh()
    pump(0.1)
    rec = win.welcome.recent
    assert rec.count() == 1
    pos = rec.visualItemRect(rec.item(0)).center()
    # (welcome.py looks reveal up in shellint when it builds the menu, so the recorder is used)
    seen: list = []
    driver.on("menu", record_menu(seen, "Copy path"))
    rec.customContextMenuRequested.emit(pos)
    pump(0.05)
    assert seen[0] == ["Open", "Show in folder", "Copy path", "Remove from list", "Clear list"]
    assert QGuiApplication.clipboard().text() == os.path.normpath(src)
    driver.on("menu", choose("Show in folder"))
    rec.customContextMenuRequested.emit(pos)
    pump(0.05)
    assert calls["reveal"] == [(src,)]
    driver.check()


def test_canvas_text_box_menu(app, windows, driver, tmp_path):  # noqa: F811
    from PySide6.QtCore import Qt

    from leaf.items import TextItem
    from tests.e2e_kit import click, key, type_text

    src = make_pdf(str(tmp_path / "doc.pdf"), rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 100, 500))
    type_text(cv, "Box text")
    win._commit_edits()
    cv.set_tool("select")
    pump(0.05)
    box = next(i for i in cv.order[0].overlays() if isinstance(i, TextItem))
    r = box.mapRectToParent(box.boundingRect())
    seen: list = []
    driver.on("menu", record_menu(seen, "Duplicate"))
    right_click(win, 0, r.center().x(), r.center().y())
    assert seen and seen[0][0] == "Edit text" and "Style…" in seen[0] and "Delete" in seen[0]
    assert len([i for i in cv.order[0].overlays() if isinstance(i, TextItem)]) == 2
    driver.check()
