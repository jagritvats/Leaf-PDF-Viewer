"""Find & replace (Edit › Replace…, Ctrl+H): a replace row under the find bar, match options on the find bar.

- Match case / Whole word / Regular expression buttons on the find bar; while any is on (or the replace row is open)
  the find bar matches with leaf/replace.py over the text as shown (text edits applied) instead of MuPDF's search.
- Replace: the current hit. Replace all: every hit, as ONE undo step (ReplaceCommand: page text through
  commands.EditPageTexts, plus text boxes and form fields when those boxes are ticked).
- Replace all scans the pages in time slices behind a progress dialog. Cancel during the scan leaves the document
  untouched (nothing is applied until the scan is complete); the apply step that follows is one command and isn't
  cancellable. Lines the engine refuses are skipped and listed in a summary dialog that jumps to them.

Each replacement is an ordinary text edit (leaf/textedit.py) of the line that holds the hit, or of the edited
line/paragraph that holds it now; matches that cross lines are counted and skipped.
"""
from __future__ import annotations

import re
from time import perf_counter

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainterPath, QTextCursor, QUndoCommand
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QGraphicsPathItem, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressDialog, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from .. import commands as C
from .. import osint
from .. import replace as R
from .. import textedit as T
from ..icons import icon

SLICE = 0.04  # s of work per event-loop turn while scanning


# ------------------------------------------------------------------------------------------------- command
class ReplaceCommand(QUndoCommand):
    """One undo step for a replace (one hit or all): page text edits (commands.EditPageTexts), text boxes
    [(item, old content, new content)] and form values {key: (old, new)}."""

    def __init__(self, canvas, text: str, page_changes=(), boxes=(), fields=None):
        super().__init__(text)
        self.pages = C.EditPageTexts(canvas, list(page_changes), text) if page_changes else None
        self.boxes = list(boxes)
        self.fields = C.SetFormValues(canvas, dict(fields), text) if fields else None

    def redo(self) -> None:
        if self.pages is not None:
            self.pages.redo()
            if not self.pages.changes and not self.boxes and self.fields is None:
                self.setObsolete(True)  # nothing could be applied: no empty undo step
        for it, _old, new in self.boxes:
            it.set_content(new)
        if self.fields is not None:
            self.fields.redo()

    def undo(self) -> None:
        if self.fields is not None:
            self.fields.undo()
        for it, old, _new in reversed(self.boxes):
            it.set_content(old)
        if self.pages is not None:
            self.pages.undo()


def is_replace_command(cmd) -> bool:
    return cmd is not None and type(cmd).__name__ == "ReplaceCommand"


# ----------------------------------------------------------------------------------- text boxes / form fields
class BoxHit:
    kind = "box"

    def __init__(self, item, n: int, m: re.Match, rect: QRectF):
        self.item, self.n, self.match, self.start, self.end = item, n, m, m.start(), m.end()
        self.rect = (rect.left(), rect.top(), rect.right(), rect.bottom())

    @property
    def order(self) -> tuple:
        return (1, self.n, self.start)


class FieldHit:
    kind = "field"

    def __init__(self, field, n: int, m: re.Match, rect: QRectF):
        self.field, self.n, self.match, self.start, self.end = field, n, m, m.start(), m.end()
        self.rect = (rect.left(), rect.top(), rect.right(), rect.bottom())

    @property
    def order(self) -> tuple:
        return (2, self.n, self.start)


def _text_boxes(pg) -> list:
    from ..items import TextItem

    return [it for it in pg.overlays() if isinstance(it, TextItem) and not it.hasFocus()]


def _text_fields(canvas, pg) -> list:
    try:
        from ..forms import TextField, page_fields

        return [f for f in page_fields(canvas, pg) if isinstance(f, TextField) and not f.readonly
                and not f.password and isinstance(f.value(), str)]
    except Exception:  # noqa: BLE001
        return []


def _box_hits(pg, rx) -> list[BoxHit]:
    out = []
    for n, it in enumerate(_text_boxes(pg)):
        text = it.document().toPlainText()
        r = it.mapRectToParent(it.boundingRect())
        out += [BoxHit(it, n, m, r) for m in rx.finditer(text) if m.end() > m.start()]
    return out


def _field_hits(canvas, pg, rx) -> list[FieldHit]:
    out = []
    for n, f in enumerate(_text_fields(canvas, pg)):
        r = f.mapRectToParent(f.r)
        out += [FieldHit(f, n, m, r) for m in rx.finditer(f.value()) if m.end() > m.start()]
    return out


def _new_box_content(item, hits: list[BoxHit], replacement: str, regex: bool) -> str:
    """The text box's content (with run formatting) after these replacements: done on a clone of its document,
    so each replacement takes the format of the text it replaces."""
    from .. import richtext

    doc = item.document().clone()
    cur = QTextCursor(doc)
    cur.beginEditBlock()
    for h in sorted(hits, key=lambda h: h.start, reverse=True):
        cur.setPosition(h.start)
        cur.setPosition(h.end, QTextCursor.MoveMode.KeepAnchor)
        cur.insertText(R.expand(h.match, replacement, regex))
    cur.endEditBlock()
    return richtext.serialize(doc)


def _new_field_value(f, hits: list[FieldHit], replacement: str, regex: bool) -> str:
    v = f.value()
    for h in sorted(hits, key=lambda h: h.start, reverse=True):
        v = v[:h.start] + R.expand(h.match, replacement, regex) + v[h.end:]
    return v


# --------------------------------------------------------------------------------------------------- the row
class ReplaceRow(QWidget):
    def __init__(self, ctl: "Replacer"):
        super().__init__()
        self.ctl = ctl
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 4)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Replace with")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMaximumWidth(320)
        self.edit.setToolTip("Enter: replace · Ctrl+Enter: replace all. With Regular expression on, \\1 or $1 "
                             "insert a group")
        self.one = QPushButton("Replace")
        self.one.setToolTip("Replace the current match and go to the next (Enter)")
        self.all = QPushButton("Replace all")
        self.all.setToolTip("Replace every match in the document as one undo step (Ctrl+Enter)")
        self.boxes = QCheckBox("Text boxes")
        self.boxes.setToolTip("Also find and replace in text boxes added with Leaf")
        self.fields = QCheckBox("Form fields")
        self.fields.setToolTip("Also find and replace in filled-in form fields")
        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (self.edit, self.one, self.all, self.boxes, self.fields, self.status):
            lay.addWidget(w)
        lay.addStretch()
        self.edit.returnPressed.connect(self._enter)
        self.one.clicked.connect(ctl.replace_one)
        self.all.clicked.connect(lambda: ctl.replace_all())
        self.boxes.toggled.connect(lambda _: ctl.options_changed())
        self.fields.toggled.connect(lambda _: ctl.options_changed())

    def _enter(self) -> None:
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            self.ctl.replace_all()
        else:
            self.ctl.replace_one()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.ctl.win.find.close_bar()
            return
        super().keyPressEvent(e)

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)
        from ..a11y import error_color  # a11y: a red readable on light, dark and high-contrast backgrounds

        self.status.setStyleSheet(f"color: {error_color(self)};" if error else "")


class _Watch(QObject):
    """Find bar hidden -> replace row hidden too."""

    def __init__(self, ctl):
        super().__init__(ctl.win)
        self.ctl = ctl
        ctl.win.find.installEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Hide and obj is self.ctl.win.find:
            self.ctl.show_row(False)
        return False


# ------------------------------------------------------------------------------------------------ controller
class Replacer(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cv = win.canvas
        self.find = win.find
        self.page_hits: dict[int, tuple] = {}  # page uid -> (segments, hits in find-bar order)
        self.summary: SummaryDialog | None = None
        self.job: ReplaceAllJob | None = None
        self._pushing = False
        self._error = ""
        # match options on the find bar
        self.case = self._opt("Aa", "Match case")
        self.word = self._opt("W", "Whole words only")
        self.regex = self._opt(".*", "Regular expression (Python syntax; \\1 or $1 in the replacement)")
        flay = self.find.layout()
        at = flay.indexOf(self.find.count)
        for i, b in enumerate((self.case, self.word, self.regex)):
            flay.insertWidget(at + i, b)
        self.row = ReplaceRow(self)
        self.row.hide()
        host = self.find.parentWidget().layout() if self.find.parentWidget() is not None else None
        if host is not None:
            host.insertWidget(host.indexOf(self.find) + 1, self.row)
        _Watch(self)
        st = getattr(win, "settings", None)
        if st is not None:
            for b, k in ((self.case, "case"), (self.word, "word"), (self.regex, "regex")):
                b.setChecked(st.value(f"replace/{k}", False, type=bool))
            self.row.boxes.setChecked(st.value("replace/boxes", False, type=bool))
            self.row.fields.setChecked(st.value("replace/fields", False, type=bool))
        for b in (self.case, self.word, self.regex):
            b.toggled.connect(lambda _: self.options_changed())
        self._sync_provider()

    def _opt(self, text: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setCheckable(True)
        b.setAutoRaise(True)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        return b

    # -- state
    def active(self) -> bool:
        return (not self.row.isHidden() or self.case.isChecked() or self.word.isChecked()
                or self.regex.isChecked())

    def options(self) -> dict:
        return dict(case=self.case.isChecked(), word=self.word.isChecked(), regex=self.regex.isChecked())

    def set_options(self, case=None, word=None, regex=None, boxes=None, fields=None) -> None:
        for b, v in ((self.case, case), (self.word, word), (self.regex, regex), (self.row.boxes, boxes),
                     (self.row.fields, fields)):
            if v is not None:
                b.setChecked(bool(v))

    def query(self) -> str:
        return self.find.edit.text().strip()

    def pattern(self, query: str | None = None) -> re.Pattern:
        return R.compile_query(self.query() if query is None else query, **self.options())

    def options_changed(self) -> None:
        st = getattr(self.win, "settings", None)
        if st is not None:
            for k, b in (("case", self.case), ("word", self.word), ("regex", self.regex), ("boxes", self.row.boxes),
                         ("fields", self.row.fields)):
                st.setValue(f"replace/{k}", b.isChecked())
        self._sync_provider()
        self.restart()

    def _sync_provider(self) -> None:
        self.find.provider = self.provide if self.active() else None

    def restart(self) -> None:
        if not self.find.isHidden() and self.query():
            self.find._start()

    def show_row(self, on: bool) -> None:
        if on == (not self.row.isHidden()):
            return
        self.row.setVisible(on)
        if not on:
            self.row.set_status("")
        self._sync_provider()
        self.restart()

    def open(self) -> None:
        """Edit › Replace…: the find bar with the replace row."""
        if self.cv.doc is None:
            return
        was = self.find.isHidden()
        if was:
            self.find.open_bar()
        self.show_row(True)
        if self.query():
            self.row.edit.setFocus()
            self.row.edit.selectAll()
        else:
            self.find.edit.setFocus()

    def set_status(self, text: str, error: bool = False) -> None:
        self.row.set_status(text, error)
        if self.row.isHidden() and text and error:
            self.find.count.setText(text)

    # -- matching (the find bar's provider)
    def hits_for(self, pg, rx) -> tuple[list, list]:
        regex = self.regex.isChecked()
        segs, hits = R.page_hits(self.cv.doc, pg.ref, rx, None if regex else self.query())
        items = list(hits)
        if not self.row.isHidden() and self.row.boxes.isChecked():
            items += _box_hits(pg, rx)
        if not self.row.isHidden() and self.row.fields.isChecked():
            items += _field_hits(self.cv, pg, rx)
        return segs, items

    def provide(self, pg, query: str) -> list[QRectF]:
        try:
            rx = self.pattern(query)
        except R.PatternError as ex:
            self.page_hits[pg.ref.uid] = ([], [])
            if self._error != str(ex):
                self._error = str(ex)
                QTimer.singleShot(0, lambda: self.set_status(self._error, error=True))
            return []
        if self._error:
            self._error = ""
            QTimer.singleShot(0, lambda: self.set_status(""))
        segs, items = self.hits_for(pg, rx)
        self.page_hits[pg.ref.uid] = (segs, items)
        return [_qrect(h.rect) for h in items]

    def current(self):
        """(page item, index on that page, segments, hit) of the find bar's current hit, or None."""
        f = self.find
        if not (0 <= f.cur < len(f.hits)):
            return None
        pg, _ = f.hits[f.cur]
        k = sum(1 for p, _ in f.hits[:f.cur] if p is pg)
        segs, items = self.page_hits.get(pg.ref.uid, ([], []))
        return (pg, k, segs, items[k]) if k < len(items) else None

    # -- replace one
    def replace_one(self) -> bool:
        """Replace the current hit and move to the next one. True when something was replaced."""
        if self.cv.doc is None or not self.query():
            return False
        if not self.active():
            self.show_row(True)
        f = self.find
        if self.find.provider is None:
            self._sync_provider()
        if not f.hits:
            if f._todo or f._debounce.isActive():
                self.set_status("Still searching…")
            else:
                f._start()
            return False
        cur = self.current()
        if cur is None:
            f._start()
            return False
        pg, k, _segs, _h = cur
        try:
            rx = self.pattern()
            R.check_template(rx, self.row.edit.text(), self.regex.isChecked())
        except R.PatternError as ex:
            self.set_status(str(ex), error=True)
            return False
        # the page may have changed since the find bar looked: match it again now
        segs, items = self.hits_for(pg, rx)
        old = self.page_hits.get(pg.ref.uid, ([], []))[1]
        if len(items) != len(old) or any(_qrect(a.rect) != _qrect(b.rect) for a, b in zip(items, old)):
            self.page_hits[pg.ref.uid] = (segs, items)
            self._refresh_page(pg, rects=[_qrect(h.rect) for h in items], after=None, keep=k)
            self.set_status("The matches on this page changed; check the highlighted one and replace again.")
            return False
        h = items[k]
        repl = self.row.edit.text()
        regex = self.regex.isChecked()
        label = f"Replace “{_short(self.query())}”"
        try:
            if h.kind == "span":
                self.set_status("This match runs across two lines; it can’t be replaced (replace it line by line).",
                                error=True)
                f.step(1)
                return False
            if h.kind == "ocr":
                self.set_status("Recognized (OCR) text can’t be replaced; run OCR again instead.", error=True)
                f.step(1)
                return False
            if h.kind == "text":
                seg = segs[h.seg]
                new = R.substitute(seg.text, [h], repl, regex)
                try:
                    warnings = T.check(self.cv.doc, pg.ref, T.KeyUnit(seg.key), new)
                except T.Refuse as ex:
                    self.set_status(f"Skipped: {ex}", error=True)
                    f.step(1)
                    return False
                cmd = ReplaceCommand(self.cv, label, [(pg.ref, seg.key, new)])
                after = (0, h.seg, h.start + len(new) - len(seg.text) + (h.end - h.start))
            elif h.kind == "box":
                content = _new_box_content(h.item, [h], repl, regex)
                cmd = ReplaceCommand(self.cv, label, boxes=[(h.item, h.item.content(), content)])
                warnings = []
                after = (1, h.n, h.start + len(R.expand(h.match, repl, regex)))
            else:
                value = _new_field_value(h.field, [h], repl, regex)
                same = [x for x in self.cv.fields_named(pg.ref.uid, h.field.name)] or [h.field]
                cmd = ReplaceCommand(self.cv, label, fields={x.key: (_form_old(self.cv, x), value) for x in same})
                warnings = []
                after = (2, h.n, h.start + len(R.expand(h.match, repl, regex)))
        except R.PatternError as ex:
            self.set_status(str(ex), error=True)
            return False
        self._push(cmd)
        failed = cmd.pages.failed if cmd.pages is not None else {}
        if failed:
            self.set_status(f"Not replaced: {next(iter(failed.values()))}", error=True)
        else:
            self.set_status(warnings[-1] if warnings else "Replaced 1.")
        segs, items = self.hits_for(pg, rx)
        self.page_hits[pg.ref.uid] = (segs, items)
        self._refresh_page(pg, rects=[_qrect(x.rect) for x in items], after=after)
        return not failed

    def _push(self, cmd) -> None:
        self._pushing = True
        try:
            self.cv.undo.push(cmd)
        finally:
            self._pushing = False

    def _refresh_page(self, pg, rects: list[QRectF], after: tuple | None, keep: int | None = None) -> None:
        """Put page `pg`'s new hits into the find bar (marks, list, count) without searching the whole document
        again, and make the first hit after `after` (an order key) current."""
        f = self.find
        idx = [i for i, (p, _) in enumerate(f.hits) if p is pg]
        if idx:
            pos = idx[0]
            del f.hits[idx[0]:idx[-1] + 1]
        else:
            order = {p: i for i, p in enumerate(self.cv.order)}
            mine = order.get(pg, 0)
            pos = next((i for i, (p, _) in enumerate(f.hits) if order.get(p, 0) > mine), len(f.hits))
        f.hits[pos:pos] = [(pg, r) for r in rects]
        curmark = getattr(f, "_curmark", None)
        mark = next((m for m in f._marks if m is not curmark and m.scene() is not None
                     and m.parentItem() is pg.content), None)
        path = QPainterPath()
        for r in rects:
            path.addRect(r)
        if mark is None and rects:
            mark = QGraphicsPathItem(pg.content)
            mark.setBrush(QColor(255, 200, 0, 90))
            mark.setPen(Qt.PenStyle.NoPen)
            mark.setZValue(40)
            f._marks.append(mark)
        if mark is not None:
            mark.setPath(path)
        if not f.hits:
            f.cur = -1
            if curmark is not None and curmark.scene() is not None:
                curmark.setPath(QPainterPath())
            f._update_count()
            return
        items = self.page_hits.get(pg.ref.uid, ([], []))[1]
        if keep is not None:
            k = min(keep, len(rects) - 1) if rects else 0
            f.cur = pos + k if rects else pos % len(f.hits)
        else:
            k = next((i for i, h in enumerate(items) if after is None or h.order >= after), len(items))
            f.cur = (pos + k) % len(f.hits)
        f._show_current()
        f._update_count()

    # -- replace all
    def replace_all(self, wait: bool = False) -> "ReplaceAllJob | None":
        """Replace every match (document text, and text boxes / form fields when ticked) as one undo step.
        `wait`: run to the end before returning (tests, scripts)."""
        if self.cv.doc is None or not self.query() or self.job is not None:
            return None
        if self.row.isHidden():
            self.show_row(True)
        try:
            rx = self.pattern()
            R.check_template(rx, self.row.edit.text(), self.regex.isChecked())
        except R.PatternError as ex:
            self.set_status(str(ex), error=True)
            return None
        job = self.job = ReplaceAllJob(self, rx, self.row.edit.text(), self.regex.isChecked(),
                                       self.row.boxes.isChecked(), self.row.fields.isChecked())
        job.start(wait)
        return job

    def job_done(self, job: "ReplaceAllJob") -> None:
        self.job = None
        t = job.totals
        if job.cancelled:
            self.set_status("Replace all cancelled; nothing was changed.")
            return
        if not t.total and not t.skipped:
            self.set_status("No matches.")
            self.win.toast(f"No matches for “{_short(self.query())}”")
            return
        self.set_status(t.headline())
        warnings = t.warnings()
        if t.skipped or warnings:
            self.show_summary(t, warnings)
        else:
            self.win.toast(t.headline())
        self.restart()

    def show_summary(self, totals: R.Totals, warnings: list[str]) -> None:
        if self.summary is not None:
            self.summary.close()
        self.summary = SummaryDialog(self, totals, warnings)
        self.summary.show()

    def go_to(self, uid: int, rect) -> None:
        pg = self.cv.items_by_uid.get(uid)
        if pg is None:
            return
        r = _qrect(rect)
        if r.isEmpty():
            idx = self.cv.order.index(pg) if pg in self.cv.order else 0
            self.cv.go_to(idx)
            return
        self.cv.ensure_rect_visible(r, pg.content)
        f = self.find
        if f.isHidden():
            return
        curmark = getattr(f, "_curmark", None)
        if curmark is not None and curmark.scene() is not None:
            curmark.setParentItem(pg.content)
            path = QPainterPath()
            path.addRect(r.adjusted(-1, -1, 1, 1))
            curmark.setPath(path)

    # -- undo/redo of a replace: the find bar searches again
    def undo_changed(self, idx: int) -> None:
        if self._pushing:
            return
        st = self.cv.undo
        if is_replace_command(st.command(idx - 1)) or is_replace_command(st.command(idx)):
            if not self.find.isHidden() and self.query():
                self.find._debounce.start()

    def doc_changed(self) -> None:
        self.page_hits.clear()
        if self.job is not None:
            self.job.cancel()
        if self.summary is not None:
            self.summary.close()
            self.summary = None


def _form_old(canvas, f):
    return canvas.doc.form_values.get(f.key, C.UNSET)


def _short(s: str, n: int = 30) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _qrect(r) -> QRectF:
    if r is None:
        return QRectF()
    x0, y0, x1, y1 = r
    return QRectF(x0, y0, x1 - x0, y1 - y0).normalized()


# -------------------------------------------------------------------------------------------- replace all job
class ReplaceAllJob(QObject):
    """Scan (time-sliced, cancellable, read-only) then apply as one ReplaceCommand."""

    def __init__(self, ctl: Replacer, rx, replacement: str, regex: bool, boxes: bool, fields: bool):
        super().__init__(ctl)
        self.ctl, self.rx, self.replacement, self.regex = ctl, rx, replacement, regex
        self.with_boxes, self.with_fields = boxes, fields
        self.cv = ctl.cv
        self.pages = list(self.cv.order)
        self.i = 0
        self.totals = R.Totals()
        self.boxes: list = []  # (item, old, new)
        self.fields: dict = {}
        self.cancelled = False
        self.finished = False
        self.command: ReplaceCommand | None = None
        self.elapsed = {"scan": 0.0, "apply": 0.0}
        self._busy = False
        self._t0 = 0.0
        self.timer = QTimer(self, interval=0, timeout=self._tick)
        self.dlg = QProgressDialog("Finding matches…", "Cancel", 0, max(len(self.pages), 1), ctl.win)
        self.dlg.setWindowTitle("Replace all")
        self.dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.dlg.setMinimumDuration(400)
        self.dlg.setAutoClose(False)
        self.dlg.setAutoReset(False)
        self.dlg.canceled.connect(self.cancel)

    def start(self, wait: bool = False) -> None:
        self._t0 = perf_counter()
        self.dlg.setValue(0)
        if wait:
            while not self.finished and not self.cancelled:
                self._tick()
        else:
            self.timer.start()

    def cancel(self) -> None:
        if self.finished or self.cancelled:
            return
        self.cancelled = True
        self.timer.stop()
        osint.progress(self.ctl.win, None, key="replace")
        self.dlg.hide()
        self.dlg.deleteLater()
        self.ctl.job_done(self)

    def _tick(self) -> None:
        if self._busy or self.cancelled or self.finished:
            return
        self._busy = True
        try:
            t0 = perf_counter()
            doc = self.cv.doc
            ocr_q = None if self.regex else self.ctl.query()
            first = self.i
            while self.i < len(self.pages):
                if self.i > first and perf_counter() - t0 >= SLICE:  # (at least one page per turn)
                    break
                pg = self.pages[self.i]
                pno = self.i + 1
                self.i += 1
                if pg.scene() is None:
                    continue
                self.totals.add(pno, R.plan_page(doc, pg.ref, pno, self.rx, self.replacement, self.regex, ocr_q))
                if self.with_boxes:
                    self._scan_boxes(pg)
                if self.with_fields:
                    self._scan_fields(pg)
            self.dlg.setValue(self.i)
            osint.progress(self.ctl.win, self.i / max(len(self.pages), 1), key="replace", keep_awake=False)
        except R.PatternError as ex:
            self.ctl.set_status(str(ex), error=True)
            self._busy = False
            self.cancel()
            return
        finally:
            self._busy = False
        if self.i >= len(self.pages) and not self.cancelled:
            self.timer.stop()
            self.elapsed["scan"] = perf_counter() - self._t0
            self._apply()

    def _scan_boxes(self, pg) -> None:
        by: dict = {}
        for h in _box_hits(pg, self.rx):
            by.setdefault(h.item, []).append(h)
        for item, hs in by.items():
            new = _new_box_content(item, hs, self.replacement, self.regex)
            old = item.content()
            self.totals.box_hits += len(hs)
            if new != old:
                self.boxes.append((item, old, new))
                self.totals.boxes += 1

    def _scan_fields(self, pg) -> None:
        by: dict = {}
        for h in _field_hits(self.cv, pg, self.rx):
            by.setdefault(h.field, []).append(h)
        names: set = set()
        for f, hs in by.items():
            new = _new_field_value(f, hs, self.replacement, self.regex)
            if f.maxlen and len(new) > f.maxlen:
                self.totals.skipped.append(R.Skip(self.pages.index(pg) + 1, pg.ref.uid, hs[0].rect,
                                                  f"The new value is longer than the field allows ({f.maxlen}).",
                                                  "longer than the field allows", len(hs), f.name or ""))
                continue
            self.totals.field_hits += len(hs) if f.name not in names else 0
            if new != f.value():
                self.fields[f.key] = (_form_old(self.cv, f), new)
                if f.name not in names:
                    self.totals.fields += 1
            names.add(f.name)

    def _apply(self) -> None:
        t = self.totals
        t0 = perf_counter()
        if t.changes or self.boxes or self.fields:
            self.dlg.setLabelText("Replacing…")
            self.dlg.setCancelButton(None)
            label = f"Replace all “{_short(self.ctl.query())}”"
            cmd = self.command = ReplaceCommand(self.cv, label, t.changes, self.boxes, self.fields)
            if cmd.pages is not None:
                pages = {}
                for ref, _k, _n in t.changes:
                    pages.setdefault(ref.uid, None)
                self.dlg.setRange(0, max(len(pages), 1))
                self.dlg.setValue(0)
                cmd.pages.progress = lambda done, total: self.dlg.setValue(done)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.ctl._push(cmd)
            finally:
                QApplication.restoreOverrideCursor()
            if cmd.pages is not None:
                cmd.pages.progress = None
                pno = {pg.ref.uid: i + 1 for i, pg in enumerate(self.pages)}
                for uid, reason in cmd.pages.failed.items():
                    t.fail_page(uid, pno.get(uid, 0), reason)
        self.elapsed["apply"] = perf_counter() - t0
        self.finished = True
        osint.progress(self.ctl.win, None, key="replace")
        self.dlg.hide()
        self.dlg.deleteLater()
        self.ctl.job_done(self)


# ------------------------------------------------------------------------------------------------- summary
class SummaryDialog(QDialog):
    """What Replace all did: the headline, then every skipped spot (double-click or Go to jumps there) and the
    warnings (fallback fonts, signed/tagged documents, …), aggregated."""

    def __init__(self, ctl: Replacer, totals: R.Totals, warnings: list[str]):
        super().__init__(ctl.win)
        self.ctl, self.totals = ctl, totals
        self.setWindowTitle("Replace all")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        self.headline = QLabel(totals.headline())
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)
        self.list = QListWidget()
        for s in totals.skipped:
            n = f" ({s.hits} matches)" if s.hits > 1 else ""
            it = QListWidgetItem(f"p. {s.pno} · {s.label}{n}" + (f" — {s.excerpt}" if s.excerpt else ""))
            it.setToolTip(s.reason)
            it.setData(Qt.ItemDataRole.UserRole, (s.uid, s.rect))
            it.setIcon(icon("close"))
            self.list.addItem(it)
        for w in warnings:
            it = QListWidgetItem(w)
            it.setToolTip(w)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = it.font()
            f.setItalic(True)
            it.setFont(f)
            self.list.addItem(it)
        self.list.setWordWrap(True)
        self.list.itemActivated.connect(self._go)
        self.list.itemDoubleClicked.connect(self._go)
        lay.addWidget(self.list)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = bb.addButton("Go to", QDialogButtonBox.ButtonRole.ActionRole)
        self.go.clicked.connect(lambda: self._go(self.list.currentItem()))
        bb.rejected.connect(self.close)
        lay.addWidget(bb)
        self.resize(560, 360)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _go(self, it) -> None:
        if it is None:
            return
        data = it.data(Qt.ItemDataRole.UserRole)
        if data:
            self.ctl.go_to(*data)

    def closeEvent(self, e) -> None:
        if self.ctl.summary is self:
            self.ctl.summary = None
        super().closeEvent(e)


# --------------------------------------------------------------------------------------------------- install
def install(win) -> None:
    if not hasattr(win, "find") or not hasattr(win, "canvas"):
        return
    ctl = win.replacer = Replacer(win)
    a = QAction(icon("find"), "&Replace…", win)
    a.setObjectName("a_replace")
    a.setProperty("icon_name", "find")
    a.setShortcut(QKeySequence("Ctrl+H"))
    a.setToolTip("Find and replace text (Ctrl+H)")
    a.setStatusTip("Replace text of the page itself (and optionally in text boxes and form fields)")
    a.triggered.connect(lambda _=False: ctl.open())
    win.a_replace = a
    win.addAction(a)
    menus = getattr(win, "menus", {})
    if "edit" in menus:
        m = menus["edit"]
        find = getattr(win, "a_find", None)
        acts = m.actions()
        if find in acts and acts.index(find) + 1 < len(acts):
            m.insertAction(acts[acts.index(find) + 1], a)
        else:
            m.addAction(a)
    find = getattr(win, "a_find", None)
    if find is not None:
        find.triggered.connect(lambda _=False: ctl.show_row(False))  # Ctrl+F: find only

    def enable() -> None:
        a.setEnabled(win.canvas.doc is not None)
        ctl.doc_changed()

    win.canvas.documentChanged.connect(enable)
    win.canvas.undo.indexChanged.connect(ctl.undo_changed)
    a.setEnabled(win.canvas.doc is not None)
