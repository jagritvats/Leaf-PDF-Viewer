"""Microsoft Store listing logos: 9:16 poster art and 1:1 box art, on the package's green plate.

    python marketing/promo/make_store_art.py        -> marketing/out/promo/store-*.png

Full-bleed (no transparency anywhere, so no accent-colored frame), the Leaf icon and the product name only
(Store rule: no other text). Same gradient and leaf as packaging/msix/make_assets.py.
"""
from __future__ import annotations

import os
import sys

if sys.platform != "win32":  # Windows: the native platform plugin has the system fonts (no window is shown)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "packaging", "msix"))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter  # noqa: E402

app = QGuiApplication.instance() or QGuiApplication(sys.argv)
import make_assets as A  # noqa: E402

NAME = "Leaf PDF Editor"
OUT = os.path.join(ROOT, "marketing", "out", "promo")


def art(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)  # RGB32: no alpha channel at all
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor(A.TOP))
    g.setColorAt(1, QColor(A.BOTTOM))
    p.fillRect(QRectF(0, 0, w, h), g)
    unit = min(w, h)
    leaf = unit * 0.42  # the leaf glyph's box; generous margins all round (Store crops differently per surface)
    text_px = unit * 0.085
    gap = unit * 0.06
    block = leaf + gap + text_px * 1.25
    top = (h - block) / 2
    A.draw_icon(p, (w - leaf) / 2, top, leaf, square=False)
    f = QFont("Segoe UI Variable Display")
    f.setPixelSize(round(text_px))
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, top + leaf + gap, w, text_px * 1.4), int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), NAME)
    p.end()
    return img


def main() -> list[str]:
    os.makedirs(OUT, exist_ok=True)
    written = []
    for kind, (w, h) in (("poster", (1440, 2160)), ("poster", (720, 1080)), ("box", (2160, 2160)), ("box", (1080, 1080))):
        path = os.path.join(OUT, f"store-{kind}-{w}x{h}.png")
        art(w, h).save(path, "PNG")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in main():
        print(path)
