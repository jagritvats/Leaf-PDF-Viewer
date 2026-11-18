"""Printing: what Save would write (built into a temp file by the normal save pipeline), rendered page by page
at the printer's resolution (capped) onto a QPrinter. Also drives the print preview."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, replace

import pymupdf
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPageLayout, QPainter, QPaintEngine
from PySide6.QtPrintSupport import (
    QAbstractPrintDialog, QPageSetupDialog, QPrintDialog, QPrinter, QPrinterInfo, QPrintPreviewDialog,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QRadioButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from . import osint, pageops

MAX_DPI = 600
BAND_PIXELS = 6_000_000  # render tall pages in horizontal bands of at most this many pixels
PDF_FILE = "\x00pdf"  # printer combo entry: write a PDF file instead


@dataclass
class PrintOptions:
    rows: list[int] | None = None  # rows of the output (= Leaf's page list); None = all
    copies: int = 1
    collate: bool = True
    fit: bool = True  # fit to the printable area; False = actual size (centered, may clip)
    auto_rotate: bool = True  # turn landscape pages on portrait paper (and vice versa)
    annotations: bool = True  # annotations, form values and Leaf's overlays
    grayscale: bool = False
    max_dpi: int = MAX_DPI
    preview_dpi: int = 150  # cap while painting an on-screen preview

    def page_sequence(self, n: int) -> list[int]:
        rows = list(range(n)) if self.rows is None else [r for r in self.rows if 0 <= r < n]
        copies = max(1, self.copies)
        if self.collate:
            return rows * copies
        return [r for r in rows for _ in range(copies)]


def placement(page_w: float, page_h: float, area_w: float, area_h: float, res: float, fit: bool,
              auto_rotate: bool) -> tuple[float, bool, QRectF]:
    """(device pixels per point, rotated?, target rect of the page as placed on the paper, in device pixels relative
    to the printable area's top-left)."""
    rotate = auto_rotate and abs(page_w - page_h) > 0.5 and (page_w > page_h) != (area_w > area_h)
    w, h = (page_h, page_w) if rotate else (page_w, page_h)
    s = min(area_w / w, area_h / h) if fit else res / 72.0
    tw, th = w * s, h * s
    return s, rotate, QRectF((area_w - tw) / 2, (area_h - th) / 2, tw, th)


class PrintJob:
    """The document as Save would write it, in a temp file, and the code to print it."""

    def __init__(self, doc, overlays_for, annotations: bool = True):
        self.doc, self.overlays_for, self.annotations = doc, overlays_for, annotations
        self._dir: str | None = None
        self._out: pymupdf.Document | None = None

    def output(self) -> pymupdf.Document:
        if self._out is None:
            self._dir = tempfile.mkdtemp(prefix="leaf-print-")
            path = os.path.join(self._dir, "print.pdf")
            # without annotations, redaction marks still apply (printing must never show redacted content)
            ovs = self.overlays_for if self.annotations else (
                lambda ref: [o for o in self.overlays_for(ref) if getattr(o, "redact", False)])
            self.doc.save(path, ovs, fast=True)
            out = pymupdf.open(path)
            if out.needs_pass:
                out.authenticate(self.doc.output_password() or "")
            self._out = out
        return self._out

    def page_count(self) -> int:
        return len(self.doc.pages)

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- printing
    def run(self, printer: QPrinter, opts: PrintOptions, progress=None) -> int:
        """Print onto `printer` (or a preview's printer). `progress(done, total) -> bool` returns False to cancel.
        Returns the number of sheets painted."""
        out = self.output()
        seq = opts.page_sequence(out.page_count)
        if opts.grayscale:
            printer.setColorMode(QPrinter.ColorMode.GrayScale)
        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Couldn't start the print job.")
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if painter.paintEngine().type() == QPaintEngine.Type.Picture:  # a print preview keeps every page in memory
            opts = replace(opts, max_dpi=min(opts.max_dpi, opts.preview_dpi))
        done = 0
        try:
            for k, row in enumerate(seq):
                if progress is not None and not progress(k, len(seq)):
                    printer.abort()
                    break
                if k:
                    printer.newPage()
                self._draw(painter, printer, out[row], opts)
                done += 1
            if progress is not None and done == len(seq):
                progress(done, len(seq))
        finally:
            painter.end()
        return done

    def _draw(self, painter: QPainter, printer: QPrinter, page: pymupdf.Page, opts: PrintOptions) -> None:
        res = printer.resolution()
        area = printer.pageLayout().paintRectPixels(res)
        pr = page.rect
        s, rotate, target = placement(pr.width, pr.height, area.width(), area.height(), res, opts.fit,
                                      opts.auto_rotate)
        z = s * min(1.0, opts.max_dpi / res)  # render scale (pixels per point)
        W, H = pr.width * s, pr.height * s  # the page in device pixels, before rotation
        cs = pymupdf.csGRAY if opts.grayscale else pymupdf.csRGB
        painter.save()
        painter.setClipRect(QRectF(0, 0, area.width(), area.height()))
        painter.translate(target.center())
        if rotate:
            painter.rotate(-90)  # landscape content reads with the sheet turned clockwise
        px_w = max(1, int(round(pr.width * z)))
        px_h = max(1, int(round(pr.height * z)))
        band = max(64, BAND_PIXELS // px_w)
        m = pymupdf.Matrix(z, z)
        for y in range(0, px_h, band):
            clip = pymupdf.Rect(pr.x0, pr.y0 + y / z, pr.x1, pr.y0 + min(px_h, y + band) / z)
            pix = page.get_pixmap(matrix=m, clip=clip, colorspace=cs, alpha=False, annots=opts.annotations)
            fmt = QImage.Format.Format_Grayscale8 if pix.n == 1 else QImage.Format.Format_RGB888
            buf = pix.samples
            img = QImage(buf, pix.width, pix.height, pix.stride, fmt)
            k = s / z
            painter.drawImage(QRectF(-W / 2 + pix.x * k, -H / 2 + pix.y * k, pix.width * k, pix.height * k), img)
            del img, buf, pix
        painter.restore()


# ------------------------------------------------------------------ UI
def _settings(win):
    return getattr(win, "settings", None)


def _flag(st, key: str, default: bool) -> bool:
    if st is None:
        return default
    v = st.value(f"print/{key}", default)
    return v in (True, "true", "1", 1)


class PrintOptionsWidget(QWidget):
    """Page range, copies, sizing and content options (used by the Print dialog)."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        cv = win.canvas
        self.n = len(cv.doc.pages)
        self.current = max(0, cv.current_index())
        sb = getattr(win, "sidebar", None)
        self.selection = sb.selected_rows() if sb is not None else []
        st = _settings(win)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        pages = QGroupBox("Pages")
        g = QVBoxLayout(pages)
        self.range_group = QButtonGroup(self)
        self.r_all = QRadioButton(f"All ({self.n})")
        self.r_current = QRadioButton(f"Current page ({self.current + 1})")
        self.r_sel = QRadioButton(f"Selected pages ({pageops.format_ranges(self.selection) if self.selection else 'none'})")
        self.r_sel.setEnabled(bool(self.selection))
        self.r_custom = QRadioButton("Pages:")
        from .features.pages import RangeEdit

        self.range_status = QLabel()
        self.range_edit = RangeEdit(self.n, "", self.range_status)
        self.range_edit.textEdited.connect(lambda _: self.r_custom.setChecked(True))
        for b in (self.r_all, self.r_current, self.r_sel, self.r_custom):
            self.range_group.addButton(b)
        self.r_all.setChecked(True)
        g.addWidget(self.r_all)
        g.addWidget(self.r_current)
        g.addWidget(self.r_sel)
        row = QHBoxLayout()
        row.addWidget(self.r_custom)
        row.addWidget(self.range_edit, 1)
        g.addLayout(row)
        g.addWidget(self.range_status)
        lay.addWidget(pages)

        copies = QHBoxLayout()
        copies.addWidget(QLabel("Copies:"))
        self.copies = QSpinBox()
        self.copies.setRange(1, 999)
        copies.addWidget(self.copies)
        self.collate = QCheckBox("Collate")
        self.collate.setChecked(True)
        self.collate.setToolTip("Print complete sets (1 2 3, 1 2 3) instead of 1 1, 2 2, 3 3")
        copies.addWidget(self.collate)
        copies.addStretch(1)
        lay.addLayout(copies)

        size = QGroupBox("Page sizing")
        sg = QVBoxLayout(size)
        self.fit = QRadioButton("Fit to printable area")
        self.actual = QRadioButton("Actual size")
        (self.fit if _flag(st, "fit", True) else self.actual).setChecked(True)
        self.auto_rotate = QCheckBox("Auto-rotate pages to match the paper")
        self.auto_rotate.setChecked(_flag(st, "auto_rotate", True))
        sg.addWidget(self.fit)
        sg.addWidget(self.actual)
        sg.addWidget(self.auto_rotate)
        lay.addWidget(size)

        content = QGroupBox("Content")
        cg = QVBoxLayout(content)
        self.annotations = QCheckBox("Annotations and form fields")
        self.annotations.setChecked(True)
        self.annotations.setToolTip("Include annotations, form field values and everything added in Leaf")
        self.grayscale = QCheckBox("Print in grayscale")
        self.grayscale.setChecked(_flag(st, "grayscale", False))
        cg.addWidget(self.annotations)
        cg.addWidget(self.grayscale)
        lay.addWidget(content)

    def rows(self) -> list[int] | None:
        if self.r_current.isChecked():
            return [self.current]
        if self.r_sel.isChecked():
            return list(self.selection)
        if self.r_custom.isChecked():
            return self.range_edit.rows()  # None if invalid
        return list(range(self.n))

    def options(self) -> PrintOptions | None:
        rows = self.rows()
        if not rows:
            return None
        return PrintOptions(rows=rows, copies=self.copies.value(), collate=self.collate.isChecked(),
                            fit=self.fit.isChecked(), auto_rotate=self.auto_rotate.isChecked(),
                            annotations=self.annotations.isChecked(), grayscale=self.grayscale.isChecked())

    def remember(self, st) -> None:
        if st is None:
            return
        st.setValue("print/fit", self.fit.isChecked())
        st.setValue("print/auto_rotate", self.auto_rotate.isChecked())
        st.setValue("print/grayscale", self.grayscale.isChecked())


def make_printer(name: str | None = None) -> QPrinter:
    if name and name != PDF_FILE:
        info = QPrinterInfo.printerInfo(name)
        if not info.isNull():
            return QPrinter(info, QPrinter.PrinterMode.HighResolution)
    p = QPrinter(QPrinter.PrinterMode.HighResolution)
    if name == PDF_FILE:
        p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    return p


class PrintDialog(QDialog):
    """Printer, page setup, the options widget; Preview… and Print."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Print")
        st = _settings(win)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        prow = QHBoxLayout()
        self.printer_box = QComboBox()
        names = QPrinterInfo.availablePrinterNames()
        for n in names:
            self.printer_box.addItem(n, n)
        self.printer_box.addItem("PDF file…", PDF_FILE)
        want = (st.value("print/printer", "") if st is not None else "") or QPrinterInfo.defaultPrinterName()
        i = self.printer_box.findData(want)
        self.printer_box.setCurrentIndex(i if i >= 0 else 0)
        prow.addWidget(self.printer_box, 1)
        self.setup_btn = QPushButton("Page setup…")
        self.setup_btn.setToolTip("Paper size, orientation and margins")
        self.setup_btn.clicked.connect(self._page_setup)
        prow.addWidget(self.setup_btn)
        self.system_btn = QPushButton("Printer settings…")
        self.system_btn.setToolTip("The system print dialog (printer preferences, duplex, trays)")
        self.system_btn.clicked.connect(self._system_dialog)
        prow.addWidget(self.system_btn)
        form.addRow("Printer:", prow)
        self.paper = QLabel()
        form.addRow("Paper:", self.paper)
        lay.addLayout(form)
        self.options_widget = PrintOptionsWidget(win, self)
        lay.addWidget(self.options_widget)
        self.error = QLabel()
        self.error.setStyleSheet("color: palette(link-visited);")
        self.error.hide()
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.print_btn = bb.addButton("Print", QDialogButtonBox.ButtonRole.AcceptRole)
        self.print_btn.setDefault(True)
        self.preview_btn = bb.addButton("Preview…", QDialogButtonBox.ButtonRole.ActionRole)
        self.preview_btn.clicked.connect(self._preview)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.printer: QPrinter = make_printer(self.printer_box.currentData())
        self.printer_box.currentIndexChanged.connect(self._printer_changed)
        self._show_paper()

    def _printer_changed(self) -> None:
        layout = self.printer.pageLayout()
        self.printer = make_printer(self.printer_box.currentData())
        self.printer.setPageOrientation(layout.orientation())
        self._show_paper()

    def _show_paper(self) -> None:
        lay = self.printer.pageLayout()
        o = "landscape" if lay.orientation() == QPageLayout.Orientation.Landscape else "portrait"
        self.paper.setText(f"{lay.pageSize().name()}, {o}")

    def _page_setup(self) -> None:
        QPageSetupDialog(self.printer, self).exec()
        self._show_paper()

    def _system_dialog(self) -> None:
        dlg = QPrintDialog(self.printer, self)
        dlg.setOption(QAbstractPrintDialog.PrintDialogOption.PrintPageRange, False)
        dlg.setOption(QAbstractPrintDialog.PrintDialogOption.PrintSelection, False)
        dlg.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, False)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            i = self.printer_box.findData(self.printer.printerName())
            if i >= 0:
                self.printer_box.blockSignals(True)
                self.printer_box.setCurrentIndex(i)
                self.printer_box.blockSignals(False)
            if self.printer.copyCount() > 1:
                self.options_widget.copies.setValue(self.printer.copyCount())
            self._show_paper()

    def _opts(self) -> PrintOptions | None:
        opts = self.options_widget.options()
        if opts is None:
            self.error.setText("Choose at least one valid page to print.")
            self.error.show()
        else:
            self.error.hide()
        return opts

    def _preview(self) -> None:
        opts = self._opts()
        if opts is not None:
            preview(self.win, opts, self.printer, parent=self)

    def _accept(self) -> None:
        opts = self._opts()
        if opts is None:
            return
        if self.printer_box.currentData() == PDF_FILE:
            base = os.path.splitext(self.win.canvas.doc.path or "Untitled")[0] + " (printed).pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Print to PDF file", base, "PDF files (*.pdf)")
            if not path:
                return
            self.printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            self.printer.setOutputFileName(path)
        st = _settings(self.win)
        if st is not None:
            st.setValue("print/printer", self.printer_box.currentData())
            self.options_widget.remember(st)
        self.opts = opts
        self.accept()


def print_allowed(win) -> bool:
    doc = win.canvas.doc
    if doc.security is None and not (doc.primary.doc.permissions & pymupdf.PDF_PERM_PRINT):
        QMessageBox.information(win, "Leaf", "The author of this document doesn't allow printing it.\n\n"
                                "Open it with its permissions password to print.")
        return False
    return True


def _job(win, annotations: bool) -> PrintJob | None:
    cv = win.canvas
    if hasattr(win, "_commit_edits"):
        win._commit_edits()
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        job = PrintJob(cv.doc, cv.overlays_for, annotations)
        job.output()
        return job
    except Exception as ex:  # noqa: BLE001
        QMessageBox.critical(win, "Leaf", f"Couldn't prepare the document for printing:\n{ex}")
        return None
    finally:
        QApplication.restoreOverrideCursor()


def print_with_progress(win, job: PrintJob, printer: QPrinter, opts: PrintOptions) -> int:
    total = len(opts.page_sequence(job.page_count()))
    prog = QProgressDialog("Printing…", "Cancel", 0, total, win)
    prog.setWindowTitle("Print")
    prog.setWindowModality(Qt.WindowModality.WindowModal)
    prog.setMinimumDuration(300)

    def step(done: int, n: int) -> bool:
        prog.setValue(done)
        osint.progress(win, done / max(n, 1), key="print")
        prog.setLabelText(f"Printing page {min(done + 1, n)} of {n}…")
        QApplication.processEvents()
        return not prog.wasCanceled()

    try:
        return job.run(printer, opts, step)
    finally:
        prog.close()
        osint.progress(win, None, key="print")


def print_document(win) -> None:
    """File › Print…"""
    if win.canvas.doc is None or not print_allowed(win):
        return
    dlg = PrintDialog(win)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    opts = dlg.opts
    job = _job(win, opts.annotations)
    if job is None:
        return
    try:
        n = print_with_progress(win, job, dlg.printer, opts)
    except Exception as ex:  # noqa: BLE001
        QMessageBox.critical(win, "Leaf", f"Printing failed:\n{ex}")
        return
    finally:
        job.close()
    if hasattr(win, "toast"):
        total = len(opts.page_sequence(len(win.canvas.doc.pages)))
        win.toast(f"Sent {n} page{'s' if n != 1 else ''} to the printer" if n == total else "Printing cancelled")


def preview_dpi(sheets: int) -> int:
    """Lower the preview resolution for long documents (the preview keeps every page in memory)."""
    return max(36, min(150, int(150 * (40 / max(40, sheets)) ** 0.5)))


def preview(win, opts: PrintOptions | None = None, printer: QPrinter | None = None, parent=None) -> None:
    """File › Print preview… (also the Print dialog's Preview button)."""
    if win.canvas.doc is None or not print_allowed(win):
        return
    if opts is None:
        st = _settings(win)
        opts = PrintOptions(fit=_flag(st, "fit", True), auto_rotate=_flag(st, "auto_rotate", True),
                            grayscale=_flag(st, "grayscale", False))
    job = _job(win, opts.annotations)
    if job is None:
        return
    printer = printer or make_printer(_settings(win).value("print/printer", "") if _settings(win) is not None else None)
    dlg = QPrintPreviewDialog(printer, parent or win)
    dlg.setWindowTitle("Print preview")
    opts = replace(opts, preview_dpi=preview_dpi(len(opts.page_sequence(job.page_count()))))
    dlg.paintRequested.connect(lambda p: job.run(p, opts))
    try:
        dlg.exec()
    finally:
        job.close()

