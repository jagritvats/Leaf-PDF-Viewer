"""App-wide part of the style: Fusion base style, palette, font, one generated style sheet, its image assets,
icon refinements and rounded popups. Applied once per process before the first window, again on a light/dark switch
or an accent change. Nothing here touches the page canvas' painting except its backdrop color."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QMenu, QProxyStyle, QStyle, QWidget

from .tokens import Tokens, alpha, build, mix, rgba

T: Tokens | None = None  # the active tokens (None: the platform's look)
_platform_accent: QColor | None = None
_platform_dark: bool | None = None
_filters: list[QObject] = []


def is_dark(app) -> bool:
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    return bool(_platform_dark)  # the platform doesn't say (some Linux desktops): its palette did, at startup


def translucent_popups() -> bool:
    """Rounded menus through a translucent popup need a compositor: always there on Wayland. X11 may run without
    one (black corners), so menus stay square there; Windows 11 rounds popups itself (DWM)."""
    return sys.platform.startswith("linux") and QGuiApplication.platformName().startswith("wayland")


# ------------------------------------------------------------------------------------------------- palette
def palette(t: Tokens) -> QPalette:
    P, G = QPalette.ColorRole, QPalette.ColorGroup
    pal = QPalette()
    roles = {
        P.Window: t.chrome, P.WindowText: t.text, P.Base: t.layer, P.AlternateBase: t.layer2, P.Text: t.text,
        P.Button: t.control, P.ButtonText: t.text, P.BrightText: QColor("#ffffff"), P.ToolTipBase: t.popup,
        P.ToolTipText: t.text, P.PlaceholderText: t.text2, P.Highlight: t.accent, P.HighlightedText: t.on_accent,
        P.Link: t.accent, P.LinkVisited: t.accent, P.Accent: t.accent, P.Light: mix(t.control, QColor("#ffffff"), 0.4),
        P.Midlight: t.stroke2, P.Mid: t.stroke2, P.Dark: mix(t.stroke2, QColor("#000000"), 0.3),
        P.Shadow: QColor(0, 0, 0, 120),
    }
    for role, c in roles.items():
        pal.setColor(role, c)
    for role in (P.WindowText, P.Text, P.ButtonText):
        pal.setColor(G.Disabled, role, t.text3)
    pal.setColor(G.Disabled, P.Highlight, t.stroke2)
    pal.setColor(G.Inactive, P.Highlight, t.accent)
    pal.setColor(G.Inactive, P.HighlightedText, t.on_accent)
    return pal


# ------------------------------------------------------------------------------------------------- assets
ASSETS = ("chevron_down", "chevron_up", "chevron_right", "chevron_down_muted", "chevron_right_muted", "check_off",
          "check_on", "check_mixed", "radio_off", "radio_on", "close", "float")


def _asset_dir(t: Tokens) -> str:
    key = hashlib.sha1(f"{t.dark}{t.accent.name()}{t.on_accent.name()}{t.text.name()}{t.text2.name()}{t.input.name()}"
                       "v4".encode()).hexdigest()[:10]
    if sys.platform == "win32":  # %TEMP% is per user here
        d = os.path.join(tempfile.gettempdir(), "leaf-style", key)
    else:  # /tmp is shared by every user, who could plant images there or squat the folder: the user's cache
        cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        d = os.path.join(cache, "leaf", "style", key)
    os.makedirs(d, exist_ok=True)
    return d


def _paint_asset(name: str, t: Tokens, p: QPainter, s: float) -> None:
    p.scale(s, s)  # 16x16 logical grid
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    def pen(c, w=1.4) -> QPen:
        return QPen(c, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    if name.startswith("chevron"):
        p.setPen(pen(t.text2 if "muted" in name else t.text, 1.3))
        pts = {"chevron_down": ((4.5, 6.5), (8, 10), (11.5, 6.5)), "chevron_up": ((4.5, 9.5), (8, 6), (11.5, 9.5)),
               "chevron_right": ((6.5, 4.5), (10, 8), (6.5, 11.5))}[name.replace("_muted", "")]
        for a, b in zip(pts, pts[1:]):
            p.drawLine(QPointF(*a), QPointF(*b))
    elif name.startswith("check"):
        on = name in ("check_on", "check_mixed")
        p.setPen(Qt.PenStyle.NoPen if on else pen(t.text2, 1.0))
        p.setBrush(t.accent if on else (t.input if t.dark else QColor(255, 255, 255, 0)))
        p.drawRoundedRect(QRectF(1.5, 1.5, 13, 13), t.r_s * 0.8, t.r_s * 0.8)
        if name == "check_on":
            p.setPen(pen(t.on_accent, 1.6))
            p.drawPolyline([QPointF(4.6, 8.2), QPointF(7, 10.6), QPointF(11.4, 5.6)])
        elif name == "check_mixed":
            p.setPen(pen(t.on_accent, 1.6))
            p.drawLine(QPointF(5, 8), QPointF(11, 8))
    elif name.startswith("radio"):
        if name == "radio_on":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.accent)
            p.drawEllipse(QRectF(1.5, 1.5, 13, 13))
            p.setBrush(t.on_accent)
            p.drawEllipse(QRectF(5, 5, 6, 6))
        else:
            p.setPen(pen(t.text2, 1.0))
            p.setBrush(QColor(255, 255, 255, 0))
            p.drawEllipse(QRectF(1.5, 1.5, 13, 13))
    elif name == "close":
        p.setPen(pen(t.text2, 1.3))
        p.drawLine(QPointF(4.5, 4.5), QPointF(11.5, 11.5))
        p.drawLine(QPointF(11.5, 4.5), QPointF(4.5, 11.5))
    elif name == "float":
        p.setPen(pen(t.text2, 1.2))
        p.drawRoundedRect(QRectF(3.5, 5.5, 8, 7), 1, 1)
        p.drawPolyline(QPolygonF([QPointF(6, 3.5), QPointF(12.5, 3.5), QPointF(12.5, 9.5)]))


def make_assets(t: Tokens) -> dict[str, str]:
    """The style sheet's images (chevrons, check marks...) at 1x/2x/3x, cached on disk per color set."""
    d = _asset_dir(t)
    out = {}
    for name in ASSETS:
        for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
            path = os.path.join(d, f"{name}{suffix}.png")
            if not os.path.exists(path):
                pm = QPixmap(16 * scale, 16 * scale)
                pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(pm)
                _paint_asset(name, t, p, scale)
                p.end()
                pm.save(path, "PNG")
        out[name] = os.path.join(d, f"{name}.png").replace("\\", "/")
    return out


# ------------------------------------------------------------------------------------------------- style sheet
def stylesheet(t: Tokens, a: dict[str, str]) -> str:
    c = {k: rgba(getattr(t, k)) for k in ("chrome", "layer", "layer2", "popup", "canvas", "stroke", "stroke2", "text",
                                           "text2", "text3", "accent", "on_accent", "accent_soft", "hover", "pressed",
                                           "control", "control_hover", "input", "scroll", "accent_hover")}
    rs, rm, rl = t.r_s, t.r_m, t.r_l
    menu_r = rl if translucent_popups() else 0  # Windows 11: DWM rounds the popup (with its native border and shadow)
    hover_border = rgba(mix(t.stroke2, t.text, 0.2))
    return f"""
QMainWindow, QDialog {{ background: {c['chrome']}; }}
QMainWindow::separator {{ background: {c['chrome']}; width: 6px; height: 6px; }}
QMainWindow::separator:hover {{ background: {c['accent']}; }}
QToolTip {{ background: {c['popup']}; color: {c['text']}; border: 1px solid {c['stroke2']}; padding: 4px 8px; border-radius: {rs}px; }}

/* menus */
QMenuBar {{ background: {c['chrome']}; border: none; padding: 2px 6px 0 6px; spacing: 1px; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: {rm}px; color: {c['text']}; }}
QMenuBar::item:selected {{ background: {c['hover']}; }}
QMenuBar::item:pressed {{ background: {c['pressed']}; }}
QMenu {{ background: {c['popup']}; border: 1px solid {c['stroke2']}; border-radius: {menu_r}px; padding: 4px; }}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: {rs}px; background: transparent; color: {c['text']}; }}
QMenu::item:selected {{ background: {c['hover']}; }}
QMenu::item:disabled {{ color: {c['text3']}; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::separator {{ height: 1px; background: {c['stroke']}; margin: 4px 6px; }}
QMenu::section {{ color: {c['text2']}; padding: 6px 12px 2px; font-weight: 600; }}
QMenu::right-arrow {{ image: url({a['chevron_right_muted']}); width: 12px; height: 12px; padding-right: 6px; }}
QMenu::indicator {{ width: 14px; height: 14px; padding-left: 6px; }}
QMenu::indicator:non-exclusive:checked, QMenu::indicator:exclusive:checked {{ image: url({a['check_on']}); }}

/* toolbars and tool buttons */
QToolBar {{ background: {c['chrome']}; border: none; padding: 0 6px 2px 6px; spacing: 2px; }}
QToolBar::separator {{ background: {c['stroke2']}; width: 1px; height: 1px; margin: 9px 6px; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: {rm}px; padding: 4px; color: {c['text']}; }}
QToolButton:hover {{ background: {c['hover']}; }}
QToolButton:pressed {{ background: {c['pressed']}; }}
QToolButton:checked {{ background: {c['accent_soft']}; }}
QToolButton:disabled {{ color: {c['text3']}; }}
QToolButton[popupMode="1"] {{ padding-right: 16px; }}
QToolButton::menu-button {{ border: none; width: 14px; border-top-right-radius: {rm}px; border-bottom-right-radius: {rm}px; }}
QToolButton::menu-button:hover {{ background: {c['hover']}; }}
QToolButton::menu-arrow {{ image: url({a['chevron_down_muted']}); width: 10px; height: 10px; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}

/* buttons */
QPushButton {{ background: {c['control']}; color: {c['text']}; border: 1px solid {c['stroke2']}; border-radius: {rm}px;
              padding: 5px 14px; min-height: 20px; }}
QPushButton:hover {{ background: {c['control_hover']}; }}
QPushButton:pressed {{ background: {c['layer2']}; color: {c['text2']}; }}
QPushButton:disabled {{ color: {c['text3']}; background: {c['layer2']}; border-color: {c['stroke']}; }}
QPushButton:focus {{ border-color: {c['accent']}; }}
QPushButton:default, QPushButton#primary {{ background: {c['accent']}; color: {c['on_accent']}; border: 1px solid {c['accent']}; }}
QPushButton#primary {{ font-weight: 600; }} /* not for :default: a font in a state is drawn but not measured (clipped) */
QPushButton:default:hover, QPushButton#primary:hover {{ background: {c['accent_hover']}; border-color: {c['accent_hover']}; }}
QPushButton:default:disabled {{ background: {c['stroke2']}; border-color: {c['stroke2']}; color: {c['text3']}; }}
QPushButton:flat {{ background: transparent; border: 1px solid transparent; color: {c['accent']}; }}
QPushButton:flat:hover {{ background: {c['hover']}; }}
QPushButton::menu-indicator {{ image: url({a['chevron_down_muted']}); subcontrol-position: right center; right: 6px; width: 10px; }}

/* inputs */
QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QDateTimeEdit {{
    background: {c['input']}; color: {c['text']}; border: 1px solid {c['stroke2']}; border-radius: {rm}px;
    padding: 4px 8px; selection-background-color: {c['accent']}; selection-color: {c['on_accent']}; }}
QLineEdit, QAbstractSpinBox, QComboBox {{ min-height: 20px; }}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{ border-color: {hover_border}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {c['accent']}; border-bottom: 2px solid {c['accent']}; padding-bottom: 3px; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ color: {c['text3']}; background: {c['layer2']}; border-color: {c['stroke']}; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; width: 22px; subcontrol-origin: padding; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url({a['chevron_down_muted']}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {c['popup']}; border: 1px solid {c['stroke2']}; padding: 4px; outline: 0; }}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 0 8px; border-radius: {rs}px; }}
QAbstractSpinBox {{ padding-right: 18px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ border: none; width: 16px; subcontrol-origin: border; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; margin: 2px 2px 0 0; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 2px 2px 0; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {c['hover']}; border-radius: {rs}px; }}
QAbstractSpinBox::up-arrow {{ image: url({a['chevron_up']}); width: 9px; height: 9px; }}
QAbstractSpinBox::down-arrow {{ image: url({a['chevron_down']}); width: 9px; height: 9px; }}
QCheckBox, QRadioButton {{ spacing: 8px; color: {c['text']}; }}
QCheckBox::indicator, QRadioButton::indicator, QTreeView::indicator, QListView::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator:unchecked, QTreeView::indicator:unchecked, QListView::indicator:unchecked {{ image: url({a['check_off']}); }}
QCheckBox::indicator:checked, QTreeView::indicator:checked, QListView::indicator:checked {{ image: url({a['check_on']}); }}
QCheckBox::indicator:indeterminate, QTreeView::indicator:indeterminate {{ image: url({a['check_mixed']}); }}
QRadioButton::indicator:unchecked {{ image: url({a['radio_off']}); }}
QRadioButton::indicator:checked {{ image: url({a['radio_on']}); }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c['text3']}; }}

QSlider::groove:horizontal {{ height: 4px; background: {c['stroke2']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {c['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 10px; height: 10px; margin: -6px 0; border-radius: 8px; background: {c['accent']};
                             border: 3px solid {c['popup']}; }}
QSlider::handle:horizontal:hover {{ background: {c['accent_hover']}; }}
QProgressBar {{ background: {c['stroke']}; border: none; border-radius: 3px; max-height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 3px; }}

/* scroll bars: thin, no arrows */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px 1px 2px 1px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 1px 2px 1px 2px; }}
QScrollBar::handle:vertical {{ background: {c['scroll']}; border-radius: 3px; min-height: 36px; margin: 0 3px; }}
QScrollBar::handle:horizontal {{ background: {c['scroll']}; border-radius: 3px; min-width: 36px; margin: 3px 0; }}
QScrollBar::handle:hover {{ background: {rgba(alpha(t.text, 0.5))}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QAbstractScrollArea::corner {{ background: transparent; border: none; }}

/* item views */
QAbstractItemView {{ border: none; outline: 0; }}
QTreeView::item, QListView::item, QTableView::item {{ padding: 4px 6px; border-radius: {rs}px; }}
QTreeView::item:hover, QListView::item:hover {{ background: {c['hover']}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{ background: {c['accent_soft']}; color: {c['text']}; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: {c['accent_soft']}; }}
QTreeView::branch:has-children:closed {{ image: url({a['chevron_right_muted']}); }}
QTreeView::branch:has-children:open {{ image: url({a['chevron_down_muted']}); }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{ background: {c['layer']}; color: {c['text2']}; border: none; border-bottom: 1px solid {c['stroke']};
                       padding: 5px 8px; font-weight: 600; }}
QTableCornerButton::section {{ background: {c['layer']}; border: none; }}

/* tabs: panel groups (pivots) and dialogs */
QTabWidget::pane {{ border: 1px solid {c['stroke']}; border-radius: {rl}px; background: {c['layer']}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{ background: transparent; color: {c['text2']}; border: none; padding: 7px 12px; margin: 0 2px;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QTabBar::tab:selected {{ border-bottom: 2px solid {c['accent']}; color: {c['text']}; }}
QMainWindow > QTabBar {{ background: {c['layer']}; border-bottom: 1px solid {c['stroke']}; }}

/* panels */
QDockWidget {{ color: {c['text']}; titlebar-close-icon: url({a['close']}); titlebar-normal-icon: url({a['float']}); }}
QDockWidget::title {{ background: {c['layer']}; padding: 8px 10px; text-align: left; }}
QWidget#dockTitle {{ background: {c['layer']}; border-bottom: 1px solid {c['stroke']}; }}
QWidget#dockTitle QLabel {{ color: {c['text']}; font-weight: 600; }}
QWidget#dockTitle QToolButton {{ padding: 2px; border-radius: {rs}px; }}

/* group boxes */
QGroupBox {{ background: {c['layer']}; border: 1px solid {c['stroke']}; border-radius: {rl}px; margin-top: 16px; padding: 12px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 2px; color: {c['text2']}; font-weight: 600; }}

/* status bar */
QStatusBar {{ background: {c['chrome']}; border-top: 1px solid {c['stroke']}; min-height: 30px; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {c['text2']}; }}
QStatusBar QAbstractSpinBox, QStatusBar QComboBox {{ min-height: 18px; padding-top: 1px; padding-bottom: 1px; }}
QStatusBar QComboBox {{ background: transparent; border-color: transparent; }}
QStatusBar QComboBox:hover {{ background: {c['hover']}; }}
QStatusBar QToolButton {{ padding: 2px; }}
"""


# ------------------------------------------------------------------------------------------------- base style
class Style(QProxyStyle):
    """Fusion with platform manners: mnemonic underlines only while Alt is held (like Windows), a quicker tooltip,
    submenus that open without a long delay, and icon sizes from the tokens."""

    alt_down = False

    def styleHint(self, hint, opt=None, widget=None, ret=None):
        H = QStyle.StyleHint
        if hint == H.SH_UnderlineShortcut:
            return 1 if Style.alt_down else 0
        if hint == H.SH_ToolTip_WakeUpDelay:
            return 450
        if hint == H.SH_Menu_SubMenuPopupDelay:
            return 120
        if hint == H.SH_DialogButtonBox_ButtonsHaveIcons:
            return 0
        if hint == H.SH_MenuBar_AltKeyNavigation and sys.platform == "win32":
            return 1  # Alt alone moves into the menu bar, like every Windows app (Fusion doesn't)
        return super().styleHint(hint, opt, widget, ret)

    def pixelMetric(self, metric, opt=None, widget=None):
        M = QStyle.PixelMetric
        if T is not None:
            if metric == M.PM_SmallIconSize:
                return 16
            if metric == M.PM_ToolBarIconSize:
                return T.tool_icon
            if metric == M.PM_DockWidgetSeparatorExtent:
                return 6
        return super().pixelMetric(metric, opt, widget)


class _AppFilter(QObject):
    """Alt held: show mnemonic underlines. Theme change: pin the fonts again (the platform theme resets per-class
    fonts) and pick up a new accent color."""

    _pending = False

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and e.key() == Qt.Key.Key_Alt:
            down = t == QEvent.Type.KeyPress
            if down != Style.alt_down:
                Style.alt_down = down
                for w in QApplication.topLevelWidgets():
                    mb = getattr(w, "menuBar", None)
                    if callable(mb) and w.isVisible():
                        try:
                            mb().update()
                        except RuntimeError:
                            pass
        elif t == QEvent.Type.ApplicationDeactivate:
            Style.alt_down = False
        elif t == QEvent.Type.ThemeChange and not self._pending:
            self._pending = True  # once per burst (every widget receives the event)
            QTimer.singleShot(0, self._theme_changed)
        return False

    def _theme_changed(self) -> None:
        self._pending = False
        app = QApplication.instance()
        if T is None or app is None:
            return
        pin_fonts(app)
        if build(T.dark, _platform_accent).accent.rgba() != T.accent.rgba():  # the user picked another accent
            from . import _reapply

            _reapply()


class _PopupFilter(QObject):
    """Rounded menus and popups (Windows 11: DWM rounds them and keeps the native shadow and border; Wayland: a
    translucent popup lets the style sheet's radius show), and editable button-less spin boxes."""

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.Type.Polish:
            if isinstance(obj, QAbstractSpinBox) and obj.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons:
                # style sheets collapse the edit field of a button-less spin box to 1 px once ::up-button is
                # styled; zero-width buttons look and behave the same (keys and wheel still step)
                obj.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
                obj.setStyleSheet(obj.styleSheet() + " QAbstractSpinBox { padding-right: 6px; }"
                                  " QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { width: 0px; border: none; }"
                                  " QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow { image: none; width: 0px; }")
            elif isinstance(obj, QMenu) and translucent_popups():
                obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                obj.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        elif t == QEvent.Type.Show and sys.platform == "win32" and isinstance(obj, QWidget) and obj.isWindow():
            menu_like = isinstance(obj, QMenu) or obj.inherits("QComboBoxPrivateContainer")
            if menu_like or obj.windowFlags() & Qt.WindowType.Popup:
                from . import dwm

                dwm.round_corners(obj, small=not menu_like)
        return False


# ------------------------------------------------------------------------------------------------- fonts
FONT_CLASSES = ("QMenuBar", "QMenu", "QTabBar", "QToolButton", "QPushButton", "QStatusBar", "QHeaderView",
                "QAbstractItemView", "QTreeView", "QListView", "QTableView", "QTipLabel", "QMessageBox", "QLabel",
                "QLineEdit", "QComboBox", "QAbstractSpinBox", "QDockWidget", "QGroupBox", "QCheckBox", "QRadioButton",
                "QTextEdit", "QPlainTextEdit", "QToolBar", "QDialog")


def pin_fonts(app) -> None:
    """The token font for the app and for every widget class. The platform theme sets per-class fonts (menu bar,
    menus, item views, status bar, tooltips...) and puts them back on every theme change, which left widgets
    measured with one font and drawn with another; this runs again after each ThemeChange."""
    t = T
    if t is None:
        return
    f = QFont(t.font) if t.font else QFont(app.font())
    f.setPointSizeF(t.pt)
    if sys.platform != "win32":
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(f)
    for cls in FONT_CLASSES:
        app.setFont(f, cls)


# ------------------------------------------------------------------------------------------------- apply
def apply(app: QApplication) -> Tokens:
    """Style, palette, fonts, style sheet and icon settings for the current light/dark scheme."""
    global T, _platform_accent, _platform_dark
    if _platform_accent is None:
        pal = QGuiApplication.palette()
        _platform_accent = pal.color(QPalette.ColorRole.Accent)
        _platform_dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
        app.setStyle(Style("Fusion"))
    t = T = build(is_dark(app), _platform_accent)
    pin_fonts(app)
    app.setPalette(palette(t))
    from .. import icons

    icons.configure(t.icon_stroke_px, t.accent)
    app.setStyleSheet(stylesheet(t, make_assets(t)))
    if not _filters:
        for cls in (_PopupFilter, _AppFilter):
            f = cls(app)
            app.installEventFilter(f)
            _filters.append(f)
    return t
