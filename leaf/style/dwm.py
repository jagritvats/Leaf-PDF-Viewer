"""Windows 11 window attributes through DwmSetWindowAttribute. Every call is a no-op elsewhere (Linux, macOS,
offscreen) and on Windows versions that don't know the attribute (Windows 10 ignores the caption color).

- caption(): the native title bar in the chrome color with matching text, so it reads as one surface with the menu
  bar and toolbar (the frame itself stays native: snap layouts, resize edges, accessibility untouched).
- round_corners(): DWMWCP_ROUND / ROUNDSMALL for menus and popups (native rounding, border and shadow).
"""
from __future__ import annotations

import sys

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def _set(widget, attr: int, value: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.platformName() != "windows":  # offscreen / minimal: no real window handle
            return False
        hwnd = wintypes.HWND(int(widget.winId()))
        v = ctypes.c_int(value)
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0
    except Exception:  # noqa: BLE001 - cosmetic only (no DWM, older Windows, ...)
        return False


def _colorref(c) -> int:
    return c.red() | (c.green() << 8) | (c.blue() << 16)


def caption(widget, bg, fg) -> None:
    _set(widget, DWMWA_CAPTION_COLOR, _colorref(bg))
    _set(widget, DWMWA_TEXT_COLOR, _colorref(fg))


def round_corners(widget, small: bool = False) -> None:
    _set(widget, DWMWA_WINDOW_CORNER_PREFERENCE, 3 if small else 2)
