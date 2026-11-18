"""Design tokens: color roles, radii, spacing and type for the light and dark themes.

Every surface the style layer paints (the app style sheet, the palette, custom-painted widgets) reads these.
Color roles:
  chrome   window / toolbar / status bar (the tint Windows 11 uses for Mica)
  layer    panels and cards that sit on the chrome
  layer2   inset areas inside a layer (list headers, the tool group's track)
  popup    menus, popups, the command palette
  canvas   the backdrop behind the pages
  stroke   hairline borders; stroke2 control borders
  text / text2 / text3   primary, secondary (WCAG AA on chrome, layer, popup and canvas), tertiary (placeholders and
                         disabled text only)
  accent / on_accent / accent_soft   selection, primary buttons, checked tools

`from_palette()` derives the same roles from a QPalette: high-contrast themes and LEAF_UI=classic keep the
platform's colors, and the widgets that paint themselves (start screen, Focus mode bars) still find every role.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtGui import QColor, QFontDatabase, QPalette


def rgba(c: QColor) -> str:
    """A color for a style sheet."""
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})" if c.alpha() < 255 else c.name()


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """`a` blended toward `b` by t (0..1), opaque."""
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(a)
    return c


@dataclass
class Tokens:
    dark: bool
    chrome: QColor
    layer: QColor
    layer2: QColor
    popup: QColor
    canvas: QColor
    stroke: QColor
    stroke2: QColor
    text: QColor
    text2: QColor
    text3: QColor
    accent: QColor
    on_accent: QColor
    # geometry (logical px)
    r_s: int = 4  # small controls (check boxes, key caps)
    r_m: int = 6  # buttons, inputs, tool buttons
    r_l: int = 8  # cards, popups, panels
    pad: int = 8  # base spacing unit (4-pt grid)
    tool_icon: int = 20
    icon_stroke_px: float = 1.5  # icon line width in logical px at every icon size
    # type
    font: str = ""
    font_display: str = ""
    pt: float = 9.5
    pt_display: float = 24.0
    platform: bool = False  # True: derived from the platform palette (high contrast, classic)

    # derived roles
    @property
    def accent_soft(self) -> QColor:
        return mix(self.layer, self.accent, 0.22 if self.dark else 0.14)

    @property
    def hover(self) -> QColor:
        return alpha(QColor(255, 255, 255) if self.dark else QColor(0, 0, 0), 0.07 if self.dark else 0.05)

    @property
    def pressed(self) -> QColor:
        return alpha(QColor(255, 255, 255) if self.dark else QColor(0, 0, 0), 0.045 if self.dark else 0.035)

    @property
    def control(self) -> QColor:
        return mix(self.layer, QColor(255, 255, 255), 0.04 if self.dark else 0.6)

    @property
    def control_hover(self) -> QColor:
        return mix(self.control, self.text, 0.05)

    @property
    def input(self) -> QColor:
        return mix(self.layer, QColor(0, 0, 0), 0.12) if self.dark else QColor(255, 255, 255)

    @property
    def scroll(self) -> QColor:
        return alpha(self.text, 0.28 if self.dark else 0.30)

    @property
    def accent_hover(self) -> QColor:
        return self.accent.darker(108) if self.dark else self.accent.lighter(110)


# ------------------------------------------------------------------------------------------------- fonts
def first_family(*names: str) -> str:
    fams = set(QFontDatabase.families())
    return next((n for n in names if n in fams), "")


def fonts() -> tuple[str, str]:
    """(body, display) families, best available. System fonts only: nothing is bundled."""
    text = first_family("Segoe UI Variable Text", "Inter", "Inter Variable", "Cantarell", "Noto Sans", "Ubuntu",
                        "DejaVu Sans", "Segoe UI")
    display = first_family("Segoe UI Variable Display", "Inter Display", "Inter", "Cantarell", "Noto Sans", "Segoe UI")
    return text, display or text


# ------------------------------------------------------------------------------------------------- accent
def system_accent(dark: bool, fallback: QColor) -> QColor:
    """Windows: the accent palette Windows 11 uses (Dark1 on light, Light2 on dark), so the shade reads on our
    surfaces. Elsewhere Qt's accent (KDE color scheme, the desktop portal on GNOME/Hyprland), else Fluent blue."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent") as k:
                data, _ = winreg.QueryValueEx(k, "AccentPalette")
            cols = [QColor(data[i * 4], data[i * 4 + 1], data[i * 4 + 2]) for i in range(len(data) // 4)]
            if len(cols) >= 6:
                return readable_accent(cols[1] if dark else cols[4], dark)
        except (OSError, TypeError, IndexError):
            pass
    if fallback is not None and fallback.isValid():
        return readable_accent(QColor(fallback), dark)
    return QColor("#60cdff" if dark else "#005fb8")


def readable_accent(c: QColor, dark: bool) -> QColor:
    """A platform accent, lightened (dark theme) or darkened (light theme) until it reads as text (links, flat
    buttons, checked icons): 4.5:1 on the surface it contrasts least with (dark: the popup; light: the chrome)."""
    from ..a11y import readable

    return readable(c, QColor("#2c2c2c") if dark else QColor("#eef1f5"))


def on(c: QColor) -> QColor:
    """Black or white text, whichever reads better on `c`."""
    from ..a11y import contrast_ratio

    black, white = QColor("#000000"), QColor("#ffffff")
    return black if contrast_ratio(c, black) >= contrast_ratio(c, white) else white


# ------------------------------------------------------------------------------------------------- build
def build(dark: bool, platform_accent: QColor | None = None) -> Tokens:
    acc = system_accent(dark, platform_accent)
    C = QColor
    if dark:
        t = Tokens(dark, chrome=C("#202020"), layer=C("#2b2b2b"), layer2=C("#262626"), popup=C("#2c2c2c"),
                   canvas=C("#191919"), stroke=C("#363636"), stroke2=C("#454545"), text=C("#ffffff"),
                   text2=C("#cfcfcf"), text3=C("#8f8f8f"), accent=acc, on_accent=on(acc))
    else:
        t = Tokens(dark, chrome=C("#eef1f5"), layer=C("#fbfbfc"), layer2=C("#f3f5f8"), popup=C("#fcfcfc"),
                   canvas=C("#e1e5eb"), stroke=C("#e0e3e8"), stroke2=C("#cdd2d9"), text=C("#1a1a1a"),
                   text2=C("#5b5f66"), text3=C("#8b9097"), accent=acc, on_accent=on(acc))
    t.font, t.font_display = fonts()
    return t


def from_palette(pal: QPalette) -> Tokens:
    """The roles from a platform palette (high contrast, classic): every text role is the palette's own text."""
    P = QPalette.ColorRole
    win, base, text = pal.color(P.Window), pal.color(P.Base), pal.color(P.WindowText)
    dark = win.lightness() < 128
    acc = pal.color(P.Highlight)
    return Tokens(dark, chrome=win, layer=base, layer2=pal.color(P.AlternateBase), popup=base,
                  canvas=win, stroke=pal.color(P.Mid), stroke2=text, text=text, text2=text,
                  text3=pal.color(QPalette.ColorGroup.Disabled, P.WindowText), accent=acc,
                  on_accent=pal.color(P.HighlightedText), font="", font_display="", pt=0.0, platform=True)
