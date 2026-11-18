"""Redaction: content under marks is really gone from the saved file; search & redact; sanitize."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QPointF, QRectF

from leaf import commands as C
from leaf import redact as R
from leaf.document import PageRef
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


def make_secret_pdf(path, rotate=0):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 100), "Public header line", fontsize=12)
    p.insert_text((72, 140), "Account TOPSECRETWORD visible", fontsize=12)
    p.insert_text((72, 180), "Write to jane.doe@example.com or call", fontsize=12)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.set_rect(pix.irect, (255, 0, 0))
    p.insert_image(pymupdf.Rect(300, 300, 400, 400), pixmap=pix)
    p.add_rect_annot(pymupdf.Rect(60, 130, 260, 150)).set_info(content="ANNOTSECRET")
    keep = p.add_text_annot((500, 700), "keep me")
    keep.update()
    if rotate:
        p.set_rotation(rotate)
    d.new_page(width=595, height=842).insert_text((72, 100), "Second page text", fontsize=12)
    d.save(path)
    d.close()


def word_rect(c, page_idx, word) -> QRectF:
    ref = c.doc.pages[page_idx]
    w = next(w for w in ref.src.words(ref.index) if w[4] == word)
    return QRectF(QPointF(w[0], w[1]), QPointF(w[2], w[3]))


def all_bytes(path) -> bytes:
    """The raw file plus every stream decompressed."""
    with open(path, "rb") as f:
        raw = f.read()
    d = pymupdf.open(path)
    parts = [raw]
    for x in range(1, d.xref_length()):
        try:
            parts.append(d.xref_object(x).encode("latin-1", "replace"))
            if d.xref_is_stream(x):
                parts.append(d.xref_stream(x) or b"")
        except Exception:  # noqa: BLE001
            pass
    d.close()
    return b"\n".join(parts)


def gone_everywhere(path, word: str) -> bool:
    blob = all_bytes(path)
    forms = [word.encode(), word.encode().hex().encode(), word.encode().hex().upper().encode(),
             word.encode("utf-16-be").hex().encode()]
    return not any(f in blob for f in forms)


def mark(c, page_idx, rect, **props):
    return add(c, page_idx, R.RedactItem("redact", rect, {"color": "#000000", "text": "", **props}))


def test_box_redaction_removes_text_image_annots(app, tmp_path):
    src = str(tmp_path / "s.pdf")
    make_secret_pdf(src)
    c = canvas_for(src)
    assert gone_everywhere(src, "TOPSECRETWORD") is False  # sanity: the check sees it before
    mark(c, 0, word_rect(c, 0, "TOPSECRETWORD").adjusted(-1, -1, 1, 1))
    mark(c, 0, QRectF(300, 300, 50, 100))  # left half of the red image
    out = str(tmp_path / "out.pdf")
    with open(src, "rb") as f:
        orig = f.read()
    assert c.doc.save(out, c.overlays_for) is False  # never incremental with redactions
    with open(out, "rb") as f:
        assert not f.read().startswith(orig)
    d = pymupdf.open(out)
    p = d[0]
    text = p.get_text()
    assert "TOPSECRETWORD" not in text and "Account" in text and "visible" in text
    assert not p.search_for("TOPSECRETWORD")
    assert gone_everywhere(out, "TOPSECRETWORD")
    # the annotation over the word is gone (with its comment), the one elsewhere stays
    kinds = [a.type[1] for a in p.annots()]
    assert kinds == ["Text"], kinds
    assert gone_everywhere(out, "ANNOTSECRET")
    # image pixels under the mark are blanked, the rest stays red
    xref = p.get_images()[0][0]
    pix = pymupdf.Pixmap(d, xref)
    if pix.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    assert pix.pixel(10, 50) != (255, 0, 0)
    assert pix.pixel(90, 50) == (255, 0, 0)
    # the fill is black where the word was
    r = p.search_for("Account")[0]
    shot = p.get_pixmap()
    assert shot.pixel(int(r.x1 + 30), int((r.y0 + r.y1) / 2)) == (0, 0, 0)


def test_redaction_rotated_and_second_source(app, tmp_path):
    a = str(tmp_path / "a.pdf")
    make_secret_pdf(a, rotate=90)  # page with its own /Rotate
    b = str(tmp_path / "b.pdf")
    make_secret_pdf(b)
    c = canvas_for(a)
    doc = c.doc
    src_b = doc.add_source(b)
    old = [(r, r.rotation) for r in doc.pages]
    p1, p2 = doc.pages
    new = [(p2, 90), (PageRef(src_b, 0), 270), (p1, 0)]  # reorder, other source, Leaf rotation
    c.undo.push(C.SetPages(c, old, new, "ops"))
    mark(c, 1, word_rect(c, 1, "TOPSECRETWORD"))
    mark(c, 2, word_rect(c, 2, "TOPSECRETWORD"))
    mark(c, 0, word_rect(c, 0, "Second"))
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert d.page_count == 3
    assert "Second" not in d[0].get_text() and "page text" in d[0].get_text()
    for i in (1, 2):
        t = d[i].get_text()
        assert "TOPSECRETWORD" not in t and "Account" in t and "visible" in t, (i, t)
    assert gone_everywhere(out, "TOPSECRETWORD")


def test_form_widget_under_redaction_removed(app, tmp_path):
    src = str(tmp_path / "f.pdf")
    make_pdf(src)
    c = canvas_for(src)
    c.doc.form_values[(c.doc.pages[0].uid, "name", (72.0, 200.0, 300.0, 220.0))] = "FIELDSECRET"
    mark(c, 0, QRectF(70, 198, 240, 25))
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    names = {w.field_name for w in d[0].widgets()}
    assert "name" not in names and {"agree", "color"} <= names
    assert gone_everywhere(out, "FIELDSECRET")


def test_text_marks_tool_and_undo(app, tmp_path):
    src = str(tmp_path / "s.pdf")
    make_secret_pdf(src)
    c = canvas_for(src)
    pg = c.order[0]
    r = word_rect(c, 0, "TOPSECRETWORD")
    c.set_tool("redact")
    c._sel = (pg, r.center(), "redact")
    c._update_text_selection(pg, r.center(), r.center())
    R.canvas_text_done(c, pg, list(c._sel_rects), c._sel_text)
    c._end_text_selection()
    marks = R.marks_of(c)
    assert len(marks) == 1 and marks[0][1].marked_text == "TOPSECRETWORD"
    assert c.doc.has_redactions(c.doc.pages, c.overlays_for)
    c.undo.undo()
    assert not R.marks_of(c) and not c.doc.has_redactions(c.doc.pages, c.overlays_for)
    # without marks the save is incremental again
    assert c.doc.save(str(tmp_path / "inc.pdf"), c.overlays_for) is True


def test_search_and_redact_email(app, tmp_path):
    src = str(tmp_path / "s.pdf")
    make_secret_pdf(src)
    c = canvas_for(src)
    preset = next(p for p in R.PRESETS if p.name == "Email addresses")
    rx = R.build_regex("", preset)
    hits = [h for i in range(len(c.doc.pages)) for h in R.search_page(c.doc, i, rx, preset.check)]
    assert [h.text for h in hits] == ["jane.doe@example.com"]
    assert R.mark_hits(c, hits) == 1
    assert c.undo.count() == 1  # one undo step
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    t = pymupdf.open(out)[0].get_text()
    assert "example.com" not in t and "Write to" in t and "or call" in t
    assert gone_everywhere(out, "jane.doe")


def test_search_presets_and_options(app, tmp_path):
    words = [(0, 0, 10, 10, w, 0, 0, i) for i, w in enumerate("Card 4111 1111 1111 1111 and 1234 5678 9012 3456".split())]
    text, spans = R.page_text(words)
    cc = next(p for p in R.PRESETS if p.name == "Credit card numbers")
    found = [m.group(0) for m in R.build_regex("", cc).finditer(text) if cc.check(m.group(0))]
    assert found == ["4111 1111 1111 1111"]  # the second fails the Luhn check
    iban = next(p for p in R.PRESETS if p.name == "IBANs")
    assert iban.check("DE89 3704 0044 0532 0130 00") and not iban.check("DE00 3704 0044 0532 0130 00")
    dates = next(p for p in R.PRESETS if p.name == "Dates")
    rx = R.build_regex("", dates)
    assert rx.search("on 2024-03-15 and") and rx.search("March 5, 2021") and rx.search("5 Mar 2021")
    plain = next(p for p in R.PRESETS if p.name == "Text")
    assert R.build_regex("top", plain).search("TOPSECRET")
    assert not R.build_regex("top", plain, case=True).search("TOPSECRET")
    assert not R.build_regex("top", plain, whole=True).search("TOPSECRET")


def make_dirty_pdf(path):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 100), "Visible body text", fontsize=12)
    p.insert_text((72, 140), "HIDDENTEXTWORD", fontsize=12, render_mode=3)
    ocg = d.add_ocg("Secret layer", on=False)
    p.insert_text((72, 180), "LAYERSECRETWORD", fontsize=12, oc=ocg)
    on = d.add_ocg("Shown layer", on=True)
    p.insert_text((72, 220), "SHOWNLAYERWORD", fontsize=12, oc=on)
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(70, 90, 200, 105), "uri": "https://example.com/"})
    p.add_text_annot((400, 400), "COMMENTSECRET")
    d.embfile_add("secret.txt", b"EMBEDDEDSECRET")
    d.set_metadata({"title": "METATITLESECRET", "author": "METAAUTHOR"})
    d.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'>XMPSECRET</x:xmpmeta>")
    js = d.get_new_xref()
    d.update_object(js, "<</S/JavaScript/JS(app.alert\\('JSSECRET'\\))>>")
    d.xref_set_key(d.pdf_catalog(), "OpenAction", f"{js} 0 R")
    d.save(path)
    d.close()


def test_sanitize(app, tmp_path):
    src = str(tmp_path / "dirty.pdf")
    make_dirty_pdf(src)
    c = canvas_for(src)
    doc = c.doc
    spec = R.Sanitize(metadata=True, javascript=True, attachments=True, comments=True, hidden_text=True,
                      hidden_layers=True, links=True)
    c.undo.push(R.SetSanitize(doc, spec))
    assert doc.sanitize == spec
    out = str(tmp_path / "clean.pdf")
    assert doc.save(out, c.overlays_for) is False
    d = pymupdf.open(out)
    p = d[0]
    assert not d.metadata.get("title") and not d.metadata.get("author")
    assert d.embfile_count() == 0
    assert not list(p.annots()) and not p.get_links()
    t = p.get_text()
    assert "Visible body text" in t and "SHOWNLAYERWORD" in t
    for w in ("METATITLESECRET", "XMPSECRET", "EMBEDDEDSECRET", "JSSECRET", "COMMENTSECRET", "HIDDENTEXTWORD",
              "LAYERSECRETWORD", "example.com"):
        assert gone_everywhere(out, w), w
    assert gone_everywhere(out, "SHOWNLAYERWORD") is False
    # undo removes the setting; the next save is incremental again
    c.undo.undo()
    assert doc.sanitize is None
    assert doc.save(str(tmp_path / "again.pdf"), c.overlays_for) is True


def test_sanitize_flatten_forms(app, tmp_path):
    src = str(tmp_path / "f.pdf")
    make_pdf(src)
    c = canvas_for(src)
    c.doc.form_values[(c.doc.pages[0].uid, "name", (72.0, 200.0, 300.0, 220.0))] = "Flattened value"
    c.undo.push(R.SetSanitize(c.doc, R.Sanitize(forms=True)))
    out = str(tmp_path / "flat.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert not d.is_form_pdf and not list(d[0].widgets())
    assert "Flattened value" in d[0].get_text()


def test_signed_detection_and_strip_layers_tokenizer(app):
    data = b"q BT (a \\) (b) EMC) Tj ET /OC /L1 BDC 0 0 m (x) Tj EMC /OC /L2 BDC (keep) Tj EMC BI /W 1 ID \x00\xff EI Q"
    new, changed = R.strip_hidden_content(data, {b"L1": 5, b"L2": 6}, lambda x: x == 5)
    assert changed and b"(x)" not in new and b"(keep)" in new and b"(a \\) (b) EMC)" in new and b"BI" in new
    d = pymupdf.open()
    d.new_page()
    assert R.signed_fields(d) == 0


# ------------------------------------------------------------------ window
def test_window_wiring(app, tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings, Qt

    from leaf.features import redact as FR
    from leaf.window import HINTS, MainWindow

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafRedactTest")
    QSettings().clear()
    w = MainWindow()
    try:
        src = str(tmp_path / "s.pdf")
        make_secret_pdf(src)
        w.open_path(src)
        cv = w.canvas
        assert w.a_tool_redact.objectName() == "a_tool_redact" and w.a_tool_redact.isEnabled()
        assert cv.tool_keys.get(Qt.Key.Key_X) == "redact"
        assert "Redact" in HINTS["whiteout"]
        w.a_tool_redact.trigger()
        assert cv.tool == "redact"
        # search dialog: email preset, run to completion, mark -> one undo step
        dlg = FR.SearchRedactDialog(w)
        dlg.preset.setCurrentIndex(next(i for i, p in enumerate(R.PRESETS) if p.name == "Email addresses"))
        dlg.start()
        for _ in range(200):
            if dlg._job is None:
                break
            app.processEvents()
        assert [h.text for h in dlg.checked_hits()] == ["jane.doe@example.com"]
        dlg.mark()
        assert len(R.marks_of(cv)) == 1 and cv.undo.count() == 1
        panel = w.redact_dock.panel
        panel.refresh()
        assert panel.list.count() == 1 and "jane.doe" in panel.list.item(0).text()
        # overlay text goes to the selected mark (undoable)
        panel.list.item(0).setSelected(True)
        panel.go_to()
        panel.overlay.setText("REDACTED")
        panel._overlay_changed()
        assert R.marks_of(cv)[0][1].props["text"] == "REDACTED"
        # sanitize setting is undoable
        assert FR.set_sanitize(cv, R.Sanitize(metadata=True))
        assert cv.doc.sanitize.metadata
        # an accepted confirmation isn't asked again for the same marks
        marks = [m for _, m in R.marks_of(cv)]
        w._redact_confirmed = (cv.doc, frozenset((id(m), tuple(m.props.items()), str(m.geometry())) for m in marks),
                               cv.doc.sanitize)
        out = str(tmp_path / "out.pdf")
        assert w._save_to(out)
        d = pymupdf.open(out)
        assert "jane.doe" not in d[0].get_text() and "REDACTED" in d[0].get_text()
        assert not d.metadata.get("title")
        panel.remove_all()
        cv.undo.undo()
        assert len(R.marks_of(cv)) == 1
    finally:
        if w.canvas.doc is not None:
            w.canvas.undo.setClean()
        w.close()
        QSettings().clear()
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])


def test_sanitize_hidden_text_keeps_links_and_annots(app, tmp_path):
    src = str(tmp_path / "dirty.pdf")
    make_dirty_pdf(src)
    d = pymupdf.open(src)
    R.apply_sanitize(d, R.Sanitize(hidden_text=True))
    p = d[0]
    assert "HIDDENTEXTWORD" not in p.get_text() and "Visible body text" in p.get_text()
    assert [ln.get("uri") for ln in p.get_links()] == ["https://example.com/"]
    assert [a.type[1] for a in p.annots()] == ["Text"]


def test_redact_selected_text_and_duplicate_clone(app, tmp_path):
    src = str(tmp_path / "s.pdf")
    make_secret_pdf(src)
    c = canvas_for(src)
    pg = c.order[0]
    a = word_rect(c, 0, "Account").center()
    b = word_rect(c, 0, "TOPSECRETWORD").center()
    c._update_text_selection(pg, a, b)  # as the Select tool does while dragging
    assert R.redact_selection(c)
    (_, m), = R.marks_of(c)
    assert m.marked_text == "Account TOPSECRETWORD"
    copy = m.clone()
    assert isinstance(copy, R.RedactItem) and copy.geometry() == m.geometry() and copy.marked_text == m.marked_text
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    t = pymupdf.open(out)[0].get_text()
    assert "Account" not in t and "TOPSECRETWORD" not in t and "visible" in t
