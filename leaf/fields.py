"""Form authoring: new fields are overlay items (move/resize/undo like the others) whose
`apply` creates a real AcroForm widget at save time."""
from __future__ import annotations

import re

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
    QVBoxLayout,
)

from . import commands as C
from .document import field_xref
from .items import ACCENT, DEFAULTS, BoxItem, to_rect

DEFAULTS.setdefault("field", {})

# type -> (label, default size, name prefix)
FIELD_TYPES: dict[str, tuple[str, tuple[float, float], str]] = {
    "text": ("Text field", (160, 22), "Text"),
    "multiline": ("Multi-line text field", (220, 66), "Text"),
    "checkbox": ("Checkbox", (14, 14), "Check"),
    "radio": ("Radio button", (14, 14), "Group"),
    "dropdown": ("Dropdown", (150, 22), "Dropdown"),
    "listbox": ("List box", (150, 60), "List"),
    "signature": ("Signature field", (180, 48), "Signature"),
}
WIDGET_TYPE = {
    "text": pymupdf.PDF_WIDGET_TYPE_TEXT,
    "multiline": pymupdf.PDF_WIDGET_TYPE_TEXT,
    "checkbox": pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
    "radio": pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON,
    "dropdown": pymupdf.PDF_WIDGET_TYPE_COMBOBOX,
    "listbox": pymupdf.PDF_WIDGET_TYPE_LISTBOX,
    "signature": pymupdf.PDF_WIDGET_TYPE_SIGNATURE,
}
CHOICE_TYPES = ("dropdown", "listbox")


def pdf_name_token(s: str, fallback: str = "On") -> str:
    """A string usable as a PDF name (radio on-states)."""
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "_", s.strip()).strip("_")
    return s or fallback


class FormFieldItem(BoxItem):
    def __init__(self, ftype: str, rect: QRectF, props: dict | None = None):
        base = {"ftype": ftype, "name": "", "tooltip": "", "required": False, "options": [],
                "export": "", "editable": False, "multi": False}
        base.update(props or {})
        super().__init__("field", rect, base)
        self.setToolTip(self.describe())

    @property
    def ftype(self) -> str:
        return self.props["ftype"]

    def describe(self) -> str:
        label = FIELD_TYPES[self.ftype][0]
        return f"{label} “{self.props['name']}” (new; double-click to edit)"

    def props_changed(self) -> None:
        self.setToolTip(self.describe())

    def clone(self) -> "FormFieldItem":
        """Copy for Duplicate: same group for radios (new button value), otherwise a fresh unique name."""
        props = dict(self.props, options=list(self.props.get("options") or []))
        canvas = getattr(self.scene(), "canvas", None) if self.scene() else None
        if canvas is not None:
            if self.ftype == "radio":
                props["export"] = next_radio_export(canvas, props["name"])
            else:
                props["name"] = unique_name(canvas, self.ftype)
        c = FormFieldItem(self.ftype, QRectF(self._r), props)
        c.set_geometry(self.geometry())
        c.setZValue(self.zValue())
        return c

    # painting --------------------------------------------------------------
    def has_signature_image(self) -> bool:
        """A signature field with a handwritten signature image placed over it counts as signed (filled)."""
        from .forms import signature_overlay_over

        return self.ftype == "signature" and signature_overlay_over(self, self.scene_rect_in_parent())

    def paint_content(self, p: QPainter) -> None:
        r = self._r
        signed = self.has_signature_image()
        pen = QPen(QColor(214, 40, 40) if self.props.get("required") and not signed else ACCENT, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        fill = QColor(ACCENT)
        fill.setAlpha(30)
        p.setBrush(fill)
        if self.ftype == "radio":
            p.drawEllipse(r)
        else:
            p.drawRect(r)
        if self.ftype in ("checkbox", "radio") or signed:
            return
        h = r.height() if self.ftype in ("text", "dropdown") else min(r.height(), 22.0)
        f = QFont("Arial")
        f.setPixelSize(max(1, round(min(10.0, h * 0.5))))
        p.setFont(f)
        p.setPen(QColor(40, 70, 150))
        text = self.props["name"]
        if self.ftype == "signature":
            text = "✎ " + text
        box = QRectF(r.left() + 3, r.top(), r.width() - 6, h)
        if self.ftype == "dropdown":
            box.setRight(box.right() - h * 0.8)
            c = QPointF(r.right() - h * 0.5, r.top() + h / 2)
            a = h * 0.16
            p.drawLine(c + QPointF(-a, -a / 2), c + QPointF(0, a / 2))
            p.drawLine(c + QPointF(0, a / 2), c + QPointF(a, -a / 2))
        p.drawText(box, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)

    # interaction -----------------------------------------------------------
    def mouseDoubleClickEvent(self, e) -> None:
        e.accept()
        edit_field_properties(self)

    def contextMenuEvent(self, e) -> None:
        s = self.scene()
        canvas = getattr(s, "canvas", None) if s else None
        if canvas is None:
            return
        menu = QMenu(canvas)
        menu.addAction("Field properties…", lambda: edit_field_properties(self))
        menu.addAction("Delete field", lambda: canvas.undo.push(C.RemoveItems([self], "Delete field")))
        digital = getattr(canvas.window(), "sign_field_digitally", None)  # features/signing.py
        if self.ftype == "signature" and digital is not None:
            menu.addSeparator()
            menu.addAction("Sign digitally…", lambda: digital(self.props["name"]))
        e.accept()
        menu.exec(e.screenPos())

    # saving ------------------------------------------------------------------
    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        pr = self.props
        ft = self.ftype
        w = pymupdf.Widget()
        w.field_type = WIDGET_TYPE[ft]
        w.field_name = pr["name"]
        w.rect = to_rect(self.scene_rect_in_parent(), m)
        if pr.get("tooltip"):
            w.field_label = pr["tooltip"]
        flags = pymupdf.PDF_FIELD_IS_REQUIRED if pr.get("required") else 0
        if ft == "multiline":
            flags |= pymupdf.PDF_TX_FIELD_IS_MULTILINE
        if ft in CHOICE_TYPES:
            w.choice_values = list(pr.get("options") or [])
            if ft == "dropdown" and pr.get("editable"):
                flags |= pymupdf.PDF_CH_FIELD_IS_EDIT
            if ft == "listbox" and pr.get("multi"):
                flags |= pymupdf.PDF_CH_FIELD_IS_MULTI_SELECT
        if ft == "radio":
            flags |= pymupdf.PDF_BTN_FIELD_IS_RADIO | pymupdf.PDF_BTN_FIELD_IS_NO_TOGGLE_TO_OFF
            w.field_value = pdf_name_token(pr.get("export") or "", "Choice1")  # becomes the on-state name
        elif ft == "checkbox":
            w.field_value = False
        w.field_flags = flags
        w.border_width = 1
        w.border_color = (0.55, 0.55, 0.6)
        w.text_fontsize = 0
        if ft in ("checkbox", "radio"):
            w.text_font = "ZaDb"
        page.add_widget(w)
        doc = page.parent
        if ft == "radio":
            # PyMuPDF turns the first radio of a group on; new fields start unselected
            w.field_value = False
        if rotate:
            doc.xref_set_key(w.xref, "MK/R", str(rotate))  # keep the field's text upright with the page
        if ft == "radio" or rotate:
            w.update()
        if ft == "radio":
            fx = field_xref(doc, w.xref)
            if doc.xref_get_key(fx, "V")[0] == "name" and doc.xref_get_key(w.xref, "AS")[1] == "/Off":
                doc.xref_set_key(fx, "V", "/Off")


# ------------------------------------------------------------- names
def existing_field_names(canvas, exclude=None) -> dict[str, str]:
    """name -> kind ('radio' or other) for widgets in the document and authored fields."""
    names: dict[str, str] = {}
    doc = canvas.doc
    if doc is None:
        return names
    for ref in doc.pages:
        page = doc.page(ref)
        if not page.first_widget:
            continue
        for w in page.widgets():
            names[w.field_name] = "radio" if w.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON else "other"
    for it in authored_fields(canvas):
        if it is not exclude:
            names[it.props["name"]] = "radio" if it.ftype == "radio" else "other"
    return names


def authored_fields(canvas) -> list[FormFieldItem]:
    out = []
    for pg in canvas.order:
        out += [c for c in pg.content.childItems() if isinstance(c, FormFieldItem) and c.scene() is not None]
    return out


def name_problem(canvas, ftype: str, name: str, exclude=None) -> str | None:
    name = name.strip()
    if not name:
        return "A field needs a name."
    if "." in name and any(not part for part in name.split(".")):
        return "Field names can't have empty parts between dots."
    kind = existing_field_names(canvas, exclude).get(name)
    if kind is None:
        return None
    if ftype == "radio" and kind == "radio":
        return None  # joins that radio group
    return f"A field named “{name}” already exists."


def unique_name(canvas, ftype: str) -> str:
    prefix = FIELD_TYPES[ftype][2]
    names = existing_field_names(canvas)
    i = 1
    while f"{prefix}{i}" in names:
        i += 1
    return f"{prefix}{i}"


def default_props(canvas, ftype: str) -> dict:
    props = {"ftype": ftype, "name": unique_name(canvas, ftype), "tooltip": "", "required": False,
             "options": ["Option 1", "Option 2", "Option 3"] if ftype in CHOICE_TYPES else [],
             "export": "", "editable": False, "multi": False}
    return props


def next_radio_export(canvas, group: str, exclude=None) -> str:
    used = {it.props.get("export") for it in authored_fields(canvas)
            if it.ftype == "radio" and it.props["name"] == group and it is not exclude}
    i = 1
    while f"Choice{i}" in used:
        i += 1
    return f"Choice{i}"


def insert_field(canvas, ftype: str, props: dict | None = None, pg=None) -> FormFieldItem | None:
    """Add a field at the center of the visible part of the current page (one undo step)."""
    pg = pg or canvas.current_page_item()
    if pg is None:
        return None
    canvas.defaults.setdefault("field", {})
    props = dict(props or default_props(canvas, ftype))
    props["ftype"] = ftype
    if ftype == "radio" and not props.get("export"):
        props["export"] = next_radio_export(canvas, props["name"])
    pr = pg.content.rect()
    vis = pg.content.mapFromScene(canvas.mapToScene(canvas.viewport().rect())).boundingRect() & pr
    center = vis.center() if not vis.isEmpty() else pr.center()
    w, h = FIELD_TYPES[ftype][1]
    w, h = min(w, pr.width() * 0.9), min(h, pr.height() * 0.9)
    rect = QRectF(center.x() - w / 2, center.y() - h / 2, w, h)
    if ftype == "radio":  # stack the next button of a group under the previous one
        mates = [c for c in pg.content.childItems() if isinstance(c, FormFieldItem) and c.ftype == "radio"
                 and c.props["name"] == props["name"] and c.scene() is not None]
        if mates:
            last = max((m.scene_rect_in_parent() for m in mates), key=lambda r: r.bottom())
            rect = QRectF(last.left(), min(last.bottom() + 8, pr.bottom() - h), w, h)
    item = FormFieldItem(ftype, rect, props)
    item.setParentItem(pg.content)
    canvas.undo.push(C.AddItems([item], f"Add {FIELD_TYPES[ftype][0].lower()}"))
    canvas.scene().clearSelection()
    item.setSelected(True)
    canvas.set_tool("select")
    return item


# ---------------------------------------------------------- properties
class FieldDialog(QDialog):
    def __init__(self, parent, canvas, props: dict, item=None):
        super().__init__(parent)
        self.canvas, self.item = canvas, item
        ft = props["ftype"]
        self.ftype = ft
        self.setWindowTitle(FIELD_TYPES[ft][0] + " properties")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(props["name"])
        form.addRow("Group name" if ft == "radio" else "Name", self.name)
        self.tooltip = QLineEdit(props.get("tooltip", ""))
        self.tooltip.setPlaceholderText("Shown when hovering the field")
        form.addRow("Tooltip", self.tooltip)
        self.export = None
        if ft == "radio":
            self.export = QLineEdit(props.get("export") or "")
            self.export.setPlaceholderText("Value when this button is chosen")
            form.addRow("Button value", self.export)
        self.options = None
        if ft in CHOICE_TYPES:
            self.options = QPlainTextEdit("\n".join(props.get("options") or []))
            self.options.setTabChangesFocus(True)
            self.options.setFixedHeight(110)
            form.addRow("Options\n(one per line)", self.options)
        self.required = QCheckBox("Required")
        self.required.setChecked(bool(props.get("required")))
        form.addRow("", self.required)
        self.editable = self.multi = None
        if ft == "dropdown":
            self.editable = QCheckBox("Allow typing a custom value")
            self.editable.setChecked(bool(props.get("editable")))
            form.addRow("", self.editable)
        if ft == "listbox":
            self.multi = QCheckBox("Allow multiple selection")
            self.multi.setChecked(bool(props.get("multi")))
            form.addRow("", self.multi)
        lay.addLayout(form)
        self.error = QLabel()
        from .a11y import error_color  # a11y: readable on any theme

        self.error.setStyleSheet(f"color: {error_color(self)};")
        self.error.hide()
        lay.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.props = dict(props)

    def _accept(self) -> None:
        name = self.name.text().strip()
        problem = name_problem(self.canvas, self.ftype, name, exclude=self.item)
        opts = [o.strip() for o in self.options.toPlainText().splitlines() if o.strip()] if self.options else []
        if problem is None and self.options is not None and not opts:
            problem = "Add at least one option."
        if problem is None and len(set(opts)) != len(opts):
            problem = "Options must be different from each other."
        if problem:
            self.error.setText(problem)
            self.error.show()
            return
        p = self.props
        p.update(name=name, tooltip=self.tooltip.text().strip(), required=self.required.isChecked())
        if self.options is not None:
            p["options"] = opts
        if self.export is not None:
            p["export"] = pdf_name_token(self.export.text()) if self.export.text().strip() else \
                next_radio_export(self.canvas, name, exclude=self.item)
        if self.editable is not None:
            p["editable"] = self.editable.isChecked()
        if self.multi is not None:
            p["multi"] = self.multi.isChecked()
        self.accept()


def edit_field_properties(item: FormFieldItem) -> None:
    s = item.scene()
    canvas = getattr(s, "canvas", None) if s else None
    if canvas is None:
        return
    dlg = FieldDialog(canvas.window(), canvas, item.props, item)
    if dlg.exec() and dlg.props != item.props:
        canvas.undo.push(C.SetProps({item: (dict(item.props), dict(dlg.props))}, "Field properties"))


def ask_new_field(win, ftype: str) -> FormFieldItem | None:
    canvas = win.canvas
    if canvas.doc is None or canvas.current_page_item() is None:
        return None
    props = default_props(canvas, ftype)
    if ftype == "radio":
        groups = [it.props["name"] for it in authored_fields(canvas) if it.ftype == "radio"]
        if groups:
            props["name"] = groups[-1]  # most likely adding the next button of the same group
        props["export"] = next_radio_export(canvas, props["name"])
    dlg = FieldDialog(win, canvas, props)
    if not dlg.exec():
        return None
    item = insert_field(canvas, ftype, dlg.props)
    if item is None:
        QMessageBox.information(win, "Leaf", "Open a page first.")
    return item
