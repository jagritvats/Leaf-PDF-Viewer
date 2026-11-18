"""Page management: Pages menu, page-list context menu and keys, clipboard, extract/split/insert dialogs,
the organizer grid and the page-size status label."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QRadioButton, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from .. import pageops
from ..sidebar import SIZES, Sidebar

APP = "Leaf"
PDF_FILTER = "PDF files (*.pdf)"
OPEN_FILTER = "Documents (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;PDF files (*.pdf);;All files (*)"


def _toast(win, text: str, undo: bool = False) -> None:
    try:
        from ..toast import show_toast

        if undo:
            show_toast(win, text, "Undo", win.canvas.undo.undo, 4000)
        else:
            show_toast(win, text)
    except Exception:  # noqa: BLE001 - fall back to the status bar
        win.statusBar().showMessage(text, 3000)


# --------------------------------------------------------------- dialogs
class RangeEdit(QLineEdit):
    """Page-range field with live validation shown in `status`."""

    def __init__(self, n: int, text: str, status: QLabel):
        super().__init__(text)
        self.n, self.status = n, status
        self.setPlaceholderText("e.g. 1-3, 7, 10-   (empty = all)")
        self.setClearButtonEnabled(True)
        self.textChanged.connect(self._check)
        self._check()

    def rows(self) -> list[int] | None:
        try:
            return pageops.parse_ranges(self.text(), self.n)
        except ValueError:
            return None

    def _check(self) -> None:
        try:
            k = len(pageops.parse_ranges(self.text(), self.n))
            self.status.setText(f"{k} of {self.n} page(s)")
            self.status.setStyleSheet("")
        except ValueError as ex:
            self.status.setText(str(ex))
            self.status.setStyleSheet("color: palette(link-visited);")


def _buttons(dlg: QDialog, ok_text: str) -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    return bb


class ExtractDialog(QDialog):
    def __init__(self, parent, n: int, default: str):
        super().__init__(parent)
        self.setWindowTitle("Extract pages")
        lay = QFormLayout(self)
        self.status = QLabel()
        self.range = RangeEdit(n, default, self.status)
        self.each = QCheckBox("Save each page as a separate file")
        lay.addRow("Pages:", self.range)
        lay.addRow("", self.status)
        lay.addRow("", self.each)
        self.bb = _buttons(self, "Extract…")
        lay.addRow(self.bb)
        self.range.textChanged.connect(lambda _: self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.range.rows() is not None))


class SplitDialog(QDialog):
    def __init__(self, parent, n: int, starts: list[int]):
        super().__init__(parent)
        self.setWindowTitle("Split document")
        self.n, self.starts = n, sorted(s for s in starts if s > 0)
        lay = QVBoxLayout(self)
        self.every = QRadioButton("Every")
        self.spin = QSpinBox()
        self.spin.setRange(1, max(1, n))
        self.spin.setValue(max(1, min(n, 1 if n <= 2 else n // 2)))
        row = QHBoxLayout()
        row.addWidget(self.every)
        row.addWidget(self.spin)
        row.addWidget(QLabel("page(s)"))
        row.addStretch()
        lay.addLayout(row)
        self.at = QRadioButton(f"Before each selected page ({pageops.format_ranges(self.starts)})" if self.starts
                               else "Before each selected page (select pages first)")
        self.at.setEnabled(bool(self.starts))
        lay.addWidget(self.at)
        g = QButtonGroup(self)
        g.addButton(self.every)
        g.addButton(self.at)
        (self.at if self.starts else self.every).setChecked(True)
        self.info = QLabel()
        lay.addWidget(self.info)
        lay.addWidget(_buttons(self, "Split…"))
        for sig in (self.every.toggled, self.spin.valueChanged):
            sig.connect(lambda *_: self._update())
        self._update()

    def groups(self) -> list[list[int]]:
        if self.at.isChecked():
            return pageops.split_at(self.n, self.starts)
        return pageops.split_every(self.n, self.spin.value())

    def _update(self) -> None:
        self.info.setText(f"Creates {len(self.groups())} file(s).")


class InsertDialog(QDialog):
    def __init__(self, parent, name: str, count: int, n: int, row: int):
        super().__init__(parent)
        self.setWindowTitle("Insert pages")
        lay = QFormLayout(self)
        lay.addRow("From:", QLabel(f"{name}  ({count} page(s))"))
        self.status = QLabel()
        self.range = RangeEdit(count, "", self.status)
        self.range.setEnabled(count > 0)
        lay.addRow("Pages:", self.range)
        lay.addRow("", self.status)
        self.where = QComboBox()
        self.where.addItems(["After page", "Before page"])
        self.page = QSpinBox()
        self.page.setRange(1, max(1, n))
        self.page.setValue(max(1, min(n, row)))
        pos = QHBoxLayout()
        pos.addWidget(self.where)
        pos.addWidget(self.page)
        pos.addWidget(QLabel(f"of {n}"))
        pos.addStretch()
        lay.addRow("Position:", pos)
        lay.addRow(_buttons(self, "Insert"))

    def row(self) -> int:
        return self.page.value() - (1 if self.where.currentIndex() == 1 else 0)


def _save_files(win, groups: list[list[int]], path: str) -> int:
    """Save each group of rows to a numbered file next to `path`. Returns how many were written."""
    doc = win.canvas.doc
    paths = [path] if len(groups) == 1 else pageops.numbered_paths(path, len(groups))
    clash = [p for p in paths if os.path.exists(p) and p != path]
    if clash and QMessageBox.question(win, APP, f"{len(clash)} file(s) like {os.path.basename(clash[0])} already exist. Replace them?") \
            != QMessageBox.StandardButton.Yes:
        return 0
    done = 0
    for p, rows in zip(paths, groups):
        if not win._save_to(p, [doc.pages[i] for i in rows]):
            break
        done += 1
    if done > 1:
        win.statusBar().showMessage(f"Saved {done} files to {os.path.dirname(path)}", 5000)
    return done


def _base(win) -> str:
    return os.path.splitext(win.canvas.doc.path or "document")[0]


def extract_dialog(win) -> None:
    doc = win.canvas.doc
    rows = win._target_rows()
    dlg = ExtractDialog(win, len(doc.pages), pageops.format_ranges(rows))
    if not dlg.exec():
        return
    sel = dlg.range.rows()
    if not sel:
        return
    each = dlg.each.isChecked() and len(sel) > 1
    title = f"Extract {len(sel)} page(s)" + (" as separate files (numbered)" if each else "")
    path, _ = QFileDialog.getSaveFileName(win, title, f"{_base(win)}-{'page' if each else 'pages'}.pdf", PDF_FILTER)
    if not path:
        return
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    _save_files(win, [[r] for r in sel] if each else [sel], path)


def split_dialog(win) -> None:
    doc = win.canvas.doc
    if not doc:
        return
    view = win._page_view()
    dlg = SplitDialog(win, len(doc.pages), view.selected_rows())
    if not dlg.exec():
        return
    groups = dlg.groups()
    path, _ = QFileDialog.getSaveFileName(win, f"Split into {len(groups)} file(s) (numbered)", f"{_base(win)}-part.pdf", PDF_FILTER)
    if path:
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        _save_files(win, groups, path)


def insert_file_dialog(win) -> None:
    doc = win.canvas.doc
    paths, _ = QFileDialog.getOpenFileNames(win, "Insert pages from", win.settings.value("lastdir", ""), OPEN_FILTER)
    if not paths:
        return
    rows = win._target_rows()
    after = (max(rows) + 1) if rows else len(doc.pages)
    if len(paths) > 1:
        win.insert_files(paths, after)
        return
    src = win._insert_source(paths[0])
    if src is None:
        return
    dlg = InsertDialog(win, os.path.basename(paths[0]), src.doc.page_count, len(doc.pages), after)
    if not dlg.exec():
        return
    sel = dlg.range.rows()
    if sel:
        win._select_pages(pageops.insert_source_pages(win.canvas, src, sel, dlg.row()))


# -------------------------------------------------------------- organizer
class Organizer(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 6, 8, 6)
        title = QLabel("Organize pages")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        hint = QLabel("Drag to reorder · Ctrl/Shift+click to select · Del deletes · Ctrl+C / Ctrl+V copy pages · double-click opens")
        hint.setEnabled(False)
        hint.setToolTip(hint.text())
        hint.setMinimumWidth(0)  # may clip on narrow windows rather than force the whole window wider
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.view = Sidebar(win.canvas, grid=True)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        lo, hi, _ = SIZES[True]
        self.slider.setRange(lo, hi)
        self.slider.setValue(self.view.thumb_w)
        self.slider.setFixedWidth(140)
        self.slider.setToolTip("Thumbnail size (Ctrl+wheel)")
        self.slider.valueChanged.connect(self.view.set_thumb_width)
        self.view.thumbSizeChanged.connect(lambda w: (self.slider.blockSignals(True), self.slider.setValue(w), self.slider.blockSignals(False)))
        done = QPushButton("Done")
        done.setDefault(True)
        done.clicked.connect(lambda: set_organizer(win, False))
        bl.addWidget(title)
        bl.addSpacing(12)
        bl.addWidget(hint, 1)
        bl.addWidget(self.slider)
        bl.addWidget(done)
        lay.addWidget(bar)
        lay.addWidget(self.view, 1)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            set_organizer(self.win, False)
            return
        super().keyPressEvent(e)


def set_organizer(win, on: bool) -> None:
    org: Organizer = win._organizer
    if on and win.canvas.doc is None:
        win.a_organizer.setChecked(False)
        return
    if on:
        org.view.rebuild()
        org.view.select_uids(win.sidebar.selected_uids())
        org.view.set_current(win.canvas.current_index())
        if hasattr(win, "show_view"):
            win.show_view("organizer")
        else:
            org.showMaximized()
        org.view.setFocus()
    else:
        uids = org.view.selected_uids()
        if hasattr(win, "show_view"):
            if win.current_view() == "organizer":
                win.show_view("document")
        else:
            org.hide()
        win.sidebar.select_uids(uids)
        if len(uids) == 1:
            win._select_pages(uids)
    win.a_organizer.blockSignals(True)
    win.a_organizer.setChecked(on)
    win.a_organizer.blockSignals(False)


# ----------------------------------------------------------------- install
def install(win) -> None:
    cv = win.canvas
    org = Organizer(win)
    win._organizer = org
    win.organizer = org.view
    if hasattr(win, "add_view"):
        win.add_view("organizer", org)
        if hasattr(win, "views"):
            win.views.currentChanged.connect(lambda _: _sync_organizer_action(win))
    else:
        org.setParent(win, Qt.WindowType.Window)
        org.setWindowTitle("Organize pages")

    def views():
        return [win.sidebar, org.view]

    def rows_for(view=None) -> list[int]:
        if view is None:
            return win._target_rows()
        return view.selected_rows() or ([cv.current_index()] if cv.order else [])

    def run(fn, view=None):
        if cv.doc is None:
            return
        win._commit_edits()
        fn(rows_for(view))

    # ---- operations (rows -> action)
    def duplicate(rows):
        win._select_pages(pageops.duplicate(cv, rows))

    def move_start(rows):
        pageops.move_rows(cv, rows, 0, "Move pages to start")
        win._select_pages([cv.doc.pages[i].uid for i in range(len(set(rows)))])

    def move_end(rows):
        n = len(cv.doc.pages)
        pageops.move_rows(cv, rows, n, "Move pages to end")
        win._select_pages([cv.doc.pages[i].uid for i in range(n - len(set(rows)), n)])

    def reverse(rows):
        pageops.reverse(cv, rows)

    def copy(rows):
        k = pageops.copy_pages(cv, rows)
        _toast(win, f"Copied {k} page(s)")
        _update_enabled()

    def cut(rows):
        if len(set(rows)) >= len(cv.doc.pages):
            QMessageBox.information(win, APP, "A PDF needs at least one page.")
            return
        pageops.copy_pages(cv, rows)
        pageops.delete(cv, rows)
        _toast(win, f"Cut {len(set(rows))} page(s)", undo=True)
        _update_enabled()

    def paste(rows):
        at = (max(rows) + 1) if rows else len(cv.doc.pages)
        kind = pageops.clipboard_kind()
        uids = pageops.paste_image(cv, at) if kind == "image" else pageops.paste_pages(cv, at)
        if uids:
            win._select_pages(uids)
        else:
            win.statusBar().showMessage("Nothing to paste: copy pages or an image first.", 3000)

    def paste_image(rows):
        at = (max(rows) + 1) if rows else len(cv.doc.pages)
        uids = pageops.paste_image(cv, at)
        if uids:
            win._select_pages(uids)
        else:
            win.statusBar().showMessage("The clipboard has no image.", 3000)

    def delete(rows):
        if len(set(rows)) >= len(cv.doc.pages):
            QMessageBox.information(win, APP, "A PDF needs at least one page.")
            return
        from ..settings import ask_delete_pages

        if not ask_delete_pages(win, len(set(rows))):
            return
        if pageops.delete(cv, rows):
            _toast(win, f"Deleted {len(set(rows))} page(s)", undo=True)

    ops = {
        "duplicate": duplicate, "copy": copy, "cut": cut, "paste": paste, "delete": delete,
    }

    def act(name, text, fn, shortcut=None, tip=None) -> QAction:
        a = QAction(text, win)
        a.setObjectName(f"a_{name}")
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        a.setToolTip((tip or text.replace("&", "").split("\t")[0].rstrip("…")) + (f"  ({sc})" if sc else ""))
        a.triggered.connect(lambda _=False: run(fn))
        win.addAction(a)
        setattr(win, f"a_{name}", a)
        return a

    # Keys shown after a tab are handled by the page list itself (so they don't clash with Ctrl+C for text).
    a_dup = act("duplicate_pages", "D&uplicate pages", duplicate, "Ctrl+Shift+D")
    a_copy = act("copy_pages", "&Copy pages\tCtrl+C", copy)
    a_cut = act("cut_pages", "Cu&t pages\tCtrl+X", cut)
    a_paste = act("paste_pages", "&Paste pages\tCtrl+V", paste, tip="Paste copied pages or a clipboard image after the selection")
    a_paste_img = act("paste_image_page", "Paste image as new page", paste_image)
    a_start = act("move_pages_start", "Move to &start\tCtrl+Home", move_start)
    a_end = act("move_pages_end", "Move to &end\tCtrl+End", move_end)
    a_rev = act("reverse_pages", "Re&verse order", reverse, tip="Reverse the selected pages (or all pages)")
    a_blank_before = act("blank_before", "Insert blank page &before", lambda rows: win.insert_blank(before=True))
    a_blank_after = act("blank_after", "Insert blank page &after", lambda rows: win.insert_blank(before=False))
    a_split = act("split_pages", "Sp&lit into files…", lambda rows: split_dialog(win))
    a_select_all = act("select_all_pages", "Select all pages\tCtrl+A", lambda rows: (win._page_view().selectAll(), win._page_view().setFocus()))
    a_org = QAction("&Organize pages", win)
    a_org.setObjectName("a_organizer")
    a_org.setCheckable(True)
    a_org.setShortcut(QKeySequence("Ctrl+Shift+O"))
    a_org.setToolTip("Organize pages: a full-window grid for reordering  (Ctrl+Shift+O)")
    a_org.toggled.connect(lambda on: set_organizer(win, on))
    win.addAction(a_org)
    win.a_organizer = a_org
    mine = [a_dup, a_copy, a_cut, a_paste, a_paste_img, a_start, a_end, a_rev, a_blank_before, a_blank_after, a_split,
            a_select_all, a_org]

    # ---- Pages menu
    m: QMenu = win.menus["pages"]
    m.clear()
    m.addAction(a_org)
    m.addSeparator()
    m.addActions([a_copy, a_cut, a_paste, a_paste_img, a_dup])
    m.addSeparator()
    m.addActions([win.a_rot_l, win.a_rot_r])
    m.addActions([a_start, a_end, a_rev])
    m.addSeparator()
    m.addActions([a_blank_before, a_blank_after, win.a_insert_file])
    m.addActions([win.a_extract, a_split])
    m.addSeparator()
    m.addActions([a_select_all, win.a_del_pages])

    def context_menu(view, pos):
        menu = QMenu(win)
        menu.addActions([a_copy, a_cut, a_paste, a_dup])
        menu.addSeparator()
        menu.addActions([win.a_rot_l, win.a_rot_r, a_start, a_end, a_rev])
        menu.addSeparator()
        menu.addActions([a_blank_before, a_blank_after, win.a_insert_file, win.a_extract, a_split])
        menu.addSeparator()
        menu.addAction(win.a_del_pages)
        menu.addSeparator()
        menu.addAction(a_org)
        view.setFocus()
        menu.exec(pos)

    try:
        win.sidebar.contextRequested.disconnect()
    except (RuntimeError, TypeError):
        pass

    def page_dropped(view, src_view, row):
        if cv.doc is None or src_view.canvas.doc is None:
            return
        rows = src_view.selected_rows()
        if rows:
            entries = pageops.materialize(cv, pageops.snapshot(src_view.canvas, rows))
            win._select_pages(pageops.insert(cv, entries, row, f"Insert {len(entries)} page(s)"))

    for view in views():
        view.contextRequested.connect(lambda pos, v=view: context_menu(v, pos))
        view.command.connect(lambda name, v=view: run(ops[name], v))
        view.pagesDropped.connect(lambda src, row, v=view: page_dropped(v, src, row))
    win.sidebar.thumbSizeChanged.connect(
        lambda w: None if win.side_dock.isFloating() else win.resizeDocks([win.side_dock], [w + 56], Qt.Orientation.Horizontal))
    def reorder_following(view, uids):
        """Reorder, then keep showing the page(s) that moved (not whatever page now sits at the old position)."""
        moved = view.selected_uids() or ([view.current_uid] if view.current_uid is not None else [])
        win._reorder(uids)
        moved = [u for u in moved if u in set(uids)]
        if moved and cv.doc is not None:
            win._select_pages(moved)
            row = next((i for i, r in enumerate(cv.doc.pages) if r.uid == moved[0]), -1)
            for v in views():
                v.set_current(row)

    org.view.orderChanged.connect(lambda uids: reorder_following(org.view, uids))
    import warnings

    with warnings.catch_warnings():  # a failed disconnect only warns
        warnings.simplefilter("ignore")
        try:
            ok = win.sidebar.orderChanged.disconnect(win._reorder)
        except (RuntimeError, TypeError, SystemError):
            ok = False
    if ok:
        win.sidebar.orderChanged.connect(lambda uids: reorder_following(win.sidebar, uids))
    org.view.filesDropped.connect(win.insert_files)
    org.view.pageActivated.connect(cv.go_to)
    org.view.itemDoubleClicked.connect(lambda it: (set_organizer(win, False), cv.go_to(org.view.row(it))))

    def pages_changed():
        _update_enabled()
        if org.isVisible():
            org.view.rebuild()
            org.view.set_current(cv.current_index())
        _page_info()

    cv.pagesChanged.connect(pages_changed)
    cv.pageChanged.connect(lambda i: (org.view.set_current(i) if org.isVisible() else None, _page_info()))

    # ---- status: size of the current page
    info = QLabel()
    info.setContentsMargins(6, 0, 6, 0)
    win.statusBar().insertPermanentWidget(0, info)
    win._page_info_label = info

    def _page_info():
        doc = cv.doc
        i = cv.current_index()
        if doc is None or not 0 <= i < len(doc.pages):
            info.setText("")
            return
        ref = doc.pages[i]
        w, h = doc.display_size(ref)
        info.setText(pageops.size_text(w, h))
        info.setToolTip(pageops.page_tooltip(doc, ref, i))

    def _update_enabled():
        has = cv.doc is not None
        for a in mine:
            a.setEnabled(has)

    _update_enabled()


def _sync_organizer_action(win) -> None:
    on = getattr(win, "current_view", lambda: "")() == "organizer"
    _organizer_chrome(win, on)
    if win.a_organizer.isChecked() != on:
        win.a_organizer.blockSignals(True)
        win.a_organizer.setChecked(on)
        win.a_organizer.blockSignals(False)


def _organizer_chrome(win, on: bool) -> None:
    """The organizer is a full-window view: the panels (Pages, Style, ...) step aside while it shows and come back
    when it closes. They are parked like on the welcome screen, so closing the window meanwhile doesn't remember a
    layout without panels."""
    from PySide6.QtWidgets import QDockWidget, QTabBar

    parked = getattr(win, "_parked_docks", None)
    if on:
        if getattr(win, "_organizer_docks", None) is not None:
            return
        docks = [d for d in win.findChildren(QDockWidget) if not d.isHidden()]
        front = [d for d in docks if not d.visibleRegion().isEmpty()]  # the current tab of tabified panels
        win._organizer_docks = (docks, front)
        if parked is not None:
            parked += [d for d in docks if d not in parked]
        for d in docks:
            d.hide()
        for tb in win.findChildren(QTabBar, options=Qt.FindChildOption.FindDirectChildrenOnly):
            tb.hide()  # Qt leaves the tab strip of tabified docks on screen
    else:
        state, win._organizer_docks = getattr(win, "_organizer_docks", None), None
        if state is None:
            return
        docks, front = state
        for d in docks:
            if parked is not None and d in parked:
                parked.remove(d)
            d.show()
        for d in front:
            d.raise_()
