"""OCR: detection, engines (RapidOCR), the undoable OCR layer, search/selection before save, the invisible text
layer after save (incl. rotated pages), the worker process and lazy imports."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QSettings

from leaf import ocr as O
from leaf.document import PageRef
from leaf.features.ocr import OcrBoxes, SetOcr
from tests.test_editor import app, canvas_for, make_pdf, pump  # noqa: F401

LINES = ["The quick brown fox jumps over the lazy dog", "Invoice number 12345 total payable"]


def _text_page_pixmap(lines, dpi=150, size=(595, 842)):
    t = pymupdf.open()
    p = t.new_page(width=size[0], height=size[1])
    for k, line in enumerate(lines):
        p.insert_text((72, 100 + 40 * k), line, fontsize=16)
    pix = p.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    t.close()
    return pix


def make_scanned(path, rotated_second=True, small=False):
    """Image-only pages: page 1 upright, page 2 stored sideways with /Rotate 90 (displays upright)."""
    d = pymupdf.open()
    if small:
        pix = _text_page_pixmap(["Leaf scanned sample"], dpi=150, size=(300, 160))
        p = d.new_page(width=300, height=160)
        p.insert_image(p.rect, pixmap=pix)
    else:
        pix = _text_page_pixmap(LINES)
        p = d.new_page(width=595, height=842)
        p.insert_image(p.rect, pixmap=pix)
        if rotated_second:
            pix2 = _text_page_pixmap(["Rotated scan page", "Another sample line"])
            p2 = d.new_page(width=842, height=595)
            p2.set_rotation(90)
            p2.insert_image(p2.rect * p2.derotation_matrix, pixmap=pix2, rotate=90)
    d.save(path, deflate=True)
    d.close()


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("ocr") / "scan.pdf")
    make_scanned(path)
    return path


@pytest.fixture(scope="module")
def recognized(scanned):
    """RapidOCR words for both pages, recognized once (in-process) and shared by the tests."""
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    eng = O.create_engine("rapidocr")
    with open(scanned, "rb") as f:
        data = f.read()
    out, secs = {}, []
    for i, words, s in O.recognize_document(data, [0, 1], eng, "auto", dpi=200):
        out[i] = words
        secs.append(s)
    print(f"\nRapidOCR: {', '.join(f'{s:.2f}' for s in secs)} s per page (200 dpi, first includes model load)")
    return out


def _texts(words):
    return {w[4].strip(".,").lower() for w in words}


def _find(words, text):
    return next(w for w in words if w[4].strip(".,").lower() == text)


def _close(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _set(c, uid, src, index, words, run=1):
    c.undo.push(SetOcr(SimpleNamespace(canvas=c), {uid: O.OcrPage(src, index, list(words), "rapidocr", "auto")}, run))


# ------------------------------------------------------------------- cheap
def test_detection(scanned, tmp_path):
    d = pymupdf.open(scanned)
    assert O.needs_ocr(d[0]) and O.needs_ocr(d[1])
    assert O.image_coverage(d[1]) > 0.95  # rotated page: boxes compared in the same (unrotated) space
    text = str(tmp_path / "text.pdf")
    make_pdf(text, form=False)
    t = pymupdf.open(text)
    assert not O.needs_ocr(t[0])
    blank = pymupdf.open()
    blank.new_page()
    assert not O.needs_ocr(blank[0])  # no image: nothing to recognize


def test_search_words_spans_and_case():
    words = [(10, 10, 50, 20, "Hello", 0, 0, 0.9), (55, 10, 95, 20, "World", 0, 1, 0.9),
             (10, 30, 40, 40, "again", 1, 0, 0.9)]
    hits = O.search_words(words, "hello wor")
    assert len(hits) == 1 and hits[0].x0 == 10 and 75 < hits[0].x1 < 90
    assert len(O.search_words(words, "AGAIN")) == 1
    assert O.search_words(words, "world again") == []  # not across lines


def test_text_layer_invisible_and_placed_on_rotated_cropped_pages():
    words = [(50, 60, 150, 80, "Hello", 0, 0, 1.0), (160, 60, 260, 80, "Wörld", 0, 1, 1.0),
             (130, 100, 200, 130, "中文字", 1, 0, 1.0)]
    for rot in (0, 90, 180, 270):
        d = pymupdf.open()
        p = d.new_page(width=400, height=600)
        p.set_cropbox(pymupdf.Rect(30, 50, 380, 560))
        p.set_rotation(rot)
        before = p.get_pixmap(dpi=50).samples
        O.write_text_layer(p, words, p.derotation_matrix)
        q = pymupdf.open(stream=d.tobytes())[0]
        assert q.get_pixmap(dpi=50).samples == before  # render mode 3: nothing visible
        for w in words:
            hit = q.search_for(w[4])
            assert hit and _close(tuple(hit[0] * q.rotation_matrix), w[:4], 1.5), (rot, w[4], hit)
        assert "Wörld" in q.get_text() and "中文字" in q.get_text()


def test_no_engine_message(app, monkeypatch, tmp_path):  # noqa: F811
    from leaf.features import ocr as F

    shown = []
    monkeypatch.setattr(O, "available_engines", lambda: [])
    monkeypatch.setattr(F.QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    path = str(tmp_path / "t.pdf")
    make_pdf(path, form=False)
    win = SimpleNamespace(canvas=canvas_for(path), _ocr=SimpleNamespace(running=False))
    F.recognize(win)
    assert shown and "isn't available" in shown[0]


def test_startup_does_not_import_engines():
    code = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "QCoreApplication.setOrganizationName('LeafTest'); QCoreApplication.setApplicationName('LeafOcrStartup')\n"
        "app = QApplication([])\n"
        "from leaf.window import MainWindow\n"
        "w = MainWindow()\n"
        "assert 'ocr' in w.installed_features and w.a_ocr is not None\n"
        "print(sorted(m for m in ('rapidocr', 'onnxruntime', 'cv2', 'numpy') if m in sys.modules))\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "[]"


# ------------------------------------------------------- RapidOCR (heavier)
def test_rapidocr_finds_words(recognized):
    assert {"quick", "brown", "lazy", "invoice", "12345"} <= _texts(recognized[0])
    assert {"rotated", "scan", "sample"} <= _texts(recognized[1])
    w = _find(recognized[0], "quick")  # inserted at baseline y=100, 16 pt: box roughly y 85..105
    assert 80 < w[1] < 95 and 98 < w[3] < 112 and 0.5 < w[7] <= 1.0


def test_layer_search_select_save_undo(app, scanned, recognized, tmp_path):  # noqa: F811
    c = canvas_for(scanned)
    doc = c.doc
    r0, r1 = doc.pages
    _set(c, r0.uid, r0.src, 0, recognized[0])
    _set(c, r1.uid, r1.src, 1, recognized[1])
    assert c.undo.count() == 1  # one run, one undo step
    # Leaf sees the words before saving: selection/copy and the find bar
    assert "quick" in _texts(r0.src.words(0))
    pg = c.order[0]
    a, b = _find(recognized[0], "the"), _find(recognized[0], "dog")
    c._update_text_selection(pg, QPointF(a[0] + 1, a[1] + 3), QPointF(b[2] - 1, b[3] - 3))
    assert "quick brown fox" in c.selected_text().lower()
    c._end_text_selection()
    assert O.search_words(r0.src.ocr[0], "brown fox")
    # save (incremental: the page list is unchanged) -> invisible, searchable text where the words are
    out = str(tmp_path / "ocr.pdf")
    assert doc.save(out, c.overlays_for) is True
    d = pymupdf.open(out)
    orig = pymupdf.open(scanned)
    for i, word in ((0, "quick"), (1, "rotated")):
        w = _find(recognized[i], word)
        hit = d[i].search_for(word)
        assert hit and _close(tuple(hit[0] * d[i].rotation_matrix), w[:4], 3), (word, hit, w)
        assert d[i].get_pixmap(dpi=40).samples == orig[i].get_pixmap(dpi=40).samples
    assert "12345" in d[0].get_text()
    d.close()
    # undo removes the layer everywhere
    c.undo.undo()
    assert "quick" not in _texts(r0.src.words(0)) and not doc.ocr
    out2 = str(tmp_path / "undone.pdf")
    doc.save(out2, c.overlays_for)
    assert not pymupdf.open(out2)[0].search_for("quick")
    c.undo.redo()
    assert "quick" in _texts(r0.src.words(0))


def test_leaf_rotation_and_duplicates(app, scanned, recognized, tmp_path):  # noqa: F811
    c = canvas_for(scanned)
    doc = c.doc
    r0 = doc.pages[0]
    _set(c, r0.uid, r0.src, 0, recognized[0])
    r0.rotation = 90  # rotated in Leaf
    dup = PageRef(r0.src, 0)  # a duplicate shares the source page (and its recognized text)
    doc.pages.append(dup)
    assert doc.ocr_words(dup) == doc.ocr_words(r0)
    out = str(tmp_path / "rot.pdf")
    assert doc.save(out, c.overlays_for) is False
    d = pymupdf.open(out)
    w = _find(recognized[0], "quick")
    want = pymupdf.Rect(w[:4]) * doc.extra_matrix(r0)  # where it shows in Leaf's rotated view
    hit = d[0].search_for("quick")
    assert d[0].rotation == 90 and hit and _close(tuple(hit[0] * d[0].rotation_matrix), tuple(want), 3)
    assert d[2].search_for("quick")  # the duplicate got its own copy of the layer


def test_window_runs_worker_process(app, tmp_path, monkeypatch):  # noqa: F811
    """Through the UI: scanned hint, worker process on one small page, undo step, view tint, remove."""
    pytest.importorskip("rapidocr")
    pytest.importorskip("onnxruntime")
    from leaf.window import MainWindow

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafOcrTest")
    QSettings().clear()
    toasts = []
    win = MainWindow()
    try:
        monkeypatch.setattr(win, "toast", lambda text, *a, **k: toasts.append(text))
        path = str(tmp_path / "small.pdf")
        make_scanned(path, small=True)
        win.open_path(path)
        pump(app, 1.0)
        assert any("looks scanned" in t for t in toasts)
        st = win._ocr
        eng = next(e for e in O.available_engines() if e.key == "rapidocr")
        t = time.perf_counter()
        st.start(dict(engine=eng, language="auto", dpi=150, rows=[0], skip=True))
        assert st.running and not win.a_ocr.isEnabled()
        while st.running and time.perf_counter() - t < 90:
            pump(app, 0.1)
        assert not st.running, "OCR worker didn't finish"
        print(f"\nworker: {time.perf_counter() - t:.1f} s (spawn + model load + 1 page), peak {st.peak_mb:.0f} MB")
        doc = win.canvas.doc
        assert "scanned" in _texts(doc.ocr_words(doc.pages[0]))
        assert win.canvas.undo.undoText().startswith("Recognize text")
        assert win.a_ocr_remove.isEnabled() and not st.files  # temp files cleaned up
        win.find.edit.setText("scanned sample")  # the find bar sees recognized (unsaved) text
        win.find._start()
        while win.find._todo:
            win.find._chunk()
        assert len(win.find.hits) == 1
        win.a_ocr_show.setChecked(True)
        assert any(isinstance(ch, OcrBoxes) for ch in win.canvas.order[0].content.childItems())
        win.a_ocr_remove.trigger()
        assert not doc.ocr and not any(isinstance(ch, OcrBoxes) for ch in win.canvas.order[0].content.childItems())
        win.canvas.undo.undo()
        assert doc.ocr
    finally:
        if win.canvas.doc is not None:
            win.canvas.undo.setClean()
        win.close()
        QSettings().clear()
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])
