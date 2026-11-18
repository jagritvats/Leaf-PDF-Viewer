"""Form filling fidelity, navigation, signature fields, reset, authoring and form data."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from leaf import commands as C
from leaf import fields as F
from leaf import forms
from leaf.document import listbox_value, rect_key
from tests.test_editor import app, canvas_for, make_pdf, pump  # noqa: F401

T = pymupdf


def _add(page, name, ftype, rect, **kw):
    w = pymupdf.Widget()
    w.field_name = name
    w.field_type = ftype
    w.rect = pymupdf.Rect(rect)
    for k, v in kw.items():
        setattr(w, k, v)
    page.add_widget(w)


def make_form(path, rotate_second=False):
    d = pymupdf.open()
    for i in range(2):
        d.new_page(width=595, height=842)
        d[i].insert_text((72, 60), f"FORM PAGE {i + 1}", fontsize=12)
    if rotate_second:
        d[1].set_rotation(90)
    p = d[0]
    _add(p, "notes", T.PDF_WIDGET_TYPE_TEXT, (72, 100, 300, 160), field_flags=T.PDF_TX_FIELD_IS_MULTILINE)
    _add(p, "zip", T.PDF_WIDGET_TYPE_TEXT, (72, 170, 240, 190), field_flags=T.PDF_TX_FIELD_IS_COMB, text_maxlen=6)
    _add(p, "short", T.PDF_WIDGET_TYPE_TEXT, (72, 200, 240, 220), text_maxlen=5)
    _add(p, "langs", T.PDF_WIDGET_TYPE_LISTBOX, (72, 230, 240, 290), choice_values=["Python", "Rust", "Go", "C"],
         field_flags=T.PDF_CH_FIELD_IS_MULTI_SELECT)
    _add(p, "size", T.PDF_WIDGET_TYPE_LISTBOX, (260, 230, 400, 290), choice_values=["S", "M", "L"])
    _add(p, "fruit", T.PDF_WIDGET_TYPE_COMBOBOX, (72, 300, 240, 320), choice_values=["Apple", "Pear"],
         field_flags=T.PDF_CH_FIELD_IS_EDIT)
    _add(p, "pick", T.PDF_WIDGET_TYPE_RADIOBUTTON, (72, 330, 86, 344), field_value="A")
    _add(p, "pick", T.PDF_WIDGET_TYPE_RADIOBUTTON, (100, 330, 114, 344), field_value="B")
    _add(p, "fixed", T.PDF_WIDGET_TYPE_TEXT, (72, 360, 240, 380), field_value="locked",
         field_flags=T.PDF_FIELD_IS_READ_ONLY)
    _add(p, "secret", T.PDF_WIDGET_TYPE_TEXT, (260, 360, 400, 380), field_flags=T.PDF_TX_FIELD_IS_PASSWORD)
    _add(p, "sig", T.PDF_WIDGET_TYPE_SIGNATURE, (72, 400, 272, 450))
    _add(d[1], "email", T.PDF_WIDGET_TYPE_TEXT, (72, 100, 300, 120), field_flags=T.PDF_FIELD_IS_REQUIRED)
    d.save(path)
    d.close()


@pytest.fixture
def form(tmp_path):
    path = str(tmp_path / "form.pdf")
    make_form(path)
    return path


def fields_by_name(c, page=0):
    return {f.name: f for f in forms.page_fields(c, c.order[page])}


def saved_widgets(path, page=0):
    d = pymupdf.open(path)
    out = {}
    for w in d[page].widgets():
        out.setdefault(w.field_name, []).append((w.field_type, w.field_value, w.rect, w.field_flags))
    return d, out


def test_text_fidelity_roundtrip(app, form, tmp_path):
    c = canvas_for(form)
    c.show()
    pump(app, 0.2)
    fs = fields_by_name(c)
    notes, zipf, short = fs["notes"], fs["zip"], fs["short"]
    assert notes.multiline and zipf.comb and short.maxlen == 5
    assert fs["fixed"].readonly and not fs["fixed"].interactive()
    # the editor enforces max length while typing, and commits clip as well
    short.editor.start_edit()
    short.editor.textCursor().insertText("abcdefgh")
    assert short.editor.toPlainText() == "abcde"
    c._clear_focus()
    assert not short.editor.editing() and short.value() == "abcde"
    c.set_form_values({notes.key: "line one\nline two", zipf.key: "123456"})
    zipf.commit("98765432")  # longer than MaxLen
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d, ws = saved_widgets(out)
    assert ws["notes"][0][1] == "line one\nline two"
    assert ws["zip"][0][1] == "987654"
    assert ws["short"][0][1] == "abcde"
    assert ws["fixed"][0][1] == "locked"
    d.close()


def test_password_is_masked(app, form):
    c = canvas_for(form)
    pw = fields_by_name(c)["secret"]
    pw.commit("hunter2")
    pw.refresh()
    assert pw.editor.toPlainText() == forms.MASK * 7
    assert pw.value() == "hunter2"


def test_listbox_multi_select_roundtrip(app, form, tmp_path):
    c = canvas_for(form)
    fs = fields_by_name(c)
    langs, size = fs["langs"], fs["size"]
    assert langs.multi and not size.multi
    langs.toggle("Python")
    langs.toggle("Go")
    langs.toggle("Rust")
    langs.toggle("Rust")  # off again
    size.toggle("M")
    assert langs.value() == ["Python", "Go"]
    fs["fruit"].commit("Banana")  # editable combo accepts typed text
    out = str(tmp_path / "lb.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    ws = {w.field_name: w for w in d[0].widgets()}
    assert listbox_value(d, ws["langs"]) == ["Python", "Go"]
    assert listbox_value(d, ws["size"]) == ["M"]
    assert ws["fruit"].field_value == "Banana"
    d.close()
    # reopening shows the same selection in Leaf
    c2 = canvas_for(out)
    assert fields_by_name(c2)["langs"].value() == ["Python", "Go"]


def test_radio_group_unique_on_state(app, form, tmp_path):
    c = canvas_for(form)
    a, b = [f for f in forms.page_fields(c, c.order[0]) if f.name == "pick"]
    assert {a.on_state, b.on_state} == {"A", "B"}
    b.activate()
    assert b.value() and not a.value()
    a.activate()
    assert a.value() and not b.value()
    out = str(tmp_path / "radio.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    states = {w.on_state(): w.field_value for w in d[0].widgets() if w.field_name == "pick"}
    assert states["A"] == "A" and states["B"] in ("Off", False, None)
    d.close()


def test_reset_form_is_one_undo_step(app, form):
    c = canvas_for(form)
    fs = fields_by_name(c)
    fs["notes"].commit("hello")
    fs["zip"].commit("111")
    fs["size"].toggle("L")
    n = c.undo.count()
    forms.reset_form(c)
    assert c.undo.count() == n + 1 and not c.doc.form_values
    assert fs["notes"].value() == "" and fs["size"].value() == ""
    c.undo.undo()
    assert fs["notes"].value() == "hello" and fs["size"].value() == "L"


def test_clear_field(app, form):
    c = canvas_for(form)
    fs = fields_by_name(c)
    fs["notes"].commit("x")
    fs["notes"].clear()
    assert fs["notes"].value() == ""
    fs["langs"].toggle("C")
    fs["langs"].clear()
    assert fs["langs"].value() == []


def test_tab_order_crosses_pages_and_required(app, form):
    c = canvas_for(form)
    c.show()
    pump(app, 0.2)
    fs = fields_by_name(c)
    nxt = c.focus_next_field(fs["sig"])  # last focusable field of page 1 -> first of page 2
    assert nxt is not None and nxt.name == "email"
    back = c.focus_next_field(nxt, back=True)
    assert back is fs["sig"]
    c._clear_focus()
    f = forms.next_required_empty(c)
    assert f is not None and f.name == "email" and f.required and f.is_empty()
    c._clear_focus()
    f.commit("me@example.com")
    assert forms.next_required_empty(c) is None


def test_signature_field_placement(app, form, tmp_path):
    c = canvas_for(form)
    sig = fields_by_name(c)["sig"]
    assert not sig.signed and sig.interactive()
    img = QImage(400, 100, QImage.Format.Format_ARGB32)
    img.fill(QColor("navy"))
    item = forms.place_signature(c, sig, img)
    r = item.scene_rect_in_parent()
    assert abs(r.width() / r.height() - 4.0) < 0.01  # aspect kept
    assert sig.r.contains(r)
    assert abs(r.center().x() - sig.r.center().x()) < 0.01 and abs(r.center().y() - sig.r.center().y()) < 0.01
    assert item in c.overlays_for(c.doc.pages[0])
    c.undo.undo()
    assert item.scene() is None
    c.undo.redo()
    out = str(tmp_path / "signed.pdf")
    assert c.doc.save(out, c.overlays_for) is True  # incremental: the signature field itself is untouched
    d = pymupdf.open(out)
    assert any(abs(pymupdf.Rect(i["bbox"]).x0 - r.x()) < 1 for i in d[0].get_image_info())
    d.close()


def test_authored_fields_on_rotated_page(app, tmp_path):
    path = str(tmp_path / "rot.pdf")
    make_form(path, rotate_second=True)
    c = canvas_for(path)
    pg = c.order[1]
    made = {}
    for ft in F.FIELD_TYPES:
        props = F.default_props(c, ft)
        if ft == "radio":
            props["name"] = "grp"
        made[ft] = F.insert_field(c, ft, props, pg)
    r2 = F.insert_field(c, "radio", dict(F.default_props(c, "radio"), name="grp"), pg)
    assert r2.props["export"] != made["radio"].props["export"]
    assert F.name_problem(c, "text", made["text"].props["name"]) is not None
    assert F.name_problem(c, "radio", "grp") is None
    assert F.name_problem(c, "text", "email") is not None  # existing PDF field
    # move the text field to a known display-space rect
    made["text"].set_geometry((QPointF(100, 50), QRectF(0, 0, 200, 30)))
    made["listbox"].set_props(dict(made["listbox"].props, multi=True, options=["a", "b"]))
    out = str(tmp_path / "authored.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    pg = d[1]
    ws = {}
    for w in pg.widgets():
        ws.setdefault(w.field_name, []).append(w)
    want = {"text": T.PDF_WIDGET_TYPE_TEXT, "multiline": T.PDF_WIDGET_TYPE_TEXT, "checkbox": T.PDF_WIDGET_TYPE_CHECKBOX,
            "dropdown": T.PDF_WIDGET_TYPE_COMBOBOX, "listbox": T.PDF_WIDGET_TYPE_LISTBOX,
            "signature": T.PDF_WIDGET_TYPE_SIGNATURE}
    for ft, wt in want.items():
        w = ws[made[ft].props["name"]][0]
        assert w.field_type == wt, ft
    assert ws[made["multiline"].props["name"]][0].field_flags & T.PDF_TX_FIELD_IS_MULTILINE
    assert ws[made["listbox"].props["name"]][0].field_flags & T.PDF_CH_FIELD_IS_MULTI_SELECT
    assert ws[made["dropdown"].props["name"]][0].choice_values == ["Option 1", "Option 2", "Option 3"]
    radios = ws["grp"]
    assert len(radios) == 2 and all(w.field_type == T.PDF_WIDGET_TYPE_RADIOBUTTON for w in radios)
    assert {w.on_state() for w in radios} == {"Choice1", "Choice2"}
    assert all(w.field_value in ("Off", False) for w in radios)  # new radios start unselected
    disp = ws[made["text"].props["name"]][0].rect * pg.rotation_matrix
    assert abs(disp.x0 - 100) < 1 and abs(disp.y0 - 50) < 1 and abs(disp.width - 200) < 1 and abs(disp.height - 30) < 1
    assert d.xref_get_key(ws[made["text"].props["name"]][0].xref, "MK/R")[1] == "90"
    d.close()
    # filled in another session, the text reads upright on screen
    d = pymupdf.open(out)
    p1 = d[1]
    w = next(w for w in p1.widgets() if w.field_name == made["text"].props["name"])
    w.field_value = "Upright"
    w.update()
    d.bake(annots=False, widgets=True)
    p = d[1]
    line = next(ln for b in p.get_text("dict")["blocks"] for ln in b.get("lines", [])
                if "Upright" in "".join(s["text"] for s in ln["spans"]))
    dx, dy = line["dir"]
    assert dx * p.rotation_matrix.a + dy * p.rotation_matrix.c > 0.99
    d.close()
    # undo removes the authored field again
    n = len(c.overlays_for(c.doc.pages[1]))
    c.undo.undo()  # listbox props were set directly; last command is the 2nd radio
    assert len(c.overlays_for(c.doc.pages[1])) == n - 1


def test_flatten_keeps_text_removes_widgets(app, form, tmp_path):
    c = canvas_for(form)
    fs = fields_by_name(c)
    fs["notes"].commit("Flattened words")
    fs["langs"].toggle("Rust")
    out = str(tmp_path / "flat.pdf")
    forms.save_flattened(c, out)
    d = pymupdf.open(out)
    assert not any(True for p in d for _ in p.widgets())
    text = d[0].get_text()
    assert "Flattened words" in text and "Rust" in text
    d.close()
    assert c.doc.form_values  # the open document keeps its editable fields
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".leaf-")]


def test_json_and_xfdf_export_import(app, form, tmp_path):
    c = canvas_for(form)
    fs = fields_by_name(c)
    fs["notes"].commit("multi\nline")
    fs["langs"].toggle("Go")
    fs["langs"].toggle("C")
    [f for f in forms.page_fields(c, c.order[0]) if f.name == "pick"][1].activate()
    fs["fruit"].commit("Kiwi")
    jpath, xpath = str(tmp_path / "data.json"), str(tmp_path / "data.xfdf")
    forms.export_json(c.doc, jpath)
    forms.export_xfdf(c.doc, xpath)
    with open(jpath, encoding="utf-8") as f:
        data = json.load(f)["fields"]
    assert data["notes"] == "multi\nline" and data["langs"] == ["Go", "C"] and data["pick"] == "B"
    assert data["fixed"] == "locked" and "sig" not in data
    assert forms.read_xfdf(xpath)["langs"] == ["Go", "C"]
    assert forms.read_xfdf(xpath)["pick"] == "B"

    c2 = canvas_for(form)
    n = forms.import_values(c2, forms.read_json(jpath))
    assert n > 0 and c2.undo.count() == 1
    vals = forms.collect_values(c2.doc)
    for k in ("notes", "langs", "pick", "fruit"):
        assert vals[k] == data[k], k
    c2.undo.undo()
    assert not c2.doc.form_values

    c3 = canvas_for(form)
    forms.import_values(c3, forms.read_xfdf(xpath))
    vals = forms.collect_values(c3.doc)
    assert vals["langs"] == ["Go", "C"] and vals["pick"] == "B" and vals["notes"] == "multi\nline"


def test_highlight_toggle_and_legacy_form(app, tmp_path):
    path = str(tmp_path / "legacy.pdf")
    make_pdf(path)
    c = canvas_for(path)
    fs = fields_by_name(c)
    assert set(fs) == {"name", "agree", "color"}
    forms.set_highlight(c, False)
    try:
        assert not fs["name"].show_own()
        fs["name"].commit("Changed")
        assert fs["name"].show_own()
    finally:
        forms.set_highlight(c, True)
    assert C.UNSET is not None


def test_authored_field_clone_gets_unique_name(app, form):
    c = canvas_for(form)
    it = F.insert_field(c, "text", None, c.order[0])
    dup = it.clone()
    assert dup.props["name"] != it.props["name"] and dup.ftype == "text"
    r = F.insert_field(c, "radio", dict(F.default_props(c, "radio"), name="g"), c.order[0])
    rd = r.clone()
    assert rd.props["name"] == "g" and rd.props["export"] != r.props["export"]
