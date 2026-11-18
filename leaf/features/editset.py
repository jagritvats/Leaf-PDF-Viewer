"""Edit sets in the window: File › Import edit set… (preview with a checkbox per change, applied as one undo step)
and File › Export edit set… (the pending changes as JSON). Format and engine: leaf/editset.py, docs/edit-set.md."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from .. import editset as E

PATH_ROLE = Qt.ItemDataRole.UserRole + 1


def _toast(win, text: str, ms: int = 3000) -> None:
    t = getattr(win, "toast", None)
    if t is not None:
        t(text, ms=ms)


def _start_dir(win) -> str:
    doc = win.canvas.doc
    return os.path.dirname(doc.path) if doc is not None and doc.path else os.path.expanduser("~")


def _stem(win) -> str:
    doc = win.canvas.doc
    return os.path.splitext(os.path.basename(doc.path))[0] if doc is not None and doc.path else "document"


class ImportDialog(QDialog):
    """Lists every change of an edit set with a checkbox (grouped by section); OK applies the checked ones."""

    def __init__(self, parent, data: dict, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Import edit set")
        self.setAccessibleName("Import edit set")
        self.data = data
        lay = QVBoxLayout(self)
        n = len(E.entries(data))
        head = QLabel(f"<b>{n} change{'s' if n != 1 else ''}</b>" + (f" from {name}" if name else "")
                      + ". Uncheck the ones you don't want; the rest are applied as one step you can undo.")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Changes in the edit set")
        self.tree.setMinimumSize(520, 320)
        groups: dict[str, QTreeWidgetItem] = {}
        for path, label, text in E.entries(data):
            g = groups.get(label)
            if g is None:
                g = groups[label] = QTreeWidgetItem(self.tree, [label])
                g.setFlags(g.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                g.setCheckState(0, Qt.CheckState.Checked)
                g.setExpanded(True)
            it = QTreeWidgetItem(g, [text])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked)
            it.setData(0, PATH_ROLE, list(path))
            it.setToolTip(0, text)
        lay.addWidget(self.tree)
        notes = []
        if data.get("pages"):
            notes.append("Page numbers refer to the new page list; unchecking the page list changes which pages "
                         "they mean.")
        skipped = (data.get("document") or {}).get("not_exported")
        if skipped:
            notes.append("Not in this edit set: " + "; ".join(skipped) + ".")
        if data.get("ocr") is not None:
            notes.append("Text recognition runs right away and may take a while.")
        if notes:
            lab = QLabel(" ".join(notes))
            lab.setWordWrap(True)
            lab.setStyleSheet("color: palette(placeholder-text);")
            lay.addWidget(lab)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.tree.setFocus()

    def items(self) -> list[QTreeWidgetItem]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            out += [g.child(k) for k in range(g.childCount())]
        return out

    def selected_paths(self) -> list[tuple]:
        return [tuple(it.data(0, PATH_ROLE)) for it in self.items() if it.checkState(0) == Qt.CheckState.Checked]

    def selected_data(self) -> dict:
        return E.subset(self.data, self.selected_paths())


def apply_selected(win, data: dict, base_dir: str | None = None) -> list[str]:
    """Apply an (already chosen) edit set to the window's document as one undo step; report the outcome."""
    n = len(E.entries(data))
    if not n:
        return []
    win.canvas._clear_focus()
    try:
        warnings = E.apply_edits(win.canvas, data, label=f"Apply edit set ({n} change{'s' if n != 1 else ''})",
                                 base_dir=base_dir)
    except Exception as ex:  # noqa: BLE001
        QMessageBox.critical(win, "Leaf", f"The edit set couldn't be applied:\n{ex}")
        return [str(ex)]
    if warnings:
        box = QMessageBox(QMessageBox.Icon.Information, "Edit set applied",
                          f"Applied, with {len(warnings)} note{'s' if len(warnings) != 1 else ''}.", parent=win)
        box.setDetailedText("\n".join(warnings))
        box.setInformativeText("\n".join(warnings[:6]) + ("\n…" if len(warnings) > 6 else ""))
        box.setModal(False)
        box.show()
    else:
        _toast(win, f"Edit set applied ({n} change{'s' if n != 1 else ''}). Ctrl+Z undoes it.")
    return warnings


def import_edit_set(win, path: str | None = None) -> bool:
    if win.canvas.doc is None:
        return False
    if path is None:
        path, _ = QFileDialog.getOpenFileName(win, "Import edit set", _start_dir(win),
                                              "Edit sets (*.json);;All files (*)")
        if not path:
            return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as ex:
        QMessageBox.warning(win, "Leaf", f"Couldn't read {os.path.basename(path)}:\n{ex}")
        return False
    problems = E.validate(data)
    if problems:
        QMessageBox.warning(win, "Leaf", f"{os.path.basename(path)} isn't a usable edit set:\n\n"
                            + "\n".join(problems[:12]) + ("\n…" if len(problems) > 12 else ""))
        return False
    if not E.entries(data):
        _toast(win, "The edit set has no changes.")
        return False
    dlg = ImportDialog(win, data, os.path.basename(path))
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    apply_selected(win, dlg.selected_data(), os.path.dirname(os.path.abspath(path)))
    return True


def export_edit_set(win, path: str | None = None) -> bool:
    if win.canvas.doc is None:
        return False
    win.canvas._clear_focus()
    data = E.export_edits(win.canvas)
    n = len(E.entries(data))
    if path is None:
        path, _ = QFileDialog.getSaveFileName(win, "Export edit set",
                                              os.path.join(_start_dir(win), _stem(win) + ".edits.json"),
                                              "Edit sets (*.json)")
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as ex:
        QMessageBox.critical(win, "Leaf", f"Couldn't write {os.path.basename(path)}:\n{ex}")
        return False
    skipped = (data.get("document") or {}).get("not_exported")
    _toast(win, f"Exported {n} change{'s' if n != 1 else ''} to {os.path.basename(path)}"
           + (f" (not included: {'; '.join(skipped)})" if skipped else ""), 4500 if skipped else 3000)
    return True


def _action(win, name: str, text: str, slot, tip: str) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    a.setProperty("base_tip", tip)
    a.setStatusTip(tip)
    a.triggered.connect(lambda _=False: slot(win))
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def install(win) -> None:
    imp = _action(win, "import_edit_set", "Import edit set…", import_edit_set,
                  "Apply changes described in a JSON edit set (preview first)")
    exp = _action(win, "export_edit_set", "Export edit set…", export_edit_set,
                  "Save the pending changes as a JSON edit set (for the command line or another copy)")
    m = getattr(win, "menus", {}).get("file")
    if m is not None:
        acts = m.actions()
        close = getattr(win, "a_close", None)
        before = None
        if close in acts:
            k = acts.index(close)
            before = acts[k - 1] if k and acts[k - 1].isSeparator() else close
        if before is None:
            m.addSeparator()
            m.addActions([imp, exp])
        else:
            m.insertSeparator(before)
            m.insertActions(before, [imp, exp])
    cv = win.canvas

    def sync() -> None:
        has = cv.doc is not None
        imp.setEnabled(has)
        exp.setEnabled(has)

    cv.pagesChanged.connect(sync)
    cv.documentChanged.connect(sync)
    sync()
