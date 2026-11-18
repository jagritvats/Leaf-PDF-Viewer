"""Page management: ordering ops, ranges, extract/split, clipboard across documents, thumbnails."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from leaf import commands as C
from leaf import pageops as P
from leaf.items import ShapeItem, TextItem
from leaf.sidebar import Sidebar
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path)
    return path


def five(tmp_path, name="five.pdf"):
    path = str(tmp_path / name)
    d = pymupdf.open()
    for i in range(5):
        d.new_page(width=300 + i, height=400).insert_text((20, 40), f"P{i + 1}")
    d.save(path)
    d.close()
    return path


def texts(path):
    d = pymupdf.open(path)
    out = [p.get_text().split()[0] if p.get_text().strip() else "" for p in d]
    d.close()
    return out


def labels(c):
    return [r.src.page(r.index).get_text().split()[0] for r in c.doc.pages]


# ------------------------------------------------------------------ ranges
def test_parse_ranges():
    assert P.parse_ranges("1-3, 7, 10-", 12) == [0, 1, 2, 6, 9, 10, 11]
    assert P.parse_ranges("", 3) == [0, 1, 2]
    assert P.parse_ranges("all", 2) == [0, 1]
    assert P.parse_ranges("-2", 5) == [0, 1]
    assert P.parse_ranges("4-2", 5) == [3, 2, 1]
    assert P.parse_ranges("2 – 3; 3 1", 5) == [1, 2, 0]  # en dash, semicolons, spaces, duplicate dropped
    assert P.parse_ranges("odd", 5) == [0, 2, 4]
    assert P.parse_ranges("even", 5) == [1, 3]
    assert P.parse_ranges("last", 5) == [4]
    assert P.parse_ranges("3-last", 4) == [2, 3]
    for bad in ("0", "6", "x", "1-2-3", "2-9", ",,"):
        with pytest.raises(ValueError):
            P.parse_ranges(bad, 5)
    assert P.format_ranges([0, 1, 2, 6, 9, 10]) == "1-3, 7, 10-11"
    assert P.format_ranges([]) == ""
    assert P.parse_ranges(P.format_ranges([4, 0, 2, 3]), 5) == [0, 2, 3, 4]


def test_split_groups_and_paths():
    assert P.split_every(5, 2) == [[0, 1], [2, 3], [4]]
    assert P.split_every(3, 10) == [[0, 1, 2]]
    assert P.split_at(5, [2, 4]) == [[0, 1], [2, 3], [4]]
    assert P.split_at(5, [0, 9]) == [[0, 1, 2, 3, 4]]
    assert P.numbered_paths("/x/a.pdf", 12)[0].replace("\\", "/") == "/x/a-01.pdf"


def test_paper_names():
    assert P.size_text(595, 842) == "A4 · 210 × 297 mm"
    assert P.size_text(842, 595).startswith("A4 landscape")
    assert P.paper_name(612, 792) == "Letter"
    assert P.paper_name(500, 500) == ""


def test_move_helpers():
    u = [1, 2, 3, 4, 5]
    assert P.move_block(u, {2, 4}, 0) == [2, 4, 1, 3, 5]
    assert P.move_block(u, {2, 4}, 5) == [1, 3, 5, 2, 4]
    assert P.move_block(u, {1, 2}, 4) == [3, 4, 1, 2, 5]  # drop index counts the original rows
    assert P.move_block(u, {3}, 3) == u
    assert P.move_by(u, {2, 4}, -1) == [2, 1, 4, 3, 5]
    assert P.move_by(u, {1, 3}, -1) == [1, 3, 2, 4, 5]  # blocked at the top
    assert P.move_by(u, {5, 3}, 1) == [1, 2, 4, 3, 5]


# --------------------------------------------------------------- page ops
def test_multi_move_by_block_and_undo(app, tmp_path):
    c = canvas_for(five(tmp_path))
    P.move_rows(c, [1, 3], 5)
    assert labels(c) == ["P1", "P3", "P5", "P2", "P4"]
    assert c.undo.count() == 1
    c.undo.undo()
    assert labels(c) == ["P1", "P2", "P3", "P4", "P5"]
    out = str(tmp_path / "moved.pdf")
    c.undo.redo()
    c.doc.save(out, c.overlays_for)
    assert texts(out) == ["P1", "P3", "P5", "P2", "P4"]


def test_sidebar_drop_and_keys_move_selection(app, tmp_path):
    c = canvas_for(five(tmp_path))
    sb = Sidebar(c)
    sb.resize(200, 900)
    sb.rebuild()
    got = []
    sb.orderChanged.connect(got.append)
    sb.select_uids([c.doc.pages[0].uid, c.doc.pages[2].uid])
    assert sb.selected_rows() == [0, 2]
    sb.move_selected_by(1)
    uids = [r.uid for r in c.doc.pages]
    assert got[-1] == [uids[1], uids[0], uids[3], uids[2], uids[4]]
    sb.move_selected_to(5)
    assert got[-1] == [uids[1], uids[3], uids[4], uids[0], uids[2]]
    # selection survives a rebuild after the page list changes
    P.reorder(c, got[-1])
    sb.rebuild()
    assert sb.selected_uids() == [uids[0], uids[2]]
    assert sb.selected_rows() == [3, 4]


def test_duplicate_reverse_undo_and_save(app, pdf, tmp_path):
    c = canvas_for(pdf)
    c.doc.form_values[(c.doc.pages[0].uid, "name", (72.0, 200.0, 300.0, 220.0))] = "Ada"
    t = add(c, 0, TextItem(QPointF(100, 400), {"size": 14}, "DUPTEXT"))
    assert t.toPlainText() == "DUPTEXT"
    new = P.duplicate(c, [0])
    assert len(c.doc.pages) == 4 and c.doc.pages[1].uid == new[0]
    assert c.doc.pages[1].src is c.doc.pages[0].src and c.doc.pages[1].index == 0
    assert [o.toPlainText() for o in c.overlays_for(c.doc.pages[1])] == ["DUPTEXT"]
    P.reverse(c, [])
    assert [r.index for r in c.doc.pages] == [2, 1, 0, 0]
    out = str(tmp_path / "dup.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert d.page_count == 4
    assert len({d[i].xref for i in range(4)}) == 4  # the duplicate is its own page object
    for i in (2, 3):
        assert "DUPTEXT" in d[i].get_text()
        w = {x.field_name: x.field_value for x in d[i].widgets()}
        assert "Ada" in w.values(), w
    assert d[1].rotation == 90  # the source's own /Rotate survives
    d.close()
    c.undo.undo()
    c.undo.undo()
    assert [r.index for r in c.doc.pages] == [0, 1, 2]


def test_extract_each_and_split_counts(app, tmp_path):
    c = canvas_for(five(tmp_path))
    rows = P.parse_ranges("1-2, 5", 5)
    paths = P.numbered_paths(str(tmp_path / "ex.pdf"), len(rows))
    for p, r in zip(paths, rows):
        c.doc.save(p, c.overlays_for, [c.doc.pages[r]])
    assert [texts(p) for p in paths] == [["P1"], ["P2"], ["P5"]]
    groups = P.split_every(5, 2)
    paths = P.numbered_paths(str(tmp_path / "part.pdf"), len(groups))
    for p, g in zip(paths, groups):
        c.doc.save(p, c.overlays_for, [c.doc.pages[i] for i in g])
    assert [texts(p) for p in paths] == [["P1", "P2"], ["P3", "P4"], ["P5"]]
    assert texts(str(tmp_path / "five.pdf")) == ["P1", "P2", "P3", "P4", "P5"]  # original untouched


def test_copy_paste_between_documents(app, pdf, tmp_path):
    a = canvas_for(pdf)
    other = str(tmp_path / "other.pdf")
    make_pdf(other)  # same field names as `a`: they get renamed on merge, values must still land
    b = canvas_for(other)
    uid = b.doc.pages[0].uid
    b.doc.form_values[(uid, "name", (72.0, 200.0, 300.0, 220.0))] = "FromB"
    b.doc.form_values[(uid, "agree", (72.0, 240.0, 86.0, 254.0))] = True
    add(b, 0, TextItem(QPointF(80, 500), {"size": 14}, "BTEXT"))
    add(b, 0, ShapeItem("rect", QRectF(100, 600, 80, 40), {"color": "#ff0000"}))
    a.doc.form_values[(a.doc.pages[0].uid, "name", (72.0, 200.0, 300.0, 220.0))] = "InA"

    assert P.copy_pages(b, [0, 1]) == 2
    assert P.clipboard_kind() == "pages"
    b.undo.push(C.SetPages(b, P.state(b.doc), P.state(b.doc)[2:], "Cut"))  # source edits later don't matter
    new = P.paste_pages(a, 1)
    assert len(new) == 2 and len(a.doc.pages) == 5
    pasted = a.doc.pages[1]
    assert pasted.src is not b.doc.pages[0].src and pasted.src in a.doc.sources
    assert pasted.src.data is b.doc.primary.data  # bytes shared, no disk read
    assert a.doc.pages[2].rotation == 0 and a.doc.page(a.doc.pages[2]).rotation == 90
    # paste again re-uses the imported source
    P.paste_pages(a, 5)
    assert a.doc.pages[5].src is pasted.src

    out = str(tmp_path / "merged.pdf")
    a.doc.save(out, a.overlays_for)
    d = pymupdf.open(out)
    assert d.page_count == 7
    assert "PAGE1" in d[1].get_text() and "BTEXT" in d[1].get_text()
    assert [an.type[1] for an in d[1].annots()] == ["Square"]
    vals = {w.field_name.split(" [")[0]: w.field_value for w in d[1].widgets()}
    assert vals["name"] == "FromB" and vals["agree"] not in ("Off", False, "")
    assert {w.field_name: w.field_value for w in d[0].widgets()}["name"] == "InA"
    assert "PAGE2" in d[2].get_text() and d[2].rotation == 90
    d.close()
    a.undo.undo()
    a.undo.undo()
    assert len(a.doc.pages) == 3


def test_paste_image_as_page(app, pdf):
    c = canvas_for(pdf)
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366cc"))
    uids = P.paste_image(c, 3, img)
    ref = c.doc.pages[3]
    assert ref.uid == uids[0]
    w, h = c.doc.display_size(ref)
    assert (round(w), round(h)) == (842, 595)  # neighbour's A4, turned to match the landscape image
    c.undo.undo()
    assert len(c.doc.pages) == 3


def test_insert_blank_matches_neighbour(app, pdf):
    c = canvas_for(pdf)
    P.insert_blank(c, 2, like=1)  # page 2 is displayed landscape (/Rotate 90)
    assert c.doc.display_size(c.doc.pages[2]) == pytest.approx((842, 595))
    P.insert_blank(c, 0)
    assert c.doc.display_size(c.doc.pages[0]) == pytest.approx((595, 842))
    assert c.undo.count() == 2


def test_thumbnail_rerenders_after_rotate(app, pdf):
    c = canvas_for(pdf)
    sb = Sidebar(c)
    sb.resize(200, 900)
    sb.show()
    sb.rebuild()
    uid = c.doc.pages[0].uid
    sb.thumb(uid)
    for _ in range(40):
        pump(app, 0.05)
        if sb.thumb(uid) is not None:
            break
    img = sb.thumb(uid)
    assert img is not None and img.height() > img.width()
    P.rotate(c, [0], 90)
    sb.rebuild()
    assert sb.thumb(uid) is None  # stale (rotated) thumbnail is not shown, a new one is queued
    for _ in range(40):
        pump(app, 0.05)
        if sb.thumb(uid) is not None:
            break
    img = sb.thumb(uid)
    assert img is not None and img.width() > img.height()
    sb.hide()


def test_repaints_do_not_requeue_a_pending_thumbnail(app, pdf):
    """Every paint asks for missing thumbnails. A thumbnail that is already queued must not be queued again: that
    re-armed the render timer on each frame, spun the GUI and starved the render worker's results (0.2.0 self-test)."""
    c = canvas_for(pdf)
    sb = Sidebar(c)
    sb.rebuild()
    uid = c.doc.pages[0].uid
    r = c.renderer
    r.timer.stop()
    calls = []
    orig = r.want_thumb
    r.want_thumb = lambda key, fn: (calls.append(key), orig(key, fn))
    for _ in range(5):
        assert sb.thumb(uid) is None
    assert len(calls) == 1
    r.thumbs.clear()
    r.asyncs[("t", id(sb), uid)] = (c.doc.pages[0], 0.1, lambda img: None)  # waiting for the worker
    assert sb.thumb(uid) is None and len(calls) == 1
    r.asyncs.clear()


def test_page_tooltip(app, pdf):
    c = canvas_for(pdf)
    tip = P.page_tooltip(c.doc, c.doc.pages[1], 1)
    assert "Page 2" in tip and "A4 landscape" in tip and "Rotated 90°" in tip


def test_reorder_and_delete_keep_the_form(app, pdf, tmp_path):
    c = canvas_for(pdf)
    P.move_rows(c, [0], 3)
    out = str(tmp_path / "re.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert d.is_form_pdf == 3 and [w.field_name for w in d[2].widgets()] == ["name", "agree", "color"]
    d.close()
    P.delete(c, [2])  # the page holding the fields
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert d.page_count == 2 and not d.is_form_pdf  # orphaned fields are dropped from /AcroForm
    d.close()


def test_fast_save_full_rewrite(app, tmp_path):
    c = canvas_for(five(tmp_path))
    P.reverse(c, [])
    out = str(tmp_path / "fast.pdf")
    assert c.doc.save(out, c.overlays_for, fast=True) is False and texts(out) == ["P5", "P4", "P3", "P2", "P1"]
