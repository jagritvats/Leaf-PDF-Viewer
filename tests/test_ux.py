"""Settings dialog, first-run tour and tips, empty states for panels, and the UX fixes that go with them."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QMessageBox

from leaf import onboarding, recovery
from leaf import settings as S
from tests.test_editor import app, make_pdf, pump  # noqa: F401

BUILT_IN = ["general", "appearance", "editing", "saving", "performance", "ocr", "signing", "privacy", "keyboard"]


@pytest.fixture
def isolated(tmp_path):
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    recovery.OVERRIDE_DIR = str(tmp_path / "recovery")
    recovery._offered = True
    yield tmp_path
    recovery.OVERRIDE_DIR = None
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)
    S.apply_theme("system")


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "doc.pdf")
    make_pdf(path, rotate_second=False, form=False)
    return path


def _new_window():
    from leaf.window import MainWindow

    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    return w


def _close(w) -> None:
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()


@pytest.fixture
def win(app, isolated):  # noqa: F811
    w = _new_window()
    pump(app, 0.05)
    yield w
    _close(w)
    pump(app, 0.05)


@pytest.fixture
def docwin(win, pdf, app):  # noqa: F811
    win.open_path(pdf)
    pump(app, 0.2)
    return win


# ------------------------------------------------------------------ dialog
def test_dialog_builds_every_built_in_page(app, win):  # noqa: F811
    dlg = S.open_settings(win)
    assert isinstance(dlg, QDialog) and dlg.isVisible() and dlg.windowTitle() == "Settings"
    keys = [dlg.list.item(i).data(S.KEY_ROLE) for i in range(dlg.list.count())]
    for k in BUILT_IN:
        assert k in keys
        dlg.show_page(k)
        w = dlg.page_widget(k)
        assert w is not None and "couldn't be shown" not in getattr(w, "text", lambda: "")(), k
        assert dlg.current_key() == k and dlg.title.text() == dlg.entries[k].title
    assert S.open_settings(win) is dlg  # one per window
    # Ctrl+, opens it; the old Keyboard shortcuts action still exists (no key clash)
    assert win.a_settings.shortcut() == QKeySequence("Ctrl+,")
    assert win.a_shortcut_prefs.shortcut().isEmpty()
    from leaf import shortcuts

    assert "Ctrl+," not in shortcuts.duplicates(win)
    # Esc closes it (Close is the only button: changes apply right away)
    QTest.keyClick(dlg, Qt.Key.Key_Escape)
    pump(app, 0.05)
    from shiboken6 import isValid

    assert not isValid(dlg) or not dlg.isVisible()


def test_register_page_and_search(app, win):  # noqa: F811
    from PySide6.QtWidgets import QLabel

    S.register_page("zz_test", "Test area", lambda w: QLabel("Frobnicate the widgets"), order=55, keywords="gizmo")
    try:
        dlg = S.SettingsDialog(win, "zz_test")
        assert dlg.current_key() == "zz_test" and isinstance(dlg.page_widget("zz_test"), QLabel)
        order = [dlg.list.item(i).data(S.KEY_ROLE) for i in range(dlg.list.count())]
        assert order.index("performance") < order.index("zz_test") < order.index("ocr")
        assert dlg.filter("gizmo") == ["zz_test"]
        assert dlg.filter("frobnicate") == ["zz_test"]  # text inside the page
        assert dlg.filter("tsa")[0] == "signing"
        assert dlg.filter("nothing-matches-this") == [] and dlg.empty.isVisibleTo(dlg)
        assert len(dlg.filter("")) == len(order)
        dlg.close()
    finally:
        S.unregister_page("zz_test")
    S.register_page("zz_broken", "Broken", lambda w: 1 / 0)
    try:
        dlg = S.SettingsDialog(win, "zz_broken")  # a failing page doesn't take the dialog down
        assert "couldn't be shown" in dlg.page_widget("zz_broken").text()
        dlg.close()
    finally:
        S.unregister_page("zz_broken")


def test_theme_switch_applies(app, win):  # noqa: F811
    dlg = S.SettingsDialog(win, "appearance")
    page = dlg.page_widget("appearance")
    hints = QGuiApplication.styleHints()
    try:
        page.theme.setCurrentIndex(page.theme.findData("dark"))
        assert win.settings.value(S.THEME) == "dark"
        assert hints.colorScheme() in (Qt.ColorScheme.Dark, Qt.ColorScheme.Unknown)  # offscreen may ignore it
        page.theme.setCurrentIndex(page.theme.findData("light"))
        assert win.settings.value(S.THEME) == "light"
    finally:
        page.theme.setCurrentIndex(page.theme.findData("system"))
        dlg.close()
    assert win.settings.value(S.THEME) == "system"


def test_settings_notes_follow_a_live_theme_switch(app, win, monkeypatch):  # noqa: F811
    """Appearance › Theme switches while the dialog is open: the notes must not keep the old theme's gray."""
    from PySide6.QtGui import QPalette

    from leaf import style
    from leaf.a11y import contrast_ratio
    from leaf.style import theme

    if not style.active():
        pytest.skip("style layer off")
    dlg = S.SettingsDialog(win, "appearance")
    dark = style.current().dark
    try:
        monkeypatch.setattr(theme, "is_dark", lambda _app: not dark)
        style._reapply()
        lab = S.note("A note under a setting")
        dlg.layout().addWidget(lab)
        pump(app, 0.05)
        monkeypatch.setattr(theme, "is_dark", lambda _app: dark)
        style._reapply()
        pump(app, 0.05)
        bg = dlg.palette().color(QPalette.ColorRole.Window)
        assert contrast_ratio(lab.palette().color(lab.foregroundRole()), bg) >= 4.5
    finally:
        monkeypatch.undo()
        style._reapply()
        dlg.close()


def test_theme_calls_style_hints(app, isolated, monkeypatch):  # noqa: F811
    calls = []
    hints = QGuiApplication.styleHints()
    monkeypatch.setattr(type(hints), "setColorScheme", lambda self, s: calls.append(s))
    S.apply_theme("dark")
    S.apply_theme("light")
    S.apply_theme("system")
    assert calls == [Qt.ColorScheme.Dark, Qt.ColorScheme.Light, Qt.ColorScheme.Unknown]


def test_general_editing_performance_pages_write_existing_keys(app, docwin):  # noqa: F811
    win = docwin
    dlg = S.SettingsDialog(win)
    g = dlg.page_widget("general")
    g.author.setText("Ada Lovelace")
    g.author.editingFinished.emit()
    assert win.canvas.annots.author == "Ada Lovelace" and win.settings.value("annotations/author") == "Ada Lovelace"
    g.units.setCurrentIndex(g.units.findData("in"))
    assert win.settings.value("crop/unit") == "in"
    win.settings.setValue("recent", [f"/x/{i}.pdf" for i in range(10)])
    g.recent.setValue(3)
    assert len(win.settings.value("recent")) == 3
    win._add_recent(os.path.abspath("y.pdf"))
    assert len(win.settings.value("recent")) == 3

    a = dlg.page_widget("appearance")
    a.night.setChecked(True)
    assert win.canvas.night and win.a_night_mode.isChecked() and S.truthy(win.settings.value("view/night"))
    a.night.setChecked(False)
    a.thumbs.setValue(160)
    assert win.sidebar.thumb_w == 160
    a.labels.setChecked(True)
    assert win.toolbar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon

    e = dlg.page_widget("editing")
    e.size.setValue(18)
    assert win.canvas.defaults["text"]["size"] == 18
    e.step.setValue(2.5)
    e.big.setValue(25)
    assert win.canvas.nudge_step == 2.5 and win.canvas.nudge_big == 25

    from leaf import canvas

    p = dlg.page_widget("performance")
    p.memory.setValue(512)
    assert canvas.IMAGE_BUDGET == 512 * 2**20
    p.memory.setValue(S.MEMORY_DEFAULT)

    sg = dlg.page_widget("signing")
    sg.tsa.setText("https://tsa.example")
    sg.tsa.editingFinished.emit()
    assert win.settings.value("signing/tsa_url") == "https://tsa.example"

    o = dlg.page_widget("ocr")
    assert o.engine.count() >= 1 and o.engine.itemData(0) == "auto"
    dlg.close()


def test_nudge_uses_the_step(app, docwin):  # noqa: F811
    from PySide6.QtCore import QRectF

    from leaf.items import ShapeItem
    from tests.test_editor import add

    cv = docwin.canvas
    docwin.settings.setValue(S.NUDGE, 4.0)
    S.apply_nudge(docwin)
    it = add(cv, 0, ShapeItem("rect", QRectF(100, 100, 50, 50)))
    cv.scene().clearSelection()
    it.setSelected(True)
    x = it.pos().x()
    cv.setFocus()
    QTest.keyClick(cv, Qt.Key.Key_Right)
    assert abs(it.pos().x() - x - 4.0) < 1e-6


def test_confirm_before_deleting_pages(app, docwin, monkeypatch):  # noqa: F811
    win = docwin
    n = len(win.canvas.doc.pages)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.Cancel)
    win.delete_pages()
    assert len(win.canvas.doc.pages) == n - 1 and not asked  # off by default
    win.canvas.undo.undo()
    win.settings.setValue(S.CONFIRM_DELETE, True)
    win.delete_pages()
    assert asked and len(win.canvas.doc.pages) == n
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    win.delete_pages()
    assert len(win.canvas.doc.pages) < n


def test_backup_and_full_rewrite(app, docwin):  # noqa: F811
    win = docwin
    doc = win.canvas.doc
    path = doc.path
    with open(path, "rb") as f:
        original = f.read()
    win.settings.setValue(S.BACKUP, True)
    win.rotate_pages(90)
    assert win.save()
    assert open(path + ".bak", "rb").read() == original
    win.rotate_pages(90)
    assert win.save()
    assert open(path + ".bak", "rb").read() == original  # once per session: still the file as it was opened

    # full rewrite: a form-value/annotation-only save would be incremental; with the setting it isn't
    dlg = S.SettingsDialog(win, "saving")
    page = dlg.page_widget("saving")
    page.full.setChecked(True)
    assert doc.force_rewrite
    page.full.setChecked(False)
    assert not doc.force_rewrite
    dlg.close()


def test_recovery_interval_and_off(app, docwin):  # noqa: F811
    win = docwin
    rec = win.recovery
    assert rec.enabled and rec.periodic.interval() == recovery.INTERVAL_MS
    dlg = S.SettingsDialog(win, "saving")
    page = dlg.page_widget("saving")
    page.recovery.setCurrentIndex(page.recovery.findData(5))
    assert rec.periodic.interval() == 5 * recovery.INTERVAL_MS and S.int_value(win.settings, S.RECOVERY_MINUTES, 1) == 5
    win.rotate_pages(90)
    page.recovery.setCurrentIndex(page.recovery.findData(0))
    assert not rec.enabled and not rec.periodic.isActive() and not rec.needed() and not rec.autosave()
    page.recovery.setCurrentIndex(page.recovery.findData(1))
    assert rec.enabled and rec.needed() and rec.autosave()  # the changes made while off get a copy
    dlg.close()


def test_restore_position_can_be_turned_off(app, isolated, pdf):  # noqa: F811
    w = _new_window()
    w.settings.setValue(S.RESTORE_POSITION, False)
    calls = []
    import leaf.features.viewing as V

    orig = V.restore_position
    V.restore_position = lambda *a: calls.append(a) or False
    try:
        w.open_path(pdf)
        pump(app, 0.1)
        assert not calls
    finally:
        V.restore_position = orig
        _close(w)


# ------------------------------------------------------------------ onboarding
def test_tour_shows_once_and_can_be_skipped(app, isolated):  # noqa: F811
    w = _new_window()
    tour = onboarding.tour_of(w)
    assert tour is not None and tour.isVisibleTo(w.welcome)
    assert tour.index == 0 and not tour.back.isVisibleTo(tour)
    tour.next.click()
    assert tour.index == 1 and "Edit text" in tour.title.text() + tour.text.text()
    tour.skip.click()
    pump(app, 0.05)
    assert onboarding.tour_of(w) is None and S.truthy(w.settings.value(onboarding.TOUR_DONE))
    _close(w)
    w2 = _new_window()
    assert onboarding.tour_of(w2) is None  # not again
    w2.a_show_tips.trigger()  # Help › Show tips again
    t2 = onboarding.tour_of(w2)
    assert t2 is not None
    for _ in range(len(t2.cards) - 1):
        t2.next.click()
    assert t2.next.text() == "Get started"
    t2.next.click()
    pump(app, 0.05)
    assert onboarding.tour_of(w2) is None and S.truthy(w2.settings.value(onboarding.TOUR_DONE))
    _close(w2)


def test_tour_escape_skips(app, isolated):  # noqa: F811
    w = _new_window()
    tour = onboarding.tour_of(w)
    QTest.keyClick(tour, Qt.Key.Key_Escape)
    pump(app, 0.05)
    assert onboarding.tour_of(w) is None and S.truthy(w.settings.value(onboarding.TOUR_DONE))
    _close(w)


def test_edit_text_refusal_gets_a_why_link_once(app, win, monkeypatch):  # noqa: F811
    shown = []
    monkeypatch.setattr(win, "toast", lambda text, action_text=None, callback=None, ms=2500: shown.append((text, action_text)))
    tool = win.textedit_tool

    class T:  # a stand-in tool whose toast is the window's (as at install)
        pass

    t = T()
    t.toast = win.toast
    onboarding.wrap_edit_text_toast(win, t)
    t.toast("Can't edit this.", ms=5000)
    t.toast("Can't edit that.", ms=5000)
    assert shown == [("Can't edit this.", "Why?"), ("Can't edit that.", None)]
    assert getattr(tool.toast, "_leaf_tip", False)  # the real tool is wrapped at install


def test_tip_once(app, win, monkeypatch):  # noqa: F811
    shown = []
    monkeypatch.setattr(win, "toast", lambda *a, **k: shown.append(a))
    assert onboarding.tip_once(win, "x", "Hello")
    assert not onboarding.tip_once(win, "x", "Hello")
    onboarding.reset(win)
    assert onboarding.tip_once(win, "x", "Hello") and len(shown) == 2


# ------------------------------------------------------------------ empty states
def test_empty_states_for_empty_panels(app, docwin):  # noqa: F811
    win = docwin
    pump(app, 0.3)
    from PySide6.QtWidgets import QDockWidget

    found = {}
    for name in ("comments", "outline", "attachments", "redactions", "signatures", "history"):
        d = win.findChild(QDockWidget, name)
        assert d is not None, name
        view = getattr(d.widget(), "tree", None) or getattr(d.widget(), "list", None) or d.widget()
        hint = getattr(view, "empty_hint", None)
        assert isinstance(hint, onboarding.EmptyHint), name
        hint.update_state()
        found[name] = not hint.isHidden()
    assert found["comments"] and found["outline"] and found["attachments"] and found["redactions"], found
    assert found["history"], found
    # an edit: History is no longer empty
    win.rotate_pages(90)
    win.history_dock.widget().empty_hint.update_state()
    assert win.history_dock.widget().empty_hint.isHidden()
    # the comments hint's action picks the note tool
    hint = win.comments_dock.widget().tree.empty_hint
    hint.button.click()
    assert win.canvas.tool == "note"


def test_empty_hint_hidden_without_document(app, win):  # noqa: F811
    hint = win.comments_dock.widget().tree.empty_hint
    hint.update_state()
    assert hint.isHidden()


# ------------------------------------------------------------------ QA fixes
def test_organizer_move_keeps_current_page(app, docwin):  # noqa: F811
    from leaf.features.pages import set_organizer

    win = docwin
    cv = win.canvas
    cv.go_to(0)
    pump(app, 0.05)
    uid = cv.doc.pages[0].uid
    set_organizer(win, True)
    org = win.organizer
    org.select_uids([uid])
    org.move_selected_by(1)
    pump(app, 0.1)
    assert cv.doc.pages[1].uid == uid
    assert org.current_uid == uid and win.sidebar.current_uid == uid
    assert cv.current_index() == 1
    org.move_selected_to(org.count())
    pump(app, 0.1)
    assert cv.doc.pages[-1].uid == uid and org.current_uid == uid
    set_organizer(win, False)
    pump(app, 0.05)
    assert cv.doc.pages[cv.current_index()].uid == uid


def test_pages_panel_move_follows_page(app, docwin):  # noqa: F811
    win = docwin
    cv = win.canvas
    cv.go_to(0)
    pump(app, 0.05)
    uid = cv.doc.pages[0].uid
    win.sidebar.select_uids([uid])
    win.sidebar.move_selected_by(1)
    pump(app, 0.1)
    assert cv.doc.pages[1].uid == uid and cv.current_index() == 1


def test_welcome_recent_list_zero_one_many(app, win, tmp_path):  # noqa: F811
    wv = win.welcome
    win.settings.setValue("recent", [])
    wv.refresh()
    assert not wv.recent.isVisibleTo(wv) and wv.empty.isVisibleTo(wv) and not wv.clear_btn.isVisibleTo(wv)
    one = str(tmp_path / "one.pdf")
    make_pdf(one, form=False)
    win.settings.setValue("recent", [one])
    wv.refresh()
    assert wv.recent.count() == 1 and wv.recent.isVisibleTo(wv) and not wv.empty.isVisibleTo(wv)
    many = []
    for i in range(12):
        p = str(tmp_path / f"m{i}.pdf")
        open(p, "wb").write(open(one, "rb").read())
        many.append(p)
    win.settings.setValue("recent", many + [str(tmp_path / "missing.pdf")])
    wv.refresh()
    assert wv.recent.count() == 8 and wv.recent.height() < 8 * 60  # capped, scrolls
    pump(app, 0.3)  # thumbnails render one per event-loop turn without errors


def test_window_remembers_maximized(app, isolated):  # noqa: F811
    w = _new_window()
    w.showMaximized()
    pump(app, 0.05)
    assert w.isMaximized()
    _close(w)
    assert S.truthy(QSettings().value("ui/maximized"))
    from leaf.window import MainWindow

    w2 = MainWindow()
    assert w2.windowState() & Qt.WindowState.WindowMaximized
    w2.show()
    pump(app, 0.05)
    assert w2.isMaximized()
    w2.showNormal()
    pump(app, 0.05)
    _close(w2)
    assert not S.truthy(QSettings().value("ui/maximized"))


def test_dialogs_fit_on_screen(app, win):  # noqa: F811
    from PySide6.QtWidgets import QLabel, QVBoxLayout

    dlg = QDialog(win)
    QVBoxLayout(dlg).addWidget(QLabel("x"))
    dlg.resize(20000, 20000)
    dlg.show()
    pump(app, 0.05)
    avail = dlg.screen().availableGeometry()
    assert dlg.width() <= avail.width() and dlg.height() <= avail.height()
    dlg.close()


def test_escape_cancels_organizer_find_and_tool(app, docwin):  # noqa: F811
    from leaf.features.pages import set_organizer

    win = docwin
    set_organizer(win, True)
    win.organizer.setFocus()
    QTest.keyClick(win.organizer, Qt.Key.Key_Escape)
    pump(app, 0.05)
    assert win.current_view() == "document"
    win.find.open_bar()
    QTest.keyClick(win.find.edit, Qt.Key.Key_Escape)
    assert win.find.isHidden()
    win.canvas.set_tool("rect")
    win.canvas.setFocus()
    QTest.keyClick(win.canvas, Qt.Key.Key_Escape)
    assert win.canvas.tool == "select"
