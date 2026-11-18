"""Page decorations: Document menu with watermark, header & footer and Bates numbering dialogs (each with a
live preview on a page thumbnail and on the canvas) and a manager to edit or remove them."""
from __future__ import annotations

import os
from dataclasses import replace

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPushButton, QRadioButton, QSpinBox, QToolButton, QVBoxLayout,
)

from .. import decorations as D
from .. import pageops
from ..canvas import to_qimage

APP = "Leaf"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)"
THUMB = 300  # preview thumbnail size (px, longest side)
TOKEN_NAMES = {"{page}": "Page number", "{pages}": "Total pages", "{label}": "Page label", "{date}": "Date",
               "{filename}": "File name", "{title}": "Document title"}


def _toast(win, text: str, undo: bool = False) -> None:
    try:
        if undo:
            win.toast(text, "Undo", win.canvas.undo.undo)
        else:
            win.toast(text)
    except Exception:  # noqa: BLE001 - fall back to the status bar
        win.statusBar().showMessage(text, 3000)


class ColorButton(QToolButton):
    def __init__(self, color: str, on_change):
        super().__init__()
        self.color = color
        self.on_change = on_change
        self.setToolTip("Color")
        self.clicked.connect(self._pick)
        self._paint()

    def _paint(self) -> None:
        pm = QPixmap(QSize(28, 16))
        pm.fill(QColor(self.color))
        self.setIcon(QIcon(pm))
        self.setIconSize(pm.size())

    def set_color(self, c: str) -> None:
        self.color = c
        self._paint()

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self, "Color")
        if c.isValid():
            self.set_color(c.name())
            self.on_change()


# ---------------------------------------------------------------- dialogs
class DecorationDialog(QDialog):
    """Common parts: text style, page selection, live thumbnail and canvas preview.

    While open, the draft is shown on the canvas by swapping it into `doc.decorations` (no undo step);
    `run()` restores the list and pushes one undoable command on OK."""

    title = "Decoration"
    kind = "header_footer"

    def __init__(self, win, spec: D.Decoration):
        super().__init__(win)
        self.win, self.cv = win, win.canvas
        self.base = spec
        self.editing = any(s.uid == spec.uid for s in D.decorations_of(self.cv.doc))
        self.setWindowTitle(("Edit " if self.editing else "") + self.title)
        self.saved = D.decorations_of(self.cv.doc)
        self.n = len(self.cv.order)
        self._thumbs: dict = {}
        self._timer = QTimer(self, singleShot=True, interval=30)
        self._timer.timeout.connect(self._refresh)

        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        outer.addLayout(left, 1)
        self.build(left, spec)
        left.addWidget(self._style_box(spec))
        left.addWidget(self._pages_box(spec))
        left.addStretch()
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply" if self.editing else "Add")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        left.addWidget(self.bb)

        right = QVBoxLayout()
        outer.addLayout(right)
        self.thumb = QLabel()
        self.thumb.setFixedSize(THUMB + 8, THUMB + 8)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setFrameShape(QLabel.Shape.StyledPanel)
        right.addWidget(self.thumb)
        row = QHBoxLayout()
        row.addWidget(QLabel("Preview page:"))
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, max(1, self.n))
        self.page_spin.setValue(max(0, self.cv.current_index()) + 1)
        self.page_spin.setSuffix(f" of {self.n}")
        self.page_spin.valueChanged.connect(self.changed)
        row.addWidget(self.page_spin)
        row.addStretch()
        right.addLayout(row)
        right.addStretch()
        self.changed()

    # ---- pieces
    def build(self, lay: QVBoxLayout, spec: D.Decoration) -> None:
        """Kind-specific widgets."""

    def _style_box(self, spec: D.Decoration) -> QGroupBox:
        box = QGroupBox("Appearance")
        f = QFormLayout(box)
        self.family = QComboBox()
        for k, name in D.FAMILIES.items():
            self.family.addItem(name, k)
        self.family.setCurrentIndex(max(0, self.family.findData(spec.family)))
        self.bold = QCheckBox("Bold")
        self.bold.setChecked(spec.bold)
        self.color = ColorButton(spec.color, self.changed)
        row = QHBoxLayout()
        row.addWidget(self.family, 1)
        row.addWidget(self.bold)
        row.addWidget(self.color)
        f.addRow("Font:", row)
        self.size = QDoubleSpinBox()
        self.size.setRange(4, 400)
        self.size.setDecimals(1)
        self.size.setSuffix(" pt")
        self.size.setValue(spec.size)
        self.size_row = QHBoxLayout()
        self.size_row.addWidget(self.size)
        self.size_row.addStretch()
        f.addRow("Size:", self.size_row)
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(round(spec.opacity * 100))
        self.layer = QComboBox()
        self.layer.addItems(["In front of page content", "Behind page content"])
        self.layer.setCurrentIndex(1 if spec.behind else 0)
        row = QHBoxLayout()
        row.addWidget(self.opacity)
        row.addWidget(self.layer, 1)
        f.addRow("Opacity:", row)
        self.style_form = f
        for sig in (self.family.currentIndexChanged, self.bold.toggled, self.size.valueChanged,
                    self.opacity.valueChanged, self.layer.currentIndexChanged):
            sig.connect(self.changed)
        return box

    def _pages_box(self, spec: D.Decoration) -> QGroupBox:
        box = QGroupBox("Pages")
        f = QFormLayout(box)
        self.mode = QComboBox()
        for k, name in D.PAGE_MODES.items():
            self.mode.addItem(name, k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(spec.pages)))
        f.addRow("Apply to:", self.mode)
        self.range_status = QLabel()
        self.range = pageops_range_edit(self.n, spec.page_range, self.range_status)
        f.addRow("Range:", self.range)
        f.addRow("", self.range_status)
        self.pages_form = f
        self.mode.currentIndexChanged.connect(self.changed)
        self.range.textChanged.connect(self.changed)
        return box

    # ---- model
    def fields(self) -> dict:
        """Kind-specific spec fields."""
        return {}

    def spec(self) -> D.Decoration:
        common = dict(family=self.family.currentData(), bold=self.bold.isChecked(), color=self.color.color,
                      size=self.size.value(), opacity=self.opacity.value() / 100, behind=self.layer.currentIndex() == 1,
                      pages=self.mode.currentData(), page_range=self.range.text().strip())
        return replace(self.base, **common, **self.fields())

    def valid(self) -> bool:
        if self.mode.currentData() == "range":
            try:
                pageops.parse_ranges(self.range.text(), self.n)
            except ValueError:
                return False
        return True

    def changed(self, *_) -> None:
        rng = self.mode.currentData() == "range"
        self.pages_form.setRowVisible(self.range, rng)
        self.pages_form.setRowVisible(self.range_status, rng)
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.valid())
        self._timer.start()

    def _draft_list(self, spec) -> list:
        if self.editing:
            return [spec if s.uid == spec.uid else s for s in self.saved]
        return self.saved + [spec]

    def _refresh(self) -> None:
        spec = self.spec()
        self.cv.doc.decorations = self._draft_list(spec)
        D.preview_for(self.cv).refresh()
        self._paint_thumb(spec)

    def _paint_thumb(self, spec) -> None:
        row = self.page_spin.value() - 1
        if not 0 <= row < self.n:
            return
        doc = self.cv.doc
        ref = doc.pages[row]
        w, h = doc.display_size(ref)
        scale = THUMB / max(w, h, 1)
        img = self._thumbs.get((ref.uid, ref.rotation))
        if img is None:
            try:
                pix, _ = doc.render(ref, scale)
                img = to_qimage(pix)
            except Exception:  # noqa: BLE001 - a broken page still gets a blank preview
                img = QPixmap(int(w * scale), int(h * scale)).toImage()
                img.fill(Qt.GlobalColor.white)
            self._thumbs[(ref.uid, ref.rotation)] = img
        out = img.copy()
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.scale(out.width() / w, out.height() / h)
        p.setClipRect(QRectF(0, 0, w, h))
        ctx = D.preview_context(self.cv)
        for s in sorted(self._draft_list(spec), key=lambda s: not s.behind):
            content = D.layout(s, row, ctx, w, h)
            if content:
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply if s.behind
                                     else QPainter.CompositionMode.CompositionMode_SourceOver)
                p.setOpacity(max(0.0, min(1.0, s.opacity)))
                D.paint_content(p, content)
        p.end()
        self.thumb.setPixmap(QPixmap.fromImage(out))

    def run(self) -> D.Decoration | None:
        ok = self.exec() == QDialog.DialogCode.Accepted
        spec = self.spec() if ok else None
        self._timer.stop()
        self.cv.doc.decorations = list(self.saved)
        if spec is not None:
            (D.update if self.editing else D.add)(self.cv, spec)
        D.preview_for(self.cv).refresh()
        return spec


def pageops_range_edit(n: int, text: str, status: QLabel) -> QLineEdit:
    from .pages import RangeEdit

    return RangeEdit(n, text, status)


class WatermarkDialog(DecorationDialog):
    title = "Add watermark"

    def build(self, lay, spec) -> None:
        box = QGroupBox("Watermark")
        f = QFormLayout(box)
        self.is_text = QRadioButton("Text")
        self.is_image = QRadioButton("Image")
        g = QButtonGroup(self)
        g.addButton(self.is_text)
        g.addButton(self.is_image)
        (self.is_image if spec.image else self.is_text).setChecked(True)
        self.text = QLineEdit(spec.text)
        self.text.setPlaceholderText("e.g. CONFIDENTIAL, DRAFT, COPY")
        tokens = QToolButton()
        tokens.setText("{ }")
        tokens.setToolTip("Insert a field")
        tokens.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tokens.setMenu(_token_menu(self, lambda t: self.text.insert(t)))
        row = QHBoxLayout()
        row.addWidget(self.is_text)
        row.addWidget(self.text, 1)
        row.addWidget(tokens)
        f.addRow(row)
        self.image_data, self.image_name = spec.image, spec.image_name
        self.image_label = QLabel(spec.image_name or "No image chosen")
        pick = QPushButton("Choose…")
        pick.clicked.connect(self._choose_image)
        row = QHBoxLayout()
        row.addWidget(self.is_image)
        row.addWidget(self.image_label, 1)
        row.addWidget(pick)
        f.addRow(row)
        self.scale = QSpinBox()
        self.scale.setRange(5, 200)
        self.scale.setSuffix(" % of page width")
        self.scale.setValue(round(spec.scale * 100))
        f.addRow("Image size:", self.scale)
        self.angle_mode = QComboBox()
        self.angle_mode.addItems(["Diagonal", "Horizontal", "Vertical", "Custom"])
        self.angle_mode.setCurrentIndex(0 if spec.diagonal else {0.0: 1, 90.0: 2}.get(float(spec.angle), 3))
        self.angle = QSpinBox()
        self.angle.setRange(-180, 180)
        self.angle.setSuffix("°")
        self.angle.setValue(int(spec.angle))
        row = QHBoxLayout()
        row.addWidget(self.angle_mode, 1)
        row.addWidget(self.angle)
        f.addRow("Angle:", row)
        self.position = QComboBox()
        self.position.addItems(["Centered", "Tiled across the page"])
        self.position.setCurrentIndex(1 if spec.tiled else 0)
        f.addRow("Position:", self.position)
        lay.addWidget(box)
        for sig in (self.is_text.toggled, self.text.textChanged, self.scale.valueChanged,
                    self.angle_mode.currentIndexChanged, self.angle.valueChanged, self.position.currentIndexChanged):
            sig.connect(self.changed)

    def _style_box(self, spec) -> QGroupBox:
        box = super()._style_box(spec)
        self.fit = QCheckBox("Fit to page")
        self.fit.setChecked(spec.fit)
        self.fit.toggled.connect(self.changed)
        self.size_row.insertWidget(1, self.fit)
        return box

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Watermark image", "", IMAGE_FILTER)
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as ex:
            QMessageBox.warning(self, APP, f"Couldn't read the image:\n{ex}")
            return
        if D.image_size(data) == (0, 0):
            QMessageBox.warning(self, APP, "That file isn't an image Leaf can read.")
            return
        self.image_data, self.image_name = data, os.path.basename(path)
        self.image_label.setText(self.image_name)
        self.is_image.setChecked(True)
        self.changed()

    def fields(self) -> dict:
        mode = self.angle_mode.currentIndex()
        angle = {1: 0.0, 2: 90.0}.get(mode, float(self.angle.value()))
        image = self.is_image.isChecked() and bool(self.image_data)
        return dict(text=self.text.text(), image=self.image_data if image else b"",
                    image_name=self.image_name if image else "", scale=self.scale.value() / 100,
                    diagonal=mode == 0, angle=angle, tiled=self.position.currentIndex() == 1, fit=self.fit.isChecked())

    def valid(self) -> bool:
        if self.is_image.isChecked() and not self.image_data:
            return False
        if self.is_text.isChecked() and not self.text.text().strip():
            return False
        return super().valid()

    def changed(self, *_) -> None:
        if not hasattr(self, "fit"):
            return
        image = self.is_image.isChecked()
        self.text.setEnabled(not image)
        self.scale.setEnabled(image)
        for w in (self.family, self.bold, self.color, self.fit, self.angle_mode):
            w.setEnabled(not image)
        self.angle.setEnabled(not image and self.angle_mode.currentIndex() == 3)
        self.size.setEnabled(not image and (not self.fit.isChecked() or self.position.currentIndex() == 1))
        super().changed()


def _token_menu(parent, insert) -> QMenu:
    m = QMenu(parent)
    for tok, name in TOKEN_NAMES.items():
        m.addAction(f"{name}\t{tok}", lambda t=tok: insert(t))
    return m


class HeaderFooterDialog(DecorationDialog):
    title = "Header & footer"

    def build(self, lay, spec) -> None:
        box = QGroupBox("Text")
        g = QGridLayout(box)
        self.slot_edits: list[QLineEdit] = []
        for c, name in enumerate(("Left", "Center", "Right")):
            hdr = QLabel(name)
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            g.addWidget(hdr, 0, c + 1)
        for r, name in enumerate(("Header", "Footer")):
            g.addWidget(QLabel(name + ":"), r + 1, 0)
            for c in range(3):
                e = QLineEdit(spec.slots[r * 3 + c] if len(spec.slots) == 6 else "")
                e.setMinimumWidth(120)
                e.setPlaceholderText("—")
                e.setToolTip(f"{name} {('left', 'center', 'right')[c]}: text and fields such as {{page}}")
                e.textChanged.connect(self.changed)
                g.addWidget(e, r + 1, c + 1)
                self.slot_edits.append(e)
        self._last = self.slot_edits[4]
        for e in self.slot_edits:
            e.installEventFilter(self)
        row = QHBoxLayout()
        tokens = QPushButton("Insert field")
        tokens.setMenu(_token_menu(self, lambda t: self._last.insert(t)))
        presets = QPushButton("Page numbers")
        pm = QMenu(self)
        for p in D.PRESETS:
            pm.addAction(p, lambda p=p: self._preset(p))
        presets.setMenu(pm)
        row.addWidget(tokens)
        row.addWidget(presets)
        row.addStretch()
        g.addLayout(row, 3, 1, 1, 3)
        lay.addWidget(box)

        box = QGroupBox("Layout")
        f = QFormLayout(box)
        self.mx = QDoubleSpinBox()
        self.my = QDoubleSpinBox()
        for s, v in ((self.mx, spec.margin_x), (self.my, spec.margin_y)):
            s.setRange(0, 400)
            s.setDecimals(0)
            s.setSuffix(" pt")
            s.setValue(v)
            s.valueChanged.connect(self.changed)
        row = QHBoxLayout()
        row.addWidget(QLabel("Sides"))
        row.addWidget(self.mx)
        row.addWidget(QLabel("Top/bottom"))
        row.addWidget(self.my)
        row.addStretch()
        f.addRow("Margins:", row)
        self.start = QSpinBox()
        self.start.setRange(-99999, 9999999)
        self.start.setValue(spec.start)
        self.start.setToolTip("The number {page} shows on the first page of the document")
        self.start.valueChanged.connect(self.changed)
        f.addRow("First page number:", self.start)
        self.date_fmt = QComboBox()
        from datetime import datetime

        now = datetime.now()
        for fmt in D.DATE_FORMATS:
            self.date_fmt.addItem(now.strftime(fmt), fmt)
        self.date_fmt.setCurrentIndex(max(0, self.date_fmt.findData(spec.date_format)))
        self.date_fmt.currentIndexChanged.connect(self.changed)
        f.addRow("Date format:", self.date_fmt)
        lay.addWidget(box)

    def eventFilter(self, obj, e) -> bool:
        if e.type() == e.Type.FocusIn and obj in self.slot_edits:
            self._last = obj
        return super().eventFilter(obj, e)

    def _preset(self, text: str) -> None:
        target = self._last if self._last.text() == "" or self._last.hasFocus() else self.slot_edits[4]
        target.setText(text)

    def fields(self) -> dict:
        return dict(slots=tuple(e.text() for e in self.slot_edits), margin_x=self.mx.value(),
                    margin_y=self.my.value(), start=self.start.value(), date_format=self.date_fmt.currentData())

    def valid(self) -> bool:
        return any(e.text().strip() for e in self.slot_edits) and super().valid()


class BatesDialog(DecorationDialog):
    title = "Bates numbering"

    def build(self, lay, spec) -> None:
        box = QGroupBox("Number")
        f = QFormLayout(box)
        self.prefix = QLineEdit(spec.prefix)
        self.prefix.setPlaceholderText("e.g. ACME-")
        self.suffix = QLineEdit(spec.suffix)
        self.start = QSpinBox()
        self.start.setRange(0, 999999999)
        self.start.setValue(spec.start)
        self.digits = QSpinBox()
        self.digits.setRange(1, 15)
        self.digits.setValue(spec.digits)
        self.slot = QComboBox()
        for k, name in D.SLOT_NAMES.items():
            self.slot.addItem(name, k)
        self.slot.setCurrentIndex(max(0, self.slot.findData(spec.slot)))
        self.sample = QLabel()
        f.addRow("Prefix:", self.prefix)
        f.addRow("Suffix:", self.suffix)
        f.addRow("Start at:", self.start)
        f.addRow("Digits:", self.digits)
        f.addRow("Position:", self.slot)
        f.addRow("Sample:", self.sample)
        lay.addWidget(box)
        self.mx = QDoubleSpinBox()
        self.my = QDoubleSpinBox()
        for s, v in ((self.mx, spec.margin_x), (self.my, spec.margin_y)):
            s.setRange(0, 400)
            s.setDecimals(0)
            s.setSuffix(" pt")
            s.setValue(v)
        row = QHBoxLayout()
        row.addWidget(QLabel("Sides"))
        row.addWidget(self.mx)
        row.addWidget(QLabel("Top/bottom"))
        row.addWidget(self.my)
        row.addStretch()
        f.addRow("Margins:", row)
        for sig in (self.prefix.textChanged, self.suffix.textChanged, self.start.valueChanged, self.digits.valueChanged,
                    self.slot.currentIndexChanged, self.mx.valueChanged, self.my.valueChanged):
            sig.connect(self.changed)

    def fields(self) -> dict:
        return dict(prefix=self.prefix.text(), suffix=self.suffix.text(), start=self.start.value(),
                    digits=self.digits.value(), slot=self.slot.currentData(), margin_x=self.mx.value(),
                    margin_y=self.my.value())

    def changed(self, *_) -> None:
        if hasattr(self, "sample") and hasattr(self, "mode"):
            spec = self.spec()
            sel = D.selection(spec, self.n)
            last = D.bates_text(spec, max(0, len(sel) - 1))
            self.sample.setText(f"{D.bates_text(spec, 0)} … {last}" if len(sel) > 1 else D.bates_text(spec, 0))
        super().changed()


DIALOGS = {"watermark": WatermarkDialog, "header_footer": HeaderFooterDialog, "bates": BatesDialog}


def edit_decoration(win, spec: D.Decoration) -> D.Decoration | None:
    if win.canvas.doc is None:
        return None
    win._commit_edits()
    return DIALOGS[spec.kind](win, spec).run()


class ManageDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win, self.cv = win, win.canvas
        self.setWindowTitle("Page decorations")
        self.resize(520, 300)
        lay = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        self.list.currentRowChanged.connect(lambda _: self._sync())
        lay.addWidget(self.list, 1)
        col = QVBoxLayout()
        add = QPushButton("Add")
        am = QMenu(self)
        for kind, name in D.KINDS.items():
            am.addAction(name + "…", lambda k=kind: self._add(k))
        add.setMenu(am)
        self.b_edit = QPushButton("Edit…")
        self.b_remove = QPushButton("Remove")
        self.b_clear = QPushButton("Remove all")
        close = QPushButton("Close")
        self.b_edit.clicked.connect(self._edit)
        self.b_remove.clicked.connect(self._remove)
        self.b_clear.clicked.connect(self._clear)
        close.clicked.connect(self.accept)
        for b in (add, self.b_edit, self.b_remove, self.b_clear):
            col.addWidget(b)
        col.addStretch()
        col.addWidget(close)
        lay.addLayout(col)
        self.cv.undo.indexChanged.connect(self._fill)
        self._fill()

    def done(self, r) -> None:
        try:
            self.cv.undo.indexChanged.disconnect(self._fill)
        except (RuntimeError, TypeError):
            pass
        super().done(r)

    def specs(self) -> list[D.Decoration]:
        return D.decorations_of(self.cv.doc) if self.cv.doc is not None else []

    def _fill(self, *_) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for s in self.specs():
            it = QListWidgetItem(D.describe(s))
            it.setData(Qt.ItemDataRole.UserRole, s.uid)
            self.list.addItem(it)
        if not self.specs():
            it = QListWidgetItem("No watermarks, headers, footers or Bates numbers yet.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        if self.specs():
            self.list.setCurrentRow(max(0, min(row, len(self.specs()) - 1)))
        self._sync()

    def _current(self) -> D.Decoration | None:
        it = self.list.currentItem()
        uid = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        return next((s for s in self.specs() if s.uid == uid), None)

    def _sync(self) -> None:
        has = self._current() is not None
        self.b_edit.setEnabled(has)
        self.b_remove.setEnabled(has)
        self.b_clear.setEnabled(bool(self.specs()))

    def _add(self, kind: str) -> None:
        add_new(self.win, kind)

    def _edit(self) -> None:
        s = self._current()
        if s is not None:
            edit_decoration(self.win, s)

    def _remove(self) -> None:
        s = self._current()
        if s is not None:
            D.remove(self.cv, s.uid)

    def _clear(self) -> None:
        D.remove_all(self.cv)


def add_new(win, kind: str) -> D.Decoration | None:
    """Open the dialog for a new decoration of `kind`, starting from the last one used in this window."""
    last = getattr(win, "_decor_last", {}).get(kind)
    factory = {"watermark": D.watermark, "header_footer": D.header_footer, "bates": D.bates}[kind]
    base = factory()
    if last is not None:
        base = replace(last, uid=base.uid)
    elif kind == "header_footer":
        base = replace(base, slots=("", "", "", "", "Page {page} of {pages}", ""))
    spec = edit_decoration(win, base)
    if spec is not None:
        win.__dict__.setdefault("_decor_last", {})[kind] = spec
        _toast(win, f"{D.KINDS[kind]} added", undo=True)
    return spec


def manage(win) -> None:
    if win.canvas.doc is None:
        return
    win._commit_edits()
    ManageDialog(win).exec()


# ---------------------------------------------------------------- install
def install(win) -> None:
    cv = win.canvas
    D.preview_for(cv)

    def act(name: str, text: str, slot, tip: str) -> QAction:
        a = QAction(text, win)
        a.setObjectName(f"a_{name}")
        a.setToolTip(tip)
        a.triggered.connect(lambda _=False: slot())
        win.addAction(a)
        setattr(win, f"a_{name}", a)
        return a

    a_wm = act("watermark", "Add &watermark…", lambda: add_new(win, "watermark"),
               "Add a text or image watermark to the pages")
    a_hf = act("header_footer", "&Header && footer…", lambda: add_new(win, "header_footer"),
               "Add headers and footers: page numbers, date, file name, title")
    a_bates = act("bates", "&Bates numbering…", lambda: add_new(win, "bates"),
                  "Stamp sequential Bates numbers on the pages")
    a_manage = act("decorations", "&Manage decorations…", lambda: manage(win),
                   "Edit or remove watermarks, headers, footers and Bates numbers")
    a_clear = act("remove_decorations", "&Remove all decorations", lambda: D.remove_all(cv),
                  "Remove all watermarks, headers, footers and Bates numbers added in Leaf")
    mine = [a_wm, a_hf, a_bates, a_manage, a_clear]

    menu = QMenu("&Document", win)
    menu.setObjectName("menu_document")
    mb = win.menuBar()
    pages = win.menus.get("pages")
    acts = mb.actions()
    after = None
    if pages is not None and pages.menuAction() in acts:
        i = acts.index(pages.menuAction())
        after = acts[i + 1] if i + 1 < len(acts) else None
    if after is not None:
        mb.insertMenu(after, menu)
    else:
        mb.addMenu(menu)
    win.menus["document"] = menu
    menu.addActions([a_wm, a_hf, a_bates])
    menu.addSeparator()
    menu.addActions([a_manage, a_clear])

    def sync(*_) -> None:
        has = cv.doc is not None
        for a in (a_wm, a_hf, a_bates, a_manage):
            a.setEnabled(has)
        a_clear.setEnabled(has and bool(D.decorations_of(cv.doc)))

    cv.pagesChanged.connect(sync)
    cv.documentChanged.connect(sync)
    cv.undo.indexChanged.connect(sync)
    sync()
