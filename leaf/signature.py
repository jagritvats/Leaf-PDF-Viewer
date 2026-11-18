"""Signature library: draw, type or import; stored as PNGs in the app data folder."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QPointF, QRect, QSize, QStandardPaths, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .icons import icon

INK = QColor(18, 38, 120)


def _heading(text: str) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setWeight(QFont.Weight.DemiBold)
    lbl.setFont(f)
    return lbl
SCRIPT_FONTS = ["Segoe Script", "Lucida Handwriting", "Brush Script MT", "Ink Free", "Mistral",
                "Dancing Script", "Great Vibes", "URW Chancery L", "Z003", "Comic Sans MS"]


def library_dir() -> str:
    d = os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), "signatures")
    os.makedirs(d, exist_ok=True)
    return d


_MASK = bytes(1 if i > 10 else 0 for i in range(256))
_KNOCK = bytes(0 if v > 200 else 255 if v <= 140 else int(255 * (200 - v) / 60) for v in range(256))


def trim(img: QImage, pad: int = 6) -> QImage:
    """Crop to the non-transparent area (row scans run at C speed via bytes ops)."""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    a = img.convertToFormat(QImage.Format.Format_Alpha8)
    w, h, bpl = a.width(), a.height(), a.bytesPerLine()
    m = bytes(a.constBits()).translate(_MASK)
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = m[y * bpl: y * bpl + w]
        i = row.find(1)
        if i >= 0:
            x0, x1 = min(x0, i), max(x1, row.rfind(1))
            y0, y1 = min(y0, y), y
    if x1 < 0:
        return img
    r = QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1).adjusted(-pad, -pad, pad, pad) & img.rect()
    return img.copy(r)


def knock_out_background(img: QImage) -> QImage:
    """Make near-white pixels transparent (for photographed/scanned signatures)."""
    if img.width() > 1600:
        img = img.scaledToWidth(1600, Qt.TransformationMode.SmoothTransformation)
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    data = bytes(gray.constBits()).translate(_KNOCK)
    alpha = QImage(data, gray.width(), gray.height(), gray.bytesPerLine(), QImage.Format.Format_Grayscale8).copy()
    out = img.convertToFormat(QImage.Format.Format_ARGB32)
    out.setAlphaChannel(alpha)
    return out


class DrawPad(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(520, 200)
        self.strokes: list[list[QPointF]] = []
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

    def clear(self) -> None:
        self.strokes = []
        self.update()

    def mousePressEvent(self, e) -> None:
        self.strokes.append([e.position()])
        self.update()

    def mouseMoveEvent(self, e) -> None:
        if self.strokes and e.buttons() & Qt.MouseButton.LeftButton:
            self.strokes[-1].append(e.position())
            self.update()

    def _paint(self, p: QPainter, width: float = 2.6) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(INK, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        for s in self.strokes:
            path = QPainterPath(s[0])
            if len(s) == 1:
                path.lineTo(s[0] + QPointF(0.1, 0.1))
            for i in range(1, len(s) - 1):
                path.quadTo(s[i], (s[i] + s[i + 1]) / 2)
            if len(s) > 1:
                path.lineTo(s[-1])
            p.drawPath(path)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.setBrush(Qt.GlobalColor.white)
        p.drawRoundedRect(self.rect().toRectF().adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        y = int(self.height() * 0.72)
        p.setPen(QPen(QColor(0, 0, 0, 70), 1, Qt.PenStyle.DashLine))
        p.drawLine(28, y, self.width() - 28, y)
        p.setPen(QColor(0, 0, 0, 110))
        p.drawText(28, y + 20, "×")
        if not self.strokes:
            p.setPen(QColor(0, 0, 0, 90))
            p.drawText(self.rect().adjusted(0, 0, 0, -(self.height() - y) - 8), int(Qt.AlignmentFlag.AlignCenter),
                       "Sign above the line with your mouse, pen or finger")
        self._paint(p)

    def image(self) -> QImage | None:
        if not self.strokes:
            return None
        scale = 3
        img = QImage(self.size() * scale, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.scale(scale, scale)
        self._paint(p)
        p.end()
        return trim(img)


class SignatureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add signature")
        self.result_image: QImage | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)

        self.lib_title = _heading("Saved signatures")
        lay.addWidget(self.lib_title)
        self.lib = QListWidget()
        self.lib.setViewMode(QListWidget.ViewMode.IconMode)
        self.lib.setIconSize(QSize(150, 56))
        self.lib.setGridSize(QSize(172, 76))
        self.lib.setFixedHeight(92)
        self.lib.setFlow(QListWidget.Flow.LeftToRight)
        self.lib.setWrapping(False)
        self.lib.setMovement(QListWidget.Movement.Static)
        self.lib.setToolTip("Double-click to place")
        self.lib.setStyleSheet("QListWidget { background: white; border-radius: 8px; border: 1px solid rgba(0,0,0,40); }"
                               "QListWidget::item { border-radius: 6px; color: black; }")
        self.lib.itemDoubleClicked.connect(lambda _: self._use_selected())
        lay.addWidget(self.lib)
        self.lib_row = QWidget()
        row = QHBoxLayout(self.lib_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.use_btn = QPushButton("Place selected")
        self.use_btn.clicked.connect(self._use_selected)
        self.del_btn = QPushButton(icon("delete"), "Delete")
        self.del_btn.setFlat(True)
        self.del_btn.clicked.connect(self._delete_selected)
        row.addWidget(self.use_btn)
        row.addWidget(self.del_btn)
        row.addStretch()
        lay.addWidget(self.lib_row)
        lay.addSpacing(10)

        lay.addWidget(_heading("New signature"))
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.pad = DrawPad()
        draw = QWidget()
        dl = QVBoxLayout(draw)
        dl.setContentsMargins(0, 10, 0, 0)
        dl.addWidget(self.pad)
        clr = QPushButton(icon("delete"), "Clear")
        clr.setFlat(True)
        clr.clicked.connect(self.pad.clear)
        dl.addWidget(clr, alignment=Qt.AlignmentFlag.AlignRight)
        self.tabs.addTab(draw, icon("ink"), "Draw")

        typed = QWidget()
        tl = QVBoxLayout(typed)
        tl.setContentsMargins(0, 10, 0, 0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Type your name")
        self.font_box = QComboBox()
        families = set(QFontDatabase.families())
        for f in [f for f in SCRIPT_FONTS if f in families] or ["Serif"]:
            self.font_box.addItem(f)
        self.preview = QLabel()
        self.preview.setMinimumHeight(120)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: white; border-radius: 8px; border: 1px solid rgba(0,0,0,40);")
        self.name.textChanged.connect(self._update_preview)
        self.font_box.currentTextChanged.connect(self._update_preview)
        tl.addWidget(self.name)
        tl.addWidget(self.font_box)
        tl.addWidget(self.preview, 1)
        self.tabs.addTab(typed, icon("text"), "Type")

        imp = QWidget()
        il = QVBoxLayout(imp)
        il.setContentsMargins(0, 10, 0, 0)
        self.img_label = QLabel("Choose a photo or scan of your signature.\nThe white background is removed automatically.")
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setMinimumHeight(140)
        self.img_label.setStyleSheet("background: white; color: #555; border-radius: 8px; border: 1px dashed rgba(0,0,0,70);")
        btn = QPushButton(icon("open"), "Choose image…")
        btn.clicked.connect(self._choose_image)
        il.addWidget(self.img_label, 1)
        il.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        self.imported: QImage | None = None
        self.tabs.addTab(imp, icon("image"), "Image")
        lay.addWidget(self.tabs, 1)

        bb = QDialogButtonBox()
        self.place_new = bb.addButton("Save && place", QDialogButtonBox.ButtonRole.AcceptRole)
        self.place_new.setDefault(True)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept_new)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load_library()

    def _render_typed(self) -> QImage | None:
        text = self.name.text().strip()
        if not text:
            return None
        f = QFont(self.font_box.currentText())
        f.setPixelSize(120)
        img = QImage(1600, 260, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(f)
        p.setPen(INK)
        p.drawText(img.rect(), int(Qt.AlignmentFlag.AlignCenter), text)
        p.end()
        return trim(img)

    def _update_preview(self) -> None:
        img = self._render_typed()
        if img is None:
            self.preview.clear()
            return
        self.preview.setPixmap(QPixmap.fromImage(img).scaledToHeight(90, Qt.TransformationMode.SmoothTransformation))

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Signature image", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            return
        if not img.hasAlphaChannel():
            img = knock_out_background(img)
        self.imported = trim(img)
        self.img_label.setPixmap(QPixmap.fromImage(self.imported).scaledToHeight(120, Qt.TransformationMode.SmoothTransformation))

    def _load_library(self) -> None:
        self.lib.clear()
        d = library_dir()
        for name in sorted(os.listdir(d), reverse=True):
            if name.lower().endswith(".png"):
                path = os.path.join(d, name)
                it = QListWidgetItem(QIcon(path), "")
                it.setData(Qt.ItemDataRole.UserRole, path)
                self.lib.addItem(it)
        has = self.lib.count() > 0
        self.use_btn.setEnabled(has)
        self.del_btn.setEnabled(has)
        for w in (self.lib_title, self.lib, self.lib_row):
            w.setVisible(has)
        if has:
            self.lib.setCurrentRow(0)

    def _use_selected(self) -> None:
        it = self.lib.currentItem()
        if it:
            self.result_image = QImage(it.data(Qt.ItemDataRole.UserRole))
            self.accept()

    def _delete_selected(self) -> None:
        it = self.lib.currentItem()
        if it:
            try:
                os.remove(it.data(Qt.ItemDataRole.UserRole))
            except OSError:
                pass
            self._load_library()

    def _accept_new(self) -> None:
        idx = self.tabs.currentIndex()
        img = self.pad.image() if idx == 0 else self._render_typed() if idx == 1 else self.imported
        if img is None or img.isNull():
            return
        img.save(os.path.join(library_dir(), f"sig-{int(time.time() * 1000)}.png"))
        self.result_image = img
        self.accept()
