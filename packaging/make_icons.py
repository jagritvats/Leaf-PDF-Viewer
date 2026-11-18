"""Render the app icon (drawn in code, leaf/icons.py) into build/icons: PNGs, a Windows .ico and a macOS .iconset.

Usage: python packaging/make_icons.py   (macOS: then `iconutil -c icns build/icons/leaf.iconset`)
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtGui import QGuiApplication, QImage, QImageWriter  # noqa: E402

app = QGuiApplication(sys.argv)
from leaf.icons import app_icon  # noqa: E402

out = os.path.join(ROOT, "build", "icons")
iconset = os.path.join(out, "leaf.iconset")
os.makedirs(iconset, exist_ok=True)
icon = app_icon()


def image(px: int) -> QImage:
    return icon.pixmap(px, px).toImage().scaled(px, px)


for px in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
    image(px).save(os.path.join(out, f"leaf-{px}.png"))
for px in (16, 32, 128, 256, 512):  # macOS iconset naming
    image(px).save(os.path.join(iconset, f"icon_{px}x{px}.png"))
    image(px * 2).save(os.path.join(iconset, f"icon_{px}x{px}@2x.png"))

ico = os.path.join(out, "leaf.ico")
if b"ico" in [bytes(f) for f in QImageWriter.supportedImageFormats()]:
    # Qt's ICO writer stores one image per file; build a multi-size .ico by hand
    import struct

    sizes = (16, 24, 32, 48, 64, 256)
    blobs = []
    for px in sizes:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image(px).save(buf, "PNG")
        blobs.append(bytes(ba))
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for px, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", px % 256, px % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(ico, "wb") as f:
        f.write(header + entries + b"".join(blobs))
print("icons written to", out)
