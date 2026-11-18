"""Text box formatting: fonts (base + embedded system fonts), character runs, paragraph and box styles, undo,
defaults. Everything checked in the saved PDF."""
from __future__ import annotations

import os
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QSettings, Qt
from PySide6.QtGui import QTextCursor

from leaf import fonts, richtext
from leaf.items import TextItem, editing_text_item
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401

BOLD, ITALIC = 16, 2

LONG = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore "
        "magna aliqua Ut enim ad minim veniam quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo "
        "consequat Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur")


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path, rotate_second=True, form=False)
    return path


def save(c, tmp_path, name="out.pdf"):
    out = str(tmp_path / name)
    c.doc.save(out, c.overlays_for)
    return pymupdf.open(out)


def spans(page):
    return [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"] if s["text"].strip()]


def span_of(page, word):
    return next(s for s in spans(page) if word in s["text"])


def select(item, start, end):
    c = QTextCursor(item.document())
    c.setPosition(start)
    c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    return c


def system_family():
    """An installed, embeddable, non-base family (None if the machine has none)."""
    ix = fonts.index()
    for fam in ("Georgia", "Verdana", "DejaVu Serif", "DejaVu Sans", "Liberation Serif", "Noto Serif", "Cambria",
                "Segoe UI", "Ubuntu", "Cantarell"):
        if fonts.embeddable(fam) and ix.find(fam, False, False):
            return fam
    return None


# ------------------------------------------------------------------ runs
def test_char_formatting_lands_in_pdf(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    box = add(c, 0, TextItem(QPointF(80, 200), {"size": 16, "color": "#1f4fd1", "italic": True}, "Plain words here"))
    richtext.format_cursor(select(box, 6, 11), {"bold": True, "color": "#d43a2a", "size": 20}, box.props)
    richtext.format_cursor(select(box, 12, 16), {"underline": True, "strike": True, "italic": False}, box.props)
    assert box.props["size"] == 16 and not box.props["bold"]  # runs don't touch the box style
    d = save(c, tmp_path)
    p = d[0]
    plain, bold, here = span_of(p, "Plain"), span_of(p, "words"), span_of(p, "here")
    assert plain["flags"] & ITALIC and not plain["flags"] & BOLD
    assert plain["color"] == 0x1F4FD1 and abs(plain["size"] - 16) < 0.01
    assert bold["flags"] & BOLD and bold["flags"] & ITALIC
    assert bold["color"] == 0xD43A2A and abs(bold["size"] - 20) < 0.01
    assert not here["flags"] & ITALIC
    # underline + strike are drawn as lines across the word
    hb = pymupdf.Rect(here["bbox"])
    lines = [dr for dr in p.get_drawings() if dr["rect"].x0 < hb.x1 and dr["rect"].x1 > hb.x0
             and hb.y0 - 2 < dr["rect"].y0 < hb.y1 + 4]
    assert lines, "no decoration lines drawn for underline/strike"
    # same baseline for the line despite mixed sizes, and it matches Qt's layout
    assert abs(plain["origin"][1] - bold["origin"][1]) < 0.01
    tl = box.document().begin().layout()
    qt_base = 200 + tl.position().y() + tl.lineAt(0).y() + tl.lineAt(0).ascent()
    assert abs(plain["origin"][1] - qt_base) < 0.6


def test_base_fonts_and_alignment(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    text = "a much longer first line\nshort"
    for i, (font, align) in enumerate([("Helvetica", "left"), ("Times", "center"), ("Courier", "right")]):
        add(c, 0, TextItem(QPointF(60, 300 + 60 * i), {"font": font, "align": align, "size": 11}, text))
    d = save(c, tmp_path)
    p = d[0]
    by_line = {}
    for s in spans(p):
        if 290 < s["origin"][1] < 460:
            key = "long" if "longer" in s["text"] else "short" if "short" in s["text"] else None
            if key:
                by_line.setdefault(s["font"], {})[key] = pymupdf.Rect(s["bbox"])
    helv = next(v for k, v in by_line.items() if "NimbusSans" in k.replace(" ", ""))
    times = next(v for k, v in by_line.items() if "NimbusRoman" in k.replace(" ", ""))
    mono = next(v for k, v in by_line.items() if "NimbusMono" in k.replace(" ", ""))
    assert abs(helv["long"].x0 - helv["short"].x0) < 1  # left
    assert abs((times["long"].x0 + times["long"].x1) / 2 - (times["short"].x0 + times["short"].x1) / 2) < 1.5  # center
    assert abs(mono["long"].x1 - mono["short"].x1) < 1.5  # right
    assert mono["short"].x0 > mono["long"].x0 + 20


def test_justify_and_spacing(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    box = add(c, 0, TextItem(QPointF(60, 420), {"size": 12, "box_w": 180, "align": "justify"}, LONG[:160]))
    box2 = add(c, 0, TextItem(QPointF(320, 420), {"size": 12, "spacing": 2.0}, "one\ntwo"))
    d = save(c, tmp_path)
    p = d[0]
    rows = {}
    for s in spans(p):
        if 400 < s["origin"][1] < 700 and s["bbox"][0] < 300:
            rows.setdefault(round(s["origin"][1], 1), []).append(s["bbox"])
    ends = [max(b[2] for b in v) for _, v in sorted(rows.items())]
    assert len(ends) >= 3
    right = 60 + 180 - box.props["pad"]
    for e in ends[:-1]:  # every line but the last reaches the right edge
        assert abs(e - right) < 2.5, (ends, right)
    one, two = span_of(p, "one"), span_of(p, "two")
    gap = two["origin"][1] - one["origin"][1]
    tl1 = box2.document().begin().layout()
    tl2 = box2.document().begin().next().layout()
    assert abs(gap - (tl2.position().y() - tl1.position().y())) < 0.5
    assert gap > 2 * 12  # double spacing


def test_wrapped_long_text_not_clipped(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    add(c, 0, TextItem(QPointF(70, 60), {"size": 12, "box_w": 180}, LONG))
    add(c, 1, TextItem(QPointF(70, 60), {"size": 12, "box_w": 180, "align": "right"}, LONG))  # page with /Rotate 90
    d = save(c, tmp_path)
    for i in (0, 1):
        p = d[i]
        squashed = "".join(p.get_text().split())
        assert "".join(LONG.split()) in squashed, i  # every character, in order (wrap points may differ)
        sizes = {round(s["size"], 2) for s in spans(p) if "Lorem" in s["text"] or "pariatur" in s["text"]}
        assert sizes == {12.0}, sizes  # never shrunk to fit


def test_devanagari_and_cjk_still_render(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    add(c, 0, TextItem(QPointF(80, 600), {"size": 14, "bold": True}, "नमस्ते दुनिया 你好"))
    d = save(c, tmp_path)
    text = d[0].get_text()
    assert "न" in text and "ते" in text and "你好" in text  # MuPDF's ToUnicode for conjuncts is lossy; glyphs are there
    assert any("Devanagari" in f[3] for f in d.get_page_fonts(0))


def test_box_fill_and_border(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    box = add(c, 0, TextItem(QPointF(100, 500), {"bg": "#fff3bf", "border": "#d43a2a", "pad": 6}, "Boxed"))
    r = box.mapRectToParent(box.box_rect())
    d = save(c, tmp_path)
    draws = d[0].get_drawings()
    fill = next(dr for dr in draws if dr.get("fill") and abs(dr["fill"][2] - 0xBF / 255) < 0.01)
    assert abs(fill["rect"].x0 - r.left()) < 0.5 and abs(fill["rect"].y1 - r.bottom()) < 0.5
    assert any(dr.get("color") and abs(dr["color"][0] - 0xD4 / 255) < 0.01 for dr in draws)
    assert d[0].search_for("Boxed")


# ----------------------------------------------------------------- fonts
def test_system_font_embedded_or_fallback(app, pdf, tmp_path):  # noqa: F811
    c = canvas_for(pdf)
    fam = system_family()
    if fam:
        add(c, 0, TextItem(QPointF(80, 200), {"font": fam, "size": 14}, "System font"))
    add(c, 0, TextItem(QPointF(80, 300), {"font": "No Such Font Family 42", "size": 14}, "Fallback"))
    d = save(c, tmp_path)
    names = " ".join(f[3] for f in d.get_page_fonts(0))
    if fam:
        assert fam.replace(" ", "") in names.replace(" ", ""), (fam, names)
        assert span_of(d[0], "System")["font"].replace("-", "").startswith(fam.replace(" ", ""))
    assert span_of(d[0], "Fallback")["font"].startswith("Nimbus"), names
    assert not fonts.embeddable("No Such Font Family 42")
    assert fonts.fallback("Some Mono Code") == "Courier"


def test_font_index_cache(app, tmp_path):  # noqa: F811
    ix = fonts.index()
    faces = [f for f in ix.load().faces if fonts._usable(f) and os.path.getsize(f["path"]) < 600_000]
    if not faces:
        pytest.skip("no system fonts")
    src = min(faces, key=lambda f: os.path.getsize(f["path"]))
    d = tmp_path / "fonts"
    d.mkdir()
    shutil.copy(src["path"], d / os.path.basename(src["path"]))
    cache = str(tmp_path / "cache" / "index.json")
    a = fonts.FontIndex([str(d)], cache).load()
    assert a.faces and os.path.exists(cache)
    fam = a.faces[0]["families"][0]
    assert a.find(fam, False, False) is not None and a.find(fam.upper(), True, True) is not None
    b = fonts.FontIndex([str(d)], cache)
    orig = fonts.scan_file
    fonts.scan_file = lambda p: (_ for _ in ()).throw(AssertionError("cache not used"))
    try:
        b.load()
    finally:
        fonts.scan_file = orig
    assert b.faces == a.faces


# ------------------------------------------------------------- undo/edit
def test_box_format_undo_restores_runs(app, pdf):  # noqa: F811
    c = canvas_for(pdf)
    box = add(c, 0, TextItem(QPointF(80, 200), {}, "one two three"))
    richtext.format_cursor(select(box, 4, 7), {"italic": True}, box.props)
    with_run = box.content()
    c.scene().clearSelection()
    box.setSelected(True)
    c.apply_props({"italic": True})  # whole box: the run override goes away
    assert box.props["italic"] and not richtext.has_overrides(box.document(), ["italic"])
    c.apply_props({"bold": True})
    assert box.props["bold"]
    c.undo.undo()
    assert not box.props["bold"] and box.props["italic"]
    c.undo.undo()
    assert not box.props["italic"] and box.content() == with_run
    c.undo.redo()
    assert box.props["italic"] and not richtext.has_overrides(box.document(), ["italic"])


def test_format_selection_while_editing(app, pdf):  # noqa: F811
    c = canvas_for(pdf)
    c.show()
    c.activateWindow()
    c.setFocus()
    pump(app, 0.05)
    box = add(c, 0, TextItem(QPointF(80, 200), {}, "alpha beta"))
    c.scene().clearSelection()
    box.setSelected(True)
    box.start_edit()
    box.setTextCursor(select(box, 6, 10))
    assert editing_text_item(c.scene()) is box
    n = c.undo.count()
    c.apply_props({"bold": True, "align": "center"})
    assert not box.props["bold"] and box.props["align"] == "center"  # char prop -> run, box prop -> SetProps
    assert c.undo.count() == n + 1
    assert box.cursor_props()["bold"]
    first = next(iter(box.document().begin())).fragment()
    assert first.text() == "alpha " and richtext.run_props(first.charFormat()) == {}
    box.document().undo()  # formatting while editing uses the document's own undo
    assert not richtext.has_overrides(box.document(), ["bold"])
    box.document().redo()
    assert richtext.has_overrides(box.document(), ["bold"])
    box.clearFocus()  # finish: one "Edit text" step that keeps the formatting
    pump(app, 0.05)
    assert not box.editing() and c.undo.undoText() == "Edit text"
    assert richtext.has_overrides(box.document(), ["bold"])
    c.undo.undo()
    assert not richtext.has_overrides(box.document(), ["bold"]) and box.toPlainText() == "alpha beta"
    c.undo.redo()
    assert richtext.has_overrides(box.document(), ["bold"])
    c.close()


def test_clone_and_width(app, pdf):  # noqa: F811
    from leaf.features.history import clone

    c = canvas_for(pdf)
    box = add(c, 0, TextItem(QPointF(80, 200), {"border": "#1a1a1a"}, "copy me please"))
    richtext.format_cursor(select(box, 0, 4), {"strike": True}, box.props)
    twin = clone(box)
    assert twin.content() == box.content() and twin.props == box.props
    w0 = box.box_rect().width()
    box._set_box_w(dict(box.props), 60.0)
    assert box.props["box_w"] == 60 and abs(box.box_rect().width() - 60) < 0.01
    assert box.box_rect().height() > 20  # wrapped
    c.undo.undo()
    assert box.props["box_w"] == 0 and abs(box.box_rect().width() - w0) < 0.5


# ------------------------------------------------------------- UI/defaults
@pytest.fixture
def win(app):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafTextFmtTest")
    QSettings().clear()
    from leaf.window import MainWindow

    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


def test_defaults_persist_and_old_settings_load(app, win, pdf):  # noqa: F811
    from leaf.window import MainWindow

    win.open_path(pdf)
    cv = win.canvas
    cv.set_tool("text")
    win.props.font_box.picked.emit("Times")
    win.props.style_btns["bold"].click()
    win.props.align_btns["center"].click()
    assert cv.defaults["text"]["font"] == "Times" and cv.defaults["text"]["bold"] and cv.defaults["text"]["align"] == "center"
    win._save_defaults()
    w2 = MainWindow()
    try:
        d = w2.canvas.defaults["text"]
        assert d["font"] == "Times" and d["bold"] is True and d["align"] == "center" and d["size"] == 12
    finally:
        w2.close()
    QSettings().setValue("defaults/text", {"color": "#ff0000", "size": 20})  # settings from an older Leaf
    w3 = MainWindow()
    try:
        d = w3.canvas.defaults["text"]
        assert d["color"] == "#ff0000" and d["size"] == 20 and d["font"] == "Helvetica" and d["bold"] is False
        item = TextItem(QPointF(0, 0), d, "x")
        assert item.props["bg"] == "" and item.props["spacing"] == 1.0
    finally:
        w3.close()


def test_panel_minibar_and_shortcuts(app, win, pdf):  # noqa: F811
    win.open_path(pdf)
    cv = win.canvas
    box = add(cv, 0, TextItem(QPointF(100, 200), {}, "Shortcut text"))
    cv.set_tool("select")
    cv.scene().clearSelection()
    box.setSelected(True)
    pump(app, 0.1)
    assert win.props.font_box.isVisibleTo(win.props) and win.props.style_btns["bold"].isVisibleTo(win.props)
    win.a_text_bold.trigger()
    assert box.props["bold"] and win.props.style_btns["bold"].isChecked()
    win.a_text_italic.trigger()
    win.a_text_underline.trigger()
    win.a_text_strike.trigger()
    assert box.props["italic"] and box.props["underline"] and box.props["strike"]
    win.a_text_bold.trigger()
    assert not box.props["bold"]
    cv.undo.undo()
    assert box.props["bold"]
    win.a_text_align_right.trigger()
    assert box.props["align"] == "right"
    mb = win.minibar
    mb.refresh()
    assert mb.font_box.isVisibleTo(mb) and mb.style_btns["bold"].isChecked()
    mb.apply({"bg": "#d0ebff"})
    assert box.props["bg"] == "#d0ebff"
    keys = {a.objectName(): a.shortcut().toString() for a in (win.a_text_bold, win.a_text_italic, win.a_text_underline,
                                                              win.a_text_strike)}
    assert keys["a_text_bold"] == "Ctrl+B" and keys["a_text_strike"] == "Ctrl+Shift+X"
    from leaf import shortcuts

    dups = {seq: [a.objectName() for a in acts] for seq, acts in shortcuts.duplicates(win).items()}
    assert not any(n.startswith("a_text_") for names in dups.values() for n in names), dups
    # editing: the minibar stays for the selection and the panel shows the format at the cursor
    box.start_edit()
    box.setTextCursor(select(box, 0, 8))
    cv.selectionInfo.emit()
    pump(app, 0.05)
    assert mb.isVisible() and not mb.del_btn.isVisibleTo(mb)
    win.a_text_bold.trigger()
    assert box.cursor_props()["bold"] == (not box.props["bold"]) or richtext.has_overrides(box.document(), ["bold"])
    box.clearFocus()
    pump(app, 0.05)


def test_text_box_fonts_subset_and_searchable(app, pdf, tmp_path):  # noqa: F811
    """Text boxes embed subset fonts (not ~1 MB per box) and ligature words stay findable in Leaf."""
    import os

    from leaf.document import SEARCH_FLAGS

    c = canvas_for(pdf)
    for k in range(3):
        add(c, 0, TextItem(QPointF(60, 300 + 40 * k), {"font": "Helvetica", "size": 12}, f"first office {k}"))
    out = str(tmp_path / "subset.pdf")
    c.doc.save(out, c.overlays_for)
    assert os.path.getsize(out) - os.path.getsize(pdf) < 120_000
    d = pymupdf.open(out)
    assert len(d[0].search_for("office", flags=SEARCH_FLAGS)) == 3
    assert any("+" in f[3] for f in d[0].get_fonts())  # subset-tagged font names
    d.close()
