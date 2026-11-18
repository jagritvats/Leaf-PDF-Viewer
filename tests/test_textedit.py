"""Edit existing text (leaf/textedit.py): fidelity (no-op edits are pixel-identical), real edits extract and search
correctly, refusals, save order, undo/redo, incremental vs rewrite, duplicated pages, display copy and worker."""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pymupdf
import pytest
from PySide6.QtCore import QRectF

from leaf import commands as C
from leaf import redact as R
from leaf import render_worker as RW
from leaf import textedit as T
from leaf.document import SEARCH_FLAGS, PageRef, PdfDocument
from leaf.items import ShapeItem
from tests.test_editor import add, app, canvas_for  # noqa: F401

WINF = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
ARIAL, TIMES, ARIALBD = (os.path.join(WINF, n) for n in ("arial.ttf", "times.ttf", "arialbd.ttf"))
YAHEI = os.path.join(WINF, "msyh.ttc")
NIRMALA = os.path.join(WINF, "Nirmala.ttc")
HAVE_TT = all(os.path.exists(p) for p in (ARIAL, TIMES, ARIALBD))
LOREM = (
    "Leaf keeps every edit as a model and replays it onto a fresh copy of the source when the file is saved. "
    "This paragraph exists to test justified text, greedy line breaking and the reuse of embedded fonts. "
    "Quick brown foxes jump over lazy dogs while the editor measures glyph widths and baselines."
)


# ------------------------------------------------------------------------------------------------- corpus
def _save(doc, path, subset=False):
    if subset:
        doc.subset_fonts()
    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


def base14(path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=300)
    p.insert_text((40, 50), "Helvetica: Invoice total 1,250.00 EUR", fontname="helv", fontsize=12)
    p.insert_text((40, 75), "Times: The quick brown fox jumps.", fontname="tiro", fontsize=13, color=(0.1, 0.2, 0.6))
    p.insert_text((40, 100), "Courier: id = 0x2F; ok", fontname="cour", fontsize=11)
    p.insert_text((40, 125), "Helvetica-Bold heading", fontname="hebo", fontsize=14)
    p.draw_rect(pymupdf.Rect(35, 160, 300, 185), color=(0.8, 0, 0), width=1)
    p.draw_line((30, 172), (380, 172), color=(0, 0.5, 0), width=0.8)
    p.insert_text((40, 177), "Text inside a box crossed by a line", fontname="helv", fontsize=11)
    return _save(doc, path)


def truetype(path, subset):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=220)
    p.insert_font(fontname="F1", fontfile=ARIAL)
    p.insert_font(fontname="F2", fontfile=TIMES)
    p.insert_font(fontname="F3", fontfile=ARIALBD)
    p.insert_text((40, 50), "Arial embedded: Order 4711 shipped", fontname="F1", fontsize=12)
    p.insert_text((40, 80), "Times New Roman: Dear Sir or Madam,", fontname="F2", fontsize=13)
    p.insert_text((40, 110), "Arial Bold: TOTAL DUE", fontname="F3", fontsize=14, color=(0.7, 0.1, 0.1))
    p.insert_text((40, 140), "Kerning AVATAR Wave Toyota", fontname="F1", fontsize=16)
    return _save(doc, path, subset=subset)


def justified(path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=300)
    css = "* {font-family: sans-serif; font-size: 11pt; text-align: justify; line-height: 1.25;}"
    p.insert_htmlbox(pymupdf.Rect(40, 40, 380, 260), f"<p>{LOREM}</p><p>{LOREM}</p>", css=css)
    return _save(doc, path, subset=True)


def two_column(path):
    doc = pymupdf.open()
    p = doc.new_page(width=500, height=320)
    for x0, x1 in ((40, 240), (255, 460)):  # 15 pt gutter
        p.insert_textbox(pymupdf.Rect(x0, 40, x1, 300), LOREM, fontname="tiro", fontsize=10)
    return _save(doc, path)


def rotated(path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=300)
    p.insert_text((40, 60), "Page with /Rotate 90 applied", fontname="helv", fontsize=12)
    p.insert_text((40, 90), "Second line on the rotated page", fontname="tiro", fontsize=12)
    p.set_rotation(90)
    q = doc.new_page(width=420, height=300)
    q.insert_text((60, 260), "Vertical text up", fontname="helv", fontsize=12, rotate=90)
    q.insert_text((150, 200), "Thirty degrees", fontname="helv", fontsize=12,
                  morph=(pymupdf.Point(150, 200), pymupdf.Matrix(-30)))
    q.insert_text((220, 60), "Upside down text", fontname="helv", fontsize=12, rotate=180)
    return _save(doc, path)


def tight(path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=200)
    lines = ["Tight leading gyp lines overlap", "Hjklb tall ascenders Qyg below", "Third line with descenders gjpqy"]
    for i, t in enumerate(lines):
        p.insert_text((40, 60 + i * 10.5), t, fontname="helv", fontsize=12)  # leading 0.875 * size
    return _save(doc, path)


def handwritten(path):
    """Raw content streams: TJ kerning, char/word spacing, Tz, a Type3 font, a font without ToUnicode, OCR-style
    invisible text and outlined text."""
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=300)
    helv = doc.get_new_xref()
    doc.update_object(helv, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>")
    procs = {}
    for name, w in (("a", 500), ("b", 600), ("c", 400), ("space", 300)):
        x = doc.get_new_xref()
        body = f"{w} 0 0 0 {w - 50} 700 d1 0 0 {w - 50} 700 re f" if name != "space" else f"{w} 0 0 0 0 0 d1"
        doc.update_object(x, "<<>>")
        doc.update_stream(x, body.encode())
        procs[name] = x
    t3 = doc.get_new_xref()
    doc.update_object(
        t3,
        "<</Type/Font/Subtype/Type3/FontBBox[0 0 600 700]/FontMatrix[0.001 0 0 0.001 0 0]"
        "/CharProcs<<" + "".join(f"/{n} {x} 0 R" for n, x in procs.items()) + ">>"
        "/Encoding<</Type/Encoding/Differences[32/space 97/a/b/c]>>/FirstChar 32/LastChar 99"
        "/Widths[300 " + "0 " * 64 + "500 600 400]>>",
    )
    garb = doc.get_new_xref()
    doc.update_object(
        garb,
        "<</Type/Font/Subtype/Type3/FontBBox[0 0 600 700]/FontMatrix[0.001 0 0 0.001 0 0]"
        "/CharProcs<<" + f"/g1 {procs['a']} 0 R/g2 {procs['b']} 0 R/g3 {procs['c']} 0 R" + ">>"
        "/Encoding<</Type/Encoding/Differences[1/g1/g2/g3]>>/FirstChar 1/LastChar 3/Widths[500 600 400]>>",
    )
    content = "".join(f"q BT {s} ET Q\n" for s in (
        "/H 14 Tf 40 270 Td [(A) 120 (V) 110 (A) 60 (T) 80 (A) 40 (R)] TJ",
        "/H 12 Tf 2 Tc 40 245 Td (Char spacing two points) Tj",
        "/H 12 Tf 6 Tw 40 220 Td (Word spacing six points) Tj",
        "/H 12 Tf 70 Tz 40 195 Td (Horizontal scale seventy) Tj",
        "/T3 20 Tf 40 160 Td (abc cab) Tj",
        "/G 20 Tf 40 125 Td (\\001\\002\\003\\002) Tj",
        "/H 11 Tf 3 Tr 40 95 Td (Invisible OCR words here) Tj",
        "/H 11 Tf 40 35 Td (Plain neighbour line after the odd ones) Tj",
    ))
    # page 2: outlined text with its own line width (MuPDF's content filter loses the width: page refused)
    px = p.xref
    qx = doc.new_page(width=420, height=150).xref
    content2 = "q BT /H 16 Tf 1 Tr 0.5 w 40 100 Td (Outlined heading) Tj ET Q\nBT /H 11 Tf 40 60 Td (Plain line) Tj ET\n"
    for pxref, text in ((px, content), (qx, content2)):
        cx = doc.get_new_xref()
        doc.update_object(cx, "<<>>")
        doc.update_stream(cx, text.encode())
        doc.xref_set_key(pxref, "Contents", f"{cx} 0 R")
        doc.xref_set_key(pxref, "Resources", f"<</Font<</H {helv} 0 R/T3 {t3} 0 R/G {garb} 0 R>>>>")
    return _save(doc, path)


def cjk(path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=200)
    p.insert_text((40, 50), "中文文本编辑测试", fontname="china-s", fontsize=14)
    if os.path.exists(YAHEI):
        p.insert_font(fontname="YH", fontfile=YAHEI)
        p.insert_text((40, 80), "微软雅黑嵌入字体", fontname="YH", fontsize=14)
    return _save(doc, path, subset=True)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    d = tmp_path_factory.mktemp("te-corpus")
    files = {"base14": base14(str(d / "base14.pdf")), "justified": justified(str(d / "justified.pdf")),
             "two_column": two_column(str(d / "two_column.pdf")), "rotated": rotated(str(d / "rotated.pdf")),
             "tight": tight(str(d / "tight.pdf")), "handwritten": handwritten(str(d / "handwritten.pdf")),
             "cjk": cjk(str(d / "cjk.pdf"))}
    if HAVE_TT:
        files["tt_full"] = truetype(str(d / "tt_full.pdf"), False)
        files["tt_sub"] = truetype(str(d / "tt_sub.pdf"), True)
    return files


# ------------------------------------------------------------------------------------------------ helpers
def gray(page, zoom=3.0) -> np.ndarray:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.int16)


def diff_px(a, b, thresh=64) -> int:
    return int((np.abs(gray(a) - gray(b)) > thresh).sum())


def unit_for(doc, ref, needle, para=False) -> T.TextUnit:
    """The unit whose (current) text contains `needle`, hit-tested like a click in its middle."""
    pt = T.page_text(ref.src, ref.index)
    box = next((T.edited_box(pt, k, t) for k, t in doc.text_edits.get(ref.uid, {}).items() if needle in t), None)
    if box is None:
        box = next(ln.bbox for ln in pt.lines if needle in ln.text)
    c = (box.tl + box.br) / 2 * pt.page.rotation_matrix
    u = T.unit_at(doc, ref, c.x, c.y, para=para)
    assert u is not None and needle in u.text, (needle, u)
    return u


def edited(ref, edits) -> pymupdf.Document:
    out = pymupdf.open(stream=ref.src.data, filetype="pdf")
    T.apply_edits(out[ref.index], edits)
    return pymupdf.open(stream=out.tobytes(garbage=3, deflate=True), filetype="pdf")


def flat(s: str) -> str:
    return "".join(s.replace("\u00a0", " ").split())


# --------------------------------------------------------------------------------------------- fidelity
NOOP = [
    ("base14", 0, "Invoice", False), ("base14", 0, "Times:", False), ("base14", 0, "Courier", False),
    ("base14", 0, "inside a box", False), ("tt_full", 0, "Arial embedded", False), ("tt_full", 0, "Kerning", False),
    ("tt_sub", 0, "Arial embedded", False), ("tt_sub", 0, "TOTAL", False), ("justified", 0, "This paragraph", False),
    ("justified", 0, "Leaf keeps", True), ("two_column", 0, "Leaf keeps", False), ("two_column", 0, "Leaf keeps", True),
    ("tight", 0, "Hjklb", False), ("rotated", 0, "/Rotate 90", False), ("rotated", 1, "Vertical", False),
    ("rotated", 1, "Thirty", False), ("rotated", 1, "Upside", False), ("handwritten", 0, "AVATAR", False),
    ("handwritten", 0, "Char spacing", False), ("handwritten", 0, "Word spacing", False),
    ("handwritten", 0, "Horizontal", False), ("handwritten", 0, "abc", False), ("cjk", 0, "中文", False),
    ("cjk", 0, "微软", False),
]


@pytest.mark.parametrize("name,pno,needle,para", NOOP, ids=[f"{n}-{s[:10]}{'-para' if p else ''}" for n, _, s, p in NOOP])
def test_noop_edit_is_pixel_identical(corpus, name, pno, needle, para):
    if name not in corpus:
        pytest.skip("font not installed")
    doc = PdfDocument.open(corpus[name])
    ref = doc.pages[pno]
    if not any(needle in ln.text for ln in T.page_text(ref.src, pno).lines):
        pytest.skip("font not installed")
    u = unit_for(doc, ref, needle, para)
    assert T.check(doc, ref, u, u.text) == []
    # a no-op that re-lays out every glyph (the text "changes" by losing trailing blanks only)
    out = edited(ref, {u.key: u.text})
    assert diff_px(doc.primary.doc[pno], out[pno]) == 0
    assert flat(u.text) in flat(out[pno].get_text())


def test_forced_relayout_of_whole_paragraph_is_close(corpus):
    """Every line of a justified paragraph re-laid out from font metrics (no line kept as it was)."""
    doc = PdfDocument.open(corpus["justified"])
    ref = doc.pages[0]
    u = unit_for(doc, ref, "Leaf keeps", para=True)
    out = pymupdf.open(stream=ref.src.data, filetype="pdf")
    pt = T.PageText(out[0])
    para = pt.find(u.key)
    plan = T.plan_para_edit(pt, para, para.text, T._Ctx(pt, T.Fallbacks()), keep_unchanged_lines=False)
    assert len(plan.unit_glyphs) > 200
    T.apply_plan(out[0], T.PagePlan(plan.unit_glyphs, plan.glyphs, [], {}))
    n = diff_px(doc.primary.doc[0], out[0])
    print(f"justified paragraph, all lines re-laid out: {n} px differ at 216 dpi")
    assert n < 3000  # sub-pixel drift only


# ------------------------------------------------------------------------------------------- real edits
def test_line_edits_extract_and_search(corpus):
    doc = PdfDocument.open(corpus["base14"])
    ref = doc.pages[0]
    u = unit_for(doc, ref, "Invoice")
    longer = "Helvetica: Invoice total 98,765.43 EUR (paid in full)"
    out = edited(ref, {u.key: longer})
    p = out[0]
    assert p.search_for("98,765.43 EUR (paid in full)", flags=SEARCH_FLAGS)
    assert not p.search_for("1,250.00", flags=SEARCH_FLAGS)
    assert p.search_for("Courier: id = 0x2F", flags=SEARCH_FLAGS)  # neighbours untouched
    words = [w[4] for w in p.get_text("words", flags=SEARCH_FLAGS)]
    assert "98,765.43" in words and "full)" in words
    shorter = "Helvetica: Invoice"
    out = edited(ref, {u.key: shorter})
    t = out[0].get_text(flags=SEARCH_FLAGS)
    assert "Invoice" in t and "total" not in t and "EUR" not in t
    # same baseline, same size: the new text sits where the old one was
    r = out[0].search_for("Helvetica: Invoice", flags=SEARCH_FLAGS)[0]
    assert abs(r.y1 - pymupdf.Rect(u.bbox).y1) < 1.5 and abs(r.x0 - u.bbox[0]) < 0.5


@pytest.mark.skipif(not HAVE_TT, reason="needs Arial/Times")
def test_fallback_glyphs_extract_as_one_line(corpus):
    """New characters missing from a subset font come from a fallback font embedded in the page resources, so the
    line stays one line and words are not split."""
    doc = PdfDocument.open(corpus["tt_sub"])
    ref = doc.pages[0]
    u = unit_for(doc, ref, "Arial embedded")
    new = "Arial embedded: Order 4712 is ready, quiz jam"
    warns = T.check(doc, ref, u, new)
    assert any("Arial" in w for w in warns), warns
    out = edited(ref, {u.key: new})
    p = out[0]
    assert p.search_for("Order 4712 is ready, quiz jam", flags=SEARCH_FLAGS)
    words = [w[4] for w in p.get_text("words", flags=SEARCH_FLAGS)]
    for w in ("4712", "ready,", "quiz", "jam"):
        assert w in words, words
    line = [ln for b in p.get_text("dict", flags=SEARCH_FLAGS)["blocks"] for ln in b.get("lines", [])
            if "Order" in "".join(s["text"] for s in ln["spans"])]
    assert len(line) == 1
    # no Form XObject: the fallback font is a page font, and it is a small subset
    assert not p.get_xobjects()
    fonts = [f for f in p.get_fonts() if "+" in f[3]]
    assert fonts
    assert len(out.tobytes()) - len(doc.primary.data) < 60_000


@pytest.mark.skipif(not os.path.exists(YAHEI), reason="needs a CJK system font")
def test_cjk_fallback_in_latin_line(corpus):
    doc = PdfDocument.open(corpus["base14"])
    ref = doc.pages[0]
    u = unit_for(doc, ref, "Invoice")
    new = "Helvetica: Invoice 東京 total"
    assert any("東京" in w or "京東" in w for w in T.check(doc, ref, u, new))
    p = edited(ref, {u.key: new})[0]
    assert p.search_for("東京 total", flags=SEARCH_FLAGS)


def test_paragraph_reflow(corpus):
    doc = PdfDocument.open(corpus["two_column"])
    ref = doc.pages[0]
    u = unit_for(doc, ref, "Leaf keeps", para=True)
    assert u.kind == "para" and len(u.line_boxes) > 3 and u.align == "left"
    new = "Leaf keeps every edit as a model. This paragraph got much shorter."
    p = edited(ref, {u.key: new})[0]
    t = flat(p.get_text(flags=SEARCH_FLAGS))
    assert flat(new) in t
    assert t.count(flat("greedy line breaking")) == 1  # the other column's copy survives, this one is gone
    # longer: grows past its lines -> warning
    longer = u.text + " " + u.text
    assert any("lines instead of" in w for w in T.check(doc, ref, u, longer))
    # newline forces a break
    p = edited(ref, {u.key: "First line\nSecond line"})[0]
    rs = [p.search_for(s, flags=SEARCH_FLAGS)[0] for s in ("First line", "Second line")]
    assert rs[1].y0 > rs[0].y1 - 1 and abs(rs[0].x0 - rs[1].x0) < 0.5


def test_rotated_page_and_text_units(corpus):
    doc = PdfDocument.open(corpus["rotated"])
    ref = doc.pages[0]  # /Rotate 90
    u = unit_for(doc, ref, "/Rotate 90")
    assert u.angle == 90.0
    page = doc.primary.doc[0]
    ins = pymupdf.Rect(u.bbox)
    assert ins in page.rect + (-1, -1, 1, 1)  # display space
    p = edited(ref, {u.key: "Page with /Rotate 90 edited"})[0]
    hit = p.search_for("edited", flags=SEARCH_FLAGS)[0] * p.rotation_matrix  # display space
    assert hit.intersects(ins + (-60, -60, 60, 60))
    ref1 = doc.pages[1]
    up = unit_for(doc, ref1, "Vertical")
    assert up.angle == 270.0
    p = edited(ref1, {unit_for(doc, ref1, "Thirty").key: "Thirty-one degrees"})[1]
    assert p.search_for("Thirty-one", flags=SEARCH_FLAGS)


# --------------------------------------------------------------------------------------------- refusals
def test_refusals(corpus):
    doc = PdfDocument.open(corpus["handwritten"])
    ref = doc.pages[0]
    pt = T.page_text(ref.src, 0)

    def unit_of(pred):
        ln = next(ln for ln in pt.lines if pred(ln.text))
        return T._make_unit(ref, pt, T.unit_key(ln), None)

    with pytest.raises(T.Refuse, match="character mapping"):
        T.check(doc, ref, unit_of(lambda t: "\ufffd" in t or any(ord(c) < 32 for c in t)))
    with pytest.raises(T.Refuse, match="OCR"):
        T.check(doc, ref, unit_of(lambda t: "Invisible" in t))
    ok = unit_of(lambda t: "Plain neighbour" in t)
    assert T.check(doc, ref, ok) == []
    with pytest.raises(T.Refuse, match="line breaks"):
        T.check(doc, ref, ok, "two\nlines")
    with pytest.raises(T.Refuse, match="script"):
        T.check(doc, ref, ok, "Plain مرحبا")
    with pytest.raises(T.Refuse, match="script"):
        T.check(doc, ref, ok, "Plain हिन्दी")
    # page 2: the outlined line itself, and any other line on a page the content filter can't reproduce
    ref = doc.pages[1]
    pt = T.page_text(ref.src, 1)
    with pytest.raises(T.Refuse, match="Outlined"):
        T.check(doc, ref, unit_of(lambda t: "Outlined" in t))
    with pytest.raises(T.Refuse, match="drawing settings"):
        T.check(doc, ref, unit_of(lambda t: "Plain line" in t))


def test_refuses_when_the_font_cannot_be_reused(tmp_path):
    """A symbolic font: untouched characters would change their look, so the edit is refused up front."""
    d = pymupdf.open()
    p = d.new_page(width=300, height=100)
    p.insert_text((20, 40), "abgd", fontname="symb", fontsize=14)
    p.insert_text((20, 70), "Plain text", fontname="helv", fontsize=12)
    doc = PdfDocument.open(_save(d, str(tmp_path / "sym.pdf")))
    ref = doc.pages[0]
    pt = T.page_text(ref.src, 0)
    ln = next(ln for ln in pt.lines if ln.text.strip() and "Plain" not in ln.text)
    with pytest.raises(T.Refuse, match="reuse this text’s font"):
        T.check(doc, ref, T._make_unit(ref, pt, T.unit_key(ln), None))
    assert T.check(doc, ref, unit_for(doc, ref, "Plain"), "Plain words") == []


@pytest.mark.skipif(not os.path.exists(NIRMALA), reason="needs Nirmala UI")
def test_refuses_shaped_script(tmp_path):
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=200)
    css = "@font-face {font-family: nir; src: url(Nirmala.ttc);} * {font-family: nir; font-size: 14pt;}"
    p.insert_htmlbox(pymupdf.Rect(40, 40, 380, 100), "<p>हिन्दी पाठ संपादन परीक्षण</p>", css=css,
                     archive=pymupdf.Archive(WINF))
    path = _save(doc, str(tmp_path / "deva.pdf"), subset=True)
    d = PdfDocument.open(path)
    ref = d.pages[0]
    pt = T.page_text(ref.src, 0)
    ln = next(ln for ln in pt.lines if ln.text.strip())
    with pytest.raises(T.Refuse):
        T.check(d, ref, T._make_unit(ref, pt, T.unit_key(ln), None))


def test_overlapping_edits_refused(corpus):
    doc = PdfDocument.open(corpus["two_column"])
    ref = doc.pages[0]
    line = unit_for(doc, ref, "Leaf keeps")
    doc.text_edits[ref.uid] = {line.key: "Leaf keeps CHANGED"}
    # clicking the edited line returns the edit (same key), in either mode
    again = unit_for(doc, ref, "Leaf keeps CHANGED", para=True)
    assert again.key == line.key and again.text == "Leaf keeps CHANGED"
    pt = T.page_text(ref.src, 0)
    para = pt.para_of(pt.find(line.key))
    with pytest.raises(T.Refuse, match="already edited"):
        T.check(doc, ref, T._make_unit(ref, pt, T.unit_key(para), None))


# ------------------------------------------------------------------------------------- document & canvas
def make_doc(path):
    d = pymupdf.open()
    for i in range(2):
        p = d.new_page(width=595, height=842)
        p.insert_text((72, 100), f"Page {i + 1} heading", fontsize=16)
        p.insert_text((72, 140), "Total amount due 1,250.00 EUR", fontsize=12)
        p.insert_text((72, 180), "Another untouched line below", fontsize=12)
    d[0].add_rect_annot(pymupdf.Rect(400, 400, 500, 450))
    d.save(path)
    d.close()
    return path


def edit(c, ref, needle, new, para=False):
    u = unit_for(c.doc, ref, needle, para)
    T.check(c.doc, ref, u, new)
    cmd = C.EditPageText(c, ref, u, new)
    c.undo.push(cmd)
    return u


def test_undo_redo_display_words_and_thumbnail_signal(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    ref = c.doc.pages[0]
    stale = []
    c.annots.thumbsStale.connect(stale.extend)
    before = gray(ref.src.render_page(0, ref.uid))
    u = edit(c, ref, "Total amount", "Total amount due 9,999.00 EUR")
    assert c.doc.text_edits == {ref.uid: {u.key: "Total amount due 9,999.00 EUR"}}
    assert ref.uid in stale
    words = [w[4] for w in ref.src.words(0, ref.uid)]
    assert "9,999.00" in words and "1,250.00" not in words
    assert "1,250.00" in [w[4] for w in ref.src.words(0)]  # the source page itself never changes
    assert c.doc.text_page(ref).search_for("9,999.00", flags=SEARCH_FLAGS)
    shown = ref.src.render_page(0, ref.uid)
    assert shown.search_for("9,999.00")
    assert int((np.abs(gray(shown) - before) > 64).sum()) > 0
    # the annotation still renders on the edited page
    pix, _ = c.doc.render(ref, 1.0)
    orig = ref.src.page(0).get_pixmap()
    assert orig.pixel(450, 400) != (255, 255, 255) and pix.pixel(450, 400) == orig.pixel(450, 400)
    # editing the same unit again replaces the edit (one key)
    u2 = unit_for(c.doc, ref, "9,999.00")
    assert u2.key == u.key
    c.undo.push(C.EditPageText(c, ref, u2, "Total amount due 5.00 EUR"))
    assert c.doc.text_edits[ref.uid] == {u.key: "Total amount due 5.00 EUR"}
    c.undo.undo()
    assert c.doc.text_edits[ref.uid] == {u.key: "Total amount due 9,999.00 EUR"}
    c.undo.undo()
    assert c.doc.text_edits == {}
    assert int((np.abs(gray(ref.src.render_page(0, ref.uid)) - before) > 0).sum()) == 0
    assert "1,250.00" in [w[4] for w in ref.src.words(0, ref.uid)]
    c.undo.redo()
    assert "9,999.00" in [w[4] for w in ref.src.words(0, ref.uid)]
    # editing back to the original text removes the edit
    u3 = unit_for(c.doc, ref, "9,999.00")
    c.undo.push(C.EditPageText(c, ref, u3, "Total amount due 1,250.00 EUR"))
    assert c.doc.text_edits == {}


def test_preview_hidden(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    ref = c.doc.pages[0]
    u = unit_for(c.doc, ref, "Total amount")
    c.preview_text_hidden(ref, u)
    p = ref.src.render_page(0, ref.uid)
    assert not p.search_for("Total amount") and p.search_for("Another untouched")
    r = pymupdf.Rect(u.bbox)
    g = gray(p, 1.0)
    assert g[int(r.y0):int(r.y1), int(r.x0):int(r.x1)].min() > 200  # blank where the line was
    c.preview_text_hidden(ref, None)
    assert ref.src.render_page(0, ref.uid).search_for("Total amount")
    # hiding an edited unit hides the replacement too; restoring shows the edit
    edit(c, ref, "Total amount", "Total amount due 7 EUR")
    c.preview_text_hidden(ref, unit_for(c.doc, ref, "7 EUR"))
    assert not ref.src.render_page(0, ref.uid).search_for("7 EUR")
    c.preview_text_hidden(ref, None)
    assert ref.src.render_page(0, ref.uid).search_for("7 EUR")


def test_save_order_with_overlay_and_redaction(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    ref = c.doc.pages[0]
    edit(c, ref, "Total amount", "Total amount due 9,999.00 EUR SECRETVALUE")
    hit = ref.src.render_page(0, ref.uid).search_for("SECRETVALUE")[0]
    add(c, 0, R.RedactItem("redact", QRectF(hit.x0 - 1, hit.y0 - 1, hit.width + 2, hit.height + 2),
                           {"color": "#000000", "text": ""}))
    add(c, 0, ShapeItem("rect", QRectF(72, 170, 50, 20), {"color": "#00ff00"}))
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    p = d[0]
    t = p.get_text(flags=SEARCH_FLAGS)
    assert "9,999.00" in t and "SECRETVALUE" not in t and "1,250.00" not in t
    assert any(a.type[1] == "Square" for a in p.annots())  # overlay written after the text edit
    assert "1,250.00" in d[1].get_text()  # the other page is untouched


def test_incremental_vs_rewrite(app, tmp_path, monkeypatch):
    src = make_doc(str(tmp_path / "a.pdf"))
    with open(src, "rb") as f:
        orig = f.read()
    c = canvas_for(src)
    ref = c.doc.pages[0]
    edit(c, ref, "Total amount", "Total amount due 9,999.00 EUR")
    out = str(tmp_path / "rewrite.pdf")
    assert c.doc.save(out, c.overlays_for) is False  # default: full rewrite, the old text is gone
    with open(out, "rb") as f:
        data = f.read()
    assert not data.startswith(orig)
    d = pymupdf.open(out)
    assert all("1,250.00" not in (d.xref_stream(x) or b"").decode("latin-1") for x in range(1, d.xref_length())
               if d.xref_is_stream(x))
    c.doc.prefer_rewrite = False
    out2 = str(tmp_path / "incr.pdf")
    assert c.doc.save(out2, c.overlays_for) is True
    with open(out2, "rb") as f:
        assert f.read().startswith(orig)  # the previous revision (old text) stays in the file
    assert "9,999.00" in pymupdf.open(out2)[0].get_text()
    # signed documents default to incremental (keeps the signature's revision) and warn
    c.doc.prefer_rewrite = None
    monkeypatch.setattr(T, "is_signed", lambda src: True)
    assert c.doc.save(str(tmp_path / "signed.pdf"), c.overlays_for) is True
    assert any("signed" in w for w in T.check(c.doc, ref, unit_for(c.doc, ref, "Another")))


def test_duplicated_page_edits_stay_per_page(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    doc = c.doc
    first = doc.pages[0]
    dup = PageRef(first.src, first.index)
    old = [(r, r.rotation) for r in doc.pages]
    c.undo.push(C.SetPages(c, old, [(first, 0), (dup, 0)] + old[1:], "Duplicate"))
    edit(c, dup, "Total amount", "Total amount due 2.00 EUR")
    assert "2.00" in [w[4] for w in dup.src.words(0, dup.uid)]
    assert "1,250.00" in [w[4] for w in first.src.words(0, first.uid)]
    a, _ = doc.render(first, 1.0)
    b, _ = doc.render(dup, 1.0)
    assert a.samples != b.samples
    assert first.src.page(0).get_pixmap(alpha=False).samples == a.samples  # the copy shows the original
    edit(c, first, "Page 1 heading", "Page 1 heading (original)")
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    t0, t1 = d[0].get_text(), d[1].get_text()
    assert "(original)" in t0 and "1,250.00" in t0 and "2.00 EUR" not in t0
    assert "2.00 EUR" in t1 and "(original)" not in t1


def test_merged_source_rotation_and_crop(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    doc = c.doc
    other = doc.add_source(make_doc(str(tmp_path / "b.pdf")))
    ref = PageRef(other, 1, rotation=90, crop=(50, 50, 500, 300))
    doc.pages = [doc.pages[0], ref]
    c.sync_pages()
    edit(c, ref, "Total amount", "Total amount due 3.50 EUR")
    pix, _ = doc.render(ref, 1.0)  # cropped + rotated display renders the slot
    assert pix.width == 250 and pix.height == 450
    out = str(tmp_path / "out.pdf")
    doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    p = d[1]
    assert "3.50 EUR" in p.get_text() and "1,250.00" not in p.get_text()
    assert p.rotation == 90 and "1,250.00" in d[0].get_text()
    hit = p.search_for("3.50 EUR")[0] * p.rotation_matrix
    assert hit.intersects(p.rect)


def test_worker_slot_matches_display(app, tmp_path):
    """The render worker mirrors a slot from Source.upage_payload without running the engine."""
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    ref = c.doc.pages[0]
    edit(c, ref, "Total amount", "Total amount due 4,321.00 EUR")
    payload = ref.src.upage_payload(ref.uid)
    assert payload is not None and payload[0] == 0
    wd = pymupdf.open(stream=ref.src.data, filetype="pdf")
    slots, pages = {}, {}
    RW._install_slot(pymupdf, wd, slots, pages, ref.src.id, ref.uid, payload)
    pno = slots[(ref.src.id, ref.uid)][0]
    assert wd[pno].get_pixmap().samples == ref.src.render_page(0, ref.uid).get_pixmap().samples
    # again (a later revision reuses the slot), then back to the source page
    RW._install_slot(pymupdf, wd, slots, pages, ref.src.id, ref.uid, ref.src.upage_payload(ref.uid))
    assert slots[(ref.src.id, ref.uid)][0] == pno and wd.page_count == pno + 1
    RW._install_slot(pymupdf, wd, slots, pages, ref.src.id, ref.uid, None)
    assert slots[(ref.src.id, ref.uid)][1] is False


def test_worker_process_renders_edit(app, tmp_path):
    c = canvas_for(make_doc(str(tmp_path / "a.pdf")))
    ref = c.doc.pages[0]
    edit(c, ref, "Total amount", "Total amount due 4,321.00 EUR")
    w = RW.RenderWorker()
    try:
        t0 = time.time()
        while not w.usable and time.time() - t0 < 20:
            time.sleep(0.05)
        assert w.usable, w.error
        rid = w.submit(ref.src, 0, 1.0, 0, None, uid=ref.uid)
        got = None
        while got is None and time.time() - t0 < 30:
            for res in w.poll():
                if res[1] == rid:
                    got = res
            time.sleep(0.01)
        assert got is not None and got[0] == "img"
        local = ref.src.render_page(0, ref.uid).get_pixmap(alpha=False)
        assert bytes(got[-1]) == bytes(local.samples_mv)
    finally:
        w.close()


def test_unit_at_is_fast_on_a_dense_page(tmp_path):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    for i in range(70):  # ~70 lines x ~95 chars
        p.insert_text((36, 30 + i * 11.3), (LOREM[(i * 7) % 60:] + " " + LOREM)[:95], fontsize=9)
    path = _save(d, str(tmp_path / "dense.pdf"))
    doc = PdfDocument.open(path)
    ref = doc.pages[0]
    t0 = time.perf_counter()
    u = T.unit_at(doc, ref, 100, 30 + 35 * 11.3 - 3)
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    for k in range(50):
        T.unit_at(doc, ref, 100 + k, 30 + (k % 70) * 11.3 - 3)
    again = (time.perf_counter() - t0) / 50
    assert u is not None
    print(f"dense page: first unit_at {first * 1000:.0f} ms, then {again * 1000:.2f} ms")
    assert first < 0.5 and again < 0.01
