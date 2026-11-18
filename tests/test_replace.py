"""Find & replace: matching over the text as shown (case, whole word, regex with groups, ligatures, lines), replace
one / replace all as page-text edits, refusals counted and listed, one undo step, saving, text boxes and form
fields, cancel, and speed on a 50-page document."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QSettings

from leaf import commands as C
from leaf import replace as R
from leaf import textedit as T
from leaf.document import SEARCH_FLAGS
from tests.test_editor import add, app, canvas_for, pump  # noqa: F401


def make_pdf(path, pages=2, hidden=True):
    d = pymupdf.open()
    for i in range(pages):
        p = d.new_page(width=595, height=842)
        p.insert_text((72, 100), f"The Cat sat on the cat mat, page {i + 1}.", fontsize=12)
        p.insert_text((72, 130), "Concatenate cat and catalog", fontsize=12)
        if hidden:
            p.insert_text((72, 160), "cat hidden under a scan", fontsize=12, render_mode=3)  # refused: invisible
        p.insert_text((72, 190), "the line ends with cat", fontsize=12)
        p.insert_text((72, 210), "mat starts this one", fontsize=12)
        p.insert_text((72, 240), "Invoice 2024-03-15 due 2024-04-01", fontsize=12)
    d.save(path)
    d.close()
    return path


def shown(c, ref) -> list[str]:
    return [s.text for s in T.shown_segments(c.doc, ref)]


# ------------------------------------------------------------------------------------------------ core
def test_patterns_case_word_regex():
    rx = R.compile_query("cat")
    assert [m.group() for m in rx.finditer("Cat cat concatenate")] == ["Cat", "cat", "cat"]
    rx = R.compile_query("cat", case=True)
    assert [m.group() for m in rx.finditer("Cat cat concatenate")] == ["cat", "cat"]
    rx = R.compile_query("cat", word=True)
    assert [m.start() for m in rx.finditer("Cat cat concatenate cat.")] == [0, 4, 20]
    rx = R.compile_query("a  b")  # any whitespace run where the query has spaces
    assert rx.search("a\nb") and rx.search("a b")
    rx = R.compile_query(r"(\d{4})-(\d\d)-(\d\d)", regex=True)
    m = rx.search("on 2024-03-15")
    assert R.expand(m, r"\3.\2.\1", True) == "15.03.2024" == R.expand(m, "$3.$2.$1", True)
    assert R.expand(m, "$$1", True) == "$1"
    with pytest.raises(R.PatternError, match="regular expression"):
        R.compile_query("(", regex=True)
    with pytest.raises(R.PatternError, match="replacement"):
        R.check_template(rx, r"\4", True)
    R.check_template(rx, r"\4", False)  # plain replacement: taken literally


def test_ligatures_match_their_letters():
    norm, pos = R.normalize("ﬁnd the ﬂow")
    assert norm == "find the flow" and pos[:3] == [0, 0, 1]
    # a hit cutting through a ligature keeps the rest of it
    h = R.Hit("text", 0, 0, 1, None, None, lead="", trail="i")
    assert R.substitute("ﬁnd", [h], "F", False) == "Find"


def test_page_hits_lines_spans_and_multiple_per_line(app, tmp_path):
    c = canvas_for(make_pdf(str(tmp_path / "a.pdf"), pages=1))
    ref = c.doc.pages[0]
    segs, hits = R.page_hits(c.doc, ref, R.compile_query("cat", word=True))
    texts = [segs[h.seg].text for h in hits if h.kind == "text"]
    assert texts.count("The Cat sat on the cat mat, page 1.") == 2  # two hits on one line
    assert "Concatenate cat and catalog" in texts and "the line ends with cat" in texts
    assert all(h.rect and h.rect[2] > h.rect[0] for h in hits)
    # a match across two lines is found as a span (counted, never replaced)
    segs, hits = R.page_hits(c.doc, ref, R.compile_query("cat mat"))
    kinds = sorted(h.kind for h in hits)
    assert kinds == ["span", "text"]
    res = R.plan_page(c.doc, ref, 1, R.compile_query("cat mat"), "dog rug", False)
    assert res.lines == 1 and [s.label for s in res.skipped] == [R.SPANS]
    # regex: ^ and $ are per line
    segs, hits = R.page_hits(c.doc, ref, R.compile_query(r"^mat", regex=True))
    assert [segs[h.seg].text for h in hits] == ["mat starts this one"]


# ----------------------------------------------------------------------------------------- canvas level
def test_replace_all_plan_refused_line_and_one_undo_step(app, tmp_path):
    c = canvas_for(make_pdf(str(tmp_path / "a.pdf")))
    rx = R.compile_query("cat", word=True)
    tot = R.Totals()
    for i, ref in enumerate(c.doc.pages):
        tot.add(i + 1, R.plan_page(c.doc, ref, i + 1, rx, "dog", False))
    # per page: 2 + 1 + 1 hits in 3 lines; the invisible line is refused
    assert tot.replaced == 8 and tot.lines == 6
    assert [(s.pno, s.label) for s in tot.skipped] == [(1, "invisible (OCR) text"), (2, "invisible (OCR) text")]
    assert tot.headline() == "Replaced 8 in 6 lines; 2 lines skipped: invisible (OCR) text (pp. 1, 2)."
    before = c.undo.count()
    cmd = C.EditPageTexts(c, tot.changes, "Replace all")
    c.undo.push(cmd)
    assert c.undo.count() == before + 1 and not cmd.failed
    for ref in c.doc.pages:
        assert shown(c, ref)[:2] == [f"The dog sat on the dog mat, page {ref.index + 1}.", "Concatenate dog and catalog"]
        assert c.doc.text_page(ref).search_for("dog and catalog", flags=SEARCH_FLAGS)
    c.undo.undo()
    assert c.doc.text_edits == {}
    for ref in c.doc.pages:
        assert "Concatenate cat and catalog" in shown(c, ref)
        assert not c.doc.text_page(ref).search_for("dog", flags=SEARCH_FLAGS)
    c.undo.redo()
    assert len(c.doc.text_edits) == 2 and all(len(v) == 3 for v in c.doc.text_edits.values())


def test_respects_existing_edit_and_hits_follow_new_text(app, tmp_path):
    c = canvas_for(make_pdf(str(tmp_path / "a.pdf"), pages=1))
    ref = c.doc.pages[0]
    u = T.unit_at(c.doc, ref, 100, 125)
    assert u.text == "Concatenate cat and catalog"
    c.undo.push(C.EditPageText(c, ref, u, "Concatenate cat and cat food"))
    rx = R.compile_query("cat", word=True)
    segs, hits = R.page_hits(c.doc, ref, rx)
    edited = [h for h in hits if segs[h.seg].edited]
    assert len(edited) == 2 and all(h.rect for h in edited)  # hits come from the edited text, as shown
    res = R.plan_page(c.doc, ref, 1, rx, "dog", False)
    new = dict((k, t) for _, k, t in res.changes)
    assert new[u.key] == "Concatenate dog and dog food"  # same key: the edit is replaced, not stacked
    c.undo.push(C.EditPageTexts(c, res.changes))
    assert c.doc.text_edits[ref.uid][u.key] == "Concatenate dog and dog food"
    segs, hits = R.page_hits(c.doc, ref, rx)
    assert [segs[h.seg].text for h in hits] == ["cat hidden under a scan"]  # only the refused line is left
    segs, hits = R.page_hits(c.doc, ref, R.compile_query("dog food"))
    assert len(hits) == 1 and hits[0].kind == "text"
    c.undo.undo()
    assert c.doc.text_edits[ref.uid][u.key] == "Concatenate cat and cat food"


def test_batch_command_drops_a_page_that_fails(app, tmp_path, monkeypatch):
    c = canvas_for(make_pdf(str(tmp_path / "a.pdf")))
    rx = R.compile_query("cat", word=True)
    changes = []
    for i, ref in enumerate(c.doc.pages):
        changes += R.plan_page(c.doc, ref, i + 1, rx, "dog", False).changes
    bad = c.doc.pages[1]
    real = c.sync_text

    def sync(ref):
        if ref.uid == bad.uid and c.doc.text_edits.get(ref.uid):
            raise T.Refuse("boom")
        return real(ref)

    monkeypatch.setattr(c, "sync_text", sync)
    cmd = C.EditPageTexts(c, changes)
    c.undo.push(cmd)
    assert cmd.failed == {bad.uid: "boom"}
    assert bad.uid not in c.doc.text_edits and len(c.doc.text_edits[c.doc.pages[0].uid]) == 3
    c.undo.undo()
    assert c.doc.text_edits == {}


# ------------------------------------------------------------------------------------------------- window
@pytest.fixture
def win(app, tmp_path):
    from leaf.window import MainWindow

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafReplaceTest")
    QSettings().clear()
    w = MainWindow()
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    if w.replacer.summary is not None:
        w.replacer.summary.close()
    w.close()
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


def search(app, w, text):
    w.find.edit.setText(text)
    w.find._debounce.stop()
    w.find._start()
    for _ in range(500):
        if not w.find._todo:
            break
        app.processEvents()
    return w.find


def test_action_shortcut_and_row(app, win, tmp_path):
    w = win
    assert w.a_replace.objectName() == "a_replace" and w.a_replace.shortcut().toString() == "Ctrl+H"
    assert w.a_replace in w.menus["edit"].actions()
    assert not w.a_replace.isEnabled()
    w.open_path(make_pdf(str(tmp_path / "a.pdf"), pages=1))
    assert w.a_replace.isEnabled()
    from leaf import shortcuts

    assert "Ctrl+H" not in shortcuts.duplicates(w)
    w.a_replace.trigger()
    assert not w.find.isHidden() and not w.replacer.row.isHidden() and w.find.provider is not None
    w.a_find.trigger()  # Ctrl+F: find only
    assert w.replacer.row.isHidden() and w.find.provider is None
    w.replacer.set_options(case=True)
    assert w.find.provider is not None  # an option on: Leaf's matching
    f = search(app, w, "Cat")
    assert len(f.hits) == 1
    w.replacer.set_options(case=False)
    f = search(app, w, "cat")
    assert w.find.provider is None and len(f.hits) >= 6  # MuPDF's search again (substrings, any case)
    w.find.close_bar()
    assert w.replacer.row.isHidden()


def test_replace_one_moves_on_and_undoes(app, win, tmp_path):
    w = win
    w.open_path(make_pdf(str(tmp_path / "a.pdf"), pages=2))
    ctl = w.replacer
    w.a_replace.trigger()
    ctl.set_options(word=True)
    f = search(app, w, "cat")
    assert len(f.hits) == 10 and f.cur == 0  # per page: Cat, cat, cat, (invisible) cat, cat
    ctl.row.edit.setText("dog")
    ref = w.canvas.doc.pages[0]
    assert ctl.replace_one()
    assert shown(w.canvas, ref)[0] == "The dog sat on the cat mat, page 1."
    assert len(f.hits) == 9 and f.cur == 0  # next hit: the second one of the same line
    pg, k, segs, h = ctl.current()
    assert segs[h.seg].text == "The dog sat on the cat mat, page 1." and h.start == 19
    assert ctl.replace_one()
    assert shown(w.canvas, ref)[0] == "The dog sat on the dog mat, page 1."
    assert w.canvas.undo.count() == 2
    assert ctl.replace_one()  # Concatenate cat ...
    # the refused (invisible) line: skipped with the reason, the find bar moves on
    assert not ctl.replace_one()
    assert "invisible" in ctl.row.status.text()
    pg, k, segs, h = ctl.current()
    assert segs[h.seg].text == "the line ends with cat"
    w.canvas.undo.undo()
    assert shown(w.canvas, ref)[1] == "Concatenate cat and catalog"
    for _ in range(50):
        app.processEvents()


def test_replace_all_window_summary_undo_save(app, win, tmp_path):
    w = win
    w.open_path(make_pdf(str(tmp_path / "a.pdf"), pages=3))
    ctl = w.replacer
    w.a_replace.trigger()
    ctl.set_options(regex=True)
    search(app, w, r"(\d{4})-(\d\d)-(\d\d)")
    ctl.row.edit.setText(r"\3.\2.\1")
    n0 = w.canvas.undo.count()
    job = ctl.replace_all(wait=True)
    assert job.finished and not job.cancelled and job.totals.replaced == 6 and job.totals.lines == 3
    assert w.canvas.undo.count() == n0 + 1
    for ref in w.canvas.doc.pages:
        assert "Invoice 15.03.2024 due 01.04.2024" in shown(w.canvas, ref)
    # word + plain replace all with a refused line: summary lists the skipped spots
    ctl.set_options(regex=False, word=True)
    search(app, w, "cat")
    ctl.row.edit.setText("dog")
    job = ctl.replace_all(wait=True)
    t = job.totals
    assert t.replaced == 12 and t.lines == 9 and len(t.skipped) == 3
    assert "3 lines skipped: invisible (OCR) text (pp. 1, 2, 3)" in t.headline()
    dlg = ctl.summary
    assert dlg is not None and dlg.list.count() >= 3 and "p. 1" in dlg.list.item(0).text()
    dlg._go(dlg.list.item(2))  # jumps (no error)
    assert w.canvas.undo.count() == n0 + 2
    # the find bar searched again: only the refused lines still match
    for _ in range(200):
        app.processEvents()
        if not w.find._todo and not w.find._debounce.isActive():
            break
    assert len(w.find.hits) == 3
    out = str(tmp_path / "out.pdf")
    assert w.canvas.doc.save(out, w.canvas.overlays_for) is not None
    d = pymupdf.open(out)
    for p in d:
        assert p.search_for("Concatenate dog and catalog", flags=SEARCH_FLAGS)
        assert p.search_for("15.03.2024", flags=SEARCH_FLAGS)
        assert not p.search_for("2024-03-15", flags=SEARCH_FLAGS)
    d.close()
    # one undo step restores every line
    w.canvas.undo.undo()
    for ref in w.canvas.doc.pages:
        assert "Concatenate cat and catalog" in shown(w.canvas, ref)
        assert "Invoice 15.03.2024 due 01.04.2024" in shown(w.canvas, ref)
    w.canvas.undo.undo()
    assert w.canvas.doc.text_edits == {}


def test_cancel_leaves_document_untouched(app, win, tmp_path, monkeypatch):
    from leaf.features import replace as FR

    monkeypatch.setattr(FR, "SLICE", 0.0)  # one page per event-loop turn
    w = win
    w.open_path(make_pdf(str(tmp_path / "a.pdf"), pages=4))
    ctl = w.replacer
    w.a_replace.trigger()
    search(app, w, "cat")
    ctl.row.edit.setText("dog")
    job = ctl.replace_all()
    job.timer.stop()
    job._tick()
    assert job.i == 1 and not job.finished
    job.cancel()
    assert job.cancelled and ctl.job is None
    assert w.canvas.doc.text_edits == {} and w.canvas.undo.count() == 0
    assert "nothing was changed" in ctl.row.status.text()


def test_text_boxes_and_form_fields(app, win, tmp_path):
    from leaf.items import TextItem

    src = str(tmp_path / "f.pdf")
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 100), "No match here", fontsize=12)
    wd = pymupdf.Widget()
    wd.field_name, wd.field_type = "city", pymupdf.PDF_WIDGET_TYPE_TEXT
    wd.rect = pymupdf.Rect(72, 300, 300, 320)
    wd.field_value = "Old Town cat cafe"
    p.add_widget(wd)
    d.save(src)
    d.close()
    w = win
    w.open_path(src)
    cv = w.canvas
    box = add(cv, 0, TextItem(QPointF(100, 400), {}, "A cat in a box, another cat"))
    ctl = w.replacer
    w.a_replace.trigger()
    ctl.set_options(boxes=True, fields=True)
    f = search(app, w, "cat")
    assert len(f.hits) == 3
    ctl.row.edit.setText("dog")
    n0 = cv.undo.count()
    job = ctl.replace_all(wait=True)
    t = job.totals
    assert (t.box_hits, t.boxes, t.field_hits, t.fields, t.replaced) == (2, 1, 1, 1, 0)
    assert box.document().toPlainText() == "A dog in a box, another dog"
    key = next(iter(cv.doc.form_values))
    assert cv.doc.form_values[key] == "Old Town dog cafe"
    assert cv.undo.count() == n0 + 1
    cv.undo.undo()
    assert box.document().toPlainText() == "A cat in a box, another cat" and not cv.doc.form_values
    # without the boxes ticked, they are left alone
    ctl.set_options(boxes=False, fields=False)
    job = ctl.replace_all(wait=True)
    assert job.totals.total == 0 and box.document().toPlainText() == "A cat in a box, another cat"


def test_fifty_pages_is_interactive(app, tmp_path):
    path = str(tmp_path / "big.pdf")
    d = pymupdf.open()
    for i in range(50):
        p = d.new_page(width=595, height=842)
        for k in range(40):
            p.insert_text((50, 60 + k * 18), f"Line {k} of page {i + 1}: the quick brown fox and the lazy fox.",
                          fontsize=11)
    d.save(path)
    d.close()
    c = canvas_for(path)
    rx = R.compile_query("fox", word=True)
    t0 = time.perf_counter()
    tot = R.Totals()
    for i, ref in enumerate(c.doc.pages):
        if i % 5 == 0:  # every 5th page has many hits, others none: typical sparse document plus dense pages
            tot.add(i + 1, R.plan_page(c.doc, ref, i + 1, rx, "cat", False))
        else:
            tot.add(i + 1, R.plan_page(c.doc, ref, i + 1, R.compile_query("zebra"), "cat", False))
    scan = time.perf_counter() - t0
    assert tot.replaced == 10 * 80 and tot.lines == 400 and not tot.skipped
    t0 = time.perf_counter()
    c.undo.push(C.EditPageTexts(c, tot.changes))
    apply = time.perf_counter() - t0
    t0 = time.perf_counter()
    c.undo.undo()
    undo = time.perf_counter() - t0
    print(f"\n50 pages, 400 lines: scan {scan:.2f}s, apply {apply:.2f}s, undo {undo:.2f}s")
    assert scan < 15 and apply < 15 and undo < 15  # (generous: CI machines; measured locally in the report)


def test_summary_aggregates_warnings_and_skips():
    t = R.Totals()
    a = R.PageResult(replaced=3, lines=2, page_warnings=["This document is digitally signed: …"],
                     fallback={("Helvetica", "Arial"): {"→"}})
    b = R.PageResult(replaced=1, lines=1, page_warnings=["This document is digitally signed: …"],
                     fallback={("Helvetica", "Arial"): {"←"}},
                     skipped=[R.Skip(2, 7, None, T.COMPLEX, R.short_reason(T.COMPLEX), 2, "…")])
    t.add(1, a)
    t.add(2, b)
    w = t.warnings()
    assert w[0] == "This document is digitally signed: …"  # once, not per page
    fb = [x for x in w if "Arial" in x]
    assert len(fb) == 1 and "←→" in fb[0] and fb[0].endswith("(pp. 1, 2)")
    assert t.headline() == "Replaced 4 in 3 lines; 1 line skipped: unsupported script (p. 2)."
    t.fail_page(7, 2, "boom")
    assert t.skipped[-1].label == "can’t be edited"
