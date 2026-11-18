"""Images already in the PDF (leaf/images.py, features/images.py) and File › Export (leaf/export.py)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import types

import pymupdf
import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from leaf import export as X
from leaf import images as I
from leaf.features import images as F
from leaf.items import TextItem
from tests.test_editor import add, app, canvas_for, pump  # noqa: F401

RED = (220, 30, 30)
BOX = pymupdf.Rect(100, 100, 200, 150)


def _png(w=20, h=10, color=RED) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def _jpeg() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 16), False)
    pix.set_rect(pix.irect, (20, 120, 220))
    return pix.tobytes("jpeg")


def make_img_pdf(path):
    """p1: text, the red image (xref X), text drawn over it; p2: X again; p3: /Rotate 90 with X; p4: a CropBox
    with X; p5: a JPEG, X inside a Form XObject, an inline image."""
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 60), "Hello images page one", fontsize=12)
    x = p.insert_image(BOX, stream=_png())
    p.insert_text((110, 130), "over", fontsize=11)
    d.new_page(width=595, height=842).insert_image(BOX, xref=x)
    p = d.new_page(width=595, height=842)
    p.insert_image(BOX, xref=x)
    p.set_rotation(90)
    p = d.new_page(width=595, height=842)
    p.insert_image(BOX, xref=x)
    p.set_cropbox(pymupdf.Rect(50, 60, 400, 700))
    p = d.new_page(width=595, height=842)
    p.insert_image(pymupdf.Rect(300, 500, 364, 532), stream=_jpeg())
    src = pymupdf.open()
    src.new_page(width=595, height=842).insert_image(pymupdf.Rect(10, 10, 110, 60), stream=_png(20, 10, (30, 160, 40)))
    p.show_pdf_page(pymupdf.Rect(0, 0, 595, 842), src, 0)
    c = p.get_contents()[-1]
    d.update_stream(c, d.xref_stream(c) + b"\nq 40 0 0 40 100 100 cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI Q\n")
    d.save(path)
    d.close()


@pytest.fixture
def ipdf(tmp_path):
    path = str(tmp_path / "img.pdf")
    make_img_pdf(path)
    return path


def tool_for(c):
    toasts = []
    win = types.SimpleNamespace(canvas=c, toast=lambda text, *a, **k: toasts.append(text), settings=None)
    t = F.ImageTool(win)
    t.actions = []
    t.toasts = toasts
    return t


def vp_point(c, page_idx, x, y) -> QPointF:
    pg = c.order[page_idx]
    return QPointF(c.mapFromScene(pg.content.mapToScene(QPointF(x, y))))


def mouse(c, kind, pos: QPointF, mods=Qt.KeyboardModifier.NoModifier, held=True):
    btn = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.LeftButton if held and kind != QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    ev = QMouseEvent(kind, pos, c.viewport().mapToGlobal(pos), btn, buttons, mods)
    QApplication.sendEvent(c.viewport(), ev)


def drag(c, page_idx, a, b, mods=Qt.KeyboardModifier.NoModifier):
    pa, pb = vp_point(c, page_idx, *a), vp_point(c, page_idx, *b)
    mouse(c, QEvent.Type.MouseButtonPress, pa, mods)
    mid = (pa + pb) / 2
    mouse(c, QEvent.Type.MouseMove, mid, mods)
    mouse(c, QEvent.Type.MouseMove, pb, mods)
    mouse(c, QEvent.Type.MouseButtonRelease, pb, mods)


def saved(c, tmp_path, name="out.pdf"):
    out = str(tmp_path / name)
    c.doc.save(out, c.overlays_for)
    return pymupdf.open(out)


def shown_boxes(page) -> list[pymupdf.Rect]:
    return [pymupdf.Rect(i["bbox"]) * page.rotation_matrix for i in page.get_image_info()]


def near(a, b, tol=0.6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(tuple(a), tuple(b)))


# ------------------------------------------------------------------------------------------------ engine
def test_scan_finds_draws_with_geometry(app, ipdf):  # noqa: F811
    doc = canvas_for(ipdf).doc
    s = doc.sources[0]
    d0 = I.draws(s, 0)
    assert len(d0) == 1 and near(d0[0].bbox, BOX) and d0[0].xref and not d0[0].in_form
    assert I.draws(s, 1)[0].xref == d0[0].xref  # the same image object used again
    rot = I.draws(s, 2)[0]
    assert near(rot.bbox, pymupdf.Rect(shown_boxes(s.page(2))[0]))  # page.rect space of the rotated page
    crop = I.draws(s, 3)[0]
    assert near(crop.bbox, (50, 40, 150, 90))  # relative to the CropBox
    p5 = I.draws(s, 4)
    assert len(p5) == 3 and sum(d.in_form for d in p5) == 1 and sum(d.inline for d in p5) == 1
    with pytest.raises(I.Refuse):
        I.check(s, 4, [d for d in p5 if d.inline][0])
    I.check(s, 4, [d for d in p5 if d.in_form][0])  # an image inside a Form XObject can be edited


def test_move_resize_save_and_other_uses_untouched(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    pump(app, 0.2)
    drag(c, 0, (150, 140), (350, 340))  # below "over": no word under the press
    assert t.selected() and c.doc.image_edits
    e = t.edit()
    assert near(e.rect, (300, 300, 400, 350), 1.0)
    t.apply(t._new_edit(QRectF(300, 300, 200, 100)), "Resize image")  # bigger
    out = saved(c, tmp_path)
    assert near(shown_boxes(out[0])[0], (300, 300, 500, 400))
    assert near(shown_boxes(out[1])[0], BOX)  # the other use of the same xref
    assert "over" in out[0].get_text()  # the rest of the page is still there
    # still painted under the text drawn after it: the draw kept its place in the content
    st = b"".join(out.xref_stream(x) for x in out[0].get_contents())
    assert st.index(b"Do") < st.rindex(b"TJ")
    c.undo.undo()
    c.undo.undo()
    assert not c.doc.image_edits
    out2 = saved(c, tmp_path, "undone.pdf")
    assert near(shown_boxes(out2[0])[0], BOX)


def test_rotated_and_cropped_pages(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    s = c.doc.sources[0]
    for idx in (2, 3):
        d = I.draws(s, idx)[0]
        b = pymupdf.Rect(d.bbox)
        pg = c.order[idx]
        hit = t.image_at(pg, QPointF(b.x0 + b.width / 2, b.y0 + b.height / 2))
        assert hit is not None and hit.key == d.key
        t.select(pg, hit)
        t.apply(t._new_edit(QRectF(b.x0 + 30, b.y0 + 40, b.width * 1.5, b.height * 1.5)), "Move image")
    out = saved(c, tmp_path)
    for idx in (2, 3):
        b = pymupdf.Rect(I.draws(s, idx)[0].bbox)
        assert near(shown_boxes(out[idx])[0], (b.x0 + 30, b.y0 + 40, b.x0 + 30 + b.width * 1.5, b.y0 + 40 + b.height * 1.5))
    assert near(shown_boxes(out[1])[0], BOX)


def test_rotate_quarter_turn_and_keys(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    d = I.draws(c.doc.sources[0], 0)[0]
    t.select(c.order[0], d)
    t.rotate(1)
    e = t.edit()
    assert e.turns == 1 and near(e.rect, (125, 75, 175, 175))
    out = saved(c, tmp_path)
    info = out[0].get_image_info()[0]
    assert near(info["bbox"], (125, 75, 175, 175))
    a, b, cc, dd = info["transform"][:4]
    assert abs(a) < 1e-3 and abs(dd) < 1e-3  # a quarter turn
    # arrow keys nudge, Delete deletes, Esc deselects
    from PySide6.QtGui import QKeyEvent
    c.setFocus()
    QApplication.sendEvent(c, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier))
    assert near(t.edit().rect, (135, 75, 185, 175))
    QApplication.sendEvent(c, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert t.edit() is None and not t.selected()
    assert c.doc.image_edits[c.doc.pages[0].uid][d.key].delete


def test_delete_removes_only_that_draw(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    t.select(c.order[0], I.draws(c.doc.sources[0], 0)[0])
    t.delete()
    out = saved(c, tmp_path)
    assert out[0].get_image_info() == [] and "over" in out[0].get_text()
    assert len(out[1].get_image_info()) == 1 and len(out[2].get_image_info()) == 1
    # display copy: the page renders without it
    pix, _ = c.doc.render(c.doc.pages[0], 1.0)
    assert pix.pixel(150, 105) == (255, 255, 255)
    c.undo.undo()
    pix, _ = c.doc.render(c.doc.pages[0], 1.0)
    assert pix.pixel(150, 105)[0] > 150 and pix.pixel(150, 105)[1] < 100


def test_replace_keeps_placement(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    t.select(c.order[0], I.draws(c.doc.sources[0], 0)[0])
    blue = _png(40, 20, (20, 40, 230))
    assert t.replace_with(blue)  # same aspect: exactly the old box
    out = saved(c, tmp_path)
    assert near(shown_boxes(out[0])[0], BOX)
    pix = out[0].get_pixmap()
    assert pix.pixel(150, 105)[2] > 150 and pix.pixel(150, 105)[0] < 100  # blue now
    assert len(out[1].get_image_info()) == 1 and out[1].get_pixmap().pixel(150, 105)[0] > 150  # still red there
    # a square replacement is fitted into the box, centered
    assert t.replace_with(_png(10, 10, (20, 40, 230)))
    out = saved(c, tmp_path, "sq.pdf")
    assert near(shown_boxes(out[0])[0], (125, 100, 175, 150))
    data, ext = I.extract(c.doc.sources[0], t.draw, t.edit())
    assert ext == "png" and data[:4] == b"\x89PNG"


def test_form_xobject_image(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    d = [x for x in I.draws(c.doc.sources[0], 4) if x.in_form][0]
    assert near(d.bbox, (10, 10, 110, 60))
    t.select(c.order[4], d)
    t.apply(t._new_edit(QRectF(200, 600, 100, 50)), "Move image")
    out = saved(c, tmp_path)
    boxes = sorted(tuple(round(v) for v in b) for b in shown_boxes(out[4]))
    assert (200, 600, 300, 650) in boxes and (10, 10, 110, 60) not in boxes
    assert (300, 500, 364, 532) in boxes  # the JPEG next to it


def test_extract_original_bytes_and_copy(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    s = c.doc.sources[0]
    jpg = I.draws(s, 4)[0]
    data, ext = I.extract(s, jpg)
    assert ext in ("jpeg", "jpg") and data == s.doc.xref_stream_raw(jpg.xref)  # the stored JPEG, byte for byte
    png = I.save_bytes(s, I.draws(s, 0)[0], None, "png")
    assert pymupdf.Pixmap(png).width == 20
    t = tool_for(c)
    t.select(c.order[0], I.draws(s, 0)[0])
    assert t.copy()
    img = QApplication.clipboard().image()
    assert img.width() == 20 and img.height() == 10
    QApplication.clipboard().clear()


def test_click_on_text_still_selects_text(app, ipdf):  # noqa: F811
    c = canvas_for(ipdf)
    t = tool_for(c)
    pump(app, 0.2)
    w = [x for x in c.doc.sources[0].words(0) if x[4] == "over"][0]
    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
    assert t.image_at(c.order[0], QPointF(cx, cy)) is None  # text wins
    assert t.image_at(c.order[0], QPointF(cx, cy), alt=True) is not None  # Alt+click picks the image
    assert t.image_at(c.order[0], QPointF(150, 145)) is not None


def test_text_and_image_edits_together(app, ipdf, tmp_path):  # noqa: F811
    from leaf import commands as C
    from leaf import textedit

    c = canvas_for(ipdf)
    ref = c.doc.pages[0]
    w = [x for x in c.doc.sources[0].words(0) if x[4] == "Hello"][0]
    unit = textedit.unit_at(c.doc, ref, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    if unit is None:
        pytest.skip("text engine can't edit this line")
    c.undo.push(C.EditPageText(c, ref, unit, "Goodbye images page one"))
    t = tool_for(c)
    t.select(c.order[0], I.draws(c.doc.sources[0], 0)[0])
    t.apply(t._new_edit(QRectF(300, 300, 100, 50)), "Move image")
    words = " ".join(x[4] for x in c.doc.sources[0].words(0, ref.uid))
    assert "Goodbye" in words
    out = saved(c, tmp_path)
    assert "Goodbye" in out[0].get_text() and near(shown_boxes(out[0])[0], (300, 300, 400, 350))
    c.undo.undo()  # the image edit; the text edit stays
    assert "Goodbye" in " ".join(x[4] for x in c.doc.sources[0].words(0, ref.uid))


# ------------------------------------------------------------------------------------------------ export
def test_export_pages_images_text(app, ipdf, tmp_path):  # noqa: F811
    c = canvas_for(ipdf)
    item = TextItem(QPointF(72, 300))
    item.setPlainText("OVERLAYWORD")
    add(c, 0, item)
    t = tool_for(c)
    t.select(c.order[0], I.draws(c.doc.sources[0], 0)[0])
    t.apply(t._new_edit(QRectF(300, 600, 100, 50)), "Move image")
    with X.snapshot(c.doc, c.overlays_for) as snap:
        files = [p for _, _, p in X.export_pages(snap.doc, [0, 2], str(tmp_path / "pages"), "doc", "png", dpi=144)]
        assert len(files) == 2 and all(os.path.exists(f) for f in files)
        assert files[0].endswith("doc-p001.png") and files[1].endswith("doc-p003.png")
        a, b = pymupdf.Pixmap(files[0]), pymupdf.Pixmap(files[1])
        assert (a.width, a.height) == (1190, 1684) and (b.width, b.height) == (1684, 1190)  # 144 dpi, rotated page
        assert a.pixel(700, 1250)[0] > 150 and a.pixel(700, 1250)[1] < 100  # the moved image is in the export
        jp = list(X.export_pages(snap.doc, [1], str(tmp_path / "pages"), "doc", "jpg", dpi=36))
        assert pymupdf.Pixmap(jp[0][2]).width == 298
        imgs = [p for _, _, p in X.export_images(snap.doc, str(tmp_path / "imgs"), "doc") if p]
        names = sorted(os.path.basename(p) for p in imgs)
        assert len(imgs) == 3, names  # red (used 4x, once), JPEG, green (in the form); inline images aren't objects
        assert any(n.endswith((".jpeg", ".jpg")) for n in names)
        jpg = [p for p in imgs if p.endswith((".jpeg", ".jpg"))][0]
        with open(jpg, "rb") as f:
            assert f.read() == _jpeg_stream(ipdf)
        txt = str(tmp_path / "t.txt")
        list(X.export_text(snap.doc, list(range(5)), txt))
        text = open(txt, encoding="utf-8").read()
        assert "OVERLAYWORD" in text and "Hello images page one" in text
        assert text.index("Hello") < text.index("OVERLAYWORD")


def _jpeg_stream(path) -> bytes:
    d = pymupdf.open(path)
    try:
        for e in d[4].get_images(full=True):
            if e[8] == "DCTDecode":
                return d.xref_stream_raw(e[0])
    finally:
        d.close()
    return b""


def test_export_markdown_headings(app, tmp_path):  # noqa: F811
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), "Big Title", fontsize=24)
    p.insert_text((72, 120), "Section", fontsize=16)
    y = 150
    for i in range(6):
        p.insert_text((72, y), f"Body text line number {i} with enough words.", fontsize=11)
        y += 30
    p.insert_text((72, y + 20), "- first point", fontsize=11)
    path = str(tmp_path / "md.pdf")
    d.save(path)
    d.close()
    c = canvas_for(path)
    md = str(tmp_path / "out.md")
    with X.snapshot(c.doc, c.overlays_for) as snap:
        list(X.export_text(snap.doc, [0], md, markdown=True))
    out = open(md, encoding="utf-8").read()
    assert "# Big Title" in out and "## Section" in out and "Body text line number 0" in out
    assert "- first point" in out


def test_window_wiring(app, ipdf, tmp_path):  # noqa: F811
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafImagesTest")
    QSettings().clear()
    from leaf.window import MainWindow

    w = MainWindow()
    try:
        w.open_path(ipdf)
        for name in ("image_replace", "image_rotate_left", "image_rotate_right", "image_save", "image_copy",
                     "image_delete", "image_restore", "export_pages", "export_images", "export_text",
                     "export_markdown"):
            a = getattr(w, f"a_{name}")
            assert a.objectName() == f"a_{name}"
        assert "export" in w.menus and "image" in w.menus
        assert not w.a_image_delete.isEnabled()
        t = w.image_tool
        t.select(w.canvas.order[0], I.draws(w.canvas.doc.sources[0], 0)[0])
        assert w.a_image_delete.isEnabled()
        w.a_delete.trigger()  # the window's Delete acts on the selected image
        assert not t.selected() and w.canvas.doc.image_edits
        # export without dialogs
        assert F.export(w, "text", rows=[0], target=str(tmp_path / "w.txt"))
        assert "Hello images" in open(tmp_path / "w.txt", encoding="utf-8").read()
        assert F.export(w, "images", rows=[0, 1], target=str(tmp_path / "wi"))
        assert len(os.listdir(tmp_path / "wi")) == 1  # deleted on page 1, still used on page 2
    finally:
        w.canvas.undo.setClean()
        w.close()
        QSettings().clear()


def test_render_worker_mirrors_image_edits(app, ipdf):  # noqa: F811
    """The out-of-process renderer shows the edited page from the slot payload (no image engine in the worker)."""
    from leaf.render_worker import _install_slot

    c = canvas_for(ipdf)
    t = tool_for(c)
    ref = c.doc.pages[0]
    t.select(c.order[0], I.draws(ref.src, 0)[0])
    t.apply(t._new_edit(QRectF(300, 300, 100, 50)), "Move image")
    pg2 = c.order[4]
    t.select(pg2, [d for d in I.draws(ref.src, 4) if d.in_form][0])
    assert t.replace_with(_png(30, 15, (20, 40, 230)))
    for r in (ref, c.doc.pages[4]):
        payload = ref.src.upage_payload(r.uid)
        assert payload is not None
        d = pymupdf.open(stream=ref.src.data, filetype="pdf")
        slots, pages = {}, {}
        _install_slot(pymupdf, d, slots, pages, ref.src.id, r.uid, payload)
        mine = d[slots[(ref.src.id, r.uid)][0]].get_pixmap(alpha=False)
        gui, _ = c.doc.render(r, 1.0)
        assert mine.samples == gui.samples
        d.close()
