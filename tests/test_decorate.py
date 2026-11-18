"""Page decorations: watermark, header & footer, Bates numbers - model, save output, preview and dialogs."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QRectF, QSettings

from leaf import decorations as D
from leaf import pageops
from leaf.items import ShapeItem, is_overlay
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path, form=False)
    return path


def _save(c, tmp_path, name="out.pdf"):
    out = str(tmp_path / name)
    inc = c.doc.save(out, c.overlays_for)
    return out, inc


def _spans(page):
    """(text, display-space direction) of every span; direction (1, 0) reads upright."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            d = pymupdf.Point(line["dir"]) * pymupdf.Matrix(page.rotation)
            for s in line["spans"]:
                out.append((s["text"], (round(d.x, 3), round(d.y, 3))))
    return out


# ------------------------------------------------------------------ model
def test_selection_modes():
    assert list(D.selection(D.header_footer(pages="all"), 5)) == [0, 1, 2, 3, 4]
    assert list(D.selection(D.header_footer(pages="odd"), 5)) == [0, 2, 4]
    assert list(D.selection(D.header_footer(pages="even"), 5)) == [1, 3]
    assert list(D.selection(D.header_footer(pages="skip_first"), 5)) == [1, 2, 3, 4]
    assert D.selection(D.header_footer(pages="range", page_range="4-, 2"), 5) == {1: 0, 3: 1, 4: 2}
    assert D.selection(D.header_footer(pages="range", page_range="9"), 5) == {}  # invalid range: nothing


def test_token_expansion():
    spec = D.header_footer(start=5, date_format="%d.%m.%Y")
    ctx = D.Context(3, "report.pdf", "", datetime(2026, 3, 4), lambda i: "iv" if i == 1 else "")
    t = "{page}/{pages} {label} {date} {filename} {title} {unknown}"
    assert D.expand(t, spec, 1, ctx) == "6/7 iv 04.03.2026 report.pdf report {unknown}"
    assert D.expand("{label}", spec, 0, ctx) == "1"  # no label: the position
    ctx.title = "Annual report"
    assert D.expand("{title}", spec, 0, ctx) == "Annual report"


def test_bates_text():
    spec = D.bates(prefix="ACME-", suffix="-X", start=42, digits=6)
    assert D.bates_text(spec, 0) == "ACME-000042-X"
    assert D.bates_text(spec, 3) == "ACME-000045-X"
    assert D.bates_text(D.bates(digits=2, start=123), 0) == "123"


def test_specs_compare_without_uid():
    a, b = D.watermark(text="X"), D.watermark(text="X")
    assert a == b and a.uid != b.uid and hash(a) == hash(b)


# ------------------------------------------------------------------- save
def test_page_numbers_follow_final_order(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.header_footer(slots=("", "", "", "", "Page {page} of {pages}", "")))
    uids = [r.uid for r in c.doc.pages]
    pageops.reorder(c, [uids[2], uids[0], uids[1]])
    out, inc = _save(c, tmp_path)
    assert not inc
    d = pymupdf.open(out)
    for i, body in enumerate(("PAGE3", "PAGE1", "PAGE2")):
        p = d[i]
        assert p.search_for(body)
        assert p.search_for(f"Page {i + 1} of 3")
        assert not p.search_for(f"Page {(i + 1) % 3 + 1} of 3")
        hit = p.search_for(f"Page {i + 1} of 3")[0] * p.rotation_matrix
        assert hit.y0 > p.rect.height * 0.9  # bottom center of the displayed page
        assert abs((hit.x0 + hit.x1) / 2 - p.rect.width / 2) < 2


def test_bates_and_page_selection(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.bates(prefix="ABC", digits=6, start=7, pages="skip_first", slot="tr"))
    D.add(c, D.header_footer(slots=("odd {page}", "", "", "", "", ""), pages="odd"))
    D.add(c, D.header_footer(slots=("", "", "", "", "", "ranged"), pages="range", page_range="2"))
    out, _ = _save(c, tmp_path)
    d = pymupdf.open(out)
    texts = [d[i].get_text() for i in range(3)]
    assert "ABC" not in texts[0]
    assert "ABC000007" in texts[1] and "ABC000008" in texts[2]
    assert "odd 1" in texts[0] and "odd" not in texts[1] and "odd 3" in texts[2]
    assert ["ranged" in t for t in texts] == [False, True, False]
    hit = d[2].search_for("ABC000008")[0] * d[2].rotation_matrix
    assert hit.y1 < 60 and hit.x1 > d[2].rect.width - 60  # top right


def test_watermark_behind_with_opacity(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.watermark(text="SECRET", opacity=0.25, behind=True))
    out, _ = _save(c, tmp_path)
    d = pymupdf.open(out)
    p = d[0]
    text = p.get_text(sort=False)
    assert "SECRET" in text
    # behind: drawn before the page's own content, which still reads in its original order
    assert text.index("SECRET") < text.index("PAGE1") < text.index("Second line")
    assert p.get_text().count("PAGE1 The quick brown fox") == 1
    spans = dict(_spans(p))
    dx, dy = spans["SECRET"]
    assert dx > 0.3 and dy < -0.3  # diagonal, rising to the right
    # the watermark is a marked artifact with its own transparency
    raw = b"".join(d.xref_stream(x) or b"" for x in range(1, d.xref_length()) if d.xref_is_stream(x))
    assert b"/Artifact" in raw and b"/Watermark" in raw
    objs = "".join(d.xref_object(x) for x in range(1, d.xref_length()))
    assert "/ca 0.250" in objs.replace("\n", " ") or "/ca .25" in objs
    # page content stream starts with the watermark: wrapped original follows
    first = p.read_contents().lstrip()
    assert first.startswith(b"q") or b"Do" in first[:200]


def test_watermark_in_front_and_tiled_image(app, pdf, tmp_path):  # noqa: F811
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(40, 20, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366cc"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    png = bytes(buf.data())
    c = canvas_for(pdf)
    D.add(c, D.watermark(image=png, image_name="logo.png", scale=0.2, tiled=True, opacity=0.5))
    out, _ = _save(c, tmp_path)
    d = pymupdf.open(out)
    infos = d[0].get_image_info()
    assert len(infos) > 4  # tiled
    # one image object shared by all tiles and pages
    assert len({x for x in range(1, d.xref_length()) if d.xref_get_key(x, "Subtype")[1] == "/Image"}) == 1


def test_upright_on_rotated_pages(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    pageops.rotate(c, [2], 90)  # page 2 rotated in Leaf; page 1 has its own /Rotate 90
    D.add(c, D.header_footer(slots=("TOP {page}", "", "", "", "", "BOTTOM {page}")))
    D.add(c, D.watermark(text="WM", diagonal=False, angle=0, fit=False, size=40))
    out, _ = _save(c, tmp_path)
    d = pymupdf.open(out)
    assert [d[i].rotation for i in range(3)] == [0, 90, 90]
    for i in range(3):
        p = d[i]
        spans = dict(_spans(p))
        for t in (f"TOP {i + 1}", f"BOTTOM {i + 1}", "WM"):
            assert spans[t] == (1.0, 0.0), (i, t, spans[t])
        top = p.search_for(f"TOP {i + 1}")[0] * p.rotation_matrix
        bottom = p.search_for(f"BOTTOM {i + 1}")[0] * p.rotation_matrix
        assert top.y1 < 60 and top.x0 < 60 and bottom.y0 > p.rect.height - 60 and bottom.x1 > p.rect.width - 60


def test_unicode_text(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.header_footer(slots=("", "Страница {page}", "", "", "", "")))
    out, _ = _save(c, tmp_path)
    d = pymupdf.open(out)
    assert "Страница 2" in d[1].get_text()
    assert dict(_spans(d[1]))["Страница 2"] == (1.0, 0.0)


def test_undo_removes(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.header_footer(slots=("", "", "", "", "Page {page}", "")))
    assert len(c.doc.decorations) == 1
    c.undo.undo()
    assert c.doc.decorations == []
    out, _ = _save(c, tmp_path)
    assert "Page 1" not in pymupdf.open(out)[0].get_text()
    c.undo.redo()
    spec = c.doc.decorations[0]
    D.update(c, D.replace(spec, slots=("", "", "", "", "Seite {page}", "")))
    assert c.doc.decorations[0].uid == spec.uid and c.doc.decorations[0].slots[4] == "Seite {page}"
    c.undo.undo()
    assert c.doc.decorations[0].slots[4] == "Page {page}"
    D.remove(c, spec.uid)
    assert c.doc.decorations == []
    c.undo.undo()
    assert len(c.doc.decorations) == 1


def test_incremental_save_keeps_original_bytes(app, pdf, tmp_path):  # noqa: F811
    with open(pdf, "rb") as f:
        orig = f.read()
    c = canvas_for(pdf)
    D.add(c, D.header_footer(slots=("", "", "", "", "Page {page} of {pages}", "")))
    out, inc = _save(c, tmp_path)
    assert inc
    with open(out, "rb") as f:
        data = f.read()
    assert data.startswith(orig) and len(data) > len(orig)
    d = pymupdf.open(out)
    assert all(d[i].search_for(f"Page {i + 1} of 3") for i in range(3))


def test_filename_and_title_tokens(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    D.add(c, D.header_footer(slots=("{filename}", "", "", "", "", "")))
    out, _ = _save(c, tmp_path, "named.pdf")
    assert "named.pdf" in pymupdf.open(out)[0].get_text()


# ---------------------------------------------------------------- preview
def test_canvas_preview_items(app, pdf):  # noqa: F811
    c = canvas_for(pdf)
    c.show()
    pump(app, 0.2)
    rect = add(c, 0, ShapeItem("rect", QRectF(100, 300, 120, 60)))
    pv = D.preview_for(c)
    assert not any(pg.__dict__.get("_decor") for pg in c.order)  # nothing until there is a decoration
    D.add(c, D.watermark(text="PREVIEW"))
    pg = c.order[0]
    layers = pg.__dict__.get("_decor")
    assert layers and all(not is_overlay(x) for x in layers)
    assert list(c.overlays_for(pg.ref)) == [rect]
    assert all(x.shape().isEmpty() for x in layers)
    # clicks still reach the overlay under the watermark
    view_pt = c.mapFromScene(rect.mapToScene(rect.boundingRect().center()))
    hit = c.itemAt(view_pt)
    assert hit is rect or (hit is not None and hit.parentItem() is rect)
    c.viewport().grab()  # paints the layers
    # page list changes refresh the preview context
    uids = [r.uid for r in c.doc.pages]
    pageops.reorder(c, [uids[1], uids[0], uids[2]])
    assert pv.context().n == 3
    c.undo.undo()
    c.undo.undo()  # back past the watermark: layers paint nothing
    assert D.decorations_of(c.doc) == []
    c.viewport().grab()
    c.close()


# ----------------------------------------------------------------- dialogs
@pytest.fixture
def win(app):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafDecorateTest")
    QSettings().clear()
    from leaf.window import MainWindow

    w = MainWindow()
    w.resize(1100, 800)
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


def test_menu_and_dialogs(app, win, pdf, tmp_path):  # noqa: F811
    from leaf.features import decorate as F

    titles = [a.text() for a in win.menuBar().actions()]
    assert "&Document" in titles and titles.index("&Document") > titles.index("&Pages")
    assert not win.a_watermark.isEnabled()
    win.open_path(pdf)
    assert win.a_watermark.isEnabled() and win.a_header_footer.isEnabled() and win.a_bates.isEnabled()
    cv = win.canvas

    dlg = F.HeaderFooterDialog(win, D.header_footer(slots=("", "", "", "", "Page {page} of {pages}", "")))
    dlg._refresh()
    assert len(cv.doc.decorations) == 1  # live draft on the canvas, no undo step yet
    assert cv.undo.count() == 0
    assert not dlg.thumb.pixmap().isNull()
    dlg.slot_edits[2].setText("{date}")
    dlg.mode.setCurrentIndex(dlg.mode.findData("range"))
    dlg.range.setText("99")
    assert not dlg.bb.button(dlg.bb.StandardButton.Ok).isEnabled()
    dlg.range.setText("2-3")
    assert dlg.bb.button(dlg.bb.StandardButton.Ok).isEnabled()
    spec = dlg.spec()
    assert spec.slots[2] == "{date}" and spec.pages == "range" and spec.page_range == "2-3"
    dlg.reject()
    cv.doc.decorations = list(dlg.saved)
    assert cv.doc.decorations == []

    D.add(cv, spec)
    assert win.a_remove_decorations.isEnabled()
    wd = F.WatermarkDialog(win, D.watermark())
    wd.angle_mode.setCurrentIndex(3)
    wd.angle.setValue(30)
    wd.layer.setCurrentIndex(1)
    s = wd.spec()
    assert not s.diagonal and s.angle == 30 and s.behind and s.text == "CONFIDENTIAL"
    wd._refresh()
    assert len(cv.doc.decorations) == 2
    wd.reject()
    cv.doc.decorations = list(wd.saved)

    bd = F.BatesDialog(win, D.bates(prefix="X", digits=3))
    assert bd.sample.text() == "X001 … X003"
    bd.reject()

    md = F.ManageDialog(win)
    assert md.list.count() == 1 and md.b_edit.isEnabled()
    md._remove()
    assert cv.doc.decorations == [] and md.list.count() == 1 and not md.b_edit.isEnabled()
    cv.undo.undo()
    assert md.list.count() == 1 and md.b_edit.isEnabled()
    md.done(0)


def test_offset_boxes_on_rotated_pages():
    for rot in (0, 90, 180, 270):
        d = pymupdf.open()
        p = d.new_page(width=600, height=800)
        d.xref_set_key(p.xref, "MediaBox", "[100 100 700 900]")
        d.xref_set_key(p.xref, "CropBox", "[150 160 650 860]")
        d[0].set_rotation(rot)
        assert D.apply_all(d, [D.header_footer(slots=("TL", "", "", "", "", "BR"))]) == 1
        p = d[0]
        assert p.rotation == rot
        tl = p.search_for("TL")[0] * p.rotation_matrix
        br = p.search_for("BR")[0] * p.rotation_matrix
        assert tl.x0 == pytest.approx(36, abs=1) and tl.y0 < 40, rot
        assert br.x1 == pytest.approx(p.rect.width - 36, abs=1) and br.y1 > p.rect.height - 30, rot
