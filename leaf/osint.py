"""OS runtime integration: small functions that do the platform-specific thing where it exists and nothing
elsewhere (ctypes only on Windows, QtDBus on Linux; every call is safe off-platform, without a window
handle and under QT_QPA_PLATFORM=offscreen).

- Recent documents: `add_recent_document(path)` -> SHAddToRecentDocs(SHARD_PATHW). Windows lists the file in
  Explorer's Recent/Quick access and Start's recommendations. The *taskbar jump list* "Recent" category of Leaf
  (AppUserModelID "Leaf.PdfEditor", set in __main__) only shows files of a type Leaf is registered for: the
  installer's .pdf OpenWithProgids entry (ProgID Leaf.pdf). A portable run (no registration) gets the system
  recent list but no jump-list entries. Linux: nothing (Qt has no API for ~/.local/share/recently-used.xbel and
  the desktops fill it from their own file dialogs).
- Taskbar progress: `progress(win, fraction)` -> ITaskbarList3 on Windows (COM vtable calls through ctypes),
  com.canonical.Unity.LauncherEntry over D-Bus on Linux (KDE Plasma, Ubuntu Dock, Dash to Dock). While any task
  runs the system is kept awake (`keep_awake`), and when a long task ends in a window that isn't active the
  window asks for attention (QApplication.alert: flashes the taskbar button / urgency hint).
- Sleep: SetThreadExecutionState(ES_SYSTEM_REQUIRED) on Windows, org.freedesktop.ScreenSaver.Inhibit on Linux
  (inhibits idle, which is what suspends a desktop session), reference counted.
- Dark title bar: DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE) following the window palette. Qt 6 does
  this itself when the *system* is dark; this keeps the frame right when the app palette differs. No Mica: it
  needs a translucent window background, which fights the palette-painted welcome screen.
- Pen input: `PenInput` (an event filter on the canvas viewport): pressure -> per-point ink width, the eraser
  end deletes ink strokes under it, the barrel button pans, touches are ignored while the pen is near (palm
  rejection), and pen strokes use the tablet's sub-pixel positions with light smoothing.
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QEvent, QObject, QPointF, QSettings, Qt

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
APP_ID = "Leaf.PdfEditor"

# settings keys (QSettings, shared by every window); all default to on
KEY_RECENT = "system/recent_docs"
KEY_PRESSURE = "system/pen_pressure"
KEY_AWAKE = "system/keep_awake"
KEY_TASKBAR = "system/taskbar_progress"

INDETERMINATE = -1.0  # progress(win, INDETERMINATE): busy, amount unknown
ALERT_AFTER = 3.0  # seconds: a task at least this long asks for attention when it ends in the background


def setting(key: str, default: bool = True) -> bool:
    try:
        v = QSettings().value(key, default)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    return bool(v)


def _native() -> bool:
    """True when windows are real native windows (not offscreen/minimal test platforms)."""
    try:
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.instance() is not None and QGuiApplication.platformName() in ("windows", "xcb", "wayland")
    except Exception:  # noqa: BLE001
        return False


def _hwnd(win) -> int | None:
    """The native top-level handle of `win` (Windows platform plugin only), without creating one."""
    if not IS_WIN or win is None or not _native():
        return None
    try:
        top = win.window()
        if top.windowHandle() is None:
            return None
        return int(top.winId()) or None
    except (RuntimeError, AttributeError):  # deleted C++ object
        return None


# ---------------------------------------------------------------------------------------- recent documents
SHARD_PATHW = 0x00000003


def add_recent_document(path: str) -> bool:
    """Add `path` to the OS recent documents (Windows). Returns True if the OS call was made."""
    if not IS_WIN or not path or not _native() or not setting(KEY_RECENT):
        return False  # (not under the offscreen test platform: tests mustn't fill the user's recent list)
    try:
        import ctypes

        fn = ctypes.windll.shell32.SHAddToRecentDocs
        fn.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
        fn.restype = None
        fn(SHARD_PATHW, os.path.abspath(path))
        return True
    except Exception:  # noqa: BLE001 - cosmetic only
        return False


# ---------------------------------------------------------------------------------------- taskbar progress
TBPF_NOPROGRESS, TBPF_INDETERMINATE, TBPF_NORMAL, TBPF_ERROR, TBPF_PAUSED = 0, 1, 2, 4, 8
_taskbar_obj = None  # ITaskbarList3*, created once (False: unavailable)


def _taskbar():
    """The ITaskbarList3 COM object (a c_void_p), or None."""
    global _taskbar_obj
    if _taskbar_obj is not None:
        return _taskbar_obj or None
    _taskbar_obj = False
    if not IS_WIN:
        return None
    try:
        import ctypes
        import uuid

        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0x2)  # apartment threaded; S_FALSE / RPC_E_CHANGED_MODE when Qt already did it
        clsid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("56FDF344-FD6D-11d0-958A-006097C9A090").bytes_le)
        iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf").bytes_le)
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return None
        if _vcall(ptr, 3, ()) != 0:  # ITaskbarList::HrInit
            return None
        _taskbar_obj = ptr
        return ptr
    except Exception:  # noqa: BLE001
        return None


def _vcall(ptr, index: int, args: tuple, argtypes: tuple = ()) -> int:
    """Call vtable slot `index` of COM object `ptr` (HRESULT-returning, stdcall)."""
    import ctypes

    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(ptr, *args)


def _set_taskbar(win, fraction: float | None, error: bool) -> bool:
    hwnd = _hwnd(win)
    tb = _taskbar() if hwnd else None
    if tb is None:
        return False
    import ctypes

    HWND, ULL, INT = ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_int
    try:
        if fraction is None:
            _vcall(tb, 10, (hwnd, TBPF_NOPROGRESS), (HWND, INT))  # SetProgressState
        elif fraction < 0:
            _vcall(tb, 10, (hwnd, TBPF_ERROR if error else TBPF_INDETERMINATE), (HWND, INT))
        else:
            _vcall(tb, 10, (hwnd, TBPF_ERROR if error else TBPF_NORMAL), (HWND, INT))
            _vcall(tb, 9, (hwnd, int(min(max(fraction, 0.0), 1.0) * 1000), 1000), (HWND, ULL, ULL))  # SetProgressValue
        return True
    except Exception:  # noqa: BLE001
        return False


def _launcher_entry(fraction: float | None) -> bool:
    """Linux: Unity LauncherEntry progress on the dock/task manager icon (needs leaf.desktop installed)."""
    if not IS_LINUX:
        return False
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusMessage
        from PySide6.QtGui import QGuiApplication

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        app = (QGuiApplication.desktopFileName() or "leaf") + ".desktop"
        msg = QDBusMessage.createSignal("/com/canonical/unity/launcherentry/leaf", "com.canonical.Unity.LauncherEntry",
                                        "Update")
        props = {"progress-visible": fraction is not None and fraction >= 0,
                 "progress": float(min(max(fraction or 0.0, 0.0), 1.0))}
        msg.setArguments([f"application://{app}", props])
        return bus.send(msg)
    except Exception:  # noqa: BLE001
        return False


_tasks: dict[tuple[int, str], dict] = {}  # (id(window), key) -> {"f": fraction, "t0": start, "awake": bool}


def progress(win, fraction: float | None, error: bool = False, key: str = "task", keep_awake: bool = True) -> None:
    """Show a long task's progress on the window's taskbar button (fraction 0..1, INDETERMINATE, or None when
    the task ends). Keeps the system awake while any `keep_awake` task runs; when a task that ran for a while
    ends in an inactive window, the window asks for attention. Never raises."""
    try:
        _progress(win, fraction, error, key, keep_awake)
    except Exception:  # noqa: BLE001 - progress display must never break the task
        pass


def _progress(win, fraction, error, key, keep_awake) -> None:
    top = win.window() if win is not None and hasattr(win, "window") else win
    k = (id(top), key)
    if fraction is None:
        t = _tasks.pop(k, None)
        if t is None:
            return
        if t["awake"]:
            release_sleep(f"{k[0]}:{key}")
        others = [v for (w, _), v in _tasks.items() if w == k[0]]
        if setting(KEY_TASKBAR):
            _set_taskbar(top, others[-1]["f"] if others else None, False)
            _launcher_entry(others[-1]["f"] if others else None)
        if time.monotonic() - t["t0"] >= ALERT_AFTER:
            alert(top)
        return
    t = _tasks.get(k)
    if t is None:
        t = _tasks[k] = {"f": fraction, "t0": time.monotonic(), "awake": False}
        if keep_awake and setting(KEY_AWAKE):
            inhibit_sleep(f"{k[0]}:{key}", "Leaf is working on a document")
            t["awake"] = True
    elif t["f"] is not None and fraction >= 0 and abs(fraction - t["f"]) < 0.004 and not error:
        return  # nothing visible changes (keeps per-step overhead negligible)
    t["f"] = fraction
    if setting(KEY_TASKBAR):
        _set_taskbar(top, fraction, error)
        _launcher_entry(fraction)


def active_tasks() -> list[str]:
    return [key for (_, key) in _tasks]


def forget_window(win_id: int) -> None:
    """A window went away: end its tasks (releases sleep inhibition)."""
    for k in [k for k in _tasks if k[0] == win_id]:
        t = _tasks.pop(k)
        if t["awake"]:
            release_sleep(f"{k[0]}:{k[1]}")


def alert(win) -> bool:
    """Ask for attention if `win` isn't the active window (taskbar flash / urgency hint)."""
    try:
        from PySide6.QtWidgets import QApplication

        if win is None or QApplication.instance() is None or win.isActiveWindow() or not win.isVisible():
            return False
        QApplication.alert(win, 0)
        return True
    except (RuntimeError, AttributeError):
        return False


# ---------------------------------------------------------------------------------------- sleep
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
_inhibitors: set[str] = set()
_os_inhibit = {"on": False, "cookie": None}


def inhibit_sleep(token: str, reason: str = "Working") -> bool:
    """Keep the system awake until `release_sleep(token)` (reference counted by token). Returns True if the
    system is now inhibited (False where unsupported)."""
    _inhibitors.add(token)
    if not _os_inhibit["on"]:
        _os_inhibit["on"] = _os_sleep(True, reason)
    return _os_inhibit["on"]


def release_sleep(token: str) -> None:
    _inhibitors.discard(token)
    if not _inhibitors and _os_inhibit["on"]:
        _os_sleep(False, "")
        _os_inhibit["on"] = False


def sleep_inhibited() -> bool:
    return bool(_os_inhibit["on"])


def _os_sleep(on: bool, reason: str) -> bool:
    if IS_WIN:
        try:
            import ctypes

            fn = ctypes.windll.kernel32.SetThreadExecutionState
            fn.argtypes = [ctypes.c_uint]
            fn.restype = ctypes.c_uint
            return fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS) != 0
        except Exception:  # noqa: BLE001
            return False
    if IS_LINUX:
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface

            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            iface = QDBusInterface("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                                   "org.freedesktop.ScreenSaver", bus)
            if not iface.isValid():
                return False
            if on:
                reply = iface.call("Inhibit", "Leaf", reason)
                args = reply.arguments()
                if not args:
                    return False
                _os_inhibit["cookie"] = int(args[0])
                return True
            cookie, _os_inhibit["cookie"] = _os_inhibit["cookie"], None
            if cookie is not None:
                iface.call("UnInhibit", cookie)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


# ---------------------------------------------------------------------------------------- dark title bar
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 before 20H1


def window_is_dark(win) -> bool:
    from PySide6.QtGui import QPalette

    return win.palette().color(QPalette.ColorRole.Window).lightness() < 128


def apply_title_bar(win) -> bool:
    """Make the native title bar dark or light to match the window's palette (Windows 10 1809+/11)."""
    hwnd = _hwnd(win)
    if hwnd is None:
        return False
    try:
        import ctypes

        val = ctypes.c_int(1 if window_is_dark(win) else 0)
        fn = ctypes.windll.dwmapi.DwmSetWindowAttribute
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        fn.restype = ctypes.c_long
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            if fn(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


class TitleBarFollower(QObject):
    """Re-applies the title bar colour when the window is shown or its palette/theme changes."""

    TYPES = (QEvent.Type.Show, QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
             QEvent.Type.ThemeChange, QEvent.Type.StyleChange)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        win.installEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.win and e.type() in self.TYPES:
            apply_title_bar(self.win)
        return False


# ---------------------------------------------------------------------------------------- pen input
PALM_GRACE = 0.6  # seconds after the last pen event during which touches still count as a resting palm


def palm_reject(device_type, pen_near: bool, since_pen: float) -> bool:
    """Should an event from `device_type` (QInputDevice.DeviceType) be ignored? Touches are dropped while a
    pen is in proximity or was used within PALM_GRACE seconds; mouse, touchpad and pen events never are."""
    from PySide6.QtGui import QInputDevice

    if device_type != QInputDevice.DeviceType.TouchScreen:
        return False
    return pen_near or since_pen < PALM_GRACE


class PenInput(QObject):
    """Event filter on a Canvas viewport. Tablet events that don't need special handling are left alone so Qt
    turns them into mouse events (every tool keeps working with a pen)."""

    SMOOTH = 0.55  # position smoothing (exponential, 1 = raw)
    PSMOOTH = 0.35  # pressure smoothing
    ERASER_PX = 7

    def __init__(self, canvas, pressure_enabled=None):
        super().__init__(canvas)
        self.cv = canvas
        self.pressure_enabled = pressure_enabled or (lambda: setting(KEY_PRESSURE))
        self.mode: str | None = None  # "ink" | "erase" | "pan" while the pen is down
        self.pen_near = False
        self.last_pen = -1e9
        self._sm: QPointF | None = None
        self._p = 0.5
        self._raw: QPointF | None = None
        self._erased: list = []
        self._pan_at: QPointF | None = None
        canvas.viewport().setTabletTracking(True)  # hover moves too: they keep "pen near" fresh for palm rejection
        canvas.viewport().installEventFilter(self)

    # -- helpers
    def _scene_pos(self, pos: QPointF) -> QPointF:
        inv, _ok = self.cv.viewportTransform().inverted()  # float precision (mapToScene takes whole pixels)
        return inv.map(pos)

    def _mouse(self, etype, e, button):
        from PySide6.QtGui import QMouseEvent

        buttons = Qt.MouseButton.NoButton if etype == QEvent.Type.MouseButtonRelease else button
        return QMouseEvent(etype, e.position(), e.globalPosition(), button, buttons, e.modifiers(), e.pointingDevice())

    def _pen_event(self, e) -> None:
        self.last_pen = time.monotonic()

    # -- event filter
    WATCHED = frozenset({
        QEvent.Type.TabletEnterProximity, QEvent.Type.TabletLeaveProximity, QEvent.Type.TabletPress,
        QEvent.Type.TabletMove, QEvent.Type.TabletRelease, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
        QEvent.Type.TouchEnd, QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick})

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t not in self.WATCHED:  # paints, resizes, ...
            return False
        if t == QEvent.Type.TabletEnterProximity:
            self.pen_near = True
            return False
        if t == QEvent.Type.TabletLeaveProximity:
            self.pen_near = False
            return False
        if t in (QEvent.Type.TabletPress, QEvent.Type.TabletMove, QEvent.Type.TabletRelease):
            self._pen_event(e)
            try:
                return self.tablet(e)
            except RuntimeError:  # canvas being destroyed
                return False
        if t in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd):
            if palm_reject(e.device().type(), self.pen_near, time.monotonic() - self.last_pen):
                e.accept()
                return True
            return False
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease,
                 QEvent.Type.MouseButtonDblClick):
            dev = e.device()
            if dev is not None and palm_reject(dev.type(), self.pen_near, time.monotonic() - self.last_pen):
                return True
        return False

    def tablet(self, e) -> bool:
        """Returns True when the event was handled here (Qt then makes no mouse event of it)."""
        from PySide6.QtGui import QPointingDevice

        cv = self.cv
        t = e.type()
        if self.mode is not None:  # a stroke/erase/pan in progress gets everything until the pen lifts
            if t == QEvent.Type.TabletMove:
                getattr(self, f"_{self.mode}_move")(e)
            elif t == QEvent.Type.TabletRelease:
                getattr(self, f"_{self.mode}_end")(e)
                self.mode = None
            e.accept()
            return True
        if t != QEvent.Type.TabletPress or cv.doc is None or getattr(cv, "presenting", False):
            return False
        if e.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):  # barrel button: pan
            self.mode = "pan"
            self._pan_at = e.position()
            cv.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif e.pointerType() == QPointingDevice.PointerType.Eraser:
            self.mode = "erase"
            self._erased = []
            self._erase_move(e)
        elif cv.tool == "ink" and e.button() == Qt.MouseButton.LeftButton:
            if not self._ink_start(e):
                return False
            self.mode = "ink"
        else:
            return False
        e.accept()
        return True

    # -- ink
    def _ink_start(self, e) -> bool:
        from .items import InkItem

        cv = self.cv
        if getattr(cv, "_space", False) or cv.page_at(self._scene_pos(e.position())) is None:
            return False  # Space+drag pans, off the page nothing to draw: let Qt make mouse events of it
        cv.mousePressEvent(self._mouse(QEvent.Type.MouseButtonPress, e, Qt.MouseButton.LeftButton))
        cr = cv._create
        if cr is None or not isinstance(cr[1], InkItem):
            return False
        pg, item, _ = cr
        lp = self._local(pg, e.position())
        item.setPos(lp)  # sub-pixel start
        self._sm = QPointF(lp)
        self._raw = QPointF(lp)
        self._p = float(e.pressure())
        if self.pressure_enabled():
            item.widths = [self._p]
        return True

    def _local(self, pg, pos: QPointF) -> QPointF:
        lp = pg.content.mapFromScene(self._scene_pos(pos))
        r = pg.content.rect()
        return QPointF(min(max(lp.x(), r.left()), r.right()), min(max(lp.y(), r.top()), r.bottom()))

    def _ink_move(self, e) -> None:
        cr = self.cv._create
        if cr is None or self._sm is None:
            return
        pg, item, _ = cr
        raw = self._local(pg, e.position())
        self._raw = raw
        a = self.SMOOTH
        self._sm = QPointF(self._sm.x() + (raw.x() - self._sm.x()) * a, self._sm.y() + (raw.y() - self._sm.y()) * a)
        self._p += (float(e.pressure()) - self._p) * self.PSMOOTH
        item.add_point(self._sm - item.pos(), self._p if self.pressure_enabled() else None)

    def _ink_end(self, e) -> None:
        cr = self.cv._create
        if cr is not None and self._raw is not None:
            item = cr[1]
            item.add_point(self._raw - item.pos(), self._p if self.pressure_enabled() else None)  # end where the pen lifted
        self._sm = self._raw = None
        self.cv.mouseReleaseEvent(self._mouse(QEvent.Type.MouseButtonRelease, e, Qt.MouseButton.LeftButton))

    # -- eraser
    def _erase_move(self, e) -> None:
        from PySide6.QtCore import QRect

        from .items import InkItem

        p = e.position().toPoint()
        r = self.ERASER_PX
        for it in self.cv.items(QRect(p.x() - r, p.y() - r, 2 * r, 2 * r)):
            if isinstance(it, InkItem) and it.isVisible() and it not in self._erased:
                self._erased.append(it)
                it.setVisible(False)  # gone at once; one undo step when the pen lifts

    def _erase_end(self, e) -> None:
        from . import commands as C

        items, self._erased = self._erased, []
        for it in items:
            it.setVisible(True)
        if items:
            annots = getattr(self.cv, "annots", None)
            if annots is not None and hasattr(annots, "with_replies"):
                items = annots.with_replies(items)
            self.cv.undo.push(C.RemoveItems(items, "Erase"))

    # -- barrel button: pan
    def _pan_move(self, e) -> None:
        if self._pan_at is None:
            return
        d = e.position() - self._pan_at
        self._pan_at = e.position()
        hb, vb = self.cv.horizontalScrollBar(), self.cv.verticalScrollBar()
        hb.setValue(hb.value() - int(round(d.x())))
        vb.setValue(vb.value() - int(round(d.y())))

    def _pan_end(self, e) -> None:
        self._pan_at = None
        try:
            self.cv.viewport().setCursor(self.cv._tool_cursor())
        except Exception:  # noqa: BLE001
            pass
