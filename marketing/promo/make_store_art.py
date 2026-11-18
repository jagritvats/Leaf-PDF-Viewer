"""Microsoft Store listing logos: 9:16 poster art, 1:1 box art, 1:1 app tile icons and 16:9 super hero art, on the package's green plate.

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


SHOT = os.path.join(ROOT, "marketing", "out", "screens", "dark", "01-hero-annotate-2560x1600.png")


def hero(w: int, h: int) -> QImage:
    """16:9 Super hero art: no title or other text (the Store overlays the product name, rating and Get button on
    the left and bottom), so the green plate, a large faint leaf on the left and the app window on the right."""
    from PySide6.QtGui import QPainterPath

    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor(A.TOP))
    g.setColorAt(1, QColor(A.BOTTOM))
    p.fillRect(QRectF(0, 0, w, h), g)
    p.setOpacity(0.12)  # watermark leaf, bleeding off the left edge
    big = h * 1.15
    A.draw_icon(p, -big * 0.32, (h - big) / 2 + h * 0.08, big, square=False)
    p.setOpacity(1.0)
    shot = QImage(SHOT)
    if shot.isNull():
        raise SystemExit(f"missing {SHOT}: run marketing/shots/shoot_all.py --only 1 --themes dark --sizes 2560x1600")
    sw = w * 0.60
    sh = sw * shot.height() / shot.width()
    x, y = w - sw - w * 0.06, (h - sh) / 2
    r = w * 0.008
    for i in range(24, 0, -1):  # soft shadow
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 5))
        d = i * w * 0.0012
        p.drawRoundedRect(QRectF(x - d, y - d + w * 0.006, sw + 2 * d, sh + 2 * d), r + d, r + d)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(x, y, sw, sh), r, r)
    p.setClipPath(clip)
    p.drawImage(QRectF(x, y, sw, sh), shot)
    p.end()
    return img


def main() -> list[str]:
    os.makedirs(OUT, exist_ok=True)
    written = []
    for kind, (w, h) in (("poster", (1440, 2160)), ("poster", (720, 1080)), ("box", (2160, 2160)), ("box", (1080, 1080))):
        path = os.path.join(OUT, f"store-{kind}-{w}x{h}.png")
        art(w, h).save(path, "PNG")
        written.append(path)
    for w, h in ((3840, 2160), (1920, 1080)):
        path = os.path.join(OUT, f"store-hero-{w}x{h}.png")
        hero(w, h).save(path, "PNG")
        written.append(path)
    for n in (300, 150, 71):  # 1:1 app tile icons: too small for the name, so the leaf alone (like the MSIX tiles)
        path = os.path.join(OUT, f"store-tile-{n}x{n}.png")
        A.render(n, n, 0.8).convertToFormat(QImage.Format.Format_RGB32).save(path, "PNG")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in main():
        print(path)
