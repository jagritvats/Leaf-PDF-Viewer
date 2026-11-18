"""Compare documents (core + view), accessibility pass, i18n scaffolding."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QLocale, QRectF, QSettings
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QSpinBox, QToolButton

from leaf import a11y, i18n
from leaf.compare import align, compare_documents, page_info, summarize
from leaf.items import ShapeItem
from tests.test_editor import add, app, make_pdf, pump  # noqa: F401

PAGES_A = [
    "The quick brown fox jumps over the lazy dog\nA second line that stays the same",
    "Chapter two talks about alpha beta gamma delta epsilon",
    "Chapter three lorem ipsum dolor sit amet consectetur",
]
PAGES_B = [
    "The quick red fox jumps over the dog\nA second line that stays the same",
    "Completely new material inserted here with unrelated vocabulary words",
    "Chapter two talks about alpha beta gamma delta epsilon",
    "Chapter three lorem ipsum dolor sit amet consectetur",
]


def _pdf(pages, img=(255, 0, 0)) -> bytes:
    d = pymupdf.open()
    for t in pages:
        p = d.new_page()
        y = 72
        for line in t.split("\n"):
            p.insert_text((72, y), line, fontsize=11)
            y += 16
        if img is not None:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pix.set_rect(pix.irect, img)
            p.insert_image(pymupdf.Rect(300, 400, 400, 500), pixmap=pix)
    return d.tobytes()


# ------------------------------------------------------------------------------------------- core
def test_text_diff_and_page_alignment():
    ch = compare_documents(_pdf(PAGES_A), _pdf(PAGES_B))
    kinds = [(c.kind, c.a_page, c.b_page) for c in ch]
    assert ("replace", 0, 0) in kinds and ("delete", 0, 0) in kinds
    rep = next(c for c in ch if c.kind == "replace")
    assert rep.a_text == "brown" and rep.b_text == "red" and rep.a_rects and rep.b_rects
    dele = next(c for c in ch if c.kind == "delete")
    assert dele.a_text == "lazy" and not dele.b_rects
    # the inserted page aligns as a gap: pages 2-3 of A match pages 3-4 of B without differences
    ins = [c for c in ch if c.kind == "page_insert"]
    assert len(ins) == 1 and ins[0].b_page == 1 and ins[0].a_page is None
    assert not [c for c in ch if c.a_page in (1, 2) or c.b_page in (2, 3)]
    s = summarize(ch)
    assert s["page_insert"] == 1 and s["words_deleted"] == 2 and s["words_inserted"] == 1


def test_deleted_page_and_insert_words():
    a = _pdf(PAGES_B, img=None)
    b = _pdf([PAGES_B[0] + " extra words", PAGES_B[2], PAGES_B[3]], img=None)
    ch = compare_documents(a, b, visual=False)
    assert [c.kind for c in ch] == ["insert", "page_delete"]
    assert ch[0].b_text == "extra words" and ch[1].a_page == 1


def test_visual_diff_detects_image_change():
    ch = compare_documents(_pdf(PAGES_A[:1], (255, 0, 0)), _pdf(PAGES_A[:1], (0, 0, 255)))
    assert [c.kind for c in ch] == ["visual"]
    x0, y0, x1, y1 = ch[0].b_rects[0]
    assert x0 <= 300 and y0 <= 400 and x1 >= 400 and y1 >= 500 and x1 - x0 < 140
    assert compare_documents(_pdf(PAGES_A[:1]), _pdf(PAGES_A[:1])) == []


def test_alignment_handles_textless_pages():
    d = pymupdf.open()
    for c in (0.0, 0.5, 1.0):
        p = d.new_page()
        p.draw_rect(p.rect, color=None, fill=(c, c, c))
    a = pymupdf.open(stream=d.tobytes())
    b = pymupdf.open(stream=d.tobytes())
    b.delete_page(1)
    rows = align([page_info(p) for p in a], [page_info(p) for p in b])
    assert rows == [(0, 0), (1, None), (2, 1)]


# ------------------------------------------------------------------------------------------- view
@pytest.fixture
def settings_sandbox(app):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafCompareTest")
    QSettings().clear()
    yield
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


@pytest.fixture
def win(settings_sandbox):
    from leaf.window import MainWindow

    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()


def test_compare_view_builds_and_navigates(app, win, tmp_path):  # noqa: F811
    from leaf.features import compare as F

    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(_pdf(PAGES_A))
    b.write_bytes(_pdf(PAGES_B))
    win.open_path(str(a))
    assert not win.a_compare_next.isEnabled()
    v = F.compare_with_file(win, str(b))
    assert win.current_view() == "compare" and win.a_compare_next.isEnabled()
    v.finish()
    assert v.done() and len(v.changes) == 3 and v.tree.topLevelItemCount() == 3
    assert "1 page inserted" in v.summary.text()
    assert len(v.items_a) == len(v.items_b) == 4 and v.items_a[1].index is None  # the gap on the A side
    assert v.items_a[2].pos().y() == v.items_b[2].pos().y()  # aligned rows
    win.a_compare_next.trigger()
    assert v.cur == 0 and v.tree.currentItem() is v.tree.topLevelItem(0)
    v.step(1)
    v.step(1)
    assert v.cur == 2 and v.changes[2].kind == "page_insert"
    v.step(1)
    assert v.cur == 0  # wraps
    v.go_to_change(2)
    assert a11y.announcements and "Inserted page" in a11y.announcements[-1]
    # synced scrolling and zoom
    v.set_zoom(1.5)
    pump(app, 0.2)
    v.view_a.verticalScrollBar().setValue(300)
    assert v.view_b.verticalScrollBar().value() == v.view_a.verticalScrollBar().value()
    assert v.view_a.transform().m11() == pytest.approx(1.5) == v.view_b.transform().m11()
    # lazy rendering + overlay mode
    pump(app, 0.5)
    assert any(it.img is not None for it in v.items_b)
    v.overlay_btn.click()
    assert v.mode == "overlay" and not v.view_a.isVisible()
    pump(app, 0.5)
    assert any(it.img_key and it.img_key[1] == "overlay" for it in v.items_b)
    v.close_view()
    assert win.current_view() == "document" and win.compare_view is None


def test_compare_with_saved_version(app, win, tmp_path):  # noqa: F811
    from leaf.features import compare as F

    a = tmp_path / "s.pdf"
    a.write_bytes(_pdf(PAGES_A[:1], img=None))
    win.open_path(str(a))
    add(win.canvas, 0, ShapeItem("rect", QRectF(300, 600, 120, 80), {"color": "#d43a2a", "width": 3.0}))
    v = F.compare_with_saved(win)
    v.finish()
    assert [c.kind for c in v.changes] == ["visual"]
    x0, y0, x1, y1 = v.changes[0].b_rects[0]
    assert x0 < 310 and y0 < 610 and x1 > 410 and y1 > 670
    v.close_view()


def test_overlay_marks_changed_pixels(app):  # noqa: F811
    from PySide6.QtGui import QColor, QImage

    from leaf.features.compare import overlay

    a = QImage(20, 20, QImage.Format.Format_RGB888)
    a.fill(QColor("white"))
    b = a.copy()
    b.setPixelColor(5, 5, QColor("black"))
    out = overlay(a, b)
    assert out.pixelColor(5, 5).red() > 150 and out.pixelColor(5, 5).green() < 100
    assert out.pixelColor(10, 10) == QColor("white")


# ------------------------------------------------------------------------------------ accessibility
def test_key_widgets_have_accessible_names(app, win, tmp_path):  # noqa: F811
    p = tmp_path / "x.pdf"
    make_pdf(str(p), form=False)
    win.open_path(str(p))
    pump(app, 0.2)
    for w in (win.canvas, win.sidebar, win.props, win.page_spin, win.zoom_slider, win.zoom_box, win.toolbar):
        iface = QAccessible.queryAccessibleInterface(w)
        assert iface is not None and iface.text(QAccessible.Text.Name).strip(), w
    for b in win.toolbar.findChildren(QToolButton):
        if b.isVisible() and b.defaultAction() is not None:
            assert a11y.name_of(b).strip(), b.defaultAction().text()
    missing = [w for w in a11y.unnamed(win) if w.isVisible()]
    assert not missing, [(type(w).__name__, w.objectName(), w.toolTip()) for w in missing]
    from leaf.props import Swatch

    chips = win.findChildren(Swatch)
    assert chips and all(c.accessibleName() for c in chips)
    item = win.sidebar.item(0)
    from PySide6.QtCore import Qt

    assert item.data(Qt.ItemDataRole.AccessibleTextRole) == "Page 1"


def test_dialog_controls_get_names(app):  # noqa: F811
    a11y.install_filter_for_tests()
    d = QDialog()
    form = QFormLayout(d)
    spin = QSpinBox()
    form.addRow("Copies", spin)
    edit = QLineEdit()
    edit.setPlaceholderText("Search actions")
    form.addRow(edit)
    btn = QToolButton()
    btn.setToolTip("Rotate left  (Ctrl+Shift+L)")
    form.addRow(btn)
    d.show()
    pump(app, 0.05)
    assert a11y.name_of(spin) == "Copies"
    assert a11y.name_of(edit) == "Search actions"
    assert btn.accessibleName() == "Rotate left"
    d.close()


def test_page_change_announced(app, win, tmp_path):  # noqa: F811
    p = tmp_path / "x.pdf"
    make_pdf(str(p), form=False)
    win.open_path(str(p))
    win.canvas.go_to(1)
    win.canvas._a11y_update()
    assert win.canvas.accessibleName() == "Page 2 of 3"
    assert a11y.announcements[-1] == "Page 2 of 3"
    assert win.canvas.accessibleDescription()


def test_focus_order_main_window(app, win, tmp_path):  # noqa: F811
    p = tmp_path / "x.pdf"
    make_pdf(str(p), form=False)
    win.open_path(str(p))
    pump(app, 0.1)
    a11y.fix_focus_order(win)
    # walk the real focus chain: Pages panel -> page -> panels on the right -> status bar
    chain, w = [], win
    for _ in range(5000):
        w = w.nextInFocusChain()
        if w is win:
            break
        if a11y._tabbable(w, win):
            chain.append(w)
    assert win.sidebar in chain and win.canvas in chain
    assert chain.index(win.sidebar) < chain.index(win.canvas)
    after = [x for x in chain[chain.index(win.canvas) + 1:] if win.props.isAncestorOf(x)]
    before = [x for x in chain[:chain.index(win.canvas)] if win.props.isAncestorOf(x)]
    assert after and not before
    assert chain.index(win.canvas) < chain.index(win.page_spin)


def test_focus_order_walk_keeps_wrappers_alive(app, win):  # noqa: F811
    """Walking the focus chain must not tie the window's Python wrapper to its last widget: deleting that widget
    (a panel's tab close button when Qt drops a tab bar, any Python-made widget) invalidated the menus' and panel
    toggles' wrappers, so opening a document left Presentation disabled and Focus mode came up without its bars."""
    import shiboken6
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QWidget

    pump(app, 0.05)
    held = [win.a_comments_panel, win.a_outline, win.a_sidebar, win.recent_menu, win.menuBar()]
    gone = QWidget(win)  # made after the start-up walk: the last widget in the focus chain
    a11y.fix_focus_order(win)
    gone.deleteLater()
    del gone
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # (an event loop does this; pump doesn't)
    assert all(shiboken6.isValid(o) for o in held)


def test_contrast_helpers(app):  # noqa: F811
    assert a11y.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert a11y.contrast_ratio(a11y.readable("#c0392b", "#202020"), "#202020") >= 4.5
    assert a11y.contrast_ratio(a11y.readable("#ffd83d", "#ffffff"), "#ffffff") >= 4.5
    for bg in ("#ffffff", "#1e1e1e"):  # toast surface/label colors in both themes (see toast._restyle)
        fg = "#1a1a1a" if bg == "#ffffff" else "#f5f5f5"
        assert a11y.contrast_ratio(fg, bg) >= 7
    assert a11y.color_name("#d43a2a") == "red" and a11y.color_name("#1f4fd1") == "blue"
    os.environ["LEAF_HIGH_CONTRAST"] = "1"
    try:
        assert a11y.high_contrast()
    finally:
        del os.environ["LEAF_HIGH_CONTRAST"]


# --------------------------------------------------------------------------------------------- i18n
def test_tr_and_loader_without_qm(app, tmp_path):  # noqa: F811
    assert i18n.tr("&File") == "&File"
    loaded = i18n.install(QLocale("de_DE"), app_dir=str(tmp_path), force=True)  # no leaf_de.qm there
    try:
        assert not any("leaf_" in os.path.basename(p) for p in loaded)
        if any("qtbase_de" in p for p in loaded):  # Qt's own strings (when the build ships translations)
            assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Abbrechen"
        assert i18n.tr("&File") == "&File"  # nothing of ours translated: the source text
    finally:
        i18n.uninstall()


def test_loader_with_qm(app, tmp_path):  # noqa: F811
    import shutil
    import subprocess
    import sys

    lrelease = shutil.which("pyside6-lrelease") or os.path.join(os.path.dirname(sys.executable), "pyside6-lrelease.exe")
    if not os.path.exists(lrelease) and not shutil.which("pyside6-lrelease"):
        pytest.skip("pyside6-lrelease not available")
    ts = tmp_path / "leaf_fr.ts"
    ts.write_text('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE TS><TS version="2.1" language="fr">'
                  '<context><name></name><message><source>&amp;File</source><translation>&amp;Fichier</translation>'
                  '</message></context></TS>', encoding="utf-8")
    subprocess.run([lrelease, str(ts), "-qm", str(tmp_path / "leaf_fr.qm")], check=True, capture_output=True)
    loaded = i18n.install(QLocale("fr_FR"), app_dir=str(tmp_path), force=True)
    try:
        assert any(p.endswith("leaf_fr.qm") for p in loaded)
        assert i18n.tr("&File") == "&Fichier"
    finally:
        i18n.uninstall()
    assert i18n.tr("&File") == "&File"


def test_pseudo_locale(app, monkeypatch):  # noqa: F811
    s = i18n.pseudo("&Save %1 of {n} <b>pages</b>\tCtrl+S")
    assert s.startswith("[&Š") and "%1" in s and "{n}" in s and "<b>" in s and s.endswith("]\tCtrl+S") and "~" in s
    assert i18n.pseudo(s) == s and i18n.pseudo("") == "" and i18n.pseudo("123") == "123"
    monkeypatch.setenv("LEAF_PSEUDO", "1")
    i18n.install(force=True)
    try:
        assert i18n.tr("&File") == i18n.pseudo("&File") != "&File"
    finally:
        i18n.uninstall()
        monkeypatch.delenv("LEAF_PSEUDO")
    assert i18n.tr("&File") == "&File"
