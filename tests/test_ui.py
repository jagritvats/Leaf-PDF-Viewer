"""UI flow: view stack, welcome screen, toasts, floating mini toolbar."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QRectF, QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel

from leaf.items import ShapeItem
from tests.test_editor import add, app, make_pdf, pump  # noqa: F401


@pytest.fixture
def settings_sandbox(app):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafUiTest")
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


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "ui.pdf")
    make_pdf(path, form=False)
    return path


def test_view_stack(app, win, pdf):  # noqa: F811
    assert win.current_view() == "welcome"
    assert win.canvas is not None and win.find is not None and win.a_undo is not None
    extra = QLabel("extra")
    win.add_view("extra", extra)
    win.show_view("extra")
    assert win.current_view() == "extra" and win.views.currentWidget() is extra
    win.show_view("welcome")
    assert not win.side_dock.isVisible()  # panels are tucked away without a document
    win.open_path(pdf)
    assert win.current_view() == "document"
    assert win.statusBar().isVisible()
    win.show_view("nope")  # unknown keys are ignored
    assert win.current_view() == "document"


def test_welcome_recent_list(app, win, pdf, tmp_path):  # noqa: F811
    missing = str(tmp_path / "gone.pdf")
    win.settings.setValue("recent", [pdf, missing])
    wv = win.welcome
    wv.refresh()
    assert wv.recent.count() == 1  # missing files are skipped
    pump(app, 0.3)
    assert not wv.recent.item(0).icon().isNull()
    from leaf.welcome import render_thumb

    assert render_thumb(pdf, 40) is not None and render_thumb(missing, 40) is None
    wv.remove(pdf)
    assert wv.recent.count() == 0 and wv.empty.isVisibleTo(wv)
    win.settings.setValue("recent", [pdf])
    wv.refresh()
    wv.clear_recent()
    assert wv.recent.count() == 0 and not win.settings.value("recent")
    win._add_recent(pdf)
    assert win.settings.value("recent")[0] == pdf


def test_toast_shows_and_hides(app, win):  # noqa: F811
    from leaf.toast import Toast, show_toast

    hits = []
    t = show_toast(win, "Saved", "Undo", lambda: hits.append(1), ms=800)
    assert isinstance(t, Toast) and t.isVisible() and t.label.text() == "Saved"
    assert show_toast(win, "Again", ms=800) is t  # one at a time, reused
    assert not t.button.isVisible()
    t2 = show_toast(win, "Saved", "Undo", lambda: hits.append(1), ms=800)
    t2.button.click()
    assert hits == [1]
    show_toast(win, "Bye", ms=800)
    pump(app, 1.5)
    assert not t.isVisible()


def test_minibar_applies_color_undoably(app, win, pdf):  # noqa: F811
    win.open_path(pdf)
    pump(app, 0.3)
    c = win.canvas
    item = add(c, 0, ShapeItem("rect", QRectF(100, 120, 120, 60), {"color": "#d43a2a"}))
    c.scene().clearSelection()
    c.ensureVisible(item.sceneBoundingRect())
    item.setSelected(True)
    pump(app, 0.2)
    mb = win.minibar
    assert mb.isVisible()
    assert c.viewport().rect().intersects(mb.geometry())
    blue = next(s for s in mb.swatches if QColor(s.color).name() == "#1f4fd1")
    blue.click()
    assert item.props["color"] == "#1f4fd1"
    assert win.props._color == "#1f4fd1"  # style panel follows
    c.undo.undo()
    assert item.props["color"] == "#d43a2a"
    c.scene().clearSelection()
    pump(app, 0.1)
    assert not mb.isVisible()
