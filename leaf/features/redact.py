"""Redaction feature: Redact tool (X), Redact selected text, Redactions panel, Search & redact, Sanitize document,
and the confirmation before a save that removes content.

The model and the save-time removal live in leaf/redact.py; this module only wires them into the window."""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import commands as C
from .. import osint
from .. import redact as R
from .. import shortcuts
from ..icons import icon

APP = "Leaf"
HINT = ("Drag across text to redact it, or drag a box over anything (Shift: always a box). Marked content is "
        "permanently removed when you save.")
WHITEOUT_HINT = "Whiteout only covers; the text stays in the file. Use Redact (X) to remove it."


def _insert_after(menu, after: QAction | None, actions: list[QAction]) -> None:
    acts = menu.actions()
    if after in acts and acts.index(after) + 1 < len(acts):
        menu.insertActions(acts[acts.index(after) + 1], actions)
    else:
        menu.addActions(actions)


def _secondary(w: QWidget) -> None:
    pal = w.palette()
    pal.setColor(w.foregroundRole(), pal.color(pal.ColorGroup.Disabled, pal.ColorRole.WindowText))
    w.setPalette(pal)


# ================================================================ review panel
class RedactPanel(QWidget):
    """Every mark in page order, with go to / remove; the overlay text for marks; sanitize status."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.cv = win.canvas
        self._items: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemDoubleClicked.connect(lambda _: self.go_to())
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_go = QPushButton("Go to")
        self.b_go.clicked.connect(self.go_to)
        self.b_remove = QPushButton("Remove")
        self.b_remove.clicked.connect(self.remove_selected)
        self.b_all = QPushButton("Remove all")
        self.b_all.clicked.connect(self.remove_all)
        row.addWidget(self.b_go)
        row.addWidget(self.b_remove)
        row.addWidget(self.b_all)
        row.addStretch()
        lay.addLayout(row)

        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("Overlay text"))
        self.overlay = QLineEdit()
        self.overlay.setPlaceholderText("none (e.g. REDACTED)")
        self.overlay.setToolTip("Text printed on the filled area of new marks, or of the selected marks")
        self.overlay.editingFinished.connect(self._overlay_changed)
        text_row.addWidget(self.overlay, 1)
        lay.addLayout(text_row)

        row2 = QHBoxLayout()
        self.b_search = QPushButton("Search && redact…")
        self.b_search.clicked.connect(lambda: search_dialog(win))
        self.b_sanitize = QPushButton("Sanitize…")
        self.b_sanitize.clicked.connect(lambda: sanitize_dialog(win))
        row2.addWidget(self.b_search)
        row2.addWidget(self.b_sanitize)
        row2.addStretch()
        lay.addLayout(row2)
        self.sanitize_label = QLabel()
        self.sanitize_label.setWordWrap(True)
        lay.addWidget(self.sanitize_label)
        note = QLabel("Fill color: Style panel. Marked text, images and drawings are removed from the file when you "
                      "save (a full rewrite). Whiteout only covers.")
        note.setWordWrap(True)
        _secondary(note)
        lay.addWidget(note)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.refresh)
        self.cv.undo.indexChanged.connect(lambda _: self.schedule())
        self.cv.documentChanged.connect(self.schedule)
        self.cv.pagesChanged.connect(self.schedule)
        self.cv.scene().selectionChanged.connect(self._overlay_from_selection)
        self.refresh()

    def schedule(self) -> None:
        if self.isVisible():
            self._timer.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.refresh()

    def refresh(self) -> None:
        doc = self.cv.doc
        marks = R.marks_of(self.cv) if doc is not None else []
        keep = {id(m) for m in self.selected_marks()}
        self.list.clear()
        self._items = []
        for i, m in marks:
            r = m.scene_rect_in_parent()
            label = f"“{m.marked_text}”" if m.marked_text else f"Area {r.width():.0f} × {r.height():.0f} pt"
            it = QListWidgetItem(f"p. {i + 1}   {label}")
            self.list.addItem(it)
            self._items.append((i, m))
            if id(m) in keep:
                it.setSelected(True)
        pages = len({i for i, _ in marks})
        if doc is None:
            self.summary.setText("No document.")
        elif not marks:
            self.summary.setText("No redaction marks. Use the Redact tool (X), Edit › Redact selected text, or "
                                 "Search & redact.")
        else:
            self.summary.setText(f"{len(marks)} mark{'s' if len(marks) != 1 else ''} on {pages} "
                                 f"page{'s' if pages != 1 else ''}, removed when you save.")
        spec = getattr(doc, "sanitize", None) if doc is not None else None
        self.sanitize_label.setText("Sanitize on save: " + ", ".join(spec.chosen()) if spec else "")
        self.sanitize_label.setVisible(bool(spec))
        for w in (self.b_search, self.b_sanitize, self.overlay):
            w.setEnabled(doc is not None)
        self.b_all.setEnabled(bool(marks))
        self._sync_buttons()
        if doc is not None and not self.cv.selected_overlays():
            self.overlay.setText(str(R._defaults(self.cv).get("text", "")))

    def _sync_buttons(self) -> None:
        sel = bool(self.list.selectedItems())
        self.b_go.setEnabled(sel)
        self.b_remove.setEnabled(sel)

    def selected_marks(self) -> list:
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        return [self._items[r][1] for r in rows if r < len(self._items)]

    def go_to(self) -> None:
        rows = sorted(self.list.row(it) for it in self.list.selectedItems())
        if not rows:
            return
        i, m = self._items[rows[0]]
        go_to_mark(self.cv, i, m)

    def remove_selected(self) -> None:
        sel = self.selected_marks()
        if sel:
            self.cv.undo.push(C.RemoveItems(sel, "Remove redaction" if len(sel) == 1 else f"Remove {len(sel)} redactions"))

    def remove_all(self) -> None:
        marks = [m for _, m in R.marks_of(self.cv)]
        if marks:
            self.cv.undo.push(C.RemoveItems(marks, "Remove all redactions"))

    def _overlay_from_selection(self) -> None:
        sel = [i for i in self.cv.selected_overlays() if R.is_mark(i)]
        if sel and not self.overlay.hasFocus():
            self.overlay.setText(str(sel[0].props.get("text", "")))

    def _overlay_changed(self) -> None:
        if self.cv.doc is None:
            return
        text = self.overlay.text().strip()
        R._defaults(self.cv)["text"] = text
        sel = [i for i in self.cv.selected_overlays() if R.is_mark(i)]
        changes = {}
        for it in sel:
            if it.props.get("text", "") != text:
                changes[it] = (dict(it.props), {**it.props, "text": text})
        if changes:
            self.cv.undo.push(C.SetProps(changes))


def go_to_mark(cv, i: int, m) -> None:
    cv.go_to(i)
    cv.scene().clearSelection()
    m.setSelected(True)
    cv.ensure_rect_visible(m.boundingRect(), m)


# ============================================================ search & redact
class SearchRedactDialog(QDialog):
    """Find text or patterns on every page (chunked, cancellable), review the hits, mark them in one undo step."""

    SLICE = 0.03  # seconds of searching per event-loop turn

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cv = win.canvas
        self.setWindowTitle("Search & Redact")
        self.resize(640, 520)
        self._hits: list[R.Hit] = []
        self._job = None
        self._searched = None  # (document, page uids) the hits belong to
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self.preset = QComboBox()
        for p in R.PRESETS:
            self.preset.addItem(p.name)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        row.addWidget(self.preset)
        self.query = QLineEdit()
        self.query.returnPressed.connect(self.start)
        row.addWidget(self.query, 1)
        self.b_search = QPushButton("Search")
        self.b_search.setDefault(True)
        self.b_search.clicked.connect(self.start)
        row.addWidget(self.b_search)
        lay.addLayout(row)

        opts = QHBoxLayout()
        self.case = QCheckBox("Match case")
        self.whole = QCheckBox("Whole words")
        opts.addWidget(self.case)
        opts.addWidget(self.whole)
        opts.addStretch()
        lay.addLayout(opts)

        prog = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.b_stop = QPushButton("Stop")
        self.b_stop.clicked.connect(self.stop)
        self.b_stop.setEnabled(False)
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.b_stop)
        lay.addLayout(prog)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Page", "Match", "Context"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self._show_hit)
        self.tree.itemChanged.connect(lambda *_: self._sync())
        lay.addWidget(self.tree, 1)

        self.status = QLabel()
        lay.addWidget(self.status)
        bb = QDialogButtonBox()
        self.b_all = bb.addButton("Check all", QDialogButtonBox.ButtonRole.ActionRole)
        self.b_none = bb.addButton("Check none", QDialogButtonBox.ButtonRole.ActionRole)
        self.b_mark = bb.addButton("Mark for redaction", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        self.b_all.clicked.connect(lambda: self._check_all(True))
        self.b_none.clicked.connect(lambda: self._check_all(False))
        self.b_mark.clicked.connect(self.mark)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._preset_changed()
        self._sync()

    # -- options
    def current_preset(self) -> R.Preset:
        return R.PRESETS[self.preset.currentIndex()]

    def _preset_changed(self) -> None:
        p = self.current_preset()
        free = p.name in ("Text", "Custom regular expression")
        self.query.setEnabled(free)
        self.query.setPlaceholderText({"Text": "Words or phrase to find",
                                       "Custom regular expression": r"Regular expression, e.g. \bID-\d{6}\b"}
                                      .get(p.name, f"Finds {p.name.lower()}"))
        self.case.setEnabled(p.case_sensitive is None)
        if free:
            self.query.setFocus()

    # -- search
    def start(self) -> None:
        self.stop()
        if self.cv.doc is None:
            return
        preset = self.current_preset()
        try:
            rx = R.build_regex(self.query.text(), preset, self.case.isChecked(), self.whole.isChecked())
        except re.error as ex:
            self.status.setText(f"Invalid search: {ex}")
            return
        self.tree.clear()
        self._hits = []
        n = len(self.cv.doc.pages)
        self.progress.setRange(0, n)
        self.progress.setValue(0)
        self._job = {"rx": rx, "check": preset.check, "page": 0, "n": n, "doc": self.cv.doc}
        self._searched = (self.cv.doc, [r.uid for r in self.cv.doc.pages])
        self.b_stop.setEnabled(True)
        self.status.setText("Searching…")
        QTimer.singleShot(0, self._step)

    def _step(self) -> None:
        job = self._job
        if job is None:
            return
        if job["doc"] is not self.cv.doc or len(job["doc"].pages) != job["n"]:
            self.stop("The document changed; search again.")
            return
        end = time.monotonic() + self.SLICE
        found = []
        while job["page"] < job["n"] and time.monotonic() < end:
            found += R.search_page(job["doc"], job["page"], job["rx"], job["check"])
            job["page"] += 1
        self._add_hits(found)
        self.progress.setValue(job["page"])
        osint.progress(self.win, job["page"] / max(job["n"], 1), key="redact-search", keep_awake=False)
        if job["page"] >= job["n"]:
            self.stop()
        else:
            QTimer.singleShot(0, self._step)

    def stop(self, msg: str | None = None) -> None:
        stopped = self._job is not None and self._job["page"] < self._job["n"]
        self._job = None
        osint.progress(self.win, None, key="redact-search")
        self.b_stop.setEnabled(False)
        if msg:
            self.status.setText(msg)
        elif stopped:
            self.status.setText(f"Stopped. {len(self._hits)} matches so far.")
        else:
            self._sync()

    def _add_hits(self, hits: list[R.Hit]) -> None:
        if not hits:
            return
        self.tree.blockSignals(True)
        for h in hits:
            it = QTreeWidgetItem([str(h.page + 1), h.text, h.context])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked)
            it.setData(0, Qt.ItemDataRole.UserRole, len(self._hits))
            self._hits.append(h)
            self.tree.addTopLevelItem(it)
        self.tree.blockSignals(False)
        self.tree.resizeColumnToContents(0)
        self._sync()

    # -- results
    def checked_hits(self) -> list[R.Hit]:
        out = []
        for k in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(k)
            if it.checkState(0) == Qt.CheckState.Checked:
                out.append(self._hits[it.data(0, Qt.ItemDataRole.UserRole)])
        return out

    def _check_all(self, on: bool) -> None:
        self.tree.blockSignals(True)
        for k in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(k).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._sync()

    def _sync(self) -> None:
        n = len(self.checked_hits())
        self.b_mark.setEnabled(n > 0)
        self.b_mark.setText(f"Mark {n} for redaction" if n else "Mark for redaction")
        if self._job is None and self._hits is not None:
            total = len(self._hits)
            pages = len({h.page for h in self._hits})
            self.status.setText(f"{total} match{'es' if total != 1 else ''} on {pages} page{'s' if pages != 1 else ''}"
                                if total else ("No matches." if self.tree.topLevelItemCount() == 0 and
                                               self.progress.maximum() else ""))

    def _show_hit(self, it: QTreeWidgetItem) -> None:
        h = self._hits[it.data(0, Qt.ItemDataRole.UserRole)]
        cv = self.cv
        if h.page < len(cv.order):
            cv.go_to(h.page)
            pg = cv.order[h.page]
            r = h.rects[0]
            for x in h.rects[1:]:
                r = r.united(x)
            cv.ensure_rect_visible(r, pg.content)

    def mark(self) -> None:
        hits = self.checked_hits()
        if not hits or self.cv.doc is None:
            return
        if self._searched != (self.cv.doc, [r.uid for r in self.cv.doc.pages]):  # pages changed since the search
            self.tree.clear()
            self._hits = []
            self.status.setText("The pages changed since the search; search again.")
            self._sync()
            return
        n = R.mark_hits(self.cv, hits)
        self.win.toast(f"Marked {n} area{'s' if n != 1 else ''} for redaction · removed when you save")
        self.accept()

    def reject(self) -> None:
        self.stop()
        super().reject()


def search_dialog(win) -> None:
    if win.canvas.doc is None:
        return
    win._commit_edits()
    dlg = getattr(win, "_search_redact_dlg", None)
    if dlg is None:
        dlg = win._search_redact_dlg = SearchRedactDialog(win)
    sel = win.canvas.selected_text().strip()
    if sel and "\n" not in sel:
        dlg.preset.setCurrentIndex(0)
        dlg.query.setText(sel)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


# ================================================================== sanitize
class SanitizeDialog(QDialog):
    def __init__(self, win, spec: R.Sanitize):
        super().__init__(win)
        self.setWindowTitle("Sanitize Document")
        lay = QVBoxLayout(self)
        intro = QLabel("Remove hidden information when the document is saved. The open file is not changed.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.boxes: dict[str, QCheckBox] = {}
        for key, (label, desc) in R.LABELS.items():
            b = QCheckBox(label)
            b.setChecked(bool(getattr(spec, key)))
            b.setToolTip(desc)
            lay.addWidget(b)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setContentsMargins(24, 0, 0, 4)
            _secondary(d)
            lay.addWidget(d)
            self.boxes[key] = b
        note = QLabel("Applied at save with a full rewrite (digital signatures become invalid). Undo with Ctrl+Z.")
        note.setWordWrap(True)
        _secondary(note)
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def spec(self) -> R.Sanitize | None:
        s = R.Sanitize(**{k: b.isChecked() for k, b in self.boxes.items()})
        return s if s else None


def set_sanitize(cv, spec) -> bool:
    doc = cv.doc
    if doc is None or spec == doc.sanitize:
        return False
    cv.undo.push(R.SetSanitize(doc, spec, "Sanitize document" if spec else "Don't sanitize"))
    return True


def sanitize_dialog(win) -> None:
    cv = win.canvas
    if cv.doc is None:
        return
    dlg = SanitizeDialog(win, cv.doc.sanitize or R.DEFAULT_SANITIZE)
    if dlg.exec() == QDialog.DialogCode.Accepted and set_sanitize(cv, dlg.spec()):
        spec = cv.doc.sanitize
        win.toast("Sanitizing when you save: " + ", ".join(spec.chosen()).lower() if spec else "Sanitizing turned off")


# ============================================================== save confirm
def confirm_save(win, pages=None) -> bool:
    """Before a save that removes content: say it's permanent and which signatures break. Asked again only when
    the marks or the sanitize setting changed since the last confirmation."""
    cv = win.canvas
    doc = cv.doc
    if doc is None:
        return True
    pages = list(doc.pages if pages is None else pages)
    marks = [it for r in pages for it in cv.overlays_for(r) if R.is_mark(it)]
    if not marks and not doc.sanitize:
        return True
    every = [it for r in doc.pages for it in cv.overlays_for(r) if R.is_mark(it)]  # extract: one question for all
    key = (doc, frozenset((id(m), tuple(m.props.items()), str(m.geometry())) for m in every), doc.sanitize)
    if getattr(win, "_redact_confirmed", None) == key:
        return True
    lines = []
    if marks:
        n = len({id(cv.page_item(r)) for r in pages if any(R.is_mark(i) for i in cv.overlays_for(r))})
        lines.append(f"{len(marks)} redaction mark{'s' if len(marks) != 1 else ''} on {n} page{'s' if n != 1 else ''}: "
                     "the text, images, drawings, annotations, form fields and links under them are removed from "
                     "the saved file. This cannot be undone in the saved file.")
    if doc.sanitize:
        lines.append("Sanitize: " + ", ".join(doc.sanitize.chosen()).lower() + ".")
    signed = sum(R.signed_fields(s.doc) for s in {r.src for r in pages})
    if signed:
        lines.append(f"The document has {signed} digital signature{'s' if signed != 1 else ''}; "
                     f"{'they' if signed != 1 else 'it'} will no longer be valid.")
    box = QMessageBox(QMessageBox.Icon.Warning, APP, "Redaction is permanent.", parent=win)
    box.setInformativeText("\n\n".join(lines) + "\n\nTip: Save As keeps the original file unchanged.")
    save = box.addButton("Save and remove", QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(save)
    box.exec()
    if box.clickedButton() is not save:
        return False
    win._redact_confirmed = key
    return True


# =================================================================== install
def _tool(win) -> QAction:
    cv = win.canvas
    a = QAction(icon("redact"), "Redact", win)
    a.setObjectName("a_tool_redact")
    a.setCheckable(True)
    a.setProperty("icon_name", "redact")
    a.setIconText("Redact")
    a.setToolTip("Redact (remove content)  (X)")
    a.triggered.connect(lambda _=False: cv.set_tool("redact"))
    win.a_tool_redact = a
    if hasattr(win, "tool_group"):
        win.tool_group.addAction(a)
    if hasattr(win, "tool_actions"):
        win.tool_actions["redact"] = a
    win.addAction(a)
    cv.tool_keys = {**cv.tool_keys, Qt.Key.Key_X: "redact"}
    menus = getattr(win, "menus", {})
    tools = getattr(win, "tool_actions", {})
    if "insert" in menus:
        _insert_after(menus["insert"], tools.get("whiteout"), [a])
    strip = getattr(win, "tool_strip", None)
    if strip is not None and strip.layout() is not None:
        b = QToolButton()
        b.setAutoRaise(True)
        b.setIconSize(QSize(20, 20))
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setDefaultAction(a)
        lay = strip.layout()
        wo = next((i for i in range(lay.count()) if isinstance(lay.itemAt(i).widget(), QToolButton)
                   and lay.itemAt(i).widget().defaultAction() is tools.get("whiteout")), -1)
        lay.insertWidget(wo + 1 if wo >= 0 else lay.count(), b)
        buttons = getattr(win, "tool_buttons", None)
        if buttons is not None:
            b.setToolButtonStyle(buttons[0].toolButtonStyle() if buttons else win.toolbar.toolButtonStyle())
            buttons.append(b)
    return a


def _dock(win) -> QDockWidget:
    panel = RedactPanel(win)
    dock = QDockWidget("Redactions", win)
    dock.setObjectName("redactions")
    dock.setWidget(panel)
    dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    if getattr(win, "props_dock", None) is not None:
        win.tabifyDockWidget(win.props_dock, dock)
        win.props_dock.raise_()
    dock.hide()
    dock.panel = panel
    return dock


def install(win) -> None:
    cv = win.canvas
    cv.defaults.setdefault(R.KIND, dict(R.DEFAULTS[R.KIND]))
    try:
        from .. import props, window

        window.HINTS["redact"] = HINT
        window.HINTS["whiteout"] = WHITEOUT_HINT
        props.TITLES.setdefault("redact", "Redact")
    except Exception:  # noqa: BLE001
        pass
    tools = getattr(win, "tool_actions", {})
    if "whiteout" in tools:
        tools["whiteout"].setStatusTip(WHITEOUT_HINT)

    _tool(win)
    win.redact_dock = _dock(win)
    a_panel = win.redact_dock.toggleViewAction()
    a_panel.setText("&Redactions panel")
    a_panel.setObjectName("a_redactions_panel")
    a_panel.setIcon(icon("redact"))
    a_panel.setProperty("icon_name", "redact")
    a_panel.triggered.connect(lambda on: on and win.redact_dock.raise_())
    win.a_redactions_panel = a_panel

    def act(name: str, text: str, slot, tip: str) -> QAction:
        a = QAction(text, win)
        a.setObjectName(f"a_{name}")
        a.setToolTip(tip)
        a.triggered.connect(lambda _=False: slot())
        win.addAction(a)
        setattr(win, f"a_{name}", a)
        return a

    def redact_sel() -> None:
        if R.redact_selection(cv):
            win.toast("Marked for redaction · removed when you save")
        else:
            win.toast("Select text first (drag across it with the Select tool)")

    a_sel = act("redact_selection", "Redact selected te&xt", redact_sel,
                "Mark the selected text for redaction (removed when you save)")
    a_sel.setIcon(icon("redact"))
    a_sel.setProperty("icon_name", "redact")
    a_search = act("search_redact", "Search && &redact…", lambda: search_dialog(win),
                   "Find text or patterns (emails, phone numbers, ...) on every page and mark them for redaction")
    a_sanitize = act("sanitize", "Sa&nitize document…", lambda: sanitize_dialog(win),
                     "Remove metadata, scripts, attachments, hidden text and layers when saving")

    menus = getattr(win, "menus", {})
    if "edit" in menus:
        _insert_after(menus["edit"], getattr(win, "a_hl_sel", None), [a_sel])
    menu = menus.get("document")
    if menu is None:
        menu = QMenu("&Document", win)
        win.menuBar().addMenu(menu)
        menus["document"] = menu
    menu.addSeparator()
    menu.addActions([a_search, a_sanitize])  # the tool is in Insert, the panel in View (one menu entry each)
    if "view" in menus:
        _insert_after(menus["view"], getattr(win, "a_comments_panel", None) or getattr(win, "a_props", None), [a_panel])

    # the confirmation sits in front of every save the user starts (Save, Save As, extract); recovery snapshots
    # and printing call PdfDocument.save directly and are not asked
    orig_save_to = win._save_to

    def _save_to(path, pages=None):
        if not confirm_save(win, pages):
            return False
        return orig_save_to(path, pages)

    win._save_to = _save_to

    def enable() -> None:
        has = cv.doc is not None
        for a in (win.a_tool_redact, a_sel, a_search, a_sanitize):
            a.setEnabled(has)
        win._redact_confirmed = None

    cv.documentChanged.connect(enable)
    enable()
    shortcuts.apply_saved(win, only_new=True)  # picks up the tool key
