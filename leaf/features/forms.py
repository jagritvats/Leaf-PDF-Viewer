"""Forms feature: a Forms menu (field highlight, next required field, reset, data export/import,
flattened copy) and form authoring (new text/checkbox/radio/dropdown/list/signature fields)."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .. import fields, forms

DATA_FILTER = "Form data (*.json *.xfdf);;JSON (*.json);;XFDF (*.xfdf)"


def _action(win, name: str, text: str, slot, shortcut: str | None = None, tip: str | None = None,
            checkable: bool = False) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if shortcut:
        a.setShortcut(QKeySequence(shortcut))
    a.setCheckable(checkable)
    sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    a.setToolTip((tip or text.replace("&", "").rstrip("…")) + (f"  ({sc})" if sc else ""))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def _toast(win, text: str) -> None:
    try:
        from ..toast import show_toast
    except ImportError:
        win.statusBar().showMessage(text, 5000)
        return
    show_toast(win, text)


def _start_dir(win) -> str:
    doc = win.canvas.doc
    if doc is not None and doc.path:
        return os.path.dirname(doc.path)
    return win.settings.value("lastdir", "") or ""


def _stem(win) -> str:
    doc = win.canvas.doc
    return os.path.splitext(os.path.basename(doc.path))[0] if doc is not None and doc.path else "form"


# ------------------------------------------------------------- actions
def next_required(win) -> None:
    if win.canvas.doc is None:
        return
    if forms.next_required_empty(win.canvas) is None:
        _toast(win, "All required fields are filled in.")


def reset_form(win) -> None:
    cv = win.canvas
    if cv.doc is None:
        return
    if not cv.doc.form_values:
        _toast(win, "The form already has its original values.")
        return
    cv._clear_focus()
    forms.reset_form(cv)


def export_data(win) -> None:
    doc = win.canvas.doc
    if doc is None:
        return
    win.canvas._clear_focus()
    path, flt = QFileDialog.getSaveFileName(win, "Export form data", os.path.join(_start_dir(win), _stem(win) + ".json"),
                                            DATA_FILTER)
    if not path:
        return
    if not path.lower().endswith((".json", ".xfdf")):
        path += ".xfdf" if "XFDF" in flt and "JSON" not in flt else ".json"
    try:
        (forms.export_xfdf if path.lower().endswith(".xfdf") else forms.export_json)(doc, path)
    except Exception as ex:  # noqa: BLE001
        QMessageBox.critical(win, "Leaf", f"Couldn't export the form data:\n{ex}")
        return
    _toast(win, f"Form data exported to {os.path.basename(path)}")


def import_data(win) -> None:
    cv = win.canvas
    if cv.doc is None:
        return
    path, _ = QFileDialog.getOpenFileName(win, "Import form data", _start_dir(win), DATA_FILTER + ";;All files (*)")
    if not path:
        return
    cv._clear_focus()
    try:
        data = forms.read_xfdf(path) if path.lower().endswith((".xfdf", ".xml")) else forms.read_json(path)
        n = forms.import_values(cv, data)
    except Exception as ex:  # noqa: BLE001
        QMessageBox.critical(win, "Leaf", f"Couldn't import the form data:\n{ex}")
        return
    _toast(win, f"Filled {n} field{'s' if n != 1 else ''}" if n else "No matching fields to change.")


def save_flattened(win) -> None:
    cv = win.canvas
    if cv.doc is None:
        return
    cv._clear_focus()
    path, _ = QFileDialog.getSaveFileName(win, "Save flattened copy",
                                          os.path.join(_start_dir(win), _stem(win) + "-flattened.pdf"), "PDF (*.pdf)")
    if not path:
        return
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    if cv.doc.path and os.path.abspath(path) == os.path.abspath(cv.doc.path):
        QMessageBox.warning(win, "Leaf", "Choose a different file: the flattened copy can't replace the open document.")
        return
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        forms.save_flattened(cv, path)
    except Exception as ex:  # noqa: BLE001
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(win, "Leaf", f"Couldn't save the flattened copy:\n{ex}")
        return
    QApplication.restoreOverrideCursor()
    _toast(win, f"Saved flattened copy {os.path.basename(path)}")


def toggle_highlight(win, on: bool) -> None:
    forms.set_highlight(win.canvas, on)
    win.settings.setValue("forms/highlight", bool(on))


# -------------------------------------------------------------- install
def install(win) -> None:
    cv = win.canvas
    cv.defaults.setdefault("field", {})

    hl = _action(win, "field_highlight", "&Highlight fields", lambda on: toggle_highlight(win, on), checkable=True,
                 tip="Show form fields with a tinted background")
    on = win.settings.value("forms/highlight", True)
    hl.setChecked(on not in (False, "false", "0", 0))
    forms.set_highlight(cv, hl.isChecked())
    nxt = _action(win, "next_required_field", "&Next required empty field", lambda: next_required(win), "Ctrl+Shift+J")
    reset = _action(win, "reset_form", "&Reset form", lambda: reset_form(win),
                    tip="Put every field back to the value it had when the file was opened")
    exp = _action(win, "export_form_data", "&Export form data…", lambda: export_data(win),
                  tip="Save field values as JSON or XFDF")
    imp = _action(win, "import_form_data", "&Import form data…", lambda: import_data(win),
                  tip="Fill fields from a JSON or XFDF file")
    flat = _action(win, "save_flattened", "Save &flattened copy…", lambda: save_flattened(win),
                   tip="Save a copy with the form fields turned into page content")
    adds = []
    for ftype, (label, _, _) in fields.FIELD_TYPES.items():
        adds.append(_action(win, f"add_{ftype}_field", f"{label}…", lambda _=False, t=ftype: fields.ask_new_field(win, t),
                            tip=f"Add a {label.lower()} to the current page"))

    menu = win.menuBar().addMenu("F&orms")
    menu.setObjectName("menu_forms")
    before = win.menus.get("pages")
    if before is not None:
        win.menuBar().removeAction(menu.menuAction())
        win.menuBar().insertMenu(before.menuAction(), menu)
    win.menus["forms"] = menu
    menu.addActions([hl, nxt])
    menu.addSeparator()
    add_menu = menu.addMenu("&Add field")
    add_menu.addActions(adds)
    menu.addSeparator()
    menu.addActions([reset, exp, imp])
    menu.addSeparator()
    menu.addAction(flat)

    fm = win.menus.get("file")
    if fm is not None:
        acts = fm.actions()
        anchor = getattr(win, "a_save_as", None)
        if anchor in acts and acts.index(anchor) + 1 < len(acts):
            fm.insertAction(acts[acts.index(anchor) + 1], flat)
        else:
            fm.addAction(flat)

    doc_actions = [nxt, reset, exp, imp, flat, *adds]

    def sync() -> None:
        has = cv.doc is not None
        for a in doc_actions:
            a.setEnabled(has)

    cv.pagesChanged.connect(sync)
    sync()
