"""Leaf's look: a Windows 11 ("Fluent") style layer over Qt's Fusion style, in light and dark, with the user's accent.

window.py calls two hooks: `prepare_app()` before it builds widgets (style, palette, font, style sheet, so sizes are
measured with the final font) and `install(win)` at the end of __init__ (panel headers, rounded page view, command
search box, status bar). Light/dark follows Settings › Appearance › Theme (or the system) and switches live.

Falls back to the platform's own look, untouched: in high-contrast mode (Windows high contrast, Qt's contrast
preference, LEAF_HIGH_CONTRAST=1; the theme's colors win) and with LEAF_UI=classic. Widgets that paint themselves
read `current()` either way: the style's tokens, or the same roles taken from the platform palette.

Modules: tokens (colors, radii, type), theme (app-wide: style, palette, style sheet, popups, fonts), chrome (per
window), dwm (Windows title bar and popup corners).
"""
from __future__ import annotations

import os

_active = False
_prepared = False


def enabled() -> bool:
    """Should the style layer run? (Not in high contrast, not with LEAF_UI=classic.)"""
    if os.environ.get("LEAF_UI", "").strip().lower() == "classic":
        return False
    from ..a11y import high_contrast

    return not high_contrast()


def active() -> bool:
    return _active


def current():
    """The active tokens, or the same roles from the platform palette when the style layer is off."""
    from . import theme

    if theme.T is not None:
        return theme.T
    from PySide6.QtWidgets import QApplication

    from .tokens import from_palette

    return from_palette(QApplication.palette())


def prepare_app() -> bool:
    """Once per process, before the first window: apply the style app-wide. Returns whether it is on."""
    global _active, _prepared
    if _prepared:
        return _active
    _prepared = True
    if not enabled():
        return False
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from .. import settings as S
    from . import theme

    app = QApplication.instance()
    S.apply_theme(str(QSettings().value(S.THEME, "system")))  # the saved light/dark choice first: one build, not two
    theme.apply(app)
    _active = True
    app.styleHints().colorSchemeChanged.connect(lambda *_: _reapply())
    return True


def _reapply() -> None:
    """Light/dark switch or a new accent: rebuild the app-wide style, then what each window copied from it."""
    from PySide6.QtWidgets import QApplication

    from ..window import _windows
    from . import chrome, theme

    if not _active:
        return
    theme.apply(QApplication.instance())
    for w in list(_windows):
        try:
            chrome.restyle(w)
        except RuntimeError:  # a window being deleted
            pass


def install(win) -> None:
    """Per-window chrome (no-op when the style layer is off)."""
    if prepare_app():
        from . import chrome

        chrome.install(win)
