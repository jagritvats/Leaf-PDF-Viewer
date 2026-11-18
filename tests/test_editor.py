"""Headless end-to-end tests: edits made in the canvas must land correctly in the saved PDF."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from leaf import commands as C
from leaf.canvas import Canvas
from leaf.document import PageRef, PdfDocument, rect_key
from leaf.items import HighlightItem, ImageItem, InkItem, LineItem, ShapeItem, TextItem


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def make_pdf(path, rotate_second=True, form=True):
    d = pymupdf.open()
    for i in range(3):
        p = d.new_page(width=595, height=842)
        p.insert_text((72, 100), f"PAGE{i + 1} The quick brown fox jumps over the lazy dog.", fontsize=12)
        p.insert_text((72, 130), "Second line of text for selection tests.", fontsize=12)
    if rotate_second:
        d[1].set_rotation(90)
    if form:
        p = d[0]
        for name, ftype, rect, extra in [
            ("name", pymupdf.PDF_WIDGET_TYPE_TEXT, (72, 200, 300, 220), {}),
            ("agree", pymupdf.PDF_WIDGET_TYPE_CHECKBOX, (72, 240, 86, 254), {}),
            ("color", pymupdf.PDF_WIDGET_TYPE_COMBOBOX, (72, 270, 200, 290), {"choice_values": ["Red", "Green", "Blue"]}),
        ]:
            w = pymupdf.Widget()
            w.field_name = name
            w.field_type = ftype
            w.rect = pymupdf.Rect(rect)
            for k, v in extra.items():
                setattr(w, k, v)
            p.add_widget(w)
    d.save(path)
    d.close()


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path)
    return path


def pump(app, secs=1.0):
    import time

    end = time.time() + secs
    while time.time() < end:
        app.processEvents()


def canvas_for(path):
    c = Canvas()
    c.resize(900, 700)
    c.set_document(PdfDocument.open(path))
    return c


def add(c, page_idx, item):
    item.setParentItem(c.order[page_idx].content)
    c.undo.push(C.AddItems([item]))
    return item


def test_overlays_land_in_display_space(app, pdf, tmp_path):
    c = canvas_for(pdf)
    # page 0 (unrotated) and page 1 (/Rotate 90): same display-space rect for both
    for i in (0, 1):
        add(c, i, ShapeItem("rect", QRectF(100, 300, 120, 60), {"color": "#ff0000"}))
        add(c, i, LineItem("arrow", QPointF(100, 400), QPointF(250, 450)))
        ink = InkItem(QPointF(0, 0))
        ink.setPos(QPointF(300, 300))
        for k in range(10):
            ink.add_point(QPointF(k * 5, k * 3))
        add(c, i, ink)
        add(c, i, TextItem(QPointF(100, 500), {"size": 14}, f"Hello overlay {i}"))
        img = QImage(40, 20, QImage.Format.Format_RGB32)
        img.fill(QColor("blue"))
        add(c, i, ImageItem("image", QRectF(300, 500, 80, 40), img))
        add(c, i, ShapeItem("whiteout", QRectF(400, 600, 50, 20), {}))
    words = c.doc.primary.words(0)
    rects = [QRectF(QPointF(w[0], w[1]), QPointF(w[2], w[3])) for w in words[:3]]
    add(c, 0, HighlightItem(rects))

    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    for i in (0, 1):
        p = d[i]
        kinds = sorted(a.type[1] for a in p.annots())
        assert "Square" in kinds and "Line" in kinds and "Ink" in kinds, kinds
        sq = next(a for a in p.annots() if a.type[1] == "Square")
        disp = sq.rect * p.rotation_matrix
        assert abs(disp.x0 - 100) < 3 and abs(disp.y0 - 300) < 3, (i, disp)
        hits = p.search_for(f"Hello overlay {i}")
        assert hits, f"text not found on page {i}"
        dh = hits[0] * p.rotation_matrix
        assert abs(dh.x0 - 102) < 6 and 500 < dh.y0 < 520, (i, dh)
        line = next(b for b in p.get_text("dict")["blocks"] if b.get("lines") and "overlay" in
                    "".join(s["text"] for s in b["lines"][0]["spans"]))["lines"][0]
        # text must read left-to-right on screen, regardless of the page's /Rotate
        dx, dy = line["dir"]
        sdx = dx * p.rotation_matrix.a + dy * p.rotation_matrix.c
        assert sdx > 0.99, (i, line["dir"])
        imgs = p.get_image_info()
        assert any(abs((pymupdf.Rect(im["bbox"]) * p.rotation_matrix).x0 - 300) < 3 for im in imgs), imgs
    assert any(a.type[1] == "Highlight" for a in d[0].annots())


def test_forms_fill_and_roundtrip(app, pdf, tmp_path):
    c = canvas_for(pdf)
    ref = c.doc.pages[0]
    page = c.doc.page(ref)
    keys = {w.field_name: (ref.uid, w.field_name, rect_key(w.rect)) for w in page.widgets()}
    c.set_form_values({keys["name"]: "Jane Q. Public", keys["agree"]: True, keys["color"]: "Green"})
    assert c.undo.count() == 1
    out = str(tmp_path / "forms.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    vals = {w.field_name: w.field_value for w in d[0].widgets()}
    assert vals["name"] == "Jane Q. Public"
    assert vals["agree"] not in ("Off", False, None)
    assert vals["color"] == "Green"
    d.close()
    # undo restores the original value for the next save
    c.undo.undo()
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert {w.field_name: w.field_value for w in d[0].widgets()}["name"] in ("", None)


def test_page_ops_and_merge(app, pdf, tmp_path):
    other = str(tmp_path / "other.pdf")
    o = pymupdf.open()
    o.new_page().insert_text((72, 72), "OTHERDOC")
    o.save(other)
    c = canvas_for(pdf)
    doc = c.doc
    src = doc.add_source(other)
    old = [(r, r.rotation) for r in doc.pages]
    p1, p2, p3 = doc.pages
    new = [(p3, 0), (PageRef(src, 0), 0), (p1, 90)]  # reorder, insert, rotate, delete p2
    c.undo.push(C.SetPages(c, old, new, "ops"))
    add(c, 2, ShapeItem("rect", QRectF(50, 50, 100, 100), {}))  # overlay on a page with extra rotation
    out = str(tmp_path / "ops.pdf")
    assert doc.save(out, c.overlays_for) is False
    d = pymupdf.open(out)
    assert d.page_count == 3
    assert "PAGE3" in d[0].get_text() and "OTHERDOC" in d[1].get_text() and "PAGE1" in d[2].get_text()
    assert d[2].rotation == 90
    assert {w.field_name for w in d[2].widgets()} == {"name", "agree", "color"}  # form survived the reorder
    last = d[2]
    sq = next(last.annots())
    assert abs(sq.rect.x0 - 50) < 3 and abs(sq.rect.y0 - 50) < 3  # stays attached to the page content
    c.undo.undo()  # the overlay
    c.undo.undo()  # the page ops
    assert [r.uid for r in doc.pages] == [p1.uid, p2.uid, p3.uid] and p1.rotation == 0


def test_incremental_save_preserves_original_bytes(app, pdf, tmp_path):
    c = canvas_for(pdf)
    add(c, 0, ShapeItem("ellipse", QRectF(100, 100, 50, 50), {}))
    with open(pdf, "rb") as f:
        orig = f.read()
    out = str(tmp_path / "inc.pdf")
    assert c.doc.save(out, c.overlays_for) is True
    with open(out, "rb") as f:
        assert f.read().startswith(orig)
    # saving over the open file works (nothing is locked) and is repeatable
    assert c.doc.save(pdf, c.overlays_for) is True
    assert c.doc.save(pdf, c.overlays_for) is True
    assert sum(1 for _ in pymupdf.open(pdf)[0].annots()) == 1


def test_undo_redo_items(app, pdf):
    c = canvas_for(pdf)
    it = add(c, 0, ShapeItem("rect", QRectF(10, 10, 20, 20), {}))
    assert it.scene() is c.scene()
    c.undo.undo()
    assert it.scene() is None and not c.overlays_for(c.doc.pages[0])
    c.undo.redo()
    assert c.overlays_for(c.doc.pages[0]) == [it]
    it.setSelected(True)
    c.apply_props({"color": "#00ff00", "width": 5.0})
    assert it.props["color"] == "#00ff00"
    c.undo.undo()
    assert it.props["color"] != "#00ff00"


def test_rendering_and_text_selection(app, pdf):
    c = canvas_for(pdf)
    c.show()
    pump(app)
    first = c.order[0]
    assert first.img is not None, "visible page was not rendered"
    pg = c.order[0]
    words = c.doc.primary.words(0)
    a = QPointF(words[0][0] + 1, words[0][1] + 2)
    b = QPointF(words[3][2] - 1, words[3][1] + 2)
    c._update_text_selection(pg, a, b)
    assert c.selected_text().startswith("PAGE1 The quick brown")
    # zooming far in renders a sharp tile instead of a giant bitmap
    c.zoom_to(8.0)
    pump(app)
    vis = [p for p in c.visible_pages() if p.img is not None]
    assert vis and vis[0].img.width() * vis[0].img.height() <= 10_000_000 * 1.05
