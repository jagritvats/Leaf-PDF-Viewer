"""OCR: make scanned pages searchable and selectable.

Document › Recognize text (OCR)… runs the engine (leaf/ocr.py) in a separate process (leaf/ocr_worker.py);
results arrive page by page as one undoable step ("Recognize text"), are immediately visible to selection,
copy and the find bar (Source.words), and become an invisible text layer when the document is saved.
A toast offers OCR once when an opened document looks scanned. View › Show recognized text tints the
recognized word boxes by confidence.

Nothing OCR-specific is imported at startup: leaf.ocr is cheap (PyMuPDF only) and the engines themselves
load inside the worker process.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPalette, QPen, QUndoCommand
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGraphicsItem, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSpinBox,
    QToolButton, QVBoxLayout, QWidget,
)

from .. import ocr as O
from .. import ocrdata, osint, pageops
from ..icons import icon

CHECK_PAGES = 6  # pages looked at when a document opens (scanned-document hint)


# ------------------------------------------------------------------ undo
class SetOcr(QUndoCommand):
    """Set/remove OCR results of pages. Pages arriving from one run merge into a single step while it is the
    latest one on the stack."""

    ID = 0x0C12

    def __init__(self, win, changes: dict, run: int = 0, text: str = "Recognize text"):
        super().__init__(text)
        self.win = win
        self.doc = win.canvas.doc
        self.run = run
        self.new = dict(changes)
        self.old = {uid: self.doc.ocr.get(uid) for uid in changes}
        self._label()

    def _label(self) -> None:
        if self.run:
            n = sum(1 for e in self.new.values() if e is not None)
            self.setText(f"Recognize text ({n} page{'s' if n != 1 else ''})")

    def _set(self, values: dict) -> None:
        for uid, e in values.items():
            self.doc.set_ocr(uid, e)
        _changed(self.win)

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)

    def id(self) -> int:
        return self.ID if self.run else -1

    def mergeWith(self, other) -> bool:
        if not isinstance(other, SetOcr) or other.run != self.run or other.doc is not self.doc:
            return False
        for uid, e in other.new.items():
            if uid not in self.old:
                self.old[uid] = other.old[uid]
            self.new[uid] = e
        self._label()
        return True


# --------------------------------------------------------- recognized-text view
class OcrBoxes(QGraphicsItem):
    """Tints recognized word boxes: green = confident, amber = unsure, red = doubtful, blue = no score."""

    def __init__(self, parent, words):
        super().__init__(parent)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setZValue(30)
        self.words = words
        self.boxes = []
        r = QRectF()
        for w in words:
            b = QRectF(w[0], w[1], w[2] - w[0], w[3] - w[1])
            c = w[7] if len(w) > 7 else -1
            col = (QColor(40, 110, 230) if c < 0 else QColor(30, 170, 80) if c >= 0.9
                   else QColor(235, 160, 0) if c >= 0.7 else QColor(220, 50, 40))
            self.boxes.append((b, col))
            r = r.united(b)
        self._rect = r

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-1, -1, 1, 1)

    def paint(self, p, opt, widget=None) -> None:
        for b, col in self.boxes:
            fill = QColor(col)
            fill.setAlpha(55)
            line = QColor(col)
            line.setAlpha(170)
            p.setPen(QPen(line, 0))
            p.setBrush(fill)
            p.drawRect(b)


def _changed(win) -> None:
    """The OCR layer changed: refresh the view tint and actions."""
    st = getattr(win, "_ocr", None)
    if st is not None:
        st.refresh_view()
        st.sync()


# ------------------------------------------------------------------ dialog
class OcrDialog(QDialog):
    """Language first; the engine follows it (Automatic = the best one installed for that language)."""

    def __init__(self, win, engines: list[O.EngineInfo]):
        super().__init__(win)
        self.setWindowTitle("Recognize text (OCR)")
        self.win = win
        self.engines = engines
        cv = win.canvas
        self.n = len(cv.doc.pages)
        st = win.settings
        lay = QVBoxLayout(self)
        intro = QLabel("Finds the text in scanned pages so you can search, select and copy it. "
                       "The text is saved invisibly behind the page image; the pages look the same. "
                       "Recognition runs on this computer.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        lay.addLayout(form)

        self.lang = QComboBox()
        self.lang.setEditable(True)
        self.lang.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lang.setToolTip("Combine languages with +, e.g. eng+hin (Tesseract)")
        self.add_langs = QPushButton("Add languages…")
        self.add_langs.setToolTip("Download Tesseract language data (only the language files are downloaded)")
        self.add_langs.clicked.connect(self._manage_languages)
        row = QHBoxLayout()
        row.addWidget(self.lang, 1)
        row.addWidget(self.add_langs)
        form.addRow("Language:", row)
        self.engine = QComboBox()
        self.engine_label = QLabel()
        er = QHBoxLayout()
        er.addWidget(self.engine, 1)
        er.addWidget(self.engine_label, 1)
        form.addRow("Engine:", er)
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        form.addRow("", self.notes)

        pages = QWidget()
        pl = QVBoxLayout(pages)
        pl.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        self.r_all = QRadioButton(f"All pages ({self.n})")
        self.r_cur = QRadioButton(f"Current page ({cv.current_index() + 1})")
        row = QHBoxLayout()
        self.r_range = QRadioButton("Pages:")
        self.range = QLineEdit()
        self.range.setPlaceholderText("e.g. 1-3, 7, 10-")
        self.range.textEdited.connect(lambda _: self.r_range.setChecked(True))
        row.addWidget(self.r_range)
        row.addWidget(self.range, 1)
        for b in (self.r_all, self.r_cur, self.r_range):
            self.group.addButton(b)
        self.r_all.setChecked(True)
        pl.addWidget(self.r_all)
        pl.addWidget(self.r_cur)
        pl.addLayout(row)
        self.skip = QCheckBox("Skip pages that already have text")
        self.skip.setChecked(True)
        self.skip.setToolTip("Pages with selectable text (or text recognized earlier) are left as they are")
        pl.addWidget(self.skip)
        form.addRow("Pages:", pages)

        self.dpi = QSpinBox()
        self.dpi.setRange(100, 600)
        self.dpi.setSingleStep(50)
        self.dpi.setSuffix(" dpi")
        self.dpi.setValue(int(st.value("ocr/dpi", O.DEFAULT_DPI)))
        self.dpi.setToolTip("Higher finds small print better but is slower. 300 dpi suits most scans.")
        form.addRow("Resolution:", self.dpi)

        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.addButton("Recognize", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_spec = None
        self._fill(str(st.value("ocr/language", "eng") or "eng"), str(st.value("ocr/engine", "auto") or "auto"))
        self.lang.currentIndexChanged.connect(self._update)
        self.lang.editTextChanged.connect(self._update)
        self.engine.currentIndexChanged.connect(self._update)

    # -------------------------------------------------------------- filling
    def _fill(self, language: str, engine: str) -> None:
        for w in (self.lang, self.engine):
            w.blockSignals(True)
        self.lang.clear()
        for code, label in O.language_choices(self.engines):
            self.lang.addItem(label, code)
        if language == "auto":  # older setting (RapidOCR's automatic mode)
            language = "eng"
        k = self.lang.findData(language)
        if k >= 0:
            self.lang.setCurrentIndex(k)
        else:
            self.lang.setEditText(language)
        self.engine.clear()
        self.engine.addItem("Automatic (best for the language)", "auto")
        for e in self.engines:
            self.engine.addItem(e.label, e.key)
        self.engine.setCurrentIndex(max(0, self.engine.findData(engine)))
        many = len(self.engines) > 1
        self.engine.setVisible(many)
        self.engine_label.setVisible(not many)
        for w in (self.lang, self.engine):
            w.blockSignals(False)
        self._update()

    def language(self) -> str:
        text = self.lang.currentText().strip()
        k = self.lang.findText(text)
        if k >= 0:
            return str(self.lang.itemData(k))
        k = self.lang.findData(text)
        return str(self.lang.itemData(k)) if k >= 0 else "+".join(O.split_language(text))

    def preferred(self) -> str:
        return str(self.engine.currentData() or "auto")

    def info(self) -> O.EngineInfo | None:
        return O.choose_engine(self.engines, self.language(), self.preferred())

    def _update(self, *_) -> None:
        lang = self.language()
        e = self.info()
        if e is None:
            missing = [p for p in O.split_language(lang) if not any(x.supports(p) for x in self.engines)]
            names = ", ".join(ocrdata.label(p) for p in missing) or lang or "this language"
            if missing:
                self.notes.setText(f"No language data for {names}. Use Add languages… to download it.")
            else:
                self.notes.setText("No engine reads this combination: Tesseract needs all its language files "
                                   "in one place (Add languages… downloads them together).")
            self.engine_label.setText("None for this language")
        else:
            pref = self.preferred()
            why = ""
            if pref != "auto" and pref != e.key:
                why = " (the chosen engine doesn't read this language)"
            self.engine_label.setText(e.label)
            self.notes.setText(f"Uses {e.label}{why}. {e.notes}".strip())
        self.ok.setEnabled(e is not None)

    def _manage_languages(self) -> None:
        dlg = LanguageDialog(self)
        dlg.exec()
        if dlg.changed:
            lang, pref = self.language(), self.preferred()
            if dlg.added and not O.choose_engine(self.engines, lang, pref):
                lang = dlg.added[-1]
            self.engines = O.available_engines() or self.engines
            self._fill(lang, pref)

    # -------------------------------------------------------------- pages
    def rows(self) -> list[int]:
        if self.r_cur.isChecked():
            return [self.win.canvas.current_index()]
        if self.r_range.isChecked():
            return pageops.parse_ranges(self.range.text(), self.n)
        return list(range(self.n))

    def _accept(self) -> None:
        try:
            rows = self.rows()
        except ValueError as ex:
            self.status.setText(str(ex))
            return
        if not rows:
            self.status.setText("No pages selected.")
            return
        lang = self.language()
        e = self.info()
        if e is None:
            self.status.setText(f"No installed engine reads {lang or 'this language'}.")
            return
        st = self.win.settings
        st.setValue("ocr/engine", self.preferred())
        st.setValue("ocr/language", lang)
        st.setValue("ocr/dpi", self.dpi.value())
        self.result_spec = dict(engine=e, language=lang, dpi=self.dpi.value(), rows=rows, skip=self.skip.isChecked(),
                                tessdata=e.tessdata_for(lang))
        self.accept()


# ------------------------------------------------------------ language data
class _Downloads(QObject):
    """Downloads language files one after another in a Python thread; signals arrive in the GUI thread."""

    progress = Signal(str, int, int)  # code, bytes so far, total (0: unknown)
    finished = Signal(str, str)  # code, error ("" = ok, "cancelled")
    all_done = Signal()

    def __init__(self, codes: list[str], folder: str, opener=None):
        super().__init__()
        self.codes, self.folder, self.opener = list(codes), folder, opener
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="leaf-tessdata", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        for code in self.codes:
            if self.stop.is_set():
                break
            try:
                ocrdata.download(code, self.folder, progress=lambda d, t, c=code: self.progress.emit(c, d, t),
                                 cancelled=self.stop.is_set, opener=self.opener)
                self.finished.emit(code, "")
            except ocrdata.Cancelled:
                self.finished.emit(code, "cancelled")
                break
            except ocrdata.DownloadError as e:
                self.finished.emit(code, str(e))
        self.all_done.emit()


class LanguageDialog(QDialog):
    """Add or remove Tesseract languages (tessdata_fast files in the user's data folder)."""

    opener = None  # tests: a fake urlopen

    def __init__(self, parent=None, folder: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("OCR languages")
        self.resize(540, 540)
        self.folder = folder or ocrdata.user_dir()
        self.changed = False
        self.added: list[str] = []
        self.job: _Downloads | None = None
        lay = QVBoxLayout(self)
        info = QLabel("Tick the languages to add, then Download. Only the language data files are downloaded "
                      "(1–15 MB each, from the Tesseract project's tessdata_fast on GitHub). "
                      "Your documents never leave this computer.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search languages or scripts")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        lay.addWidget(self.filter)
        self.list = QListWidget()
        self.list.itemChanged.connect(lambda *_: self._sync())
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        lay.addWidget(self.list, 1)
        where = QLabel(f"Saved in: {self.folder}")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        where.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay.addWidget(where)
        prow = QHBoxLayout()
        self.plabel = QLabel()
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(12)
        self.stop = QPushButton("Cancel download")
        self.stop.clicked.connect(self.cancel)
        prow.addWidget(self.plabel, 1)
        prow.addWidget(self.bar, 1)
        prow.addWidget(self.stop)
        lay.addLayout(prow)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.get = bb.addButton("Download", QDialogButtonBox.ButtonRole.ActionRole)
        self.remove = bb.addButton("Remove", QDialogButtonBox.ButtonRole.ActionRole)
        self.get.clicked.connect(self.download_checked)
        self.remove.clicked.connect(self.remove_current)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._show_progress(False)
        self._populate()

    # -------------------------------------------------------------- list
    def _populate(self) -> None:
        mine = set(ocrdata.languages_in(self.folder))
        shipped = set(ocrdata.languages_in(ocrdata.bundled_dir()))
        other = set(ocrdata.languages_in(O.find_tessdata()))
        self.list.blockSignals(True)
        self.list.clear()
        for code, name, script in sorted(ocrdata.LANGUAGES, key=lambda x: x[1].casefold()):
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, code)
            if code in mine and code != "eng":
                state = "installed"  # downloaded: removable
            elif code in shipped:
                state = "built in"
            elif code in other:
                state = "installed with Tesseract"
            else:
                state = "installed" if code in mine else ""
            it.setData(Qt.ItemDataRole.UserRole + 1, state)
            it.setText(f"{name} ({code})  ·  {script}" + (f"  —  {state}" if state else ""))
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if not state:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
                it.setCheckState(Qt.CheckState.Unchecked)
            it.setFlags(flags)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self._filter(self.filter.text())
        self._sync()

    def _filter(self, text: str) -> None:
        t = text.strip().casefold()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().casefold())

    def item(self, code: str) -> QListWidgetItem | None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == code:
                return self.list.item(i)
        return None

    def checked(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable and it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _removable(self) -> str | None:
        it = self.list.currentItem()
        if it is None or it.data(Qt.ItemDataRole.UserRole + 1) != "installed":
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def _sync(self) -> None:
        busy = self.job is not None
        n = len(self.checked())
        self.get.setText(f"Download ({n})" if n else "Download")
        self.get.setEnabled(n > 0 and not busy)
        self.remove.setEnabled(self._removable() is not None and not busy)
        self.list.setEnabled(not busy)

    def _show_progress(self, on: bool) -> None:
        for w in (self.plabel, self.bar, self.stop):
            w.setVisible(on)

    # -------------------------------------------------------------- actions
    def download_checked(self) -> None:
        codes = self.checked()
        if not codes or self.job is not None:
            return
        self.errors: list[str] = []
        self.job = _Downloads(codes, self.folder, self.opener)
        self.job.progress.connect(self._progress)
        self.job.finished.connect(self._one_done)
        self.job.all_done.connect(self._all_done)
        self.status.setText("")
        self.plabel.setText(f"Downloading {ocrdata.label(codes[0])}…")
        self.bar.setRange(0, 0)
        self._show_progress(True)
        self._sync()
        self.job.start()

    def _progress(self, code: str, done: int, total: int) -> None:
        mb = f"{done / 2**20:.1f}" + (f" of {total / 2**20:.1f} MB" if total else " MB")
        self.plabel.setText(f"Downloading {ocrdata.label(code)}: {mb}")
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)

    def _one_done(self, code: str, error: str) -> None:
        if not error:
            self.changed = True
            self.added.append(code)
        elif error != "cancelled":
            self.errors.append(error)

    def _all_done(self) -> None:
        job, self.job = self.job, None
        if job is not None:
            job.thread.join(1)
        self._show_progress(False)
        msgs = []
        if self.added:
            msgs.append("Added: " + ", ".join(ocrdata.NAMES.get(c, c) for c in self.added) + ".")
        msgs += self.errors
        self.status.setText("  ".join(msgs) or "Download cancelled.")
        self._populate()

    def cancel(self) -> None:
        if self.job is not None:
            self.job.stop.set()
            self.plabel.setText("Cancelling…")

    def remove_current(self) -> None:
        code = self._removable()
        if not code:
            return
        try:
            ocrdata.remove(code, self.folder)
        except (OSError, ocrdata.DownloadError) as e:
            self.status.setText(f"Couldn't remove {ocrdata.label(code)}: {e}")
            return
        self.changed = True
        self.added = [c for c in self.added if c != code]
        self.status.setText(f"Removed {ocrdata.NAMES.get(code, code)}.")
        self._populate()

    def done(self, r: int) -> None:  # Close / Esc while downloading: stop and wait for the thread
        job = self.job
        if job is not None:
            job.stop.set()
            job.thread.join(5)
            self._all_done()
        super().done(r)


# ------------------------------------------------------------- progress panel
class ProgressPanel(QWidget):
    def __init__(self, on_cancel):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(6)
        self.label = QLabel()
        self.bar = QProgressBar()
        self.bar.setFixedWidth(120)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(12)
        self.cancel = QToolButton()
        self.cancel.setIcon(icon("close"))
        self.cancel.setAutoRaise(True)
        self.cancel.setToolTip("Stop recognizing text (pages done so far are kept)")
        self.cancel.clicked.connect(on_cancel)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.cancel)

    def set_progress(self, done: int, total: int, text: str) -> None:
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        self.label.setText(text)


# --------------------------------------------------------------------- runs
class OcrState:
    """Per window: the running job (if any), the recognized-text view, actions."""

    _runs = 0

    def __init__(self, win):
        self.win = win
        self.proc = None
        self.doc = None
        self.keys: dict[int, tuple] = {}  # key -> (uid, src, index)
        self.files: list[str] = []
        self.total = self.done = self.found = self.skipped = 0
        self.errors: list[str] = []
        self.secs: list[float] = []
        self.peak_mb = 0.0
        self.run = 0
        self.spec: dict = {}
        self.t0 = 0.0
        self.timer = QTimer(win)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self._poll)
        self.panel = ProgressPanel(self.cancel)
        self.panel.hide()
        win.statusBar().addWidget(self.panel)
        self.view_items: dict[int, OcrBoxes] = {}
        self.show_boxes = False

    @property
    def running(self) -> bool:
        return self.proc is not None

    # ---------------------------------------------------------- start / stop
    def start(self, spec: dict) -> None:
        from ..ocr_worker import OcrProcess

        cv = self.win.canvas
        doc = cv.doc
        refs = [doc.pages[r] for r in spec["rows"]]
        if spec["skip"]:
            refs = [r for r in refs if not doc.ocr_words(r)]
        if not refs:
            self.win.toast("Those pages already have recognized text.")
            return
        sources: dict[int, tuple] = {}
        pages = []
        self.keys = {}
        seen = set()
        for k, r in enumerate(refs):
            if (r.src.id, r.index) in seen:  # duplicates share the result
                continue
            seen.add((r.src.id, r.index))
            if r.src.id not in sources:
                fd, path = tempfile.mkstemp(prefix="leaf-ocr-", suffix=".pdf")
                with os.fdopen(fd, "wb") as f:
                    f.write(r.src.data)
                self.files.append(path)
                sources[r.src.id] = (path, r.src.password)
            self.keys[k] = (r.uid, r.src, r.index)
            pages.append((k, r.src.id, r.index))
        e: O.EngineInfo = spec["engine"]
        OcrState._runs += 1
        self.run = OcrState._runs
        self.doc = doc
        self.spec = spec
        self.total, self.done, self.found, self.skipped = len(pages), 0, 0, 0
        self.errors, self.secs, self.peak_mb = [], [], 0.0
        self.t0 = time.monotonic()
        try:
            tessdata = spec.get("tessdata") or e.tessdata_for(spec["language"]) or e.tessdata
            self.proc = OcrProcess(e.key, tessdata, spec["language"], spec["dpi"], spec["skip"], sources, pages)
        except Exception as ex:  # noqa: BLE001
            self._cleanup()
            QMessageBox.warning(self.win, "Recognize text", f"Couldn't start text recognition:\n{ex}")
            return
        self.panel.set_progress(0, self.total, "Recognizing text: starting…")
        osint.progress(self.win, 0.0, key="ocr")
        self.panel.show()
        self.timer.start()
        self.sync()

    def cancel(self) -> None:
        if not self.running:
            return
        self._poll()  # take what has arrived
        if not self.running:
            return
        self._finish(cancelled=True)

    def shutdown(self) -> None:
        """Stop the worker and delete its temp files (safe while the window is being destroyed)."""
        p, self.proc = self.proc, None
        if p is not None:
            p.close()
        for f in self.files:
            try:
                os.remove(f)
            except OSError:
                pass
        self.files = []

    def _cleanup(self) -> None:
        self.timer.stop()
        self.shutdown()
        self.panel.hide()
        osint.progress(self.win, None, key="ocr")

    def _finish(self, cancelled: bool = False, fatal: str = "") -> None:
        self._cleanup()
        self.sync()
        if fatal:
            QMessageBox.warning(self.win, "Recognize text", fatal)
            return
        n = self.found
        speed = f"  ·  {sum(self.secs) / len(self.secs):.1f} s per page" if self.secs else ""
        if cancelled:
            msg = f"Stopped. Text recognized on {n} page{'s' if n != 1 else ''}."
        elif n:
            msg = f"Text recognized on {n} page{'s' if n != 1 else ''}{speed}. Save to keep it."
        elif self.skipped == self.done and self.done:
            msg = "Those pages already have text."
        else:
            msg = "No text found."
        if self.errors:
            msg += f"  {len(self.errors)} page(s) failed: {self.errors[0]}"
        self.win.toast(msg, ms=5000)

    def _poll(self) -> None:
        p = self.proc
        if p is None:
            self.timer.stop()
            return
        cv = self.win.canvas
        for msg in p.poll():
            kind = msg[0]
            if kind == "start":
                self.panel.set_progress(self.done, self.total,
                                        f"Recognizing text: page {self.done + 1} of {self.total}…")
                continue
            elif kind == "page":
                _, k, words, secs = msg
                self.done += 1
                self.secs.append(secs)
                uid, src, index = self.keys[k]
                if words and cv.doc is self.doc:
                    self.found += 1
                    entry = O.OcrPage(src, index, list(words), self.spec["engine"].key, self.spec["language"])
                    cv.undo.push(SetOcr(self.win, {uid: entry}, self.run))
            elif kind == "skip":
                self.done += 1
                self.skipped += 1
            elif kind == "error":
                self.done += 1
                self.errors.append(msg[2])
            elif kind == "fatal":
                self._finish(fatal=msg[1])
                return
            elif kind == "done":
                self.peak_mb = msg[1]
                self._finish()
                return
            self.panel.set_progress(self.done, self.total,
                                    f"Recognizing text: {self.done} of {self.total} pages")
            osint.progress(self.win, self.done / max(self.total, 1), key="ocr")

    def document_changed(self) -> None:
        if self.running and self.win.canvas.doc is not self.doc:
            self._cleanup()
        self.view_items.clear()  # the scene was cleared
        self.refresh_view()
        self.sync()

    # ---------------------------------------------------------- view tint
    def set_show(self, on: bool) -> None:
        self.show_boxes = on
        self.refresh_view()

    def refresh_view(self) -> None:
        cv = self.win.canvas
        doc = cv.doc
        for it in self.view_items.values():
            try:
                sc = it.scene()
                if sc is not None:
                    sc.removeItem(it)
                else:  # its page is out of the scene (deleted page): detach so an undo doesn't bring it back
                    it.setParentItem(None)
            except RuntimeError:  # already deleted with the scene (new document)
                pass
        self.view_items.clear()
        if not self.show_boxes or doc is None or not doc.ocr:
            return
        for pg in cv.order:
            words = doc.ocr_words(pg.ref)
            if words:
                self.view_items[pg.ref.uid] = OcrBoxes(pg.content, words)

    # ---------------------------------------------------------- actions
    def sync(self, *_) -> None:
        win = self.win
        doc = win.canvas.doc
        win.a_ocr.setEnabled(doc is not None and not self.running)
        win.a_ocr_remove.setEnabled(doc is not None and bool(doc.ocr) and not self.running)
        win.a_ocr_show.setEnabled(doc is not None)


def _scanned(doc) -> bool:
    """The first pages look like scans (most of them have no text but a page-sized image)."""
    refs = doc.pages[:CHECK_PAGES]
    if not refs:
        return False
    hits = sum(O.needs_ocr(doc.page(r)) for r in refs)
    return hits * 2 >= len(refs)


def recognize(win) -> None:
    cv = win.canvas
    if cv.doc is None:
        return
    st: OcrState = win._ocr
    if st.running:
        return
    engines = O.available_engines()
    if not engines:
        QMessageBox.information(win, "Recognize text", O.NOT_AVAILABLE)
        return
    dlg = OcrDialog(win, engines)
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_spec is None:
        return
    st.start(dlg.result_spec)


def remove_all(win) -> None:
    cv = win.canvas
    doc = cv.doc
    if doc is None or not doc.ocr:
        return
    cv.undo.push(SetOcr(win, {uid: None for uid in doc.ocr}, 0, "Remove recognized text"))
    win.toast("Recognized text removed.")


def install(win) -> None:
    cv = win.canvas

    def act(name: str, text: str, slot, tip: str, checkable: bool = False) -> QAction:
        a = QAction(text, win)
        a.setObjectName(f"a_{name}")
        a.setToolTip(tip)
        a.setProperty("base_tip", tip)
        a.setCheckable(checkable)
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        win.addAction(a)
        setattr(win, f"a_{name}", a)
        return a

    a_run = act("ocr", "Recognize &text (OCR)…", lambda: recognize(win),
                "Make scanned pages searchable and selectable (text recognition)")
    a_remove = act("ocr_remove", "Remove recogni&zed text", lambda: remove_all(win),
                   "Remove the text recognized in Leaf (not yet saved)")
    st = win._ocr = OcrState(win)
    a_show = act("ocr_show", "Show recogni&zed text", st.set_show,
                 "Tint the words found by text recognition (green: confident, amber/red: unsure)", True)

    menus = getattr(win, "menus", {})
    m = menus.get("document")
    if m is not None:
        m.addSeparator()
        m.addActions([a_run, a_remove])
    v = menus.get("view")
    if v is not None:
        v.addAction(a_show)

    def opened() -> None:
        st.document_changed()
        doc = cv.doc
        if doc is None or getattr(doc, "_ocr_offered", False):
            return
        doc._ocr_offered = True  # once per document
        QTimer.singleShot(600, lambda: _offer(doc))

    def _offer(doc) -> None:
        if cv.doc is not doc or doc.ocr or st.running:
            return
        try:
            scanned = _scanned(doc)
        except Exception:  # noqa: BLE001 - a hint only
            return
        if scanned:
            win.toast("This document looks scanned. Recognize its text to search and select it.",
                      "Recognize text…", lambda: recognize(win), ms=9000)

    cv.documentChanged.connect(opened)
    cv.pagesChanged.connect(lambda: st.refresh_view() if st.show_boxes else None)
    win.destroyed.connect(lambda *_: st.shutdown())
    st.sync()
