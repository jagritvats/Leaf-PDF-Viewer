"""Reduce file size (leaf/optimize.py) and page cropping (leaf/crop.py)."""
from __future__ import annotations

import os
import random
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QRectF, QSettings

from leaf import crop as K
from leaf import optimize as O
from leaf import pageops
from leaf.items import ShapeItem
from tests.test_editor import app, make_pdf, canvas_for, pump, add  # noqa: F401


def photo_png(w=640, h=420, seed=1) -> bytes:
    """A noisy, photo-like RGB image (compresses badly as PNG, well as JPEG)."""
    rnd = random.Random(seed)
    buf = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 3
            buf[i] = (x * 255 // w + rnd.randint(0, 40)) & 255
            buf[i + 1] = (y * 255 // h + rnd.randint(0, 40)) & 255
            buf[i + 2] = rnd.randint(0, 255)
    return pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(buf), False).tobytes("png")


@pytest.fixture(scope="module")
def photo():
    return photo_png()


def photo_pdf(path, png, **save):
    d = pymupdf.open()
    for i in range(2):
        p = d.new_page()
        p.insert_image(pymupdf.Rect(72, 72, 272, 203), stream=png)
        p.insert_text((72, 300), f"Caption {i + 1} stays searchable", fontsize=12)
    d.save(path, **save)
    d.close()
    return path


def words_of(path, pno=0, password=None):
    d = pymupdf.open(path)
    if password:
        d.authenticate(password)
    out = {}
    p = d[pno]
    for t in p.get_text("words"):
        out.setdefault(t[4], pymupdf.Rect(t[:4]) * p.rotation_matrix)
    d.close()
    return out


# =============================================================== optimize
def test_preset_shrinks_photo_and_keeps_text(tmp_path, photo):
    src = photo_pdf(str(tmp_path / "photo.pdf"), photo, deflate=True)
    before = os.path.getsize(src)
    assert before > 300_000
    out = str(tmp_path / "small.pdf")
    seen = []
    size = O.optimize_file(src, out, O.preset("ebook"), progress=lambda f, t: seen.append(f))
    assert size == os.path.getsize(out) and size < before / 3
    assert seen and seen[-1] == 1.0 and seen == sorted(seen)
    d = pymupdf.open(out)
    assert d.page_count == 2
    assert "Caption 2 stays searchable" in d[1].get_text()
    assert d[0].get_images()  # the image is still there, only smaller
    d.close()
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".part")]


def test_lossless_never_grows_and_keeps_text(tmp_path, photo):
    src = photo_pdf(str(tmp_path / "plain.pdf"), photo)  # uncompressed: lossless shrinks it
    out = str(tmp_path / "lossless.pdf")
    size = O.optimize_file(src, out, O.preset("lossless"))
    assert size < os.path.getsize(src)
    assert "Caption 1" in pymupdf.open(out)[0].get_text()
    # an already compact file: the result is never bigger
    again = str(tmp_path / "again.pdf")
    size2 = O.optimize_file(out, again, O.preset("lossless"))
    assert size2 <= size
    assert "Caption 1" in pymupdf.open(again)[0].get_text()
    # images are untouched by the lossless preset
    a, b = pymupdf.open(src), pymupdf.open(out)
    assert a.extract_image(a[0].get_images()[0][0])["width"] == b.extract_image(b[0].get_images()[0][0])["width"]


def test_encrypted_stays_encrypted(tmp_path, photo):
    src = photo_pdf(str(tmp_path / "enc.pdf"), photo, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u",
                    owner_pw="o", permissions=pymupdf.PDF_PERM_PRINT)
    out = str(tmp_path / "enc-small.pdf")
    O.optimize_file(src, out, O.preset("screen"), password="u")
    assert os.path.getsize(out) < os.path.getsize(src)
    d = pymupdf.open(out)
    assert d.needs_pass
    assert d.authenticate("o") & 4  # owner
    assert "AES" in d.metadata["encryption"]
    d.close()
    d = pymupdf.open(out)
    assert d.authenticate("u") and "Caption 1" in d[0].get_text()
    assert not d.permissions & pymupdf.PDF_PERM_COPY
    d.close()


def test_breakdown_counts_images(tmp_path, photo):
    src = photo_pdf(str(tmp_path / "photo.pdf"), photo, deflate=True)
    d = pymupdf.open(src)
    total = os.path.getsize(src)
    parts = O.breakdown(d, total)
    assert sum(parts.values()) == total
    assert parts["images"] > 0.8 * total and parts["content"] > 0
    assert O.human(1536) == "1.5 KB"
    assert not O.has_signatures(d)


def test_gray_and_strip_options(tmp_path, photo):
    src = photo_pdf(str(tmp_path / "photo.pdf"), photo, deflate=True)
    d = pymupdf.open(src)
    d.set_metadata({"title": "Secret title", "author": "Someone"})
    d.embfile_add("note.txt", b"attached")
    d.saveIncr()
    d.close()
    out = str(tmp_path / "gray.pdf")
    O.optimize_file(src, out, O.Options(images=True, dpi=96, quality=60, gray=True, strip_metadata=True,
                                        strip_embedded=True))
    d = pymupdf.open(out)
    assert not d.metadata.get("title") and not d.metadata.get("author")
    assert d.embfile_count() == 0
    pix = d[0].get_pixmap(clip=pymupdf.Rect(100, 100, 200, 180))
    s = pix.samples
    assert all(s[i] == s[i + 1] == s[i + 2] for i in range(0, len(s), 3 * 37))  # gray pixels
    d.close()


@pytest.fixture
def win(app, tmp_path):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafOptimizeTest")
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


def test_dialog_runs_worker_and_writes_copy(app, win, tmp_path, photo, monkeypatch):  # noqa: F811
    from leaf.features import optimize as F

    src = photo_pdf(str(tmp_path / "photo.pdf"), photo, deflate=True)
    win.open_path(src)
    pump(app, 0.2)
    add(win.canvas, 0, ShapeItem("rect", QRectF(300, 400, 80, 40), {"color": "#ff0000"}))  # unsaved edit
    for name in ("a_reduce_size", "a_crop_tool", "a_crop_pages", "a_auto_crop", "a_remove_crop"):
        assert getattr(win, name).objectName() == name
    assert win.a_reduce_size in win.menus["document"].actions()
    dlg = F.OptimizeDialog(win)
    dlg.radios["screen"].setChecked(True)
    target = str(tmp_path / "out" / "photo-small.pdf")
    os.makedirs(os.path.dirname(target))
    # the open file itself is refused
    monkeypatch.setattr(F.QFileDialog, "getSaveFileName", lambda *a, **k: (src, ""))
    monkeypatch.setattr(F.QMessageBox, "information", lambda *a, **k: None)
    dlg._start()
    assert dlg.stack.currentIndex() == 0 and not dlg.job.running
    monkeypatch.setattr(F.QFileDialog, "getSaveFileName", lambda *a, **k: (target, ""))
    dlg._start()
    assert dlg.stack.currentIndex() == 1
    end = time.time() + 60
    while dlg.stack.currentIndex() == 1 and time.time() < end:
        pump(app, 0.05)
    assert dlg.stack.currentIndex() == 2, dlg.status.text()
    assert os.path.getsize(target) < os.path.getsize(src) / 3
    d = pymupdf.open(target)
    assert d.page_count == 2 and d[0].first_annot is not None  # the unsaved rectangle made it in
    d.close()
    assert sorted(os.listdir(os.path.dirname(target))) == ["photo-small.pdf"]  # no temp files left
    assert "smaller" in dlg.result.text()
    dlg.accept()


def test_cancel_cleans_up(app, win, tmp_path, photo, monkeypatch):  # noqa: F811
    from leaf.features import optimize as F

    src = photo_pdf(str(tmp_path / "photo.pdf"), photo, deflate=True)
    win.open_path(src)
    dlg = F.OptimizeDialog(win)
    target = str(tmp_path / "out" / "x.pdf")
    os.makedirs(os.path.dirname(target))
    monkeypatch.setattr(F.QFileDialog, "getSaveFileName", lambda *a, **k: (target, ""))
    dlg._start()
    dlg._cancel()
    assert dlg.stack.currentIndex() == 0 and not dlg.job.running
    assert os.listdir(os.path.dirname(target)) == []
    dlg.reject()


# =================================================================== crop
def crop_pdf(path):
    """Page 0 plain, page 1 /Rotate 90, page 2 with an existing non-zero CropBox, page 3 MediaBox not at 0,0."""
    d = pymupdf.open()
    for i in range(4):
        p = d.new_page(width=595, height=842)
        p.insert_text((150, 300), f"Target{i}", fontsize=24)
        p.insert_text((72, 700), "Footer text", fontsize=10)
    d[1].set_rotation(90)
    d[2].set_cropbox(pymupdf.Rect(40, 30, 560, 800))
    d.xref_set_key(d[3].xref, "MediaBox", "[-20 -40 575 802]")
    d.save(path)
    d.close()
    return path


def test_cropbox_saved_matches(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    before = [words_of(src, i)[f"Target{i}"] for i in range(4)]
    crops = {}
    for i, ref in enumerate(doc.pages):
        t = before[i]
        crops[ref] = K.as_crop((t.x0 - 30, t.y0 - 20, t.x1 + 50, t.y1 + 60))
    assert K.set_crops(c, crops) == 4
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    for i in range(4):
        cr = pymupdf.Rect(crops[doc.pages[i]])
        p = d[i]
        assert abs(p.rect.width - cr.width) < 0.05 and abs(p.rect.height - cr.height) < 0.05
        w = words_of(out, i)
        assert list(w) == [f"Target{i}"]  # the footer is cropped away
        moved = before[i] - (cr.x0, cr.y0, cr.x0, cr.y0)
        assert all(abs(a - b) < 0.05 for a, b in zip(w[f"Target{i}"], moved))
    # explicit boxes: plain page = the crop itself; existing CropBox = shifted by its offset
    assert all(abs(a - b) < 0.05 for a, b in zip(d[0].cropbox, crops[doc.pages[0]]))
    cr2 = pymupdf.Rect(crops[doc.pages[2]])
    assert all(abs(a - b) < 0.05 for a, b in zip(d[2].cropbox, cr2 + (40, 30, 40, 30)))
    cr1 = pymupdf.Rect(crops[doc.pages[1]]) * pymupdf.open(src)[1].derotation_matrix
    assert all(abs(a - b) < 0.05 for a, b in zip(d[1].cropbox, cr1))
    assert d[1].rotation == 90
    d.close()


def test_overlay_stays_on_content(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    for i in (0, 1, 2):
        t = words_of(src, i)[f"Target{i}"]
        add(c, i, ShapeItem("rect", QRectF(t.x0 - 4, t.y0 - 4, t.width + 8, t.height + 8), {"color": "#ff0000"}))
        doc.pages[i].crop = K.as_crop((t.x0 - 60, t.y0 - 50, t.x1 + 80, t.y1 + 90))
    c.sync_pages()
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    for i in (0, 1, 2):
        p = d[i]
        w = words_of(out, i)[f"Target{i}"]
        a = p.first_annot.rect * p.rotation_matrix
        # same center (the annotation rect also includes its border width)
        assert abs((a.x0 + a.x1) - (w.x0 + w.x1)) < 0.2 and abs((a.y0 + a.y1) - (w.y0 + w.y1)) < 0.2, (i, a, w)
        assert abs(a.width - (w.width + 8)) < 3
    d.close()


def test_display_size_render_and_undo(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    ref = doc.pages[1]  # /Rotate 90: page.rect is 842 x 595
    pg = c.page_item(ref)
    full = (pg.w, pg.h)
    crop = (100.0, 50.0, 400.0, 250.0)
    K.set_crops(c, {ref: crop})
    assert (pg.w, pg.h) == (300.0, 200.0) == doc.display_size(ref)
    # the content transform puts the crop's corner at the page item's origin
    assert pg.content.mapToParent(pg.content.rect().topLeft()) == pg.page_rect().topLeft()
    assert pg.content.mapToParent(100, 50).x() == 0 and pg.content.rect() == QRectF(100, 50, 300, 200)
    pix, (x, y, w, h) = doc.render(ref, 2.0)
    assert (pix.width, pix.height) == (600, 400) and (round(x), round(y)) == (0, 0)
    # a clip (display space) inside the crop
    pix, (x, y, w, h) = doc.render(ref, 1.0, clip=(10, 10, 60, 40))
    assert abs(x - 10) <= 1 and abs(w - 50) <= 1
    # Leaf rotation on top of the crop
    ref.rotation = 90
    c.sync_pages()
    assert (pg.w, pg.h) == (200.0, 300.0)
    ref.rotation = 0
    c.sync_pages()
    c.undo.undo()
    assert ref.crop is None and (pg.w, pg.h) == full
    c.undo.redo()
    assert ref.crop == crop


def test_sidebar_thumbnail_follows_crop(app, win, tmp_path):  # noqa: F811
    from leaf.sidebar import ASPECT_ROLE

    src = crop_pdf(str(tmp_path / "c.pdf"))
    win.open_path(src)
    pump(app, 0.3)
    cv = win.canvas
    ref = cv.doc.pages[0]
    K.set_crops(cv, {ref: (0.0, 0.0, 400.0, 200.0)})
    item = win.sidebar.item(0)
    assert abs(item.data(ASPECT_ROLE) - 0.5) < 1e-6
    end = time.time() + 10
    img = None
    while time.time() < end:
        img = win.sidebar.thumb(ref.uid)
        if img is not None:
            break
        pump(app, 0.05)
    assert img is not None and abs(img.height() / img.width() - 0.5) < 0.02
    assert win.a_remove_crop.isEnabled()
    cv.undo.undo()
    assert not win.a_remove_crop.isEnabled()


def test_auto_crop_finds_text_bbox(app, tmp_path):  # noqa: F811
    src = str(tmp_path / "t.pdf")
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((200, 300), "Only this text", fontsize=20)
    p.insert_text((200, 340), "and this line", fontsize=20)
    p = d.new_page(width=595, height=842)  # blank: nothing to crop
    d.save(src)
    d.close()
    c = canvas_for(src)
    doc = c.doc
    words = words_of(src, 0)
    bbox = pymupdf.Rect()
    for r in words.values():
        bbox |= r
    crop = pymupdf.Rect(K.auto_crop(doc, doc.pages[0], pad=0))
    assert (bbox + (-2, -2, 2, 2)).contains(crop)  # ink lies within the word boxes
    assert abs(crop.x0 - bbox.x0) < 4 and abs(crop.x1 - bbox.x1) < 4
    assert abs(crop.y0 - bbox.y0) < 8 and abs(crop.y1 - bbox.y1) < 8
    padded = pymupdf.Rect(K.auto_crop(doc, doc.pages[0], pad=10))
    assert abs(padded.x0 - (crop.x0 - 10)) < 0.01
    assert K.auto_crop(doc, doc.pages[1]) is None


def test_margins_roundtrip_and_other_pages(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    for i, rot in ((0, 0), (1, 0), (0, 90), (1, 270)):
        ref = doc.pages[i]
        ref.rotation = rot
        m = (10.0, 20.0, 30.0, 40.0)
        cr = K.crop_from_margins(doc, ref, m)
        assert all(abs(a - b) < 0.01 for a, b in zip(K.margins_of(doc, ref, cr), m))
        W, H = K.full_size(doc, ref)
        ref.crop = cr
        assert all(abs(a - b) < 0.01 for a, b in zip(doc.display_size(ref), (W - 40, H - 60)))
        ref.crop = None
        ref.rotation = 0
    assert K.crop_from_margins(doc, doc.pages[0], (400, 0, 400, 0)) is None  # nothing left
    assert K.clamp(doc.page_rect(doc.pages[0]), (-10, -10, 1000, 1000)) is None  # whole page = no crop


def test_crop_survives_duplicate_paste_and_extract(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    crop = (50.0, 60.0, 400.0, 500.0)
    K.set_crops(c, {doc.pages[0]: crop})
    pageops.duplicate(c, [0])
    assert doc.pages[1].crop == crop and doc.pages[1] is not doc.pages[0]
    pageops.copy_pages(c, [0])
    c2 = canvas_for(src)  # another window
    pageops.paste_pages(c2, 0)
    assert c2.doc.pages[0].crop == crop
    out = str(tmp_path / "extract.pdf")
    doc.save(out, c.overlays_for, pages=[doc.pages[1]])
    d = pymupdf.open(out)
    assert d.page_count == 1 and abs(d[0].rect.width - 350) < 0.05
    d.close()
    # merged into another document's save (pasted page from a different source + reorder)
    out2 = str(tmp_path / "merged.pdf")
    c2.doc.save(out2, c2.overlays_for)
    d = pymupdf.open(out2)
    assert abs(d[0].rect.height - 440) < 0.05 and abs(d[1].rect.height - 842) < 0.05
    d.close()
    pageops._clip.clear()  # leave neither Leaf's nor the system clipboard holding pages
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().clear()


def test_crop_dialog_and_tool_menu(app, win, tmp_path, monkeypatch):  # noqa: F811
    from leaf.features import optimize as F

    src = crop_pdf(str(tmp_path / "c.pdf"))
    win.open_path(src)
    pump(app, 0.2)
    cv = win.canvas
    dlg = F.CropDialog(win, 0)
    dlg.unit.setCurrentText("pt")
    for sp, v in zip(dlg.spins, (10, 20, 30, 40)):
        sp.setValue(v)
    assert dlg.margins() == (10, 20, 30, 40)
    dlg.unit.setCurrentText("mm")
    assert all(abs(a - b) < 0.2 for a, b in zip(dlg.margins(), (10, 20, 30, 40)))
    dlg.which.setCurrentIndex(dlg.which.findData("all"))
    assert dlg.rows() == [0, 1, 2, 3]
    dlg.which.setCurrentIndex(dlg.which.findData("even"))
    assert dlg.rows() == [1, 3]
    dlg.reject()
    n = F.crop_rows_by_margins(win, [0, 2], (10, 20, 30, 40))
    assert n == 2 and cv.doc.pages[1].crop is None
    assert all(abs(a - b) < 0.01 for a, b in zip(K.margins_of(cv.doc, cv.doc.pages[2]), (10, 20, 30, 40)))
    assert cv.undo.undoText() == "Crop 2 pages"
    assert F.remove_crop(win) == 2 and all(r.crop is None for r in cv.doc.pages)
    # crop tool is a canvas tool with the C key
    cv.set_tool("crop")
    assert win.a_crop_tool.isChecked()
    cv.set_tool("select")
    assert not win.a_crop_tool.isChecked()


def test_crop_with_leaf_rotation_saves(app, tmp_path):  # noqa: F811
    src = crop_pdf(str(tmp_path / "c.pdf"))
    c = canvas_for(src)
    doc = c.doc
    ref = doc.pages[0]
    K.set_crops(c, {ref: (100.0, 200.0, 500.0, 400.0)})
    ref.rotation = 90
    c.sync_pages()
    assert doc.display_size(ref) == (200.0, 400.0)
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)  # full rewrite (rotation changed)
    d = pymupdf.open(out)
    assert d[0].rotation == 90 and abs(d[0].rect.width - 200) < 0.05 and abs(d[0].rect.height - 400) < 0.05
    assert all(abs(a - b) < 0.05 for a, b in zip(d[0].cropbox, (100, 200, 500, 400)))
    d.close()


def test_crop_tool_drag(app, win, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    from leaf.features import optimize as F

    src = crop_pdf(str(tmp_path / "c.pdf"))
    win.open_path(src)
    pump(app, 0.2)
    cv = win.canvas
    cv.set_tool("crop")
    pg = cv.order[0]
    vp = cv.viewport()

    def at(x, y):
        return cv.mapFromScene(pg.content.mapToScene(x, y))

    picked = []
    monkeypatch.setattr(F, "_exec_menu", lambda menu, at: picked.append([x.text() for x in menu.actions()])
                        or menu.actions()[0])
    a, b = at(100, 60), at(300, 200)
    QTest.mousePress(vp, F.Qt.MouseButton.LeftButton, F.Qt.KeyboardModifier.NoModifier, a)
    QTest.mouseMove(vp, QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2))
    QTest.mouseMove(vp, b)
    QTest.mouseRelease(vp, F.Qt.MouseButton.LeftButton, F.Qt.KeyboardModifier.NoModifier, b)
    assert picked and picked[0][0] == "Crop this page" and "Crop all 4 pages" in picked[0]
    crop = cv.doc.pages[0].crop
    assert crop is not None and all(abs(u - v) < 2 for u, v in zip(crop, (100, 60, 300, 200)))
    assert cv.doc.pages[1].crop is None
    assert cv.undo.undoText() == "Crop page"
