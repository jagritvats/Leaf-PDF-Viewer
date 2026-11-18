"""OS integration (leaf/osint.py): off-platform safety, taskbar progress + sleep inhibition bookkeeping, pen input
(pressure widths, eraser, barrel-button pan, palm rejection) and the pressure ink written into the PDF."""
from __future__ import annotations

import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QInputDevice, QMouseEvent, QPointingDevice, QTabletEvent
from PySide6.QtWidgets import QApplication, QWidget

from leaf import osint
from leaf.items import InkItem, ink_centerline, pressure_outline, pressure_width
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401

CAPS = QInputDevice.Capability.Position | QInputDevice.Capability.Pressure
_devices: dict = {}


def device(kind: str) -> QPointingDevice:
    if kind not in _devices:
        if kind == "touch":
            _devices[kind] = QPointingDevice("Test touch", 9003, QInputDevice.DeviceType.TouchScreen,
                                             QPointingDevice.PointerType.Finger, QInputDevice.Capability.Position, 10, 0)
        else:
            ptype = QPointingDevice.PointerType.Eraser if kind == "eraser" else QPointingDevice.PointerType.Pen
            _devices[kind] = QPointingDevice(f"Test {kind}", 9001 if kind == "pen" else 9002,
                                             QInputDevice.DeviceType.Stylus, ptype, CAPS, 1, 3)
    return _devices[kind]


def tablet(cv, etype, pos: QPointF, pressure: float, kind: str = "pen", button=Qt.MouseButton.LeftButton) -> bool:
    buttons = Qt.MouseButton.NoButton if etype == QEvent.Type.TabletRelease else button
    ev = QTabletEvent(etype, device(kind), pos, cv.viewport().mapToGlobal(pos), pressure, 0, 0, 0, 0, 0,
                      Qt.KeyboardModifier.NoModifier, button, buttons)
    QApplication.sendEvent(cv.viewport(), ev)
    return ev.isAccepted()


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "pen.pdf")
    make_pdf(path, rotate_second=False, form=False)
    return path


def page_point(cv, idx: int, x: float, y: float) -> QPointF:
    """Viewport position of page point (x, y) on page `idx`."""
    pg = cv.order[idx]
    return QPointF(cv.mapFromScene(pg.content.mapToScene(QPointF(x, y))))


def draw_stroke(cv, pressures, kind="pen") -> None:
    start = page_point(cv, 0, 100, 200)
    tablet(cv, QEvent.Type.TabletPress, start, pressures[0], kind)
    for k, p in enumerate(pressures[1:], 1):
        tablet(cv, QEvent.Type.TabletMove, start + QPointF(k * 6, 4 * math.sin(k / 3)), p, kind)
    tablet(cv, QEvent.Type.TabletRelease, start + QPointF(len(pressures) * 6, 0), pressures[-1], kind)


def inks(cv) -> list:
    return [it for it in cv.order[0].content.childItems() if isinstance(it, InkItem)]


# ------------------------------------------------------------------------------------------- off-platform safety
def test_calls_are_safe_without_a_window(app):  # noqa: F811
    osint.progress(None, 0.5)
    osint.progress(None, None)
    w = QWidget()  # never shown: no native handle
    osint.progress(w, 0.25, key="t1")
    osint.progress(w, osint.INDETERMINATE, key="t1", error=True)
    osint.progress(w, None, key="t1")
    osint.progress(w, None, key="never-started")
    assert "t1" not in osint.active_tasks()
    assert osint.apply_title_bar(w) is False  # offscreen: no native window
    assert osint.alert(w) is False and osint.alert(None) is False
    assert osint._hwnd(w) is None
    assert osint.add_recent_document("") is False
    # the test platform never touches the user's real recent list
    assert osint.add_recent_document(os.path.abspath(__file__)) is False
    osint.forget_window(id(w))


def test_recent_documents_respects_setting(app, monkeypatch):  # noqa: F811
    monkeypatch.setattr(osint, "_native", lambda: True)
    monkeypatch.setattr(osint, "setting", lambda key, default=True: False)
    assert osint.add_recent_document(__file__) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM taskbar")
def test_windows_taskbar_com_object(app):  # noqa: F811
    tb = osint._taskbar()
    assert tb is not None and tb.value
    assert osint._taskbar() is tb  # created once


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-ops")
def test_non_windows_no_ops(app):  # noqa: F811
    assert osint._taskbar() is None
    assert osint.add_recent_document(__file__) is False


# ------------------------------------------------------------------------------------------- progress + sleep
def test_progress_inhibits_sleep_until_every_task_ends(app, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(osint, "_os_sleep", lambda on, reason: calls.append(on) or True)
    monkeypatch.setattr(osint, "setting", lambda key, default=True: True)
    w = QWidget()
    osint.progress(w, 0.0, key="ocr")
    assert osint.sleep_inhibited() and calls == [True]
    osint.progress(w, 0.5, key="ocr")
    osint.progress(w, 0.1, key="export")
    assert calls == [True]  # reference counted
    osint.progress(w, None, key="ocr")
    assert osint.sleep_inhibited()
    osint.progress(w, None, key="export")
    assert not osint.sleep_inhibited() and calls == [True, False]
    osint.progress(w, 0.3, key="replace", keep_awake=False)  # interactive scans don't hold the system awake
    assert not osint.sleep_inhibited() and calls == [True, False]
    osint.progress(w, None, key="replace")
    # a window closing mid-task releases it too
    osint.progress(w, 0.2, key="optimize")
    assert osint.sleep_inhibited()
    osint.forget_window(id(w))
    assert not osint.sleep_inhibited() and osint.active_tasks() == []


def test_keep_awake_setting_off(app, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(osint, "_os_sleep", lambda on, reason: calls.append(on) or True)
    monkeypatch.setattr(osint, "setting", lambda key, default=True: key != osint.KEY_AWAKE)
    w = QWidget()
    osint.progress(w, 0.0, key="ocr")
    osint.progress(w, None, key="ocr")
    assert calls == []


def test_real_sleep_inhibit_set_and_released(app):  # noqa: F811
    osint.inhibit_sleep("test", "test")
    on = osint.sleep_inhibited()
    if sys.platform == "win32":
        assert on  # SetThreadExecutionState always works
    osint.release_sleep("test")
    assert not osint.sleep_inhibited()


# ------------------------------------------------------------------------------------------- pen input
def test_palm_rejection_logic(app):  # noqa: F811
    T, M, S = QInputDevice.DeviceType.TouchScreen, QInputDevice.DeviceType.Mouse, QInputDevice.DeviceType.Stylus
    assert osint.palm_reject(T, True, 100.0)
    assert osint.palm_reject(T, False, 0.1)
    assert not osint.palm_reject(T, False, osint.PALM_GRACE + 1)
    assert not osint.palm_reject(M, True, 0.0)
    assert not osint.palm_reject(S, True, 0.0)


def test_palm_rejection_filters_touch_mouse_events(app, pdf):  # noqa: F811
    cv = canvas_for(pdf)
    pen = osint.PenInput(cv, pressure_enabled=lambda: True)
    pos = page_point(cv, 0, 300, 300)

    def touch_press():
        return QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, device("touch"))

    assert pen.eventFilter(cv.viewport(), touch_press()) is False  # no pen around: touch works
    tablet(cv, QEvent.Type.TabletMove, pos, 0.0)  # pen hovering
    assert pen.eventFilter(cv.viewport(), touch_press()) is True  # resting palm ignored
    mouse = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    assert pen.eventFilter(cv.viewport(), mouse) is False  # a real mouse always works
    pen.last_pen -= 10
    pen.eventFilter(cv.viewport(), QEvent(QEvent.Type.TabletEnterProximity))
    assert pen.eventFilter(cv.viewport(), touch_press()) is True
    pen.eventFilter(cv.viewport(), QEvent(QEvent.Type.TabletLeaveProximity))
    assert pen.eventFilter(cv.viewport(), touch_press()) is False


def test_pen_pressure_gives_variable_width(app, pdf, tmp_path):  # noqa: F811
    cv = canvas_for(pdf)
    osint.PenInput(cv, pressure_enabled=lambda: True)
    cv.set_tool("ink")
    pressures = [0.1 + 0.9 * k / 30 for k in range(31)]
    draw_stroke(cv, pressures)
    items = inks(cv)
    assert len(items) == 1 and cv.undo.count() == 1
    ink = items[0]
    assert ink.has_pressure()
    ws = ink.point_widths()
    assert len(ws) == len(ink.pts) > 10
    assert max(ws) > 2 * min(ws)  # light start, heavy end
    assert ws[-1] > ws[0]
    base = float(ink.props["width"])
    assert max(ws) <= pressure_width(base, 1.0) + 1e-9
    # sub-pixel start, not snapped to whole viewport pixels
    assert abs(ink.pos().x() - 100) < 1.5 and abs(ink.pos().y() - 200) < 1.5
    # drawn as a filled outline covering the stroke
    assert ink._outline().contains(ink.pts[len(ink.pts) // 2])

    out = str(tmp_path / "pen-out.pdf")
    cv.doc.save(out, cv.overlays_for)
    d = pymupdf.open(out)
    page = d[0]
    a = next(a for a in page.annots() if a.type[1] == "Ink")
    avg = sum(ws) / len(ws)
    assert abs(a.border["width"] - avg) < 0.05
    ap = bytes(a._getAP()).decode("latin-1")
    assert " rg" in ap and "\nf\n" in ap and " RG" not in ap  # filled outline, not a stroked line
    assert a.rect.contains(pymupdf.Rect(100, 197, 280, 203))
    d.close()
    # undo removes the stroke in one step
    cv.undo.undo()
    assert not inks(cv)


def test_pen_without_pressure_setting(app, pdf):  # noqa: F811
    cv = canvas_for(pdf)
    osint.PenInput(cv, pressure_enabled=lambda: False)
    cv.set_tool("ink")
    draw_stroke(cv, [0.2, 0.9, 0.5, 1.0, 0.3, 0.8])
    (ink,) = inks(cv)
    assert not ink.has_pressure() and ink.widths == []
    assert len(set(ink.point_widths())) == 1


def test_pen_in_other_tools_is_left_to_qt(app, pdf):  # noqa: F811
    cv = canvas_for(pdf)
    osint.PenInput(cv)
    cv.set_tool("select")
    assert tablet(cv, QEvent.Type.TabletPress, page_point(cv, 0, 100, 100), 0.5) is False
    tablet(cv, QEvent.Type.TabletRelease, page_point(cv, 0, 100, 100), 0.0)
    cv.set_tool("ink")  # off the page: nothing to draw, Qt makes mouse events of it
    assert tablet(cv, QEvent.Type.TabletPress, QPointF(2, 2), 0.5) is False
    assert not inks(cv)


def test_eraser_end_deletes_ink_under_it(app, pdf):  # noqa: F811
    cv = canvas_for(pdf)
    osint.PenInput(cv)
    ink = InkItem(QPointF(0, 0))
    ink.setPos(QPointF(200, 300))
    for k in range(20):
        ink.add_point(QPointF(k * 3, 0))
    add(cv, 0, ink)
    other = InkItem(QPointF(0, 0))
    other.setPos(QPointF(200, 500))
    other.add_point(QPointF(40, 0))
    add(cv, 0, other)
    n = cv.undo.count()
    cv.set_tool("select")  # the eraser works whatever the tool
    p = page_point(cv, 0, 220, 300)
    assert tablet(cv, QEvent.Type.TabletPress, p, 0.5, "eraser")
    assert not ink.isVisible()
    tablet(cv, QEvent.Type.TabletMove, p + QPointF(3, 0), 0.5, "eraser")
    tablet(cv, QEvent.Type.TabletRelease, p + QPointF(3, 0), 0.0, "eraser")
    assert ink.scene() is None and other.scene() is not None
    assert cv.undo.count() == n + 1 and cv.undo.undoText() == "Erase"
    cv.undo.undo()
    assert ink.scene() is not None and ink.isVisible()


def test_barrel_button_pans(app, pdf):  # noqa: F811
    cv = canvas_for(pdf)
    osint.PenInput(cv)
    cv.set_tool("ink")
    vb = cv.verticalScrollBar()
    assert vb.maximum() > 0
    vb.setValue(200)
    p = page_point(cv, 0, 300, 400)
    assert tablet(cv, QEvent.Type.TabletPress, p, 0.5, button=Qt.MouseButton.RightButton)
    tablet(cv, QEvent.Type.TabletMove, p - QPointF(0, 80), 0.5, button=Qt.MouseButton.RightButton)
    tablet(cv, QEvent.Type.TabletRelease, p - QPointF(0, 80), 0.0, button=Qt.MouseButton.RightButton)
    assert vb.value() == 280
    assert not inks(cv)  # panning drew nothing


def test_pressure_outline_is_consistently_wound(app):  # noqa: F811
    pts = [(k * 0.7, 10 * math.sin(k / 2)) for k in range(60)] + [(40, 0), (40, 0), (0, 0)]  # tight turns, repeats
    ws = [1 + 4 * abs(math.sin(k / 7)) for k in range(len(pts))]
    polys = pressure_outline(ink_centerline(pts, ws))
    assert polys
    for poly in polys:
        area = sum(poly[k - 1][0] * poly[k][1] - poly[k][0] * poly[k - 1][1] for k in range(len(poly)))
        assert area >= 0  # all the same way round: a non-zero fill never cancels overlaps
    assert len(pressure_outline(ink_centerline([(5, 5)], [3]))) == 1  # a dot


def test_clone_keeps_pressure(app, pdf):  # noqa: F811
    from leaf import pageops
    from leaf.features import history

    ink = InkItem(QPointF(0, 0))
    for k in range(5):
        ink.add_point(QPointF(k * 3, k), pressure=k / 5)
    assert ink.has_pressure()
    for fn in (pageops.clone_overlay, history.clone):
        c = fn(ink)
        assert c is not None and c.widths == ink.widths


# ------------------------------------------------------------------------------------------- window wiring
def test_feature_installed_in_window(app, pdf, monkeypatch, tmp_path):  # noqa: F811
    from PySide6.QtCore import QCoreApplication, QSettings

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafOsintTest")
    QSettings().clear()
    try:
        from leaf.window import MainWindow

        added = []
        monkeypatch.setattr(osint, "add_recent_document", lambda p: added.append(p) or True)
        w = MainWindow()
        try:
            assert isinstance(w.pen_input, osint.PenInput)
            w.open_path(pdf)
            assert added == [os.path.abspath(pdf)]
            from leaf.features import osint as F

            page = F.settings_page(w)
            from PySide6.QtWidgets import QCheckBox

            cb = page.findChild(QCheckBox, "system_pen_pressure")
            assert cb.isChecked()
            cb.setChecked(False)
            assert osint.setting(osint.KEY_PRESSURE) is False
            cb.setChecked(True)
            assert osint.setting(osint.KEY_PRESSURE) is True
            page.deleteLater()
        finally:
            w.canvas.undo.setClean()
            w.close()
            pump(app, 0.05)
    finally:
        QSettings().clear()
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])
