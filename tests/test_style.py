"""The look (leaf/style), Focus mode (leaf/features/focus.py) and the start screen (leaf/welcome.py)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import QApplication

from tests.e2e_kit import (  # noqa: F401
    CTRL, SHIFT, activate, click, driver, key, open_in, pump, sandbox, shortcut, wait_until, windows,
)
from tests.test_editor import app, make_pdf  # noqa: F401


@pytest.fixture
def doc(tmp_path):
    path = str(tmp_path / "focus.pdf")
    make_pdf(path, form=False)
    return path


def _key(w, k, mods=Qt.KeyboardModifier.NoModifier) -> None:
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyPress, k, mods))
    QApplication.sendEvent(w, QKeyEvent(QEvent.Type.KeyRelease, k, mods))
    pump(0.03)


def _chrome(win) -> dict:
    return {"menu": win.menuBar().isVisible(), "toolbar": win.toolbar.isVisible(), "status": win.statusBar().isVisible(),
            "pages": win.side_dock.isVisible()}


# ------------------------------------------------------------------ tokens
@pytest.mark.parametrize("dark", [True, False])
def test_tokens_text_meets_wcag_aa(app, dark):  # noqa: F811
    from leaf.a11y import contrast_ratio
    from leaf.style.tokens import build

    from leaf.style import tokens as T

    for acc in ("#0078d4", "#e81123", "#ffb900", "#00cc6a", "#881798", "#3daee9"):  # Windows swatches, KDE Breeze
        t = build(dark, QColor(acc))
        t.accent = T.readable_accent(QColor(acc), dark)  # the non-Windows path (Windows reads its own palette)
        t.on_accent = T.on(t.accent)
        for fg in ("text", "text2"):
            for bg in ("chrome", "layer", "layer2", "popup", "canvas", "accent_soft"):
                r = contrast_ratio(getattr(t, fg), getattr(t, bg))
                assert r >= 4.5, (dark, acc, fg, bg, round(r, 2))
        assert contrast_ratio(t.on_accent, t.accent) >= 4.5
        for bg in ("chrome", "layer", "popup"):  # accent-colored text: links, flat buttons, checked icons
            assert contrast_ratio(t.accent, getattr(t, bg)) >= 4.5, (dark, acc, bg)
        assert contrast_ratio(t.text3, t.layer) >= 3.0  # placeholders and disabled text only


def test_style_active_and_window_dressed(app, windows):  # noqa: F811
    from leaf import style

    win = windows()
    assert style.active() and not style.current().platform
    assert win.command_search.isVisible() and win.command_search.accessibleName() == "Search commands"
    assert "QMenu" in app.styleSheet() and app.palette().color(app.palette().ColorRole.Window) == style.current().chrome
    t = style.current()
    assert win.canvas.backgroundBrush().color() == t.canvas
    win.command_search.click()
    pump(0.1)
    from leaf.palette import CommandPalette

    pal = win.findChild(CommandPalette)
    assert pal is not None and pal.isVisible()
    pal.hide()


def test_idle_window_does_not_resync_panels(app, windows, doc):  # noqa: F811
    """Dressing a panel tab bar again (setElideMode relayouts it even unchanged) posted a LayoutRequest that queued
    the next sync: a 0 ms loop at full CPU that also repainted the panel corners."""
    win = windows()
    open_in(win, doc)
    win.comments_dock.show()  # a tab group on the right
    pump(0.2)
    ticks = []
    win.docks._later.timeout.connect(lambda: ticks.append(1))
    pump(0.5)
    assert len(ticks) <= 2


def test_page_hits_ignore_the_shadow_margin(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    pg = win.canvas.order[0]
    r = pg.scene_page_rect()
    assert pg.sceneBoundingRect().contains(r) and pg.sceneBoundingRect() != r  # room for the shadow
    assert win.canvas.page_at(r.center()) is pg
    assert win.canvas.page_at(r.topLeft() - QPointF(3, 3)) is None  # in the shadow, not on the page


# ------------------------------------------------------------------ focus mode
def test_focus_mode_toggle_restores_chrome(app, windows, doc):  # noqa: F811
    win = windows()
    assert not win.a_focus_mode.isEnabled()  # nothing to focus on without a document
    open_in(win, doc)
    before = _chrome(win)
    assert all(before.values())
    assert win.a_focus_mode.isEnabled() and win.a_focus_mode in win.menus["view"].actions()
    win.a_focus_mode.trigger()
    pump(0.1)
    fm = win.focus_mode
    assert fm.on and win.a_focus_mode.isChecked()
    assert not any(_chrome(win).values())
    assert len(fm.bars) == 2 and all(b.isVisible() for b in fm.bars)
    tools, page = fm.bars
    assert tools.parentWidget() is win.canvas and "1" in page.now.text() and page.total.text() == "of 3"
    win.canvas.go_to(2)
    pump(0.05)
    assert page.now.text() == "3"
    win.a_focus_mode.trigger()
    pump(0.1)
    assert not fm.on and not win.a_focus_mode.isChecked()
    assert _chrome(win) == before
    assert not any(b.isVisible() for b in fm.bars)
    assert win.settings.value("view/focus_mode") in (False, "false")


def test_focus_mode_shortcut_and_escape(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    activate(win)
    win.canvas.setFocus()
    before = _chrome(win)
    shortcut(win, "Ctrl+Shift+F")
    assert win.focus_mode.on
    from leaf import shortcuts

    assert "Ctrl+Shift+F" not in shortcuts.duplicates(win)
    cv = win.canvas
    cv.set_tool("rect")
    _key(cv, Qt.Key.Key_Escape)  # first Esc: back to the Select tool, still in Focus mode
    assert cv.tool == "select" and win.focus_mode.on
    _key(cv, Qt.Key.Key_Escape)
    assert not win.focus_mode.on and _chrome(win) == before
    shortcut(win, "Ctrl+Shift+F")
    assert win.focus_mode.on
    shortcut(win, "Ctrl+Shift+F")  # the same shortcut leaves
    assert not win.focus_mode.on and _chrome(win) == before


def test_focus_mode_menu_bar_on_alt_and_f10(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    activate(win)
    win.a_focus_mode.trigger()
    pump(0.05)
    mb, cv = win.menuBar(), win.canvas
    assert not mb.isVisible()
    _key(cv, Qt.Key.Key_F10)
    assert mb.isVisible()
    _key(cv, Qt.Key.Key_Escape)  # Esc puts the menu bar away first, Focus mode stays
    assert not mb.isVisible() and win.focus_mode.on
    _key(cv, Qt.Key.Key_Alt)  # Alt alone
    assert mb.isVisible()
    _key(cv, Qt.Key.Key_Alt)
    assert not mb.isVisible()
    win.a_focus_mode.trigger()
    assert mb.isVisible() and not win.focus_mode.on


def test_focus_mode_bars_slide_away_while_reading(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    win.a_focus_mode.trigger()
    pump(0.05)
    bars = win.focus_mode.bars
    _key(win.canvas, Qt.Key.Key_PageDown)
    assert not any(b.shown for b in bars)
    from tests.e2e_kit import move

    vp = win.canvas.viewport()
    move(vp, vp.rect().center(), held=False)
    move(vp, vp.rect().center() + QPoint(40, 40), held=False)
    assert all(b.shown for b in bars)
    win.a_focus_mode.trigger()


def test_focus_mode_is_remembered_and_closing_saves_normal_layout(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    win.a_focus_mode.trigger()
    pump(0.05)
    assert win.settings.value("view/focus_mode") in (True, "true")
    win.close()  # leaves Focus mode first: the toolbar is saved visible
    pump(0.1)
    win2 = windows()
    assert win2.toolbar.isVisible() and not win2.focus_mode.on  # no document yet
    open_in(win2, doc)
    wait_until(lambda: win2.focus_mode.on, msg="focus mode restored")
    assert not win2.toolbar.isVisible() and not win2.statusBar().isVisible()
    win2.a_focus_mode.trigger()
    assert win2.toolbar.isVisible() and win2.statusBar().isVisible()


def test_focus_mode_hides_bars_while_presenting(app, windows, doc):  # noqa: F811
    win = windows()
    open_in(win, doc)
    win.a_focus_mode.trigger()
    pump(0.05)
    win.a_presentation.trigger()
    pump(0.1)
    assert win.canvas.presenting and not any(b.isVisible() for b in win.focus_mode.bars)
    win.viewing.exit_presentation()
    pump(0.1)
    assert all(b.isVisible() for b in win.focus_mode.bars)
    win.a_focus_mode.trigger()


# ------------------------------------------------------------------ start screen
def test_start_screen_recent_open_and_clear(app, windows, tmp_path):  # noqa: F811
    paths = []
    for name in ("alpha.pdf", "beta.pdf"):
        p = str(tmp_path / name)
        make_pdf(p, form=False)
        paths.append(p)
    win = windows()
    wv = win.welcome
    win.settings.setValue("recent", [])
    wv.refresh()
    assert win.current_view() == "welcome"
    assert wv.title.text() == "Welcome to Leaf" and wv.drop.isVisibleTo(wv) and wv.empty.isVisibleTo(wv)
    win.settings.setValue("recent", paths)
    wv.refresh()
    assert wv.title.text() == "Pick up where you left off"
    assert wv.recent.count() == 2 and wv.recent.isVisibleTo(wv) and not wv.drop.isVisibleTo(wv)
    assert wv.recent.item(0).data(Qt.ItemDataRole.AccessibleTextRole) == "alpha.pdf"
    wait_until(lambda: wv.recent.item(1).data(Qt.ItemDataRole.UserRole + 7) is not None, msg="covers")
    keys = [w.accessibleName() for w in wv.hint.findChildren(type(wv.hint)) if w.accessibleName()]
    assert any("Open" in k for k in keys) and any("Command palette" in k for k in keys)
    # open: activating a cover opens that file
    wv.recent.setCurrentRow(1)
    wv.recent.setFocus()
    key(wv.recent, Qt.Key.Key_Return)
    wait_until(lambda: win.canvas.doc is not None, msg="opened from the start screen")
    assert os.path.normcase(win.canvas.doc.path) == os.path.normcase(paths[1]) and win.current_view() == "document"
    # clear
    wv.refresh()
    assert wv.recent.count() == 2  # opening put beta first
    assert os.path.normcase(wv.recent.item(0).data(Qt.ItemDataRole.UserRole)) == os.path.normcase(paths[1])
    wv.clear_btn.click()
    assert wv.recent.count() == 0 and not win.settings.value("recent") and wv.empty.isVisibleTo(wv)


def test_start_screen_scrolls_past_five_covers(app, windows, tmp_path):  # noqa: F811
    one = str(tmp_path / "one.pdf")
    make_pdf(one, form=False)
    many = []
    for i in range(7):
        p = str(tmp_path / f"f{i}.pdf")
        with open(one, "rb") as a, open(p, "wb") as b:
            b.write(a.read())
        many.append(p)
    win = windows()
    win.settings.setValue("recent", many)
    win.welcome.refresh()
    wv = win.welcome
    assert wv.recent.count() == 7
    assert wv.recent.width() < 7 * 190  # five side by side, the rest scroll
    wv.recent.setCurrentRow(0)
    key(wv.recent, Qt.Key.Key_Delete)  # Delete removes the current one
    assert wv.recent.count() == 6
