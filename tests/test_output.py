"""Output: printing (to a PDF printer), document properties/metadata, password protection."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QMarginsF, QRectF, QSettings
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtPrintSupport import QPrinter

from leaf import docprops, printing
from leaf.document import PdfDocument
from leaf.docprops import Security, SetMetadata, SetSecurity
from leaf.items import ShapeItem
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401

MARGIN = 36.0  # points


def _pdf_printer(path: str, landscape: bool = False, res: int = 72) -> QPrinter:
    p = QPrinter(QPrinter.PrinterMode.HighResolution)
    p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    p.setOutputFileName(path)
    p.setResolution(res)
    p.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    p.setPageOrientation(QPageLayout.Orientation.Landscape if landscape else QPageLayout.Orientation.Portrait)
    p.setPageMargins(QMarginsF(MARGIN, MARGIN, MARGIN, MARGIN), QPageLayout.Unit.Point)
    return p


def _mixed_pdf(path: str) -> None:
    """Page 1 portrait A4, page 2 landscape, page 3 small (200 x 100)."""
    d = pymupdf.open()
    for w, h in ((595, 842), (842, 595), (200, 100)):
        p = d.new_page(width=w, height=h)
        p.insert_text((20, 40), f"{w}x{h}", fontsize=12)
    d.save(path)
    d.close()


def _image_boxes(path: str) -> list[list[pymupdf.Rect]]:
    d = pymupdf.open(path)
    out = [[pymupdf.Rect(i["bbox"]) for i in page.get_image_info()] for page in d]
    d.close()
    return out


def _union(rects) -> pymupdf.Rect:
    r = pymupdf.Rect(rects[0])
    for x in rects[1:]:
        r |= x
    return r


@pytest.fixture
def mixed(tmp_path):
    path = str(tmp_path / "mixed.pdf")
    _mixed_pdf(path)
    return path


# ---------------------------------------------------------------- printing
def test_print_range_copies_collate(app, mixed, tmp_path):  # noqa: F811
    c = canvas_for(mixed)
    job = printing.PrintJob(c.doc, c.overlays_for)
    try:
        out = str(tmp_path / "printed.pdf")
        n = job.run(_pdf_printer(out), printing.PrintOptions(rows=[0, 2], copies=2, collate=False))
        assert n == 4
        d = pymupdf.open(out)
        assert d.page_count == 4
        d.close()
        # page ranges come from pageops' parser
        from leaf.pageops import parse_ranges

        out2 = str(tmp_path / "printed2.pdf")
        assert job.run(_pdf_printer(out2), printing.PrintOptions(rows=parse_ranges("2-3", 3))) == 2
        assert printing.PrintOptions(rows=[0, 1], copies=2).page_sequence(3) == [0, 1, 0, 1]
        assert printing.PrintOptions(rows=[0, 1], copies=2, collate=False).page_sequence(3) == [0, 0, 1, 1]
    finally:
        job.close()


def test_print_fit_actual_and_auto_rotate(app, mixed, tmp_path):  # noqa: F811
    c = canvas_for(mixed)
    job = printing.PrintJob(c.doc, c.overlays_for)
    aw, ah = 595.28 - 2 * MARGIN, 841.89 - 2 * MARGIN  # printable area on A4 portrait
    try:
        out = str(tmp_path / "fit.pdf")
        job.run(_pdf_printer(out), printing.PrintOptions(fit=True, auto_rotate=True))
        boxes = [_union(b) for b in _image_boxes(out)]
        # portrait A4 page fills the printable width
        assert abs(boxes[0].width - aw) < 2 and boxes[0].height <= ah + 1
        # the landscape page is turned to the portrait paper and fills it too
        assert boxes[1].height > boxes[1].width and abs(boxes[1].width - aw) < 2
        # the small (landscape) page is turned, enlarged to fit and centered
        assert abs(boxes[2].height - ah) < 2 and abs(boxes[2].width - ah / 2) < 2
        assert abs((boxes[2].x0 + boxes[2].x1) / 2 - 595.28 / 2) < 1.5

        out = str(tmp_path / "actual.pdf")
        job.run(_pdf_printer(out), printing.PrintOptions(fit=False, auto_rotate=False, rows=[1, 2]))
        land, small = [_union(b) for b in _image_boxes(out)]
        assert abs(small.width - 200) < 1.5 and abs(small.height - 100) < 1.5  # actual size
        assert abs((small.x0 + small.x1) / 2 - 595.28 / 2) < 1.5  # centered on the paper
        # without auto-rotate the landscape page stays landscape (and is clipped to the printable area)
        assert land.width >= aw - 1 and land.height < land.width

        out = str(tmp_path / "landscape_paper.pdf")
        job.run(_pdf_printer(out, landscape=True), printing.PrintOptions(fit=True, auto_rotate=True, rows=[0, 1]))
        portrait, landscape = [_union(b) for b in _image_boxes(out)]
        assert portrait.width > portrait.height  # the portrait page is turned onto landscape paper
        assert landscape.width > landscape.height
    finally:
        job.close()


def test_print_bands_grayscale_and_annotations(app, mixed, tmp_path, monkeypatch):  # noqa: F811
    c = canvas_for(mixed)
    add(c, 0, ShapeItem("rect", QRectF(100, 300, 120, 60), {"color": "#ff0000"}))
    monkeypatch.setattr(printing, "BAND_PIXELS", 20000)  # force several bands per page
    job = printing.PrintJob(c.doc, c.overlays_for)
    try:
        assert len(list(job.output()[0].annots())) == 1
        out = str(tmp_path / "bands.pdf")
        job.run(_pdf_printer(out), printing.PrintOptions(rows=[0], grayscale=True))
        d = pymupdf.open(out)
        info = d[0].get_image_info()
        assert len(info) > 3
        whole = _union([pymupdf.Rect(i["bbox"]) for i in info])
        assert abs(whole.width - (595.28 - 2 * MARGIN)) < 2
        assert all(i["colorspace"] == 1 for i in info)
        d.close()
    finally:
        job.close()
    plain = printing.PrintJob(c.doc, c.overlays_for, annotations=False)
    try:
        assert not list(plain.output()[0].annots())
    finally:
        plain.close()
    assert printing.preview_dpi(10) == 150 and printing.preview_dpi(4000) == 36


def test_placement_math():
    s, rot, r = printing.placement(842, 595, 500, 800, 300, True, True)
    assert rot and abs(r.width() - 500) < 1e-6 and abs(r.height() - 500 * 842 / 595) < 1e-6
    s, rot, r = printing.placement(100, 50, 500, 700, 300, False, True)
    assert rot and abs(s - 300 / 72) < 1e-9 and abs(r.width() - 50 * s) < 1e-6


def test_print_encrypted_output(app, pdf_enc, tmp_path):  # noqa: F811
    doc = PdfDocument.open(pdf_enc, "u")
    job = printing.PrintJob(doc, lambda ref: [])
    try:
        out = str(tmp_path / "p.pdf")
        assert job.run(_pdf_printer(out), printing.PrintOptions()) == 3
    finally:
        job.close()


# ---------------------------------------------------------------- metadata
@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path, form=False)
    d = pymupdf.open(path)
    d.set_metadata({"title": "Old title", "author": "Old author", "creationDate": "D:20200102030405+01'00'"})
    d.set_xml_metadata(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreatorTool="Tests">'
        '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Old title</rdf:li></rdf:Alt></dc:title>'
        '<dc:creator><rdf:Seq><rdf:li>Old author</rdf:li></rdf:Seq></dc:creator>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
    d.saveIncr()
    d.close()
    return path


@pytest.fixture
def pdf_enc(tmp_path):
    src = str(tmp_path / "plain.pdf")
    make_pdf(src, form=False)
    d = pymupdf.open(src)
    path = str(tmp_path / "enc.pdf")
    d.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o",
           permissions=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY)
    d.close()
    return path


def _meta(path, pw=None):
    d = pymupdf.open(path)
    if pw:
        d.authenticate(pw)
    m, x = d.metadata, d.get_xml_metadata()
    d.close()
    return m, x


def test_metadata_incremental_full_and_undo(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    doc = c.doc
    new = docprops.metadata_changes(doc, {"title": "New title", "author": "Old author", "subject": "S",
                                          "keywords": "a, b"})
    assert new == {"title": "New title", "subject": "S", "keywords": "a, b"}
    assert docprops.metadata_changes(doc, {"title": "Old title", "author": "Old author"}) is None
    c.undo.push(SetMetadata(doc, new))
    out = str(tmp_path / "inc.pdf")
    assert doc.save(out, c.overlays_for) is True  # metadata alone keeps incremental save
    m, xmp = _meta(out)
    assert (m["title"], m["author"], m["subject"], m["keywords"]) == ("New title", "Old author", "S", "a, b")
    assert m["creationDate"] == "D:20200102030405+01'00'" and m["modDate"]
    assert "New title" in xmp and "Old title" not in xmp and "Old author" in xmp and "a, b" in xmp
    assert "xpacket" in xmp and "CreatorTool" in xmp

    doc.pages[0].rotation = 90  # force a full rewrite
    out2 = str(tmp_path / "full.pdf")
    assert doc.save(out2, c.overlays_for) is False
    assert _meta(out2)[0]["title"] == "New title"
    doc.pages[0].rotation = 0

    c.undo.undo()
    assert doc.metadata_edits == {}
    out3 = str(tmp_path / "undone.pdf")
    doc.save(out3, c.overlays_for)
    assert _meta(out3)[0]["title"] == "Old title"
    c.undo.redo()
    assert doc.metadata_edits["title"] == "New title"


def test_xmp_and_dates():
    assert docprops.update_xmp("no packet here", {"title": "x"}) is None
    xml = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description rdf:about="" xmlns:pdf="http://ns.adobe.com/pdf/1.3/" pdf:Keywords="old"/>'
           '</rdf:RDF></x:xmpmeta>')
    new = docprops.update_xmp(xml, {"keywords": "k1", "title": "T"})
    assert 'Keywords="k1"' in new and "x-default" in new and ">T<" in new and "ns0" not in new
    dt = docprops.parse_pdf_date("D:20231015123045+02'00'")
    assert (dt.year, dt.month, dt.hour, dt.utcoffset().total_seconds()) == (2023, 10, 12, 7200)
    assert docprops.parse_pdf_date("D:2023").month == 1
    assert docprops.parse_pdf_date("garbage") is None
    assert docprops.format_date("D:20231015123045Z").startswith("2023-10-15 12:30")


# ---------------------------------------------------------------- security
def test_encrypt_with_passwords_and_undo(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    doc = c.doc
    perms = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY
    c.undo.push(SetSecurity(doc, Security("user", "owner", perms)))
    assert docprops.is_protected(doc)
    out = str(tmp_path / "locked.pdf")
    assert doc.save(out, c.overlays_for) is False  # protection forces a full rewrite
    d = pymupdf.open(out)
    assert d.needs_pass and not d.authenticate("wrong")
    assert d.authenticate("user") == 2
    assert d.permissions & pymupdf.PDF_PERM_PRINT and not d.permissions & pymupdf.PDF_PERM_COPY
    assert not d.permissions & pymupdf.PDF_PERM_MODIFY
    assert "AES" in d.metadata["encryption"] and "256" in d.metadata["encryption"]
    assert d.page_count == 3
    d.close()
    d = pymupdf.open(out)
    assert d.authenticate("owner") & 4
    d.close()
    # reopening the saved file in Leaf works with the password
    with pytest.raises(Exception):
        PdfDocument.open(out)
    assert len(PdfDocument.open(out, "user").pages) == 3

    c.undo.undo()
    assert doc.security is None and not docprops.is_protected(doc)
    out2 = str(tmp_path / "unlocked.pdf")
    assert doc.save(out2, c.overlays_for) is True
    assert not pymupdf.open(out2).needs_pass


def test_remove_password(app, pdf_enc, tmp_path):  # noqa: F811
    doc = PdfDocument.open(pdf_enc, "u")
    info = docprops.security_info(doc)
    assert info.encrypted and info.open_password and not info.pending
    assert not info.perms & pymupdf.PDF_PERM_COPY
    assert not docprops.owner_access(doc.primary) and docprops.owner_access(doc.primary, "o")
    doc.security = Security()
    out = str(tmp_path / "open.pdf")
    doc.save(out, lambda ref: [])
    d = pymupdf.open(out)
    assert not d.needs_pass and not d.metadata["encryption"] and d.page_count == 3
    d.close()


def test_opened_encrypted_keeps_encryption(app, pdf_enc, tmp_path):  # noqa: F811
    from PySide6.QtCore import QRectF

    from leaf.canvas import Canvas

    c = Canvas()
    c.resize(900, 700)
    c.set_document(PdfDocument.open(pdf_enc, "u"))
    add(c, 0, ShapeItem("rect", QRectF(100, 300, 120, 60), {"color": "#ff0000"}))
    c.undo.push(SetMetadata(c.doc, {"title": "Secret"}))
    inc = str(tmp_path / "inc.pdf")
    assert c.doc.save(inc, c.overlays_for) is True
    c.doc.pages[1].rotation = 90
    full = str(tmp_path / "full.pdf")
    assert c.doc.save(full, c.overlays_for) is False
    fast = str(tmp_path / "fast.pdf")
    c.doc.save(fast, c.overlays_for, fast=True)  # crash-recovery copies stay encrypted too
    for path in (inc, full, fast):
        d = pymupdf.open(path)
        assert d.needs_pass, path
        assert d.authenticate("u") == 2 and d.page_count == 3
        assert d.metadata["title"] == "Secret"
        assert len(list(d[0].annots())) == 1
        assert not d.permissions & pymupdf.PDF_PERM_COPY  # the original permissions are kept
        d.close()
        d = pymupdf.open(path)
        assert d.authenticate("o") & 4
        d.close()


# ---------------------------------------------------------------- UI
@pytest.fixture
def win(app, tmp_path):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafOutputTest")
    QSettings().clear()
    from leaf.window import MainWindow

    w = MainWindow()
    w.resize(1000, 700)
    w.show()
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


def test_properties_dialog_and_actions(app, win, pdf):  # noqa: F811
    for name in ("a_print", "a_print_preview", "a_doc_props", "a_protect"):
        a = getattr(win, name)
        assert a.objectName() == name and not a.isEnabled()
    assert win.a_print.shortcut().toString() == "Ctrl+P"
    win.open_path(pdf)
    assert win.a_doc_props.isEnabled()
    dlg = docprops.PropertiesDialog(win)
    assert dlg.fields["title"].text() == "Old title"
    assert dlg.fields["author"].text() == "Old author"
    labels = [w.text() for w in dlg.findChildren(type(dlg.sec_state))]
    assert any(t.startswith("2020-01-02 03:04") for t in labels)
    assert "3" in labels and any("A4" in t for t in labels)
    assert any("No protection" in t for t in labels)
    dlg.tabs.setCurrentIndex(1)
    for _ in range(50):
        if dlg.fonts_done():
            break
        pump(app, 0.02)
    assert dlg.fonts_done() and dlg.fonts.topLevelItemCount() >= 1
    assert dlg.fonts.topLevelItem(0).text(0) == "Helvetica"
    dlg.fields["title"].setText("Edited")
    dlg._pending_sec, dlg._sec_changed = Security("pw"), True
    assert dlg.commit()
    dlg.deleteLater()
    assert win.canvas.doc.metadata_edits == {"title": "Edited"}
    assert win.canvas.doc.security == Security("pw")
    assert "(protected)" in win.windowTitle()
    win.canvas.undo.undo()  # one macro step
    assert win.canvas.doc.metadata_edits == {} and win.canvas.doc.security is None
    assert "(protected)" not in win.windowTitle()


def test_print_dialog_options(app, win, pdf):  # noqa: F811
    win.open_path(pdf)
    dlg = printing.PrintDialog(win)
    w = dlg.options_widget
    assert w.options().rows == [0, 1, 2]
    w.r_current.setChecked(True)
    assert w.options().rows == [w.current]
    w.range_edit.setText("3, 1")
    w.r_custom.setChecked(True)
    assert w.options().rows == [2, 0]
    w.range_edit.setText("9")
    assert w.options() is None
    w.actual.setChecked(True)
    w.grayscale.setChecked(True)
    w.r_all.setChecked(True)
    o = w.options()
    assert not o.fit and o.grayscale and o.annotations
    dlg.deleteLater()


def test_preview_renders_pages(app, mixed):  # noqa: F811
    from PySide6.QtPrintSupport import QPrintPreviewWidget

    c = canvas_for(mixed)
    job = printing.PrintJob(c.doc, c.overlays_for)
    try:
        p = QPrinter(QPrinter.PrinterMode.HighResolution)
        p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)  # no system printer needed
        w = QPrintPreviewWidget(p)
        seen = []
        w.paintRequested.connect(lambda pr: seen.append(job.run(pr, printing.PrintOptions(rows=[0, 1]))))
        w.updatePreview()
        assert seen == [2] and w.pageCount() == 2
        w.deleteLater()
    finally:
        job.close()
