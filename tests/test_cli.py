"""Command line (leaf/cli.py) and edit sets (leaf/editset.py, features/editset.py)."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt

from leaf import cli, editset as E
from leaf.document import PdfDocument
from leaf.items import InkItem, LineItem, NoteItem, ShapeItem, TextItem
from tests.test_editor import app, make_pdf, canvas_for, pump, add  # noqa: F401


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path)
    return path


def run(capsys, *argv) -> tuple[int, dict]:
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["code"] == code and data["ok"] == (code in (0, 5))
    return code, data


def page_texts(path) -> list[str]:
    d = pymupdf.open(path)
    try:
        return [p.get_text() for p in d]
    finally:
        d.close()


# ------------------------------------------------------------------------------------------------ read-only
def test_info_text_fields_annots(app, pdf, capsys):
    code, r = run(capsys, "info", pdf)
    assert code == 0 and r["command"] == "info" and r["page_count"] == 3
    assert r["pages"][1]["rotation"] == 90 and r["forms"] == {"present": True, "fields": 3}
    assert r["encryption"]["encrypted"] is False and r["signatures"] == []

    code, r = run(capsys, "text", pdf, "--pages", "1", "--words")
    assert code == 0 and len(r["pages"]) == 1 and "PAGE1" in r["pages"][0]["text"]
    w = r["pages"][0]["words"][0]
    assert w[4] == "PAGE1" and len(w) == 5 and 60 < w[0] < 80

    code, r = run(capsys, "fields", pdf)
    assert {f["name"]: f["type"] for f in r["fields"]} == {"name": "text", "agree": "checkbox", "color": "combobox"}
    assert next(f for f in r["fields"] if f["name"] == "color")["options"] == ["Red", "Green", "Blue"]

    code, r = run(capsys, "annots", pdf)
    assert code == 0 and r["annotations"] == []
    assert cli.main(["text", pdf]) == 0 and "Second line" in capsys.readouterr().out  # plain text mode


def test_errors_and_exit_codes(app, pdf, tmp_path, capsys, monkeypatch):
    code, r = run(capsys, "info", str(tmp_path / "missing.pdf"))
    assert code == 1 and "No such file" in r["error"]
    code, r = run(capsys, "rotate", pdf, "--pages", "9")
    assert code == 2 and "doesn't exist" in r["error"]
    code, r = run(capsys, "rotate", pdf, "-o", pdf)  # output == input without --in-place
    assert code == 2
    code, r = run(capsys, "delete", pdf, "--pages", "all")
    assert code == 2
    assert cli.main([]) == 2
    capsys.readouterr()
    assert cli.main(["rotate", "--bogus-flag", pdf]) == 2  # argparse usage error
    capsys.readouterr()
    # encrypted input: no password in the environment -> 3; with it -> works
    enc = str(tmp_path / "enc.pdf")
    d = pymupdf.open(pdf)
    d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    d.close()
    monkeypatch.delenv("LEAF_PASSWORD", raising=False)
    code, r = run(capsys, "info", enc)
    assert code == 3
    monkeypatch.setenv("LEAF_PASSWORD", "nope")
    assert run(capsys, "info", enc)[0] == 3
    monkeypatch.setenv("LEAF_PASSWORD", "u")
    code, r = run(capsys, "info", enc)
    assert code == 0 and r["encryption"]["encrypted"]


# ------------------------------------------------------------------------------------------------ page list
def test_page_commands(app, pdf, tmp_path, capsys):
    out = str(tmp_path / "rot.pdf")
    code, r = run(capsys, "rotate", pdf, "--pages", "1", "--angle", "180", "-o", out)
    assert code == 0 and r["output"] == out and pymupdf.open(out)[0].rotation == 180
    # never overwrites without --force; default names are new files next to the input
    assert run(capsys, "rotate", pdf, "-o", out)[0] == 2
    code, r = run(capsys, "delete", pdf, "--pages", "2")
    assert code == 0 and r["output"].endswith("in-deleted.pdf") and r["pages"] == 2
    code, r2 = run(capsys, "delete", pdf, "--pages", "2")
    assert r2["output"].endswith("in-deleted-2.pdf")
    code, r = run(capsys, "reorder", pdf, "--order", "3")
    assert [t.split()[0] for t in page_texts(r["output"])] == ["PAGE3", "PAGE1", "PAGE2"]
    code, r = run(capsys, "extract", pdf, "--pages", "2-3")
    assert r["pages"] == 2 and pymupdf.open(r["output"]).page_count == 2
    code, r = run(capsys, "extract", pdf, "--pages", "1,3", "--separate", "-o", str(tmp_path / "sep"))
    assert len(r["files"]) == 2 and all(os.path.exists(f["output"]) for f in r["files"])
    code, r = run(capsys, "split", pdf, "--every", "2")
    assert [f["pages"] for f in r["files"]] == [[1, 2], [3]]
    code, r = run(capsys, "split", pdf, "--range", "1", "--range", "2-3", "-o", str(tmp_path / "parts"))
    assert [f["pages"] for f in r["files"]] == [[1], [2, 3]]
    code, r = run(capsys, "merge", pdf, out, "-o", str(tmp_path / "m.pdf"))
    assert code == 0 and r["pages"] == 6
    # in place, explicitly
    cp = str(tmp_path / "copy.pdf")
    with open(pdf, "rb") as a, open(cp, "wb") as b:
        b.write(a.read())
    code, r = run(capsys, "rotate", cp, "--in-place")
    assert r["output"] == os.path.abspath(cp) and pymupdf.open(cp)[0].rotation == 90


def test_crop_optimize_export(app, pdf, tmp_path, capsys):
    code, r = run(capsys, "crop", pdf, "--pages", "1", "--margins", "10,10,10,10", "--unit", "mm")
    assert code == 0
    p = pymupdf.open(r["output"])[0]
    assert abs(p.rect.width - (595 - 2 * 72 / 2.54)) < 1
    code, r = run(capsys, "crop", pdf, "--rect", "1,1,2,2")  # too small everywhere: nothing done
    assert code == 4 and "output" not in r
    code, r = run(capsys, "optimize", pdf, "--preset", "screen")
    assert code == 0 and r["after"] > 0 and os.path.exists(r["output"])
    code, r = run(capsys, "export", pdf, "--format", "markdown", "-o", str(tmp_path / "x.md"))
    assert code == 0 and "PAGE1" in open(r["files"][0], encoding="utf-8").read()
    code, r = run(capsys, "export", pdf, "--format", "png", "--pages", "2", "--dpi", "20", "-o", str(tmp_path / "png"))
    assert len(r["files"]) == 1 and r["files"][0].endswith(".png")
    code, r = run(capsys, "export", pdf, "--format", "bmpx", "-o", str(tmp_path / "bad"))
    assert code == 2


# ------------------------------------------------------------------------------------------------ edits
def test_fill(app, pdf, tmp_path, capsys):
    data = tmp_path / "v.json"
    data.write_text(json.dumps({"fields": {"color": "Green"}}), encoding="utf-8")
    code, r = run(capsys, "fill", pdf, "name=Ada", "agree=true", "nosuch=1", "--data", str(data))
    assert code == 5 and any("nosuch" in w for w in r["refused"])
    code, f = run(capsys, "fields", r["output"])
    vals = {x["name"]: x["value"] for x in f["fields"]}
    assert vals == {"name": "Ada", "agree": True, "color": "Green"}
    code, r = run(capsys, "fill", pdf, "nosuch=1")
    assert code == 4 and "output" not in r


def test_edit_text_and_replace(app, pdf, tmp_path, capsys):
    code, r = run(capsys, "edit-text", pdf, "--page", "1", "--list")
    assert any(ln["text"] == "Second line of text for selection tests." for ln in r["lines"])
    code, r = run(capsys, "edit-text", pdf, "--page", "1", "--find", "Second line of text for selection tests.",
                  "--text", "Edited by the CLI.")
    assert code == 0, r
    assert "Edited by the CLI." in page_texts(r["output"])[0] and "Second line" not in page_texts(r["output"])[0]
    # not on the page: refused, nothing written, a suggestion given
    code, r = run(capsys, "edit-text", pdf, "--page", "1", "--find", "Second line of text", "--text", "x")
    assert code == 4 and "output" not in r and "Did you mean" in r["warnings"][0]
    code, r = run(capsys, "replace", pdf, "--find", "lazy", "--replace", "sleepy", "--pages", "1-2")
    assert code == 0 and r["replaced"] == 2
    t = page_texts(r["output"])
    assert "sleepy" in t[0] and "sleepy" in t[1] and "lazy" in t[2]
    code, r = run(capsys, "replace", pdf, "--find", "zebra", "--replace", "x")
    assert code == 4
    code, r = run(capsys, "replace", pdf, "--find", "(", "--replace", "x", "--regex")
    assert code == 4 and any("regular expression" in w for w in r["warnings"])


def test_redact(app, pdf, tmp_path, capsys):
    code, r = run(capsys, "redact", pdf, "--text", "brown fox", "--dry-run")
    assert code == 0 and len(r["marks"]) == 3 and "output" not in r
    code, r = run(capsys, "redact", pdf, "--text", "brown fox", "--rect", "1:72,120,300,135")
    assert code == 0 and len(r["marks"]) == 4
    t = page_texts(r["output"])
    assert all("brown" not in x for x in t) and "Second line" not in t[0] and "Second line" in t[2]
    code, r = run(capsys, "redact", pdf, "--preset", "email")
    assert code == 4  # no matches
    code, r = run(capsys, "redact", pdf, "--preset", "nosuch")
    assert code == 2


def test_apply_and_edits_template(app, pdf, tmp_path, capsys):
    code, r = run(capsys, "edits", pdf)
    tpl = r["edits"]
    assert tpl["leaf_edits"] == 1 and tpl["document"]["pages"] == 3 and "name" in tpl["document"]["fields"]
    assert E.validate(tpl) == []
    code, r = run(capsys, "edits", "--example")
    assert E.validate(r["edits"]) == []
    sig = tmp_path / "sig.png"
    from PySide6.QtGui import QColor, QImage
    img = QImage(20, 10, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    img.save(str(sig))
    edits = {
        "leaf_edits": 1,
        "pages": [{"page": 3}, {"page": 1, "rotate": 90}, {"blank": [300, 400]}],
        "forms": {"name": "Grace"},
        "annotations": [
            {"page": 1, "type": "rect", "rect": [100, 300, 200, 350], "style": {"color": "#00aa00"}, "contents": "c"},
            {"page": 2, "type": "text", "at": [80, 500], "text": "Stamped"},
            {"page": 3, "type": "image", "rect": [10, 10, 50, 30], "file": "sig.png"},
            {"type": "highlight", "search": "quick"},
        ],
        "metadata": {"title": "Applied"},
    }
    ef = tmp_path / "e.json"
    ef.write_text(json.dumps(edits), encoding="utf-8")
    code, r = run(capsys, "apply", pdf, str(ef), "--dry-run")
    assert code == 0 and "output" not in r and len(r["changes"]) == 7
    code, r = run(capsys, "apply", pdf, str(ef), "-o", str(tmp_path / "out.pdf"))
    assert code == 0, r
    d = pymupdf.open(r["output"])
    assert d.page_count == 3 and d.metadata["title"] == "Applied" and d[1].rotation == 90
    assert d[0].get_text().startswith("PAGE3") and "Stamped" in d[1].get_text()
    assert sorted(a.type[1] for a in d[0].annots()) == ["Highlight", "Square"]
    assert d[2].get_images() and d[2].rect.width == 300
    d.close()
    # malformed -> 2 with the problems listed
    ef.write_text(json.dumps({"leaf_edits": 1, "annotations": [{"page": 0, "type": "blob"}]}), encoding="utf-8")
    code, r = run(capsys, "apply", pdf, str(ef))
    assert code == 2 and len(r["problems"]) == 2


def test_sign_and_verify(app, pdf, tmp_path, capsys, monkeypatch):
    from leaf import signing as S
    p12 = str(tmp_path / "me.p12")
    S.create_identity(p12, "secret", "Cli Signer", algorithm="ec")
    monkeypatch.delenv("LEAF_SIGN_PASSWORD", raising=False)
    code, r = run(capsys, "sign", pdf, "--identity", p12)
    assert code == 3 and "never read from the command line" in r["error"]
    monkeypatch.setenv("LEAF_SIGN_PASSWORD", "wrong")
    assert run(capsys, "sign", pdf, "--identity", p12)[0] == 3
    monkeypatch.setenv("MY_PW", "secret")
    code, r = run(capsys, "sign", pdf, "--identity", p12, "--identity-password-env", "MY_PW", "--page", "1",
                  "--rect", "300,700,500,760", "--reason", "Approved")
    assert code == 0 and r["signer"] == "Cli Signer"
    code, v = run(capsys, "verify", r["output"])
    assert code == 0 and v["signatures"][0]["intact"] and v["signatures"][0]["state"] == "untrusted"
    assert run(capsys, "verify", r["output"], "--require-trust")[0] == 4
    code, v = run(capsys, "verify", pdf)
    assert code == 4 and v["signatures"] == []


def test_ocr(app, tmp_path, capsys):
    from leaf import ocr as O
    if not O.available_engines():
        pytest.skip("no OCR engine")
    # a scanned page: text only as an image
    src = pymupdf.open()
    pg = src.new_page(width=300, height=120)
    pg.insert_text((20, 60), "Hello scanned world", fontsize=24)
    pix = pg.get_pixmap(dpi=200)
    scan = pymupdf.open()
    sp = scan.new_page(width=300, height=120)
    sp.insert_image(sp.rect, pixmap=pix)
    path = str(tmp_path / "scan.pdf")
    scan.save(path)
    code, r = run(capsys, "ocr", path, "--dpi", "200")
    assert code == 0 and r["ocr_pages"] == 1
    assert "scanned" in page_texts(r["output"])[0].lower()
    code, r = run(capsys, "ocr", path, "--lang", "zzz")
    assert code == 4


# ------------------------------------------------------------------------------------------------ edit sets
def test_validate_problems():
    assert E.validate([]) == ["An edit set is a JSON object."]
    p = E.validate({"leaf_edits": 2, "bogus": 1})
    assert any("version 2" in x for x in p) and any("bogus" in x for x in p)
    assert any("leaf_edits" in x for x in E.validate({}))
    p = E.validate({"leaf_edits": 1,
                    "pages": [{"page": 0}, {"blank": [10]}, {"page": 1, "rotate": 45}],
                    "forms": {"a": {"x": 1}},
                    "text_edits": [{"page": 1}],
                    "annotations": [{"page": 1, "type": "rect", "rect": [1, 2, 3]},
                                    {"page": 1, "type": "line", "from": [0, 0], "to": [1, 1], "style": {"size": 3}},
                                    {"page": 1, "type": "ink", "points": [[0, "a"]]}],
                    "redactions": [{"preset": "zip"}, {"page": 1}],
                    "crop": [{"page": 1, "rect": [0, 0, 9, 9], "auto": True}],
                    "metadata": {"producer": "x"},
                    "decorations": [{"kind": "logo"}],
                    "sanitize": {"metadata": "yes"},
                    "ocr": {"dpi": 5}})
    for needle in ("pages[0]", "pages[1]", "pages[2]", "forms.a", "text_edits[0]: needs \"text\"",
                   "text_edits[0]: needs \"find\"", "annotations[0]", "unknown style \"size\"", "annotations[2]",
                   "redactions[0]", "redactions[1]", "crop[0]", "metadata.producer", "decorations[0]",
                   "sanitize.metadata", "ocr.dpi"):
        assert any(needle in x for x in p), (needle, p)
    assert E.validate(cli.EXAMPLE) == []


def test_headless_apply_matches_entries_and_subset(app, pdf):
    data = {"leaf_edits": 1, "forms": {"name": "A", "agree": True}, "metadata": {"title": "T", "author": "B"},
            "annotations": [{"page": 1, "type": "note", "at": [50, 50], "contents": "n"}]}
    paths = [p for p, _, _ in E.entries(data)]
    assert paths == [("forms", "name"), ("forms", "agree"), ("annotations", 0), ("metadata", "title"),
                     ("metadata", "author")]
    sub = E.subset(data, [("forms", "agree"), ("metadata", "author")])
    assert sub == {"leaf_edits": 1, "forms": {"agree": True}, "metadata": {"author": "B"}}
    doc = PdfDocument.open(pdf)
    report: dict = {}
    assert E.apply_to_document(doc, data, report=report) == [] and report["refused"] == []
    assert doc.metadata_edits == {"title": "T", "author": "B"} and E.overlay_count(doc) == 1
    with pytest.raises(E.EditSetError):
        E.apply_to_document(doc, {"leaf_edits": 1, "pages": "x"})


def _gui_edits(c):
    """A bit of everything, made the way the GUI makes it."""
    from leaf import commands as C, pageops
    from leaf.crop import set_crops
    from leaf.docprops import SetMetadata
    from leaf.redact import KIND, RedactItem
    from leaf.textedit import unit_at

    doc = c.doc
    # page list: move page 3 first, rotate (now) page 2 = original page 1, duplicate it (copy right after it)
    pageops.reorder(c, [doc.pages[2].uid, doc.pages[0].uid, doc.pages[1].uid])
    pageops.rotate(c, [1], 90)
    pageops.duplicate(c, [1])
    assert len(doc.pages) == 4
    add(c, 0, ShapeItem("rect", QRectF(100, 300, 120, 60), {"color": "#ff0000"}))
    add(c, 1, LineItem("arrow", QPointF(100, 400), QPointF(250, 450)))
    ink = InkItem(QPointF(0, 0))
    ink.setPos(QPointF(300, 300))
    for k in range(10):
        ink.add_point(QPointF(k * 5, k * 3), 0.2 + 0.05 * k)  # pen pressure
    add(c, 1, ink)
    add(c, 1, TextItem(QPointF(100, 500), {"size": 14, "bold": True}, "Hello round trip"))
    add(c, 2, NoteItem(QPointF(40, 40), None, {"contents": "a note"}))
    add(c, 3, RedactItem(KIND, QRectF(70, 85, 120, 20), {"color": "#000000", "text": ""}))
    ref = doc.pages[1]  # original page 1
    u = unit_at(doc, ref, 100, 125)
    assert u is not None and u.text.startswith("Second line")
    c.undo.push(C.EditPageText(c, ref, u, "Second line, edited in the GUI."))
    c.set_form_values({(ref.uid, "name", next(k[2] for k in _widget_keys(doc, ref) if k[1] == "name")): "Zed"})
    set_crops(c, {doc.pages[2]: (20.0, 20.0, 500.0, 800.0)})
    c.undo.push(SetMetadata(doc, {"title": "Round trip"}))


def _widget_keys(doc, ref):
    from leaf.document import rect_key
    return [(ref.uid, w.field_name, rect_key(w.rect)) for w in doc.page(ref).widgets()]


def _summary(path):
    d = pymupdf.open(path)
    out = []
    for p in d:
        annots = sorted((a.type[1], tuple(round(v) for v in a.rect)) for a in p.annots())
        out.append((p.rotation, tuple(round(v) for v in p.cropbox), annots, p.get_text("text").split()))
    meta = d.metadata["title"]
    d.close()
    return out, meta


def test_export_apply_round_trip_equals_gui(app, pdf, tmp_path):
    c = canvas_for(pdf)
    _gui_edits(c)
    gui_out = str(tmp_path / "gui.pdf")
    c.doc.save(gui_out, c.overlays_for)
    data = json.loads(json.dumps(E.export_edits(c)))  # through JSON, as a file would
    assert E.validate(data) == []
    assert data["pages"] == [{"page": 3}, {"page": 1, "rotate": 90}, {"page": 1, "rotate": 90}, {"page": 2}]
    assert {a["type"] for a in data["annotations"]} == {"rect", "arrow", "ink", "text", "note"}
    assert len(data["redactions"]) == 1 and len(data["text_edits"]) == 1 and data["forms"] == {"name": {"value": "Zed", "pages": [2]}}
    assert "not_exported" not in data.get("document", {})
    ink = next(a for a in data["annotations"] if a["type"] == "ink")
    assert len(ink["pressure"]) == len(ink["points"]) == 10 and ink["pressure"][0] == 0.25
    old = json.loads(json.dumps(data))  # an edit set without pressure (older format) stays valid
    next(a for a in old["annotations"] if a["type"] == "ink").pop("pressure")
    assert E.validate(old) == []
    bad = json.loads(json.dumps(data))
    next(a for a in bad["annotations"] if a["type"] == "ink")["pressure"] = [0.5]
    assert any("pressure" in p for p in E.validate(bad))

    # the same edit set on a fresh canvas (GUI apply) and headless (CLI apply) gives the same file
    c2 = canvas_for(pdf)
    assert E.apply_edits(c2, data) == []
    assert c2.undo.count() == 1 and c2.undo.text(0) == "Apply edit set"
    ink2 = next(it for pg in c2.order for it in pg.overlays() if isinstance(it, InkItem))
    assert ink2.has_pressure() and abs(ink2.widths[-1] - 0.65) < 1e-6
    out2 = str(tmp_path / "gui2.pdf")
    c2.doc.save(out2, c2.overlays_for)
    doc3 = PdfDocument.open(pdf)
    assert E.apply_to_document(doc3, data) == []
    out3 = str(tmp_path / "headless.pdf")
    E.save(doc3, out3)
    want = _summary(gui_out)
    assert _summary(out2) == want
    assert _summary(out3) == want
    assert want[1] == "Round trip" and "edited" in " ".join(want[0][1][3])
    # undo takes the whole edit set back in one step
    c2.undo.undo()
    assert len(c2.doc.pages) == 3 and not c2.doc.text_edits and not c2.doc.form_values
    assert not c2.doc.metadata_edits and all(r.crop is None for r in c2.doc.pages)
    assert not any(pg.overlays() for pg in c2.order)


def test_gui_import_preview_applies_selected_as_one_step(app, pdf, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from leaf.features import editset as F
    from leaf.window import MainWindow

    win = MainWindow()
    try:
        win.open_path(pdf)
        pump(app, 0.2)
        assert win.a_import_edit_set.isEnabled() and win.a_export_edit_set.isEnabled()
        assert win.a_import_edit_set in win.menus["file"].actions()
        cv = win.canvas
        data = {"leaf_edits": 1, "forms": {"name": "Pick me", "color": "Blue"},
                "annotations": [{"page": 1, "type": "rect", "rect": [100, 300, 200, 350]},
                                {"page": 2, "type": "ellipse", "rect": [100, 300, 200, 350]}],
                "metadata": {"title": "Chosen", "author": "Not chosen"}}
        path = tmp_path / "set.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        seen = {}

        def fake_exec(dlg):
            seen["n"] = len(dlg.items())
            for it in dlg.items():  # uncheck the second field, the ellipse and the author
                if it.text(0).startswith(("“color”", "Page 2", "Author")):
                    it.setCheckState(0, Qt.CheckState.Unchecked)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(F.ImportDialog, "exec", fake_exec)
        start = cv.undo.count()
        assert F.import_edit_set(win, str(path))
        assert seen["n"] == 6
        assert cv.undo.count() == start + 1 and cv.undo.text(cv.undo.count() - 1).startswith("Apply edit set (3")
        assert set(v for v in cv.doc.form_values.values()) == {"Pick me"}
        assert cv.doc.metadata_edits == {"title": "Chosen"}
        assert [it.kind for it in cv.order[0].overlays() if not hasattr(it, "xref")] == ["rect"]
        assert not [it for it in cv.order[1].overlays() if not hasattr(it, "xref")]
        cv.undo.undo()
        assert not cv.doc.form_values and not cv.doc.metadata_edits
        assert not [it for it in cv.order[0].overlays() if not hasattr(it, "xref")]
        # export writes the pending changes (after redo)
        cv.undo.redo()
        out = tmp_path / "out.edits.json"
        assert F.export_edit_set(win, str(out))
        back = json.loads(out.read_text(encoding="utf-8"))
        assert back["metadata"] == {"title": "Chosen"} and back["forms"] == {"name": "Pick me"}
        assert len(back["annotations"]) == 1
        # an invalid file is refused before any dialog
        bad = tmp_path / "bad.json"
        bad.write_text("{\"leaf_edits\": 7}", encoding="utf-8")
        monkeypatch.setattr(F.QMessageBox, "warning", lambda *a, **k: None)
        assert not F.import_edit_set(win, str(bad))
        assert cv.undo.count() == start + 1
    finally:
        win.canvas.undo.clear()
        win.close()
        win.deleteLater()
        pump(app, 0.1)


def test_main_dispatches_cli(pdf, capsys, monkeypatch):
    import sys

    from leaf import __main__ as M
    monkeypatch.setattr(sys, "argv", ["leaf", "info", pdf, "--json"])
    assert M.main() == 0
    assert json.loads(capsys.readouterr().out)["page_count"] == 3
    monkeypatch.setattr(sys, "argv", ["leaf", "cli", "text", pdf, "--pages", "2"])
    assert M.main() == 0
    assert "PAGE2" in capsys.readouterr().out


def test_images_decorations_sanitize_replace_gui_equals_headless(app, tmp_path):
    path = str(tmp_path / "img.pdf")
    d = pymupdf.open()
    p = d.new_page(width=400, height=300)
    p.insert_text((40, 40), "Color of the colour box", fontsize=12)
    for k, col in enumerate(((1, 0, 0), (0, 0, 1))):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8 + k, 8), False)
        pix.set_rect(pix.irect, tuple(int(255 * c) for c in col))
        p.insert_image(pymupdf.Rect(40 + 120 * k, 80, 140 + 120 * k, 180), pixmap=pix)
    d.save(path)
    d.close()
    data = {"leaf_edits": 1,
            "images": [{"page": 1, "index": 0, "delete": True},
                       {"page": 1, "index": 1, "rect": [200, 150, 300, 250], "turns": 1},
                       {"page": 1, "index": 5, "delete": True}],
            "replace": [{"find": "colour", "replace": "color"}],
            "decorations": [{"kind": "header_footer", "slots": ["", "", "", "", "Page {page} of {pages}", ""]}],
            "sanitize": {"metadata": True}}
    c = canvas_for(path)
    warnings = E.apply_edits(c, data, label="Mine")
    assert warnings == ["Page 1: image #6 not found (the page has 2 image(s)); skipped."]
    assert c.undo.count() == 1 and c.undo.text(0) == "Mine"
    gui = str(tmp_path / "gui.pdf")
    c.doc.save(gui, c.overlays_for)
    doc = PdfDocument.open(path)
    report: dict = {}
    E.apply_to_document(doc, data, report=report)
    assert report["replaced"] == 1 and len(report["refused"]) == 1
    head = str(tmp_path / "headless.pdf")
    E.save(doc, head)
    for f in (gui, head):
        pg = pymupdf.open(f)[0]
        info = pg.get_image_info()
        assert len(info) == 1 and abs(info[0]["bbox"][0] - 200) < 1, info
        assert "Color of the color box" in pg.get_text() and "Page 1 of 1" in pg.get_text()
    assert _summary(gui) == _summary(head)
    c.undo.undo()
    assert not c.doc.image_edits and not c.doc.text_edits and not c.doc.decorations and c.doc.sanitize is None
