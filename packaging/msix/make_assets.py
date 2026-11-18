"""Render the MSIX visual assets (tiles, app list icons, store logo) into build/msix/Assets.

    python packaging/msix/make_assets.py [out_dir]

The icon is drawn in code like leaf/icons.py app_icon() (same leaf path and colors), but at the exact pixel size of
each asset instead of scaling a 256 px bitmap. Each asset is written at scale-100 (the unqualified name the
manifest references) and scale-200; Square44x44Logo also gets the targetsize-N (plated and altform-unplated)
variants the taskbar and Start use. build_msix.ps1 indexes the qualified files with makepri.

Plated assets (tiles, the store logo, the plated app-list icons) are full-bleed: the green fills the whole square
and only the white leaf is drawn on it. Windows and the Store draw those on a plate in the manifest's
BackgroundColor, so any transparent margin showed up as a blue (accent-colored) frame. Only the altform-unplated
icons (taskbar, Windows 11 Start) keep the rounded green square on transparency.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication(sys.argv)
from leaf.icons import _leaf_path  # noqa: E402

# name -> (width, height, icon size as a fraction of the height)
ASSETS = {
    "Square44x44Logo": (44, 44, 1.0),
    "SmallTile": (71, 71, 0.6),
    "Square150x150Logo": (150, 150, 0.55),
    "Wide310x150Logo": (310, 150, 0.55),
    "Square310x310Logo": (310, 310, 0.5),
    "StoreLogo": (50, 50, 1.0),
}
TARGET_SIZES = (16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256)


TOP, BOTTOM = "#43c463", "#1f8a3b"  # the icon's gradient
PLATE = "#2fa84f"  # its middle: AppxManifest.xml BackgroundColor (tests/test_packaging.py keeps them equal)


def draw_icon(p: QPainter, x: float, y: float, size: float, square: bool = True) -> None:
    """The Leaf icon in the size x size square at (x, y): the white leaf on a green rounded square, or (square=False)
    the white leaf alone, for drawing on a full-bleed green plate."""
    p.save()
    p.translate(x, y)
    p.scale(size / 24, size / 24)
    p.setPen(Qt.PenStyle.NoPen)
    if square:
        g = QLinearGradient(0, 0, 0, 24)
        g.setColorAt(0, QColor(TOP))
        g.setColorAt(1, QColor(BOTTOM))
        p.setBrush(g)
        p.drawRoundedRect(QRectF(1, 1, 22, 22), 5.5, 5.5)
    p.translate(12, 12)
    p.scale(0.82, 0.82)
    p.translate(-12, -12.5)
    p.setBrush(QColor(255, 255, 255, 240))
    p.drawPath(_leaf_path())
    p.setPen(QPen(QColor("#2a9a48"), 1.3 if size >= 32 else 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(5, 19), QPointF(14.5, 9.5))
    p.restore()


def render(w: int, h: int, frac: float, plated: bool = True) -> QImage:
    """plated: full-bleed green with the leaf (frac = leaf area / height); unplated: the rounded icon on transparency."""
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if plated:
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor(TOP))
        g.setColorAt(1, QColor(BOTTOM))
        p.fillRect(QRectF(0, 0, w, h), g)
    s = h * frac
    draw_icon(p, (w - s) / 2, (h - s) / 2, s, square=not plated)
    p.end()
    return img


def main(out: str) -> list[str]:
    os.makedirs(out, exist_ok=True)
    written = []

    def save(img: QImage, name: str) -> None:
        path = os.path.join(out, name)
        img.save(path, "PNG")
        written.append(path)

    for name, (w, h, frac) in ASSETS.items():
        save(render(w, h, frac), f"{name}.png")
        save(render(w * 2, h * 2, frac), f"{name}.scale-200.png")
    for n in TARGET_SIZES:
        save(render(n, n, 1.0), f"Square44x44Logo.targetsize-{n}.png")
        save(render(n, n, 1.0, plated=False), f"Square44x44Logo.targetsize-{n}_altform-unplated.png")
    return written


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "msix", "Assets")
    files = main(target)
    print(f"{len(files)} assets written to {target}")
