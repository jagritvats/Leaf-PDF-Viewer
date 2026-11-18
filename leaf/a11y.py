"""Accessibility: accessible names for every control, page announcements, focus order, high contrast, contrast.

- `install(win)` (called once per window from features/compare.py so no shared file has to): an application-wide
  event filter names every interactive widget as it is polished (dialogs included) when it has no accessible name
  of its own: from its tooltip (shortcut hint stripped), placeholder text or its QFormLayout label; dock contents
  are named after the dock. The canvas gets "Page N of M" as its accessible name (screen readers announce the
  change) and the page's text as its description; an announcement event is sent too. The main window's Tab order
  becomes toolbar → Pages panel → page → panels on the right → bottom panels.
- `high_contrast()`: Windows high-contrast themes (SPI_GETHIGHCONTRAST), Qt's contrast preference (6.10+), or
  LEAF_HIGH_CONTRAST=1. Our own painted surfaces (toasts, mini toolbar, tool strip) switch to plain palette colors.
- `contrast_ratio()` / `readable()`: WCAG 2 contrast for the few colors we pick ourselves (toasts, error labels).
"""
from __future__ import annotations

import os
import re
import sys
from collections import deque

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAccessible, QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QApplication, QComboBox, QDockWidget,
    QFormLayout, QGraphicsView, QLabel, QLineEdit, QPlainTextEdit, QTextEdit, QToolBar, QWidget,
)

INTERACTIVE = (QAbstractButton, QComboBox, QAbstractSlider, QAbstractSpinBox, QLineEdit, QAbstractItemView,
               QGraphicsView, QTextEdit, QPlainTextEdit)
PAGE_TEXT_LIMIT = 4000  # characters of the current page's text exposed as the canvas description
announcements: deque[str] = deque(maxlen=50)  # last messages announced (tests, debugging)
_filter = None

NAMED_COLORS = {
    "black": "#000000", "dark gray": "#555555", "gray": "#868e96", "light gray": "#d0d0d0", "white": "#ffffff",
    "red": "#d43a2a", "orange": "#e8862a", "yellow": "#ffd83d", "green": "#2f9e44", "light green": "#8ce99a",
    "blue": "#1f4fd1", "light blue": "#74c0fc", "purple": "#7b3fd1", "pink": "#ffa8d8", "brown": "#8b5a2b",
    "cream": "#fff3bf", "pale green": "#d3f9d8", "pale blue": "#d0ebff", "pale red": "#ffe3e3",
}


# ------------------------------------------------------------------------------------------------- colors
def color_name(color: str) -> str:
    """A human name for a color (nearest of a small list), for screen readers."""
    c = QColor(color)
    if not c.isValid():
        return color
    best, dist = color, 1e9
    for name, hexv in NAMED_COLORS.items():
        q = QColor(hexv)
        d = (c.red() - q.red()) ** 2 * 0.3 + (c.green() - q.green()) ** 2 * 0.59 + (c.blue() - q.blue()) ** 2 * 0.11
        if d < dist:
            best, dist = name, d
    return best


def _lum(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def contrast_ratio(a, b) -> float:
    """WCAG 2 contrast ratio of two colors (1..21)."""
    la, lb = _lum(QColor(a)), _lum(QColor(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def readable(fg, bg, minimum: float = 4.5) -> QColor:
    """`fg`, lightened or darkened (keeping its hue) until it reaches `minimum` contrast on `bg`."""
    c, back = QColor(fg), QColor(bg)
    dark_bg = _lum(back) < 0.18
    for _ in range(40):
        if contrast_ratio(c, back) >= minimum:
            return c
        h, s, l, a = c.getHslF()
        l = min(1.0, l + 0.04) if dark_bg else max(0.0, l - 0.04)
        c = QColor.fromHslF(max(h, 0.0), s, l, a)
    return QColor("#ffffff") if dark_bg else QColor("#000000")


def error_color(widget: QWidget | None = None) -> str:
    """A red that is readable on the widget's window background (light, dark and high-contrast themes)."""
    pal = widget.palette() if widget is not None else QApplication.palette()
    return readable("#c0392b", pal.color(QPalette.ColorRole.Window)).name()


def high_contrast() -> bool:
    env = os.environ.get("LEAF_HIGH_CONTRAST")
    if env is not None:
        return env not in ("", "0")
    try:
        hints = QGuiApplication.styleHints().accessibility()
        if hints.contrastPreference() == Qt.ContrastPreference.HighContrast:
            return True
    except Exception:  # noqa: BLE001  (Qt < 6.10)
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            class HIGHCONTRAST(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint), ("lpszDefaultScheme", ctypes.c_wchar_p)]

            hc = HIGHCONTRAST()
            hc.cbSize = ctypes.sizeof(HIGHCONTRAST)
            if ctypes.windll.user32.SystemParametersInfoW(0x0042, hc.cbSize, ctypes.byref(hc), 0):  # SPI_GETHIGHCONTRAST
                return bool(hc.dwFlags & 1)  # HCF_HIGHCONTRASTON
        except Exception:  # noqa: BLE001
            pass
    return False


# -------------------------------------------------------------------------------------------------- names
def clean_tip(tip: str) -> str:
    """Tooltip text usable as a name: first line, no rich text, no trailing "(shortcut)" hint."""
    t = re.sub(r"<[^>]+>", "", tip or "").strip().split("\n", 1)[0]
    t = re.sub(r"\s*\([^()]*\)\s*$", "", t).strip()
    return t.replace("&&", "\0").replace("&", "").replace("\0", "&").rstrip("…").rstrip(".").strip()


def name_of(w: QWidget) -> str:
    """What a screen reader would call the widget (empty when it has no name)."""
    iface = QAccessible.queryAccessibleInterface(w)
    return iface.text(QAccessible.Text.Name) if iface is not None and iface.isValid() else w.accessibleName()


def _form_label(w: QWidget) -> str:
    cur = w
    for _ in range(3):
        parent = cur.parentWidget()
        if parent is None:
            return ""
        lay = parent.layout()
        if isinstance(lay, QFormLayout):
            lbl = lay.labelForField(cur)
            if isinstance(lbl, QLabel) and lbl.text().strip():
                return clean_tip(lbl.text())
        cur = parent
    return ""


def derive_name(w: QWidget) -> str:
    tip = clean_tip(w.toolTip())
    if tip:
        return tip
    ph = getattr(w, "placeholderText", None)
    if callable(ph) and ph():
        return clean_tip(ph())
    if isinstance(w, QComboBox) and w.isEditable() and w.lineEdit() and w.lineEdit().placeholderText():
        return clean_tip(w.lineEdit().placeholderText())
    label = _form_label(w)
    if label:
        return label
    parent = w.parentWidget()
    if isinstance(parent, QDockWidget) and parent.widget() is w and parent.windowTitle():
        return f"{clean_tip(parent.windowTitle())} panel"
    return ""


def ensure_name(w: QWidget) -> None:
    if w.accessibleName():
        return
    if isinstance(w, INTERACTIVE) or (isinstance(w.parentWidget(), QDockWidget) and w.parentWidget().widget() is w):
        if name_of(w):
            return
        n = derive_name(w)
        if n:
            w.setAccessibleName(n)
            full = re.sub(r"<[^>]+>", "", w.toolTip() or "").strip()
            if full and full != n and not w.accessibleDescription():
                w.setAccessibleDescription(full)


def name_all(root: QWidget) -> None:
    for w in [root, *root.findChildren(QWidget)]:
        ensure_name(w)


def unnamed(root: QWidget) -> list[QWidget]:
    """Visible interactive widgets a screen reader has no name for (tests, audits)."""
    out = []
    for w in root.findChildren(QWidget):
        if isinstance(w, INTERACTIVE) and w.isVisibleTo(root) and w.focusPolicy() != Qt.FocusPolicy.NoFocus or (
                isinstance(w, QAbstractButton) and w.isVisibleTo(root)):
            if isinstance(w.parentWidget(), (QComboBox, QAbstractSpinBox)):  # their own line edits
                continue
            if not name_of(w).strip():
                out.append(w)
    return out


class _Namer(QObject):
    """App-wide: names widgets as they are polished, and re-applies high-contrast styling on theme changes."""

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.Type.Polish and isinstance(obj, QWidget):
            ensure_name(obj)
        return False


# ---------------------------------------------------------------------------------------------- announce
def announce(widget: QWidget, text: str) -> None:
    """Tell assistive technology about `text` (a page change, the current difference, ...)."""
    announcements.append(text)
    try:
        from PySide6.QtGui import QAccessibleAnnouncementEvent

        ev = QAccessibleAnnouncementEvent(widget, text)
        QAccessible.updateAccessibility(ev)
    except Exception:  # noqa: BLE001  (Qt < 6.8)
        pass


def _page_announcer(win) -> None:
    cv = win.canvas
    cv.setAccessibleName("Page view")
    cv.setAccessibleDescription("No document open")
    timer = QTimer(cv, singleShot=True, interval=350)

    def update() -> None:
        doc = cv.doc
        if doc is None or not cv.order:
            cv.setAccessibleName("Page view")
            cv.setAccessibleDescription("No document open")
            return
        i = max(0, min(cv.current_index(), len(cv.order) - 1))
        name = f"Page {i + 1} of {len(cv.order)}"
        if cv.accessibleName() == name and getattr(cv, "_a11y_uid", None) == cv.order[i].ref.uid:
            return
        cv._a11y_uid = cv.order[i].ref.uid
        try:
            text = doc.text_page(cv.order[i].ref).get_text().strip()
        except Exception:  # noqa: BLE001
            text = ""
        text = " ".join(text.split())
        if len(text) > PAGE_TEXT_LIMIT:
            text = text[:PAGE_TEXT_LIMIT] + "…"
        cv.setAccessibleName(name)  # fires QAccessible::NameChanged
        cv.setAccessibleDescription(text or "This page has no text")
        announce(cv, name)

    timer.timeout.connect(update)
    cv.pageChanged.connect(lambda _i: timer.start())
    cv.pagesChanged.connect(timer.start)
    cv.documentChanged.connect(timer.start)
    cv._a11y_update = update  # tests: run now


# ------------------------------------------------------------------------------------------- focus order
def focus_segments(win) -> list[QWidget]:
    """Roots of the main window's Tab order: toolbars → Pages panel (and other left panels) → find bar → page →
    right panels → bottom panels. Everything else (status bar, dock tab bars) follows."""
    def docks(area):
        ds = [d for d in win.findChildren(QDockWidget) if win.dockWidgetArea(d) == area and d.widget() is not None]
        ds.sort(key=lambda d: (d.geometry().top(), d.geometry().left()))
        return [d.widget() for d in ds]

    roots: list[QWidget] = list(win.findChildren(QToolBar))
    left = docks(Qt.DockWidgetArea.LeftDockWidgetArea)
    side = getattr(win, "sidebar", None)
    if side is not None and side in left:
        left.remove(side)
        left.insert(0, side)
    roots += left
    find = getattr(win, "find", None)
    if find is not None:
        roots.append(find)
    roots.append(win.canvas)
    roots += docks(Qt.DockWidgetArea.RightDockWidgetArea) + docks(Qt.DockWidgetArea.BottomDockWidgetArea)
    return roots


def _tabbable(w: QWidget, win) -> bool:
    if not (w.focusPolicy() & Qt.FocusPolicy.TabFocus) == Qt.FocusPolicy.TabFocus or w.window() is not win:
        return False
    p = w.parentWidget()
    return not (p is not None and (p.focusProxy() is w or isinstance(p, (QComboBox, QAbstractSpinBox))))


def focus_chain(win) -> list[QWidget]:
    """Every widget that can take Tab focus, in the intended order (hidden ones too, so they land in the right
    place when they appear)."""
    order, w = [], win
    for _ in range(20000):  # the existing chain order is kept inside each segment
        w = w.nextInFocusChain()
        if w is win or w is None:
            break
        if _tabbable(w, win):
            order.append(w)
    roots = focus_segments(win)
    buckets: list[list[QWidget]] = [[] for _ in range(len(roots) + 1)]
    for x in order:
        k = next((i for i, r in enumerate(roots) if r is x or r.isAncestorOf(x)), len(roots))
        buckets[k].append(x)
    return [x for b in buckets for x in b]


def fix_focus_order(win) -> None:
    chain = focus_chain(win)
    for a, b in zip(chain, chain[1:]):
        try:
            QWidget.setTabOrder(a, b)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------- install
def install_filter() -> None:
    """The app-wide naming filter (once per process)."""
    global _filter
    app = QApplication.instance()
    if _filter is None and app is not None:
        _filter = _Namer(app)
        app.installEventFilter(_filter)


install_filter_for_tests = install_filter


def install(win) -> None:
    install_filter()
    win.toolbar.setAccessibleName("Main toolbar")
    win.statusBar().setAccessibleName("Status bar")
    for w, n in ((win.page_spin, "Page number"), (win.zoom_slider, "Zoom"), (win.zoom_box, "Zoom level"),
                 (win.page_total, "Page count"), (win.page_size, "Page size")):
        if not w.accessibleName():
            w.setAccessibleName(n)
    if getattr(win, "find", None) is not None:
        win.find.setAccessibleName("Find bar")
        win.find.edit.setAccessibleName("Find in document")
        win.find.prev.setAccessibleName("Previous match")
        win.find.next.setAccessibleName("Next match")
        win.find.count.setAccessibleName("Matches")
    _page_announcer(win)
    QTimer.singleShot(0, lambda: (name_all(win), fix_focus_order(win)))
    # panels created later (Comments, Outline, ...) are slotted in when a document opens
    win.canvas.documentChanged.connect(lambda: QTimer.singleShot(0, lambda: fix_focus_order(win)))
