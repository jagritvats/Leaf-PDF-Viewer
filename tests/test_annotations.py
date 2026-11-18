"""Existing annotations (adopt, edit in place, delete), sticky notes, replies and the Comments panel."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRectF

from leaf import annots as A
from leaf import commands as C
from leaf.document import PageRef
from leaf.items import HighlightItem, ShapeItem
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


def annotate(path, rotate_second=False, tag="A"):
    """3 pages; page 0 gets a Square, Ink, Highlight and a Text note with a reply; page 2 a Circle."""
    make_pdf(path, rotate_second=rotate_second, form=False)
    d = pymupdf.open(path)
    p = d[0]
    a = p.add_rect_annot(pymupdf.Rect(100, 300, 220, 360))
    a.set_colors(stroke=(1, 0, 0))
    a.set_info(content=f"{tag} square", title="Bob")
    a.update()
    a = p.add_ink_annot([[(300, 300), (320, 310), (340, 330)], [(300, 350), (330, 360)]])
    a.set_colors(stroke=(0, 0, 1))
    a.set_info(title="Carol")
    a.update()
    q = p.search_for("quick brown")[0]
    a = p.add_highlight_annot(q)
    a.set_info(title="Bob")
    a.update()
    n = p.add_text_annot((400, 500), f"{tag} note text")
    n.set_info(title="Alice")
    n.update()
    r = p.add_text_annot((400, 500), "a reply")
    r.set_info(title="Bob")
    r.set_irt_xref(n.xref)
    r.update()
    c = d[2].add_circle_annot(pymupdf.Rect(50, 50, 150, 120))
    c.set_info(content=f"{tag} circle")
    c.update()
    d.saveIncr()
    d.close()


@pytest.fixture
def apdf(tmp_path):
    path = str(tmp_path / "annots.pdf")
    annotate(path)
    return path


def snapshot(path):
    """xref -> (subtype, object source, appearance stream) for every annotation of every page."""
    d = pymupdf.open(path)
    out = {}
    for page in d:
        for a in page.annots():
            ap = d.xref_get_key(a.xref, "AP/N")
            stream = d.xref_stream(int(ap[1].split()[0])) if ap[0] == "xref" else None
            out[a.xref] = (a.type[1], d.xref_object(a.xref), stream, a.info.get("content"), page.number)
    d.close()
    return out


def by_kind(items, kind):
    return next(i for i in items if i.kind == kind)


def test_adopt_page(app, apdf):
    c = canvas_for(apdf)
    items = c.annots.ensure(c.order[0])
    kinds = sorted(i.kind for i in items)
    assert kinds == ["highlight", "ink", "note", "rect", "reply"], kinds
    sq = by_kind(items, "rect")
    assert sq.meta["author"] == "Bob" and sq.meta["contents"] == "A square"
    assert abs(sq.scene_rect_in_parent().left() - 99) < 2 and abs(sq.scene_rect_in_parent().top() - 299) < 2
    assert sq.props["color"] == "#ff0000"
    hl = by_kind(items, "highlight")
    assert "quick brown" in hl.marked_text
    reply = by_kind(items, "reply")
    assert reply.target is by_kind(items, "note") and reply.meta["contents"] == "a reply"
    assert not any(i.is_dirty() for i in items)
    assert c.overlays_for(c.doc.pages[0]) == []  # untouched annotations aren't rewritten
    assert c.order[1].adopted is None  # lazily, per page


def test_untouched_saved_byte_for_byte(app, apdf, tmp_path):
    c = canvas_for(apdf)
    for pg in c.order:
        c.annots.ensure(pg)
    add(c, 1, ShapeItem("rect", QRectF(10, 10, 20, 20), {}))  # an unrelated edit, so something is saved
    out = str(tmp_path / "out.pdf")
    assert c.doc.save(out, c.overlays_for) is True
    before, after = snapshot(apdf), snapshot(out)
    for xref, v in before.items():
        assert after[xref] == v, xref


def _edit_and_check(c, out, inc_expected, square_page=0):
    """Move the square, recolor the ink, delete the highlight; returns the saved snapshot."""
    pg = c.order[square_page]
    items = c.annots.ensure(pg)
    sq, ink, hl = by_kind(items, "rect"), by_kind(items, "ink"), by_kind(items, "highlight")
    c.undo.push(C.MoveItems({sq: (QPointF(sq.pos()), sq.pos() + QPointF(40, 25))}))
    ink.setSelected(True)
    c.apply_props({"color": "#00ff00"})
    ink.setSelected(False)
    c.undo.push(C.RemoveItems([hl]))
    c.annots.sync()
    assert c.doc.save(out, c.overlays_for) is inc_expected
    return sq, ink, hl


def test_move_recolor_delete_incremental(app, apdf, tmp_path):
    c = canvas_for(apdf)
    before = snapshot(apdf)
    out = str(tmp_path / "out.pdf")
    sq, ink, hl = _edit_and_check(c, out, True)
    after = snapshot(out)
    d = pymupdf.open(out)
    p = d[0]
    a = p.load_annot(sq.xref)  # same object, moved in place, appearance kept
    assert abs(a.rect.x0 - 139) < 1 and abs(a.rect.y0 - 324) < 1, a.rect
    assert after[sq.xref][2] == before[sq.xref][2] and a.info["content"] == "A square"
    k = p.load_annot(ink.xref)
    assert tuple(round(v, 2) for v in k.colors["stroke"]) == (0.0, 1.0, 0.0)
    assert hl.xref not in after and all(v[0] != "Highlight" for v in after.values())
    # the note, its reply and the other page's circle are untouched
    for xref, v in before.items():
        if xref not in (sq.xref, ink.xref, hl.xref):
            assert after[xref] == v, xref
    with open(apdf, "rb") as f:
        orig = f.read()
    with open(out, "rb") as f:
        assert f.read().startswith(orig)


def test_display_hides_changed_and_undo_restores(app, apdf, tmp_path):
    c = canvas_for(apdf)
    src = c.doc.primary
    items = c.annots.ensure(c.order[0])
    sq = by_kind(items, "rect")
    ref = c.doc.pages[0]
    clip = (100, 300, 220, 360)
    base = c.doc.render(ref, 1.0, clip)[0].samples
    assert not sq.draws_self()
    c.undo.push(C.RemoveItems([sq]))
    c.annots.sync()
    assert sq.xref in src.hidden_annots
    gone = c.doc.render(ref, 1.0, clip)[0].samples
    assert gone != base  # the page image no longer shows the deleted square
    c.undo.undo()
    c.annots.sync()
    assert sq.xref not in src.hidden_annots and sq.scene() is c.scene()
    assert c.doc.render(ref, 1.0, clip)[0].samples == base
    out = str(tmp_path / "out.pdf")
    add(c, 1, ShapeItem("rect", QRectF(10, 10, 20, 20), {}))
    c.doc.save(out, c.overlays_for)
    assert snapshot(out)[sq.xref] == snapshot(apdf)[sq.xref]  # undo of delete restores the saved annotation
    # a move: hidden in the page image, drawn by the item
    c.undo.push(C.MoveItems({sq: (QPointF(sq.pos()), sq.pos() + QPointF(5, 5))}))
    c.annots.sync()
    assert sq.draws_self() and sq.xref in src.hidden_annots
    with open(apdf, "rb") as f:
        assert f.read() == src.data  # the source bytes stay pristine


def test_reorder_and_merge_second_source(app, apdf, tmp_path):
    other = str(tmp_path / "other.pdf")
    annotate(other, tag="B")
    c = canvas_for(apdf)
    doc = c.doc
    src2 = doc.add_source(other)
    p1, p2, p3 = doc.pages
    old = [(r, r.rotation) for r in doc.pages]
    new = [(p3, 0), (PageRef(src2, 0), 0), (p1, 90), (PageRef(src2, 2), 0)]
    c.undo.push(C.SetPages(c, old, new, "ops"))
    # edits on the primary's page 0 (now at position 2, rotated) and the second source's page 0 (position 1)
    sq, ink, hl = _edit_and_check(c, str(tmp_path / "x.pdf"), False, square_page=2)
    items2 = c.annots.ensure(c.order[1])
    sq2, note2 = by_kind(items2, "rect"), by_kind(items2, "note")
    c.undo.push(C.RemoveItems([sq2]))
    c.annots.set_comment(note2, "edited B note")
    circ = by_kind(c.annots.ensure(c.order[3]), "ellipse")  # untouched, from the second source
    c.annots.sync()
    out = str(tmp_path / "out.pdf")
    assert doc.save(out, c.overlays_for) is False
    d = pymupdf.open(out)
    assert d.page_count == 4

    pages = [d[i] for i in range(d.page_count)]  # Annot objects need their page alive

    def annots(i):
        return {a.info.get("content") or a.type[1]: a for a in pages[i].annots()}

    a2 = annots(2)  # primary page 0, moved/recolored/deleted
    assert "Highlight" not in a2 and "A square" in a2
    r = a2["A square"].rect
    assert abs(r.x0 - 139) < 1 and abs(r.y0 - 324) < 1, r
    ink2 = next(a for a in pages[2].annots() if a.type[1] == "Ink")
    assert tuple(round(v, 2) for v in ink2.colors["stroke"]) == (0.0, 1.0, 0.0)
    a1 = annots(1)  # second source page 0
    assert "B square" not in a1 and "edited B note" in a1 and "Highlight" in a1
    assert annots(3)["B circle"].type[1] == "Circle"
    assert "A circle" in annots(0)
    # untouched annotations keep their appearance streams
    snap = snapshot(out)
    orig = snapshot(other)
    circle_ap = next(v[2] for v in orig.values() if v[3] == "B circle")
    assert any(v[3] == "B circle" and v[2] == circle_ap for v in snap.values())
    assert circ.xref not in doc.sources[1].hidden_annots


def test_note_roundtrip_and_reply(app, apdf, tmp_path):
    c = canvas_for(apdf)
    c.annots.prompt = lambda item, title, author, when, text, can_delete: ("Remember the milk", "Dana")
    pg = c.order[1]
    note = c.annots.add_note(pg, QPointF(200, 200))
    assert note is not None and c.undo.count() == 1 and note.meta["author"] == "Dana"
    assert c.annots.author == "Dana"
    reply = c.annots.add_reply(note, "Done!")
    old_note = by_kind(c.annots.ensure(c.order[0]), "note")
    c.annots.add_reply(old_note, "Answer to Alice")
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    p1 = d[1]
    texts = [a for a in p1.annots() if a.type[1] == "Text"]
    top = next(a for a in texts if not a.irt_xref)
    assert top.info["content"] == "Remember the milk" and top.info["title"] == "Dana" and top.info["creationDate"]
    r = next(a for a in texts if a.irt_xref)
    assert r.irt_xref == top.xref and r.info["content"] == "Done!"
    assert abs(top.rect.x0 - 190) < 2 and abs(top.rect.y0 - 190) < 2, top.rect
    pg0 = d[0]
    p0 = [a for a in pg0.annots() if a.type[1] == "Text"]
    assert any(a.irt_xref == old_note.xref and a.info["content"] == "Answer to Alice" for a in p0)
    d.close()
    # editing an existing note's text
    c.annots.prompt = lambda *a: ("changed", "Dana")
    c.annots.open_editor(old_note)
    assert old_note.meta["contents"] == "changed"
    c.annots.sync()
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    pg0 = d[0]
    assert pg0.load_annot(old_note.xref).info["content"] == "changed"
    c.undo.undo()
    assert old_note.meta["contents"] == "A note text"
    assert reply.target is note


def test_delete_parent_takes_replies(app, apdf, tmp_path):
    c = canvas_for(apdf)
    items = c.annots.ensure(c.order[0])
    note, reply = by_kind(items, "note"), by_kind(items, "reply")
    note.setSelected(True)
    c.delete_selected()
    assert note.scene() is None and reply.scene() is None
    c.annots.sync()
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    assert not [a for a in d[0].annots() if a.type[1] == "Text"]
    c.undo.undo()
    assert note.scene() is c.scene() and reply.scene() is c.scene()


def test_leaf_highlight_stores_text(app, apdf, tmp_path):
    c = canvas_for(apdf)
    pg = c.order[1]
    words = c.doc.primary.words(1)
    c._update_text_selection(pg, QPointF(words[0][0] + 1, words[0][1] + 2), QPointF(words[2][2] - 1, words[2][1] + 2))
    sel = c.selected_text()
    assert c.highlight_selection()
    hl = next(i for i in pg.overlays() if isinstance(i, HighlightItem))
    assert hl.marked_text == sel
    c.annots.sync()
    assert hl.meta.get("author") == c.annots.author
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    p1 = d[1]
    h = next(a for a in p1.annots() if a.type[1] == "Highlight")
    assert h.info["content"] == sel and h.info["title"] == c.annots.author


def test_rotated_source_page_move(app, tmp_path):
    path = str(tmp_path / "rot.pdf")
    make_pdf(path, rotate_second=True, form=False)
    d = pymupdf.open(path)
    p = d[1]  # /Rotate 90
    a = p.add_rect_annot(pymupdf.Rect(100, 200, 160, 240))
    a.update()
    d.saveIncr()
    d.close()
    c = canvas_for(path)
    sq = by_kind(c.annots.ensure(c.order[1]), "rect")
    before = sq.scene_rect_in_parent()
    c.undo.push(C.MoveItems({sq: (QPointF(sq.pos()), sq.pos() + QPointF(30, 10))}))
    out = str(tmp_path / "out.pdf")
    c.doc.save(out, c.overlays_for)
    d = pymupdf.open(out)
    p = d[1]
    disp = p.load_annot(sq.xref).rect * p.rotation_matrix
    assert abs(disp.x0 - (before.left() + 30)) < 1 and abs(disp.y0 - (before.top() + 10)) < 1, (disp, before)


def _panel(c, app):
    from leaf.comments import CommentsPanel

    panel = CommentsPanel(c)
    panel.resize(320, 600)
    panel.show()
    for _ in range(50):
        pump(app, 0.02)
        if not c.annots.busy():
            break
    panel.rebuild()
    return panel


def _rows(panel):
    return [panel.item_of(n) for n in panel._walk() if panel.item_of(n) is not None]


def test_comments_panel_lists_filters_selects(app, apdf):
    c = canvas_for(apdf)
    panel = _panel(c, app)
    items = _rows(panel)
    kinds = sorted(i.kind for i in items)
    assert kinds == ["ellipse", "highlight", "ink", "note", "rect", "reply"], kinds  # all pages, adopted lazily
    note = next(i for i in items if i.kind == "note")
    reply_node = panel.node_of(next(i for i in items if i.kind == "reply"))
    assert panel.item_of(reply_node.parent()) is note  # threaded
    assert "A note text" in panel.node_of(note).text(0)
    # filters
    panel.type_box.setCurrentIndex(panel.type_box.findData("Note"))
    assert sorted(i.kind for i in _rows(panel)) == ["note", "reply"]
    panel.type_box.setCurrentIndex(0)
    panel.author_box.setCurrentIndex(panel.author_box.findData("Bob"))
    assert sorted(i.kind for i in _rows(panel)) == ["highlight", "rect"]
    panel.author_box.setCurrentIndex(0)
    # click selects on the page
    sq = next(i for i in _rows(panel) if i.kind == "rect")
    panel.tree.setCurrentItem(panel.node_of(sq))
    assert c.selected_overlays() == [sq]
    # and the page selection is followed
    c.scene().clearSelection()
    note.setSelected(True)
    assert panel._current is note
    # inline comment edit, one undo step
    panel.edit.setPlainText("new words")
    panel.commit()
    assert note.meta["contents"] == "new words"
    pump(app, 0.15)
    assert "new words" in panel.node_of(note).text(0)
    c.undo.undo()
    pump(app, 0.15)
    assert note.meta["contents"] == "A note text"
    # reply from the panel
    panel._current = note
    panel.reply.setText("panel reply")
    panel.send_reply()
    pump(app, 0.15)
    replies = [i for i in _rows(panel) if i.kind == "reply"]
    assert len(replies) == 2 and any(r.meta["contents"] == "panel reply" for r in replies)


def test_comments_panel_live_updates(app, apdf):
    c = canvas_for(apdf)
    panel = _panel(c, app)
    n0 = len(_rows(panel))
    add(c, 1, ShapeItem("rect", QRectF(10, 10, 40, 40), {}))
    pump(app, 0.15)
    rows = _rows(panel)
    assert len(rows) == n0 + 1
    new = next(i for i in rows if i.kind == "rect" and not isinstance(i, A.Adopted))
    assert new.meta["author"] == c.annots.author
    sq = next(i for i in rows if isinstance(i, A.AdoptedShape) and i.kind == "rect")
    panel._current = sq
    panel.delete_current()
    pump(app, 0.15)
    assert sq not in _rows(panel) and len(_rows(panel)) == n0
    c.undo.undo()
    c.undo.undo()
    pump(app, 0.15)
    assert sq in _rows(panel) and len(_rows(panel)) == n0 - 0


def test_window_note_tool_and_panel(app, apdf, tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings, Qt

    from leaf import shortcuts

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafAnnotTest")
    QSettings().clear()
    from leaf.window import MainWindow

    w = MainWindow()
    try:
        w.resize(1100, 800)
        w.show()
        w.open_path(apdf)
        pump(app, 0.2)
        assert w.canvas.tool_keys.get(Qt.Key.Key_N) == "note"
        assert w.a_tool_note.isEnabled() and w.a_comments_panel.shortcut().toString() == "F9"
        dup = shortcuts.duplicates(w)
        assert not any(a.objectName() in ("a_tool_note", "a_comments_panel") for acts in dup.values() for a in acts), dup
        w.a_tool_note.trigger()
        assert w.canvas.tool == "note"
        w.a_comments_panel.trigger()
        pump(app, 0.3)
        assert w.comments_dock.isVisible()
        w.canvas.annots.set_author("Erin")
        assert QSettings().value("annotations/author") == "Erin"
    finally:
        w.canvas.undo.setClean()
        w.close()
        QSettings().clear()
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])
