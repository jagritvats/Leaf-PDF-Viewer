"""Attachments: the files embedded in the document (/EmbeddedFiles) and file-attachment annotations on its pages.

Model: `PdfDocument.attachments` is None while the embedded files are untouched (saving leaves them as they are), else
a tuple of `Attachment`: the original ones by their /EmbeddedFiles key (possibly renamed or re-described), and files
added in Leaf with their bytes. Changed only through the undoable `SetAttachments`; written at save by
`write_attachments` (a document-level step, before Sanitize, which may still remove every attachment).

File-attachment annotations belong to their page: the panel lists them (open, save, go to) and removes one as the
annotation it is (an undoable delete through the canvas, like deleting it on the page)."""
from __future__ import annotations

import itertools
import os
import tempfile
from dataclasses import dataclass, field, replace

import pymupdf
from PySide6.QtCore import QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QUndoCommand
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

_ids = itertools.count(1)
ROLE = Qt.ItemDataRole.UserRole + 1
# opening these runs code (or a script host); asked about first
RISKY = {".exe", ".com", ".bat", ".cmd", ".msi", ".msp", ".scr", ".pif", ".cpl", ".dll", ".jar", ".js", ".jse", ".vbs",
         ".vbe", ".wsf", ".wsh", ".ps1", ".psm1", ".hta", ".lnk", ".reg", ".sh", ".bash", ".zsh", ".command", ".app",
         ".py", ".pyw", ".pl", ".rb", ".php", ".appimage", ".deb", ".rpm", ".dmg", ".pkg", ".run", ".bin", ".desktop",
         ".url", ".scf", ".inf", ".msc", ".gadget", ".application", ".appx", ".msix", ".xll", ".xlam", ".docm",
         ".xlsm", ".pptm", ".iso", ".img", ".vhd", ".vhdx"}


@dataclass(frozen=True)
class Attachment:
    name: str  # file name shown and written as /F and /UF
    desc: str = ""
    key: str | None = None  # the original /EmbeddedFiles name; None: added in Leaf
    data: bytes | None = field(default=None, compare=False, repr=False)  # content of a file added in Leaf
    size: int = 0
    id: int = field(default_factory=lambda: next(_ids))


def is_risky(name: str) -> bool:
    return os.path.splitext(name.strip().lower())[1] in RISKY


def source_files(doc) -> list[Attachment]:
    d = doc.primary.doc
    out = []
    try:
        names = d.embfile_names()
    except Exception:  # noqa: BLE001
        return out
    for key in names:
        try:
            info = d.embfile_info(key)
        except Exception:  # noqa: BLE001
            continue
        name = info.get("ufilename") or info.get("filename") or key
        desc = (info.get("description") or info.get("desc") or "")
        out.append(Attachment(name, "" if desc in (name, key) else desc, key, None,
                              int(info.get("size") or info.get("length") or 0)))
    return out


def files_of(doc) -> list[Attachment]:
    return list(doc.attachments) if doc.attachments is not None else source_files(doc)


def data_of(doc, a: Attachment) -> bytes:
    if a.data is not None:
        return a.data
    return doc.primary.doc.embfile_get(a.key)


def _page_files(page) -> list[tuple]:
    """(xref, file name, description, size) of a source page's file-attachment annotations."""
    out = []
    for x, t, _ in page.annot_xrefs():
        if t != pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
            continue
        try:
            info = page.load_annot(x).file_info
        except Exception:  # noqa: BLE001
            continue
        name = info.get("ufilename") or info.get("filename") or "attachment"
        desc = info.get("description") or info.get("desc") or ""
        out.append((x, name, "" if desc == name else desc, int(info.get("size") or info.get("length") or 0)))
    return out


def annotation_files(canvas, cache: dict | None = None) -> list[tuple]:
    """File-attachment annotations on the pages as shown: (page item, xref, file name, description, size), in page
    order, without the ones deleted in Leaf. `cache` keeps each source page's list (sources never change)."""
    doc = canvas.doc
    out = []
    if doc is None:
        return out
    for pg in canvas.order:
        key = (id(pg.ref.src), pg.ref.index)
        found = cache.get(key) if cache is not None else None
        if found is None:
            try:
                found = _page_files(doc.page(pg.ref))
            except Exception:  # noqa: BLE001
                found = []
            if cache is not None:
                cache[key] = found
        if not found:
            continue
        gone = set()
        if pg.adopted is not None:
            xs = {f[0] for f in found}
            gone = {it.xref for it in pg.adopted if getattr(it, "xref", None) in xs and it.scene() is None}
        out += [(pg, x, name, desc, size) for x, name, desc, size in found if x not in gone]
    return out


def annotation_data(canvas, pg, xref: int) -> bytes:
    return canvas.doc.page(pg.ref).load_annot(xref).get_file()


def unique_key(name: str, taken) -> str:
    taken = set(taken)
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    for n in itertools.count(2):
        k = f"{stem} ({n}){ext}"
        if k not in taken:
            return k


def _sig(out, name: str) -> tuple:
    i = out.embfile_info(name)
    return i.get("ufilename"), i.get("filename"), i.get("length"), i.get("size")


def delete_embedded(out: pymupdf.Document, key: str) -> None:
    """Remove one /EmbeddedFiles entry. (PyMuPDF's embfile_del removes the wrong array items for any entry but the
    first: it deletes positions idx and idx + 1 of the key/value array instead of 2 idx and 2 idx + 1.)"""
    names = out.embfile_names()
    if key not in names:
        return
    idx = names.index(key)
    want = [n for n in names if n != key]
    sig = {n: _sig(out, n) for n in want}
    try:
        from pymupdf import mupdf

        pdf = mupdf.pdf_document_from_fz_document(out.this)
        arr = mupdf.pdf_dict_getl(mupdf.pdf_trailer(pdf), mupdf.pdf_new_name("Root"), mupdf.pdf_new_name("Names"),
                                  mupdf.pdf_new_name("EmbeddedFiles"), mupdf.pdf_new_name("Names"))
        if not mupdf.pdf_is_array(arr) or mupdf.pdf_array_len(arr) < 2 * idx + 2:
            raise ValueError("unexpected name tree")
        mupdf.pdf_array_delete(arr, 2 * idx + 1)
        mupdf.pdf_array_delete(arr, 2 * idx)
    except Exception:  # noqa: BLE001  (a name tree with /Kids, other bindings: the library's own call)
        out.embfile_del(key)
    if out.embfile_names() != want or any(_sig(out, n) != sig[n] for n in want):
        raise RuntimeError(f"Couldn't remove the attachment {key!r} cleanly.")


def write_attachments(doc, out: pymupdf.Document) -> None:
    """Make the output's embedded files match `doc.attachments`."""
    items = list(doc.attachments or ())
    orig = list(out.embfile_names())
    keep = {a.key for a in items if a.key}
    for k in orig:
        if k not in keep:
            delete_embedded(out, k)
    names = set(out.embfile_names())
    for a in items:
        if a.key and a.key in names:
            info = out.embfile_info(a.key)
            cur = info.get("ufilename") or info.get("filename") or a.key
            cur_desc = (info.get("description") or info.get("desc") or "")
            if cur_desc in (cur, a.key):
                cur_desc = ""
            if a.name != cur or a.desc != cur_desc:
                out.embfile_upd(a.key, filename=a.name, ufilename=a.name, desc=a.desc or a.name)
        elif a.data is not None:
            key = unique_key(a.name, names)
            names.add(key)
            out.embfile_add(key, a.data, filename=a.name, ufilename=a.name, desc=a.desc or a.name)


# --------------------------------------------------------------------------------------------------- commands
class SetAttachments(QUndoCommand):
    def __init__(self, canvas, old, new, text: str, on_change=None):
        super().__init__(text)
        self.canvas, self.old, self.new, self.on_change = canvas, old, new, on_change

    def _set(self, v) -> None:
        self.canvas.doc.attachments = v
        if self.on_change is not None:
            self.on_change()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def set_files(canvas, files, text: str, on_change=None) -> bool:
    doc = canvas.doc
    new = tuple(files)
    cur = files_of(doc)
    if [(a.name, a.desc, a.key, a.id if a.key is None else 0) for a in new] == \
            [(a.name, a.desc, a.key, a.id if a.key is None else 0) for a in cur]:
        return False
    canvas.undo.push(SetAttachments(canvas, doc.attachments, new, text, on_change))
    return True


def add_files(canvas, paths, on_change=None) -> list[Attachment]:
    """Attach files (read now, kept in memory until saved). Returns the new attachments."""
    added = []
    for p in paths:
        with open(p, "rb") as f:
            data = f.read()
        added.append(Attachment(os.path.basename(p), "", None, data, len(data)))
    if added:
        n = len(added)
        set_files(canvas, files_of(canvas.doc) + added, "Attach file" if n == 1 else f"Attach {n} files", on_change)
    return added


def remove_file(canvas, ident: int, on_change=None) -> bool:
    files = files_of(canvas.doc)
    keep = [a for a in files if a.id != ident]
    return len(keep) != len(files) and set_files(canvas, keep, "Remove attachment", on_change)


def edit_file(canvas, ident: int, name: str, desc: str, on_change=None) -> bool:
    name = os.path.basename(name.strip().replace("\\", "/")) or ""
    if not name:
        return False
    files = [replace(a, name=name, desc=desc.strip()) if a.id == ident else a for a in files_of(canvas.doc)]
    return set_files(canvas, files, "Rename attachment", on_change)


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
    return ""


# ------------------------------------------------------------------------------------------------- seams (tests)
def confirm_risky(parent, name: str) -> bool:
    r = QMessageBox.warning(parent, "Open attachment",
                            f"“{name}” is a program or script. Opening it can run code on your computer.\n\n"
                            "Open it only if you trust where this document came from.",
                            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                            QMessageBox.StandardButton.Cancel)
    return r == QMessageBox.StandardButton.Open


def open_url(url: QUrl) -> bool:
    return QDesktopServices.openUrl(url)


def ask_details(parent, name: str, desc: str):
    """(name, description) or None."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Attachment")
    f = QFormLayout(dlg)
    e_name = QLineEdit(name)
    e_desc = QLineEdit(desc)
    f.addRow("File name:", e_name)
    f.addRow("Description:", e_desc)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    f.addRow(bb)
    dlg.resize(380, dlg.sizeHint().height())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return e_name.text(), e_desc.text()


def ask_save_path(parent, name: str) -> str:
    path, _ = QFileDialog.getSaveFileName(parent, "Save attachment as", name)
    return path


def ask_open_paths(parent) -> list[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, "Attach files")
    return paths


_temp_dir: str | None = None


def temp_copy(name: str, data: bytes) -> str:
    """Write an attachment to a private temp folder (a fresh sub-folder per file, its own name kept)."""
    global _temp_dir
    if _temp_dir is None or not os.path.isdir(_temp_dir):
        _temp_dir = tempfile.mkdtemp(prefix="leaf-attachments-")
    d = tempfile.mkdtemp(dir=_temp_dir)
    safe = "".join(c for c in os.path.basename(name.replace("\\", "/")) if c not in '<>:"/\\|?*' and ord(c) >= 32)
    path = os.path.join(d, safe.strip(" .") or "attachment")
    with open(path, "wb") as f:
        f.write(data)
    return path


# ---------------------------------------------------------------------------------------------------- panel
class _Tree(QTreeWidget):
    filesDropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    @staticmethod
    def _paths(e) -> list[str]:
        md = e.mimeData()
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and os.path.isfile(u.toLocalFile())]

    def dragEnterEvent(self, e) -> None:
        if self._paths(e):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if self._paths(e):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        paths = self._paths(e)
        if paths:
            e.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            super().dropEvent(e)


class AttachmentsPanel(QWidget):
    changed = Signal()  # the list changed (count for the dock title)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.rows: list = []  # per top-level row: ("file", Attachment) | ("annot", (page item, xref, name))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setEnabled(False)
        self.tree = _Tree()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.setHeaderLabels(["Name", "Size", "Description"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.header().setStretchLastSection(True)
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 60)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.note)
        btns = QHBoxLayout()
        btns.setSpacing(4)
        self.b_add = QPushButton("Add…")
        self.b_open = QPushButton("Open")
        self.b_save = QPushButton("Save as…")
        self.b_edit = QPushButton("Rename…")
        self.b_remove = QPushButton("Remove")
        for b in (self.b_add, self.b_open, self.b_save, self.b_edit, self.b_remove):
            b.setAutoDefault(False)
            btns.addWidget(b)
        lay.addLayout(btns)
        self.b_add.setToolTip("Attach files to the document (or drop them on this list)")
        self.b_open.setToolTip("Open with the app your system uses for this kind of file")
        self.b_edit.setToolTip("Change the file name or description")
        self.b_add.clicked.connect(self.add_dialog)
        self.b_open.clicked.connect(lambda: self.open_row(self.current_row()))
        self.b_save.clicked.connect(lambda: self.save_row(self.current_row()))
        self.b_edit.clicked.connect(lambda: self.edit_row(self.current_row()))
        self.b_remove.clicked.connect(lambda: self.remove_row(self.current_row()))
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.open_row(it.data(0, ROLE)))
        self.tree.filesDropped.connect(self.add_paths)
        self.tree.installEventFilter(self)
        self._stale = True
        self._annot_cache: dict = {}
        self._timer = QTimer(self, singleShot=True, interval=80, timeout=self.refresh)
        canvas.documentChanged.connect(self._doc_changed)
        canvas.pagesChanged.connect(self.schedule)
        canvas.undo.indexChanged.connect(self._undo_changed)  # (bound: disconnected when the panel goes)
        self._sync_buttons()

    # ------------------------------------------------------------- listing
    def _undo_changed(self, _i: int = 0) -> None:
        from shiboken6 import isValid

        if not isValid(self):  # the undo stack can outlive the panel while a window closes
            return
        self.schedule()

    def _doc_changed(self) -> None:
        self._annot_cache = {}
        self.schedule()

    def schedule(self) -> None:
        self._stale = True
        if self.isVisible():
            self._timer.start()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self._stale:
            self.refresh()

    def count(self) -> int:
        return len(self.rows)

    def refresh(self) -> None:
        self._stale = False
        sel = self.current_row()
        sel_key = (sel[0], sel[1].id) if sel and sel[0] == "file" else (sel[0], sel[1][1]) if sel else None
        self.tree.clear()
        self.rows = []
        doc = self.canvas.doc
        if doc is not None:
            for a in files_of(doc):
                it = QTreeWidgetItem([a.name, fmt_size(a.size), a.desc])
                it.setToolTip(0, a.name + ("\n(added, written when you save)" if a.key is None else ""))
                if is_risky(a.name):
                    it.setToolTip(0, it.toolTip(0) + "\nA program or script: opening it can run code.")
                self._add_row(it, ("file", a))
            for pg, x, name, desc, size in annotation_files(self.canvas, self._annot_cache):
                i = self.canvas.order.index(pg) if pg in self.canvas.order else -1
                it = QTreeWidgetItem([name, fmt_size(size), f"On page {i + 1}" + (f" · {desc}" if desc else "")])
                it.setToolTip(0, f"{name}\nAttached to page {i + 1} (a file-attachment comment)")
                self._add_row(it, ("annot", (pg, x, name)))
        san = getattr(doc, "sanitize", None) if doc is not None else None
        if doc is None:
            self.note.setText("")
        elif san and getattr(san, "attachments", False):
            self.note.setText("Sanitize is set to remove all attachments when you save.")
        elif not self.rows and getattr(self.tree, "empty_hint", None) is None:  # else the list's own empty state says it
            self.note.setText("No attached files. Drop files here or click Add.")
        else:
            self.note.setText("")
        self.note.setVisible(bool(self.note.text()))
        if sel_key is not None:
            for k in range(self.tree.topLevelItemCount()):
                row = self.tree.topLevelItem(k).data(0, ROLE)
                key = (row[0], row[1].id) if row[0] == "file" else (row[0], row[1][1])
                if key == sel_key:
                    self.tree.setCurrentItem(self.tree.topLevelItem(k))
                    break
        self._sync_buttons()
        self.changed.emit()

    def _add_row(self, it: QTreeWidgetItem, row) -> None:
        it.setData(0, ROLE, row)
        it.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tree.addTopLevelItem(it)
        self.rows.append(row)

    def current_row(self):
        it = self.tree.currentItem()
        return it.data(0, ROLE) if it is not None and it.isSelected() else None

    def select(self, k: int) -> None:
        it = self.tree.topLevelItem(k)
        if it is not None:
            self.tree.setCurrentItem(it)
            it.setSelected(True)

    def _sync_buttons(self) -> None:
        row = self.current_row()
        has_doc = self.canvas.doc is not None
        self.b_add.setEnabled(has_doc)
        for b in (self.b_open, self.b_save, self.b_remove):
            b.setEnabled(row is not None)
        self.b_edit.setEnabled(row is not None and row[0] == "file")
        if row is not None and row[0] == "annot":
            self.b_remove.setToolTip("Delete this file-attachment comment from its page")
        else:
            self.b_remove.setToolTip("Remove the attached file from the document")

    def eventFilter(self, obj, e) -> bool:
        if obj is self.tree and e.type() == QEvent.Type.KeyPress:
            row = self.current_row()
            if row is not None and e.key() == Qt.Key.Key_Delete:
                self.remove_row(row)
                return True
            if row is not None and e.key() == Qt.Key.Key_F2:
                self.edit_row(row)
                return True
        return super().eventFilter(obj, e)

    # ------------------------------------------------------------- actions
    def data_of(self, row) -> tuple[str, bytes]:
        kind, v = row
        if kind == "file":
            return v.name, data_of(self.canvas.doc, v)
        pg, x, name = v
        return name, annotation_data(self.canvas, pg, x)

    def open_row(self, row) -> str | None:
        if row is None:
            return None
        try:
            name, data = self.data_of(row)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Open attachment", f"Couldn't read the attachment:\n{ex}")
            return None
        if is_risky(name) and not confirm_risky(self, name):
            return None
        path = temp_copy(name, data)
        open_url(QUrl.fromLocalFile(path))
        return path

    def save_row(self, row) -> str | None:
        if row is None:
            return None
        name = row[1].name if row[0] == "file" else row[1][2]
        path = ask_save_path(self, name)
        if not path:
            return None
        try:
            _, data = self.data_of(row)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Save attachment", f"Couldn't save the attachment:\n{ex}")
            return None
        return path

    def add_dialog(self) -> None:
        if self.canvas.doc is None:
            return
        paths = ask_open_paths(self)
        if paths:
            self.add_paths(paths)

    def add_paths(self, paths) -> None:
        if self.canvas.doc is None or not paths:
            return
        try:
            add_files(self.canvas, paths, self.refresh)
        except OSError as ex:
            QMessageBox.warning(self, "Attach files", f"Couldn't read the file:\n{ex}")

    def edit_row(self, row) -> None:
        if row is None or row[0] != "file":
            return
        a = row[1]
        r = ask_details(self, a.name, a.desc)
        if r is not None:
            edit_file(self.canvas, a.id, r[0], r[1], self.refresh)

    def remove_row(self, row) -> None:
        if row is None:
            return
        if row[0] == "file":
            remove_file(self.canvas, row[1].id, self.refresh)
            return
        pg, x, _ = row[1]
        from . import commands as C
        items = self.canvas.annots.ensure(pg)
        it = next((i for i in items if getattr(i, "xref", None) == x and i.scene() is not None), None)
        if it is None:
            self.canvas.message.emit("That attachment can't be removed here; select it on its page and delete it.")
            return
        it.setSelected(False)
        self.canvas.undo.push(C.RemoveItems([it], "Delete attachment"))
        self.refresh()
