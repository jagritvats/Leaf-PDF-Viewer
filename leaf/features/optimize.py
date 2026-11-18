"""Optimize: Reduce file size… (presets, size breakdown, optimizer in a worker process with progress and cancel)
and page cropping (crop tool, Crop pages… dialog, auto-crop whitespace, remove crop)."""
from __future__ import annotations

import multiprocessing as mp
import os
import tempfile

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGraphicsItem, QGraphicsPathItem, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import crop as K
from .. import optimize as O
from .. import osint, pageops
from ..document import PageRef

APP = "Leaf"
PDF_FILTER = "PDF files (*.pdf)"
UNITS = {"pt": 1.0, "mm": 72 / 25.4, "in": 72.0}
CATEGORIES = (("images", "Images"), ("fonts", "Fonts"), ("content", "Page content"), ("other", "Other"))


def _toast(win, text: str, undo: bool = False) -> None:
    try:
        if undo:
            win.toast(text, "Undo", win.canvas.undo.undo, 4000)
        else:
            win.toast(text)
    except Exception:  # noqa: BLE001 - fall back to the status bar
        win.statusBar().showMessage(text, 3000)


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _remove(*paths) -> None:
    for p in paths:
        if p:
            try:
                os.remove(p)
            except OSError:
                pass


# ============================================================ reduce file size
class OptimizeJob(QObject):
    """Runs `optimize.optimize_file` in a spawned process (frozen-safe: the packaged exe re-enters through
    multiprocessing.freeze_support). Falls back to running here if a process can't be started."""

    progress = Signal(float, str)
    finished = Signal(int)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.conn = None
        self.over = False
        self.timer = QTimer(self, interval=80, timeout=self._poll)

    def start(self, src: str, dst: str, opts: O.Options, password: str | None) -> None:
        self.over = False
        try:
            ctx = mp.get_context("spawn")
            self.conn, child = ctx.Pipe(duplex=False)
            self.proc = ctx.Process(target=O.run_worker, args=(child, src, dst, O.options_dict(opts), password),
                                    daemon=True, name="leaf-optimize")
            self.proc.start()
            child.close()
        except Exception:  # noqa: BLE001 - no worker: do it here (blocks the UI meanwhile)
            self.proc = self.conn = None
            QTimer.singleShot(0, lambda: self._run_here(src, dst, opts, password))
            return
        self.timer.start()

    def _run_here(self, src, dst, opts, password) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            size = O.optimize_file(src, dst, opts, password, lambda f, t: self.progress.emit(f, t))
        except Exception as e:  # noqa: BLE001
            self._end()
            QApplication.restoreOverrideCursor()
            self.failed.emit(str(e) or type(e).__name__)
            return
        QApplication.restoreOverrideCursor()
        self._end()
        self.finished.emit(size)

    def _poll(self) -> None:
        if self.conn is None:
            return
        try:
            while self.conn.poll():
                msg = self.conn.recv()
                if msg[0] == "progress":
                    self.progress.emit(msg[1], msg[2])
                elif msg[0] == "done":
                    self._end()
                    self.finished.emit(msg[1])
                    return
                elif msg[0] == "err":
                    self._end()
                    self.failed.emit(msg[1])
                    return
        except (EOFError, OSError):
            pass
        if self.proc is not None and not self.proc.is_alive() and not self.over:
            code = self.proc.exitcode
            self._end()
            self.failed.emit(f"The optimizer stopped unexpectedly (exit code {code}).")

    def _end(self) -> None:
        self.over = True
        self.timer.stop()
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None
        if self.proc is not None:
            self.proc.join(0.5)
        self.proc = None

    def cancel(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(3)
        self._end()

    @property
    def running(self) -> bool:
        return self.proc is not None and not self.over


class SizeBars(QWidget):
    """Per-category size breakdown: label, bar, size."""

    def __init__(self, parts: dict[str, int], total: int):
        super().__init__()
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(4)
        for row, (key, name) in enumerate(CATEGORIES):
            n = parts.get(key, 0)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setRange(0, 1000)
            bar.setValue(int(1000 * n / max(total, 1)))
            bar.setFixedHeight(8)
            g.addWidget(QLabel(name), row, 0)
            g.addWidget(bar, row, 1)
            size = QLabel(f"{O.human(n)}  ({100 * n / max(total, 1):.0f}%)")
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(size, row, 2)
        g.setColumnStretch(1, 1)


class OptimizeDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cv = win.canvas
        doc = self.cv.doc
        self.setWindowTitle("Reduce File Size")
        self.job = OptimizeJob(self)
        self.job.progress.connect(self._progress)
        self.job.finished.connect(self._done)
        self.job.failed.connect(self._failed)
        self.tmp_in = self.tmp_out = self.target = None
        self.before = 0

        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.addWidget(self.stack)

        # ---- page 0: settings
        setup = QWidget()
        v = QVBoxLayout(setup)
        v.setContentsMargins(0, 0, 0, 0)
        primary = doc.primary
        total = len(primary.data)
        try:
            parts = O.breakdown(primary.doc, total)
        except Exception:  # noqa: BLE001 - the breakdown is informative only
            parts = {"other": total}
        box = QGroupBox(f"{primary.name}  ·  {O.human(total)}")
        bl = QVBoxLayout(box)
        bl.addWidget(SizeBars(parts, total))
        if len(doc.sources) > 1 or not self.cv.undo.isClean():
            note = QLabel("The copy includes your unsaved changes and inserted pages, as Save would write them.")
            note.setWordWrap(True)
            note.setEnabled(False)
            bl.addWidget(note)
        v.addWidget(box)

        pbox = QGroupBox("Preset")
        pl = QVBoxLayout(pbox)
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for key, (name, desc, _) in O.PRESETS.items():
            self._add_preset(pl, key, name, desc)
        self._add_preset(pl, "custom", "Custom", "Choose each setting below.")
        v.addWidget(pbox)

        self.custom = QGroupBox("Settings")
        f = QFormLayout(self.custom)
        self.c_images = QCheckBox("Downsample and recompress images")
        f.addRow(self.c_images)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 1200)
        self.dpi.setSuffix(" dpi")
        self.threshold = QSpinBox()
        self.threshold.setRange(0, 2400)
        self.threshold.setSuffix(" dpi")
        self.threshold.setSpecialValueText("auto (1.5 × target)")
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        f.addRow("Target resolution:", self.dpi)
        f.addRow("Only images above:", self.threshold)
        f.addRow("JPEG quality:", self.quality)
        self.c_lossless = QCheckBox("Also re-encode lossless images (PNG-like) as JPEG")
        f.addRow(self.c_lossless)
        self.c_gray = QCheckBox("Convert the whole document to grayscale")
        self.c_fonts = QCheckBox("Subset embedded fonts (skipped for fillable forms)")
        self.c_clean = QCheckBox("Compact page content, drop unused fonts and resources")
        self.c_thumbs = QCheckBox("Remove embedded page thumbnails")
        self.c_meta = QCheckBox("Remove metadata (title, author, XMP)")
        self.c_files = QCheckBox("Remove attached files")
        for c in (self.c_gray, self.c_fonts, self.c_clean, self.c_thumbs, self.c_meta, self.c_files):
            f.addRow(c)
        self.c_images.toggled.connect(self._sync_enabled)
        v.addWidget(self.custom)

        self.sig_note = QLabel()
        self.sig_note.setWordWrap(True)
        if O.has_signatures(primary.doc):
            self.sig_note.setText("This document is digitally signed. The optimized copy is rewritten, so its "
                                  "signatures will no longer be valid.")
            self.sig_note.setStyleSheet("color: palette(link-visited);")
        else:
            self.sig_note.hide()
        v.addWidget(self.sig_note)
        if primary.doc.metadata.get("encryption") or doc.security is not None:
            enc = QLabel("Password protection and permissions are kept.")
            enc.setEnabled(False)
            v.addWidget(enc)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.go = bb.addButton("Save optimized copy as…", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setDefault(True)
        bb.accepted.connect(self._start)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.stack.addWidget(setup)

        # ---- page 1: running
        run = QWidget()
        rl = QVBoxLayout(run)
        self.status = QLabel("Preparing…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        rl.addStretch(1)
        rl.addWidget(self.status)
        rl.addWidget(self.bar)
        rl.addStretch(1)
        rb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        rb.rejected.connect(self._cancel)
        rl.addWidget(rb)
        self.stack.addWidget(run)

        # ---- page 2: result
        res = QWidget()
        xl = QVBoxLayout(res)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        xl.addStretch(1)
        xl.addWidget(self.result)
        xl.addStretch(1)
        xb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.open_btn = xb.addButton("Open optimized file", QDialogButtonBox.ButtonRole.ActionRole)
        self.folder_btn = xb.addButton("Show folder", QDialogButtonBox.ButtonRole.ActionRole)
        self.open_btn.clicked.connect(self._open_result)
        self.folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.target))))
        xb.rejected.connect(self.accept)
        xl.addWidget(xb)
        self.stack.addWidget(res)

        last = str(win.settings.value("optimize/preset", "ebook"))
        self.radios.get(last, self.radios["ebook"]).setChecked(True)
        self._preset_changed()
        self.setMinimumWidth(460)

    def _add_preset(self, layout, key: str, name: str, desc: str) -> None:
        r = QRadioButton(name)
        r.setToolTip(desc)
        r.setProperty("preset", key)
        self.group.addButton(r)
        self.radios[key] = r
        row = QHBoxLayout()
        row.addWidget(r)
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setEnabled(False)
        d.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(d, 1)
        layout.addLayout(row)
        r.toggled.connect(lambda on: on and self._preset_changed())

    def preset_key(self) -> str:
        b = self.group.checkedButton()
        return b.property("preset") if b is not None else "ebook"

    def _preset_changed(self) -> None:
        key = self.preset_key()
        if key != "custom":
            self._show(O.preset(key))
        self.custom.setEnabled(key == "custom")
        self._sync_enabled()

    def _show(self, o: O.Options) -> None:
        self.c_images.setChecked(o.images)
        self.dpi.setValue(o.dpi)
        self.threshold.setValue(o.threshold)
        self.quality.setValue(o.quality)
        self.c_lossless.setChecked(o.lossless_images)
        self.c_gray.setChecked(o.gray)
        self.c_fonts.setChecked(o.subset_fonts)
        self.c_clean.setChecked(o.clean)
        self.c_thumbs.setChecked(o.strip_thumbnails)
        self.c_meta.setChecked(o.strip_metadata)
        self.c_files.setChecked(o.strip_embedded)

    def _sync_enabled(self) -> None:
        on = self.c_images.isChecked() and self.preset_key() == "custom"
        for w in (self.dpi, self.threshold, self.quality, self.c_lossless):
            w.setEnabled(on)

    def options(self) -> O.Options:
        key = self.preset_key()
        if key != "custom":
            return O.preset(key)
        return O.Options(
            images=self.c_images.isChecked(), dpi=self.dpi.value(), threshold=self.threshold.value(),
            quality=self.quality.value(), lossless_images=self.c_lossless.isChecked(), gray=self.c_gray.isChecked(),
            subset_fonts=self.c_fonts.isChecked(), strip_metadata=self.c_meta.isChecked(),
            strip_thumbnails=self.c_thumbs.isChecked(), strip_embedded=self.c_files.isChecked(),
            clean=self.c_clean.isChecked(),
        )

    # ---------------------------------------------------------------- run
    def _start(self) -> None:
        doc = self.cv.doc
        base = os.path.splitext(doc.path or os.path.join(os.path.expanduser("~"), "document"))[0]
        path, _ = QFileDialog.getSaveFileName(self, "Save Optimized Copy As", base + "-optimized.pdf", PDF_FILTER)
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if _same_file(path, doc.path):
            QMessageBox.information(self, APP, "Choose a different file name: the optimized copy never replaces "
                                               "the open document.")
            return
        self.win.settings.setValue("optimize/preset", self.preset_key())
        self.target = os.path.abspath(path)
        self.win._commit_edits()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fd, self.tmp_in = tempfile.mkstemp(prefix="leaf-opt-in-", suffix=".pdf")
            os.close(fd)
            doc.save(self.tmp_in, self.cv.overlays_for)  # exactly what Save would write
            fd, self.tmp_out = tempfile.mkstemp(prefix=".leaf-opt-", suffix=".pdf", dir=os.path.dirname(self.target))
            os.close(fd)
        except Exception as ex:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            self._cleanup()
            QMessageBox.critical(self, APP, f"Couldn't prepare the copy:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        self.before = os.path.getsize(self.tmp_in)
        self.stack.setCurrentIndex(1)
        self.status.setText("Starting…")
        self.bar.setValue(0)
        osint.progress(self.win, 0.0, key="optimize")
        self.job.start(self.tmp_in, self.tmp_out, self.options(), doc.output_password())

    def _progress(self, f: float, text: str) -> None:
        self.bar.setValue(int(f * 100))
        osint.progress(self.win, f, key="optimize")
        self.status.setText(text)

    def _done(self, size: int) -> None:
        try:
            os.replace(self.tmp_out, self.target)
        except OSError as ex:
            self._cleanup()
            self._failed(f"Couldn't write {os.path.basename(self.target)}:\n{ex}")
            return
        self.tmp_out = None
        self._cleanup()
        name = os.path.basename(self.target)
        if size >= self.before:
            how = "It was already about as small as these settings allow; the copy is unchanged in size."
        else:
            how = f"<b>{O.human(self.before)} → {O.human(size)}</b>  ({100 * (self.before - size) / self.before:.0f}% smaller)"
        self.result.setText(f"Saved <b>{name}</b><br><br>{how}")
        self.stack.setCurrentIndex(2)
        self.open_btn.setFocus()

    def _failed(self, msg: str) -> None:
        self._cleanup()
        self.stack.setCurrentIndex(0)
        QMessageBox.critical(self, APP, f"Couldn't reduce the file size:\n{msg}\n\nNothing was written.")

    def _cancel(self) -> None:
        self.job.cancel()
        self._cleanup()
        self.stack.setCurrentIndex(0)

    def _cleanup(self) -> None:
        osint.progress(self.win, None, key="optimize")
        _remove(self.tmp_in, self.tmp_out, self.tmp_out and self.tmp_out + ".part")
        self.tmp_in = self.tmp_out = None

    def _open_result(self) -> None:
        target = self.target
        self.accept()
        if target:
            self.win.open_path(target)

    def reject(self) -> None:
        if self.job.running:
            self._cancel()
            return
        self._cleanup()
        super().reject()

    def closeEvent(self, e) -> None:
        if self.job.running:
            self.job.cancel()
        self._cleanup()
        super().closeEvent(e)


def reduce_size(win) -> None:
    if win.canvas.doc is None:
        return
    OptimizeDialog(win).exec()


# ==================================================================== cropping
def _target_rows(win) -> list[int]:
    return win._target_rows() if hasattr(win, "_target_rows") else [win.canvas.current_index()]


def _selected_rows(win) -> list[int]:
    view = win._page_view() if hasattr(win, "_page_view") else win.sidebar
    return view.selected_rows()


def rows_for(win, which: str, current: int) -> list[int]:
    n = len(win.canvas.doc.pages)
    if which == "current":
        return [current]
    if which == "selected":
        return _selected_rows(win) or [current]
    if which == "all":
        return list(range(n))
    if which == "odd":
        return list(range(0, n, 2))
    if which == "even":
        return list(range(1, n, 2))
    return pageops.parse_ranges(which, n)


def crop_rows_by_margins(win, rows, margins, text=None) -> int:
    doc = win.canvas.doc
    crops = {}
    for i in rows:
        ref = doc.pages[i]
        crops[ref] = K.crop_from_margins(doc, ref, margins)
    return K.set_crops(win.canvas, crops, text)


def auto_crop_rows(win, rows, pad: float = 6.0) -> int:
    """Trim each page to its own content (progress dialog for long runs)."""
    doc = win.canvas.doc
    crops = {}
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        for k, i in enumerate(rows):
            ref = doc.pages[i]
            try:
                c = K.auto_crop(doc, ref, pad)
            except Exception:  # noqa: BLE001 - a page that can't render keeps its crop
                continue
            if c is not None:
                crops[ref] = c
            if k % 8 == 7:
                QApplication.processEvents()
    finally:
        QApplication.restoreOverrideCursor()
    n = len(rows)
    return K.set_crops(win.canvas, crops, f"Auto-crop {n} pages" if n > 1 else "Auto-crop page")


def remove_crop(win, rows=None) -> int:
    doc = win.canvas.doc
    if doc is None:
        return 0
    rows = range(len(doc.pages)) if rows is None else rows
    return K.set_crops(win.canvas, {doc.pages[i]: None for i in rows})


class CropPreview(QWidget):
    """The uncropped page with the crop shown: the rest is dimmed."""

    def __init__(self):
        super().__init__()
        self.img: QImage | None = None
        self.full = (1.0, 1.0)
        self.margins = (0.0, 0.0, 0.0, 0.0)
        self.setMinimumSize(QSize(220, 260))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_page(self, doc, ref) -> None:
        full = PageRef(ref.src, ref.index, ref.rotation)  # the same page, uncropped
        self.full = doc.display_size(full)
        scale = 520 / max(self.full)
        try:
            pix, _ = doc.render(full, scale)
            self.img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        except Exception:  # noqa: BLE001
            self.img = None
        self.update()

    def set_margins(self, m) -> None:
        self.margins = tuple(m)
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        W, H = self.full
        s = min((self.width() - 8) / W, (self.height() - 8) / H)
        page = QRectF((self.width() - W * s) / 2, (self.height() - H * s) / 2, W * s, H * s)
        p.fillRect(page, Qt.GlobalColor.white)
        if self.img is not None:
            p.drawImage(page, self.img)
        l, t, r, b = self.margins
        keep = QRectF(page.left() + l * s, page.top() + t * s, max(0.0, (W - l - r) * s), max(0.0, (H - t - b) * s))
        path = QPainterPath()
        path.addRect(page)
        path.addRect(keep)
        p.fillPath(path, QColor(0, 0, 0, 110))
        pen = QPen(self.palette().color(QPalette.ColorRole.Highlight), 1.5)
        p.setPen(pen)
        p.drawRect(keep)
        p.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
        p.drawRect(page)


class CropDialog(QDialog):
    """Margins per side (pt/mm/in) or trim-to-content, for the current/selected/all/odd/even pages or a range."""

    def __init__(self, win, row: int):
        super().__init__(win)
        self.win = win
        self.doc = doc = win.canvas.doc
        self.row = row
        self.ref = doc.pages[row]
        self.setWindowTitle("Crop Pages")
        n = len(doc.pages)
        self.preview = CropPreview()
        self.preview.set_page(doc, self.ref)

        form = QFormLayout()
        self.unit = QComboBox()
        self.unit.addItems(list(UNITS))
        unit = str(win.settings.value("crop/unit", "mm"))
        self.unit.setCurrentText(unit if unit in UNITS else "mm")
        self._unit = self.unit.currentText()
        form.addRow("Units:", self.unit)
        self.spins: list[QDoubleSpinBox] = []
        W, H = K.full_size(doc, self.ref)
        for name in ("Left", "Top", "Right", "Bottom"):
            sp = QDoubleSpinBox()
            sp.setDecimals(2)
            sp.setRange(0, max(W, H))
            sp.valueChanged.connect(self._update)
            self.spins.append(sp)
        self._set_margins(K.margins_of(doc, self.ref))
        form.addRow("Top:", self.spins[1])
        form.addRow("Bottom:", self.spins[3])
        form.addRow("Left:", self.spins[0])
        form.addRow("Right:", self.spins[2])
        self.unit.currentTextChanged.connect(self._unit_changed)

        self.fit = QCheckBox("Trim each page to its own content (auto-crop whitespace)")
        self.pad = QDoubleSpinBox()
        self.pad.setRange(0, 200)
        self.pad.setValue(float(win.settings.value("crop/pad", 6.0)))
        self.pad.setSuffix(" pt")
        self.fit.toggled.connect(self._update)
        self.pad.valueChanged.connect(self._update)
        auto = QPushButton("Fit this page's content")
        auto.setToolTip("Fill in the margins from the content of the page shown")
        auto.clicked.connect(self._auto_margins)
        form.addRow(auto)
        form.addRow(self.fit)
        form.addRow("Padding:", self.pad)

        self.which = QComboBox()
        sel = len(_selected_rows(win))
        for text, key in (("Current page", "current"), (f"Selected pages ({sel})", "selected"), ("All pages", "all"),
                          ("Odd pages", "odd"), ("Even pages", "even"), ("Page range…", "range")):
            if key == "selected" and sel < 2:
                continue
            self.which.addItem(text, key)
        self.status = QLabel()
        from .pages import RangeEdit
        self.range = RangeEdit(n, f"{row + 1}", self.status)
        self.which.currentIndexChanged.connect(self._which_changed)
        form.addRow("Apply to:", self.which)
        form.addRow("Pages:", self.range)
        form.addRow("", self.status)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Crop", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setDefault(True)
        self.remove_btn = bb.addButton("Remove crop", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.removing = False
        self.remove_btn.clicked.connect(self._remove)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

        right = QVBoxLayout()
        right.addLayout(form)
        right.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.preview, 1)
        top.addLayout(right)
        lay = QVBoxLayout(self)
        lay.addLayout(top, 1)
        lay.addWidget(bb)
        self._which_changed()
        self._update()

    # margins are kept in points; spins show them in the chosen unit
    def margins(self) -> tuple:
        f = UNITS[self._unit]
        return tuple(sp.value() * f for sp in self.spins)

    def _set_margins(self, m) -> None:
        f = UNITS[self._unit]
        for sp, v in zip(self.spins, m):
            sp.blockSignals(True)
            sp.setValue(v / f)
            sp.blockSignals(False)

    def _unit_changed(self, unit: str) -> None:
        m = self.margins()
        self._unit = unit
        for sp in self.spins:
            sp.setDecimals(0 if unit == "pt" else (1 if unit == "mm" else 2))
            sp.setSingleStep(1 if unit != "in" else 0.05)
        W, H = K.full_size(self.doc, self.ref)
        for sp in self.spins:
            sp.setMaximum(max(W, H) / UNITS[unit])
        self._set_margins(m)
        self._update()

    def _auto_margins(self) -> None:
        r = K.auto_crop(self.doc, self.ref, self.pad.value())
        self._set_margins(K.margins_of(self.doc, self.ref, r) if r else (0, 0, 0, 0))
        self._update()

    def _which_changed(self, *_) -> None:
        self.range.setVisible(self.which.currentData() == "range")
        self.status.setVisible(self.which.currentData() == "range")

    def _update(self, *_) -> None:
        fit = self.fit.isChecked()
        for sp in self.spins:
            sp.setEnabled(not fit)
        self.pad.setEnabled(fit)
        if fit:
            r = K.auto_crop(self.doc, self.ref, self.pad.value())
            self.preview.set_margins(K.margins_of(self.doc, self.ref, r) if r else (0, 0, 0, 0))
        else:
            self.preview.set_margins(self.margins())
        W, H = K.full_size(self.doc, self.ref)
        l, t, r, b = self.margins()
        self.ok.setEnabled(fit or (W - l - r >= K.MIN_SIDE and H - t - b >= K.MIN_SIDE))

    def rows(self) -> list[int] | None:
        key = self.which.currentData()
        if key == "range":
            return self.range.rows()
        return rows_for(self.win, key, self.row)

    def _remove(self) -> None:
        self.removing = True
        self.accept()

    def accept(self) -> None:
        if self.rows() is None:
            self.range.setFocus()
            return
        self.win.settings.setValue("crop/unit", self._unit)
        self.win.settings.setValue("crop/pad", self.pad.value())
        super().accept()


def crop_dialog(win) -> None:
    cv = win.canvas
    if cv.doc is None or not cv.order:
        return
    dlg = CropDialog(win, cv.current_index())
    if not dlg.exec():
        return
    rows = dlg.rows() or []
    if dlg.removing:
        n = remove_crop(win, rows)
        if n:
            _toast(win, f"Removed the crop from {n} page(s)", undo=True)
        return
    if dlg.fit.isChecked():
        n = auto_crop_rows(win, rows, dlg.pad.value())
    else:
        n = crop_rows_by_margins(win, rows, dlg.margins())
    if n:
        _toast(win, f"Cropped {n} page(s)", undo=True)
    else:
        _toast(win, "Nothing to crop")


class CropTool(QObject):
    """The "crop" canvas tool: drag a rectangle on a page, then choose which pages get that crop.

    An event filter on the canvas viewport, so the canvas itself needs no crop-specific code."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cv = win.canvas
        self.drag = None  # (page item, start point in page.rect space)
        self.shade: QGraphicsPathItem | None = None
        self.cv.viewport().installEventFilter(self)
        self.cv.toolChanged.connect(self._tool_changed)

    def _tool_changed(self, tool: str) -> None:
        if tool == "crop":
            self.cv.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self._end()

    def eventFilter(self, obj, e) -> bool:
        cv = self.cv
        if cv.tool != "crop" or cv.doc is None or getattr(cv, "_space", False):
            return False
        t = e.type()
        if t == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton:
            sp = cv.mapToScene(e.position().toPoint())
            pg = cv.page_at(sp)
            if pg is not None:
                cv.setFocus()
                self.drag = (pg, self._clamped(pg, pg.content.mapFromScene(sp)))
                self._band(QRectF(self.drag[1], self.drag[1]))
            return True
        if t == QEvent.Type.MouseMove:
            if self.drag is not None:
                pg, start = self.drag
                p = self._clamped(pg, pg.content.mapFromScene(cv.mapToScene(e.position().toPoint())))
                self._band(QRectF(start, p).normalized())
            elif cv.viewport().cursor().shape() != Qt.CursorShape.CrossCursor:
                cv.viewport().setCursor(Qt.CursorShape.CrossCursor)
            return True
        if t == QEvent.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton:
            if self.drag is not None:
                pg, start = self.drag
                p = self._clamped(pg, pg.content.mapFromScene(cv.mapToScene(e.position().toPoint())))
                rect = QRectF(start, p).normalized()
                self._ask(pg, rect, e.globalPosition().toPoint())
            return True
        if t == QEvent.Type.MouseButtonDblClick:
            return True
        return False

    @staticmethod
    def _clamped(pg, p: QPointF) -> QPointF:
        r = pg.content.rect()
        return QPointF(min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom()))

    def _band(self, rect: QRectF) -> None:
        pg = self.drag[0]
        if self.shade is None or self.shade.parentItem() is not pg.content:
            self._drop_band()
            self.shade = QGraphicsPathItem(pg.content)
            self.shade.setZValue(90)
            self.shade.setBrush(QColor(0, 0, 0, 100))
            pen = QPen(self.cv.palette().color(QPalette.ColorRole.Highlight), 1.5)
            pen.setCosmetic(True)
            self.shade.setPen(pen)
            self.shade.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self.shade.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRect(pg.content.rect())
        path.addRect(rect)
        self.shade.setPath(path)

    def _drop_band(self) -> None:
        if self.shade is not None and self.shade.scene() is not None:
            self.shade.scene().removeItem(self.shade)
        self.shade = None

    def _end(self) -> None:
        self.drag = None
        self._drop_band()

    def _ask(self, pg, rect: QRectF, at) -> None:
        win, doc = self.win, self.cv.doc
        ref = pg.ref
        crop = K.clamp(doc.page_rect(ref), (rect.left(), rect.top(), rect.right(), rect.bottom()))
        if crop is None or rect.width() < K.MIN_SIDE or rect.height() < K.MIN_SIDE:
            self._end()
            return
        row = doc.pages.index(ref)
        sel = [r for r in _selected_rows(win)]
        m = QMenu(self.cv)
        choices = [("Crop this page", [row])]
        if len(sel) > 1:
            choices.append((f"Crop selected pages ({len(sel)})", sel))
        n = len(doc.pages)
        if n > 1:
            choices.append((f"Crop all {n} pages", list(range(n))))
            choices.append(("Crop odd pages", list(range(0, n, 2))))
            if n > 2 or row % 2:
                choices.append(("Crop even pages", list(range(1, n, 2))))
        acts = {}
        for text, rows in choices:
            acts[m.addAction(text)] = rows
        m.addSeparator()
        m.addAction("Cancel")
        chosen = _exec_menu(m, at)
        self._end()
        rows = acts.get(chosen)
        if not rows:
            return
        if rows == [row]:
            k = K.set_crops(self.cv, {ref: crop})
        else:
            margins = K.margins_of(doc, ref, crop)
            crops = {doc.pages[i]: K.crop_from_margins(doc, doc.pages[i], margins) for i in rows}
            crops[ref] = crop
            k = K.set_crops(self.cv, crops)
        if k:
            _toast(win, f"Cropped {k} page(s)", undo=True)


def _exec_menu(menu: QMenu, at):
    """Show the crop choices (a seam for tests: a modal menu would block them)."""
    return menu.exec(at)


# ===================================================================== install
def _action(win, name: str, text: str, slot, tip: str | None = None, checkable: bool = False) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    a.setCheckable(checkable)
    a.setProperty("base_tip", tip or text.replace("&", "").rstrip("…"))
    a.setToolTip(tip or text.replace("&", "").rstrip("…"))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def install(win) -> None:
    cv = win.canvas

    a_reduce = _action(win, "reduce_size", "Reduce file si&ze…", lambda: reduce_size(win),
                       "Save a smaller copy: recompress images, subset fonts, remove unused objects")
    a_crop_tool = _action(win, "crop_tool", "C&rop tool", lambda: cv.set_tool("crop"),
                          "Drag a rectangle on a page to crop it  (C)", checkable=True)
    a_crop = _action(win, "crop_pages", "&Crop pages…", lambda: crop_dialog(win),
                     "Crop pages by margins, or trim them to their content")
    a_auto = _action(win, "auto_crop", "&Auto-crop whitespace",
                     lambda: _auto_crop_action(win), "Trim the white margins of the selected pages (or this page)")
    a_uncrop = _action(win, "remove_crop", "Remove cr&op", lambda: _remove_crop_action(win),
                       "Show the selected pages (or this page) uncropped again")

    # crop is a canvas tool: single key C (if free), exclusive with the other tools, status-bar hint
    tool_keys = getattr(cv, "tool_keys", None)
    if isinstance(tool_keys, dict) and Qt.Key.Key_C not in tool_keys:
        tool_keys[Qt.Key.Key_C] = "crop"
    group = getattr(win, "tool_group", None)
    if group is not None:
        group.addAction(a_crop_tool)
    tools = getattr(win, "tool_actions", None)
    if isinstance(tools, dict):
        tools["crop"] = a_crop_tool
    try:
        from .. import window as W
        W.HINTS.setdefault("crop", "Drag a rectangle on a page, then choose which pages to crop. Esc to finish.")
    except Exception:  # noqa: BLE001
        pass
    win.crop_tool = CropTool(win)

    menus = getattr(win, "menus", {})
    pm = menus.get("pages")
    if pm is not None:
        pm.addSeparator()
        pm.addActions([a_crop_tool, a_crop, a_auto, a_uncrop])
    dm = menus.get("document")
    if dm is not None:
        dm.addSeparator()
        dm.addAction(a_reduce)
    fm = menus.get("file")
    if fm is not None:  # after the Save group
        items = fm.actions()
        save_as = getattr(win, "a_save_as", None)
        before = None
        if save_as in items:
            k = items.index(save_as) + 1
            while k < len(items) and not items[k].isSeparator():
                k += 1
            before = items[k] if k < len(items) else None
        if before is not None:
            fm.insertAction(before, a_reduce)
        else:
            fm.addAction(a_reduce)

    def sync(*_) -> None:
        has = cv.doc is not None
        for a in (a_reduce, a_crop_tool, a_crop, a_auto):
            a.setEnabled(has)
        a_uncrop.setEnabled(has and any(r.crop is not None for r in cv.doc.pages))

    cv.pagesChanged.connect(sync)
    cv.documentChanged.connect(sync)
    sync()


def _auto_crop_action(win) -> None:
    if win.canvas.doc is None:
        return
    n = auto_crop_rows(win, _target_rows(win), float(win.settings.value("crop/pad", 6.0)))
    _toast(win, f"Cropped {n} page(s) to their content" if n else "No white margins to trim", undo=bool(n))


def _remove_crop_action(win) -> None:
    doc = win.canvas.doc
    if doc is None:
        return
    rows = [i for i in _target_rows(win) if doc.pages[i].crop is not None]
    if not rows:  # nothing selected is cropped: offer the whole document
        rows = [i for i, r in enumerate(doc.pages) if r.crop is not None]
    n = remove_crop(win, rows)
    if n:
        _toast(win, f"Removed the crop from {n} page(s)", undo=True)
