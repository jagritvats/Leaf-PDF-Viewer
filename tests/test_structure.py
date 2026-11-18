"""Structure: links (add/edit/delete, page targets by uid), bookmark editing, attachments; all round-trip through save."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from leaf import attachments as AT
from leaf import commands as C
from leaf import links as LK
from leaf import outline as OL
from leaf.outline import OutlinePanel
from tests.test_editor import app, canvas_for, make_pdf, pump  # noqa: F401


def doc_pdf(path, n=4, toc=True, files=False, rotate=1):
    d = pymupdf.open()
    for i in range(n):
        p = d.new_page(width=600, height=800)
        p.insert_text((72, 90), f"Chapter {i + 1}", fontsize=24)
        p.insert_text((72, 140), f"Body text of page {i + 1} for the tests.", fontsize=11)
        p.insert_text((72, 400), f"Section {i + 1}.2", fontsize=18)
    if rotate is not None:
        d[rotate].set_rotation(90)
    p = d[0]
    p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 200, 200, 220), "uri": "https://orig.example/"})
    p.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 240, 200, 260), "page": 2,
                   "to": pymupdf.Point(0, 0)})
    if toc:
        d.set_toc([[1, "One", 1], [2, "One.A", 1], [1, "Two", 2], [1, "Three", 3], [1, "Web", -1,
                   {"kind": pymupdf.LINK_URI, "uri": "https://toc.example/"}]])
    if files:
        d.embfile_add("notes.txt", b"hello notes", filename="notes.txt", ufilename="notes.txt", desc="Some notes")
        d.embfile_add("data.csv", b"a,b\n1,2\n")
    d.save(path)
    d.close()


@pytest.fixture
def spdf(tmp_path):
    path = str(tmp_path / "s.pdf")
    doc_pdf(path)
    return path


def save(c, path):
    return c.doc.save(str(path), c.overlays_for)


def links_of(path, pno):
    d = pymupdf.open(str(path))
    try:
        return [dict(ln) for ln in d[pno].get_links()]
    finally:
        d.close()


def close(a, b, tol=1.0):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ------------------------------------------------------------------------------------------------- links
def test_links_round_trip_uri_page_email_file(app, spdf, tmp_path):
    c = canvas_for(spdf)
    p0, p1, p2 = c.doc.pages[:3]
    LK.add_links(c, p0, [LK.Link((300, 500, 400, 520), "uri", "https://example.com/a(b)?q=ü")])
    LK.add_links(c, p1, [LK.Link((100, 50, 300, 80), "email", LK.mailto("me@example.com", "Hi there"))])
    LK.add_links(c, p0, [LK.Link((300, 600, 400, 620), "page", uid=p2.uid, point=(0, 400)),
                         LK.Link((300, 650, 400, 670), "file", "other.pdf")])
    assert c.undo.count() == 3
    shown = [ln for _, ln in c.links_on(c.order[0])]
    assert len(shown) == 5  # 2 originals + 3 new
    out = tmp_path / "out.pdf"
    assert save(c, out)  # links alone keep the save incremental
    L0 = links_of(out, 0)
    uri = next(ln for ln in L0 if ln["kind"] == pymupdf.LINK_URI and "example.com" in ln["uri"])
    assert uri["uri"].startswith("https://example.com/a(b)?q=") and close(tuple(uri["from"]), (300, 500, 400, 520))
    goto = next(ln for ln in L0 if ln["kind"] == pymupdf.LINK_GOTO and close(tuple(ln["from"]), (300, 600, 400, 620)))
    assert goto["page"] == 2 and abs(goto["to"].y - 400) < 1
    f = next(ln for ln in L0 if ln["kind"] == pymupdf.LINK_GOTOR)
    assert f["file"] == "other.pdf"
    # page 2 is rotated: the rect is in its page.rect (display) space
    L1 = links_of(out, 1)
    assert len(L1) == 1 and L1[0]["uri"] == "mailto:me@example.com?subject=Hi%20there"
    assert close(tuple(L1[0]["from"]), (100, 50, 300, 80))
    # the originals are untouched
    assert sum(1 for ln in L0 if ln.get("uri") == "https://orig.example/") == 1


def test_internal_link_follows_reorder_and_deleted_target(app, spdf, tmp_path):
    c = canvas_for(spdf)
    p0, p1, p2, p3 = c.doc.pages
    LK.add_links(c, p0, [LK.Link((300, 600, 400, 620), "page", uid=p2.uid)])
    LK.add_links(c, p3, [LK.Link((300, 600, 400, 620), "page", uid=p1.uid)])
    c.undo.push(C.SetPages(c, [(r, r.rotation) for r in c.doc.pages], [(r, r.rotation) for r in (p2, p3, p0, p1)],
                           "Reorder"))
    pg = c.order[2]  # p0
    hit = next(ln for _, ln in c.links_on(pg) if ln.get("leaf_link"))
    assert c.link_target_text(pg, hit) == "Go to page 1"
    c.follow_link(pg, hit)
    assert c.current_index() == 0
    out = tmp_path / "re.pdf"
    assert not save(c, out)
    new = [ln for ln in links_of(out, 2) if ln["kind"] == pymupdf.LINK_GOTO and close(tuple(ln["from"]), (300, 600, 400, 620))]
    assert len(new) == 1 and new[0]["page"] == 0  # p2 is the first page now
    assert [ln["page"] for ln in links_of(out, 1) if ln["kind"] == pymupdf.LINK_GOTO] == [3]
    # delete p1: p3's link has no target any more; it is reported, then left out
    c.undo.push(C.SetPages(c, [(r, r.rotation) for r in c.doc.pages], [(r, r.rotation) for r in (p2, p3, p0)], "Del"))
    assert LK.dropped_targets(c.doc) == 1
    out2 = tmp_path / "del.pdf"
    save(c, out2)
    assert not [ln for ln in links_of(out2, 1) if ln["kind"] == pymupdf.LINK_GOTO]


def test_edit_and_delete_original_links_with_undo(app, spdf, tmp_path):
    c = canvas_for(spdf)
    ref = c.doc.pages[0]
    pg = c.order[0]
    shown = [ln for _, ln in c.links_on(pg)]
    uri = next(ln for ln in shown if ln["kind"] == pymupdf.LINK_URI)
    goto = next(ln for ln in shown if ln["kind"] == pymupdf.LINK_GOTO)
    LK.delete_link(c, ref, uri)
    spec = LK.from_link_dict(c.doc, ref, goto)
    assert spec.kind == "page" and spec.uid == c.doc.pages[2].uid
    LK.update_link(c, ref, goto, LK.Link(spec.rect, "uri", "https://edited.example/"))
    kinds = sorted(ln["kind"] for _, ln in c.links_on(pg))
    assert kinds == [pymupdf.LINK_URI]
    out = tmp_path / "e.pdf"
    save(c, out)
    L = links_of(out, 0)
    assert [(ln["kind"], ln.get("uri")) for ln in L] == [(pymupdf.LINK_URI, "https://edited.example/")]
    assert close(tuple(L[0]["from"]), (72, 240, 200, 260))
    # edit the new link again, then undo everything
    new = next(ln for _, ln in c.links_on(pg) if ln.get("leaf_link"))
    LK.update_link(c, ref, new, LK.Link(spec.rect, "email", "mailto:x@y.z", id=new["leaf_link"].id))
    assert next(ln for _, ln in c.links_on(pg))["uri"] == "mailto:x@y.z"
    while c.undo.canUndo():
        c.undo.undo()
    assert ref.uid not in c.doc.links
    assert len(c.links_on(pg)) == 2
    out2 = tmp_path / "u.pdf"
    assert save(c, out2)
    assert open(out2, "rb").read() == open(spdf, "rb").read()  # nothing to append


def test_new_link_under_a_redaction_is_left_out(app, spdf, tmp_path):
    from PySide6.QtCore import QRectF

    from leaf.redact import RedactItem
    c = canvas_for(spdf)
    LK.add_links(c, c.doc.pages[0], [LK.Link((300, 500, 400, 520), "uri", "https://x.example/"),
                                     LK.Link((300, 700, 400, 720), "uri", "https://keep.example/")])
    mark = RedactItem(rect=QRectF(290, 490, 150, 40))
    mark.setParentItem(c.order[0].content)
    c.undo.push(C.AddItems([mark]))
    out = tmp_path / "r.pdf"
    save(c, out)
    uris = [ln.get("uri") for ln in links_of(out, 0)]
    assert "https://x.example/" not in uris and "https://keep.example/" in uris


# --------------------------------------------------------------------------------------------- bookmarks
def outline_rows(path):
    d = pymupdf.open(str(path))
    try:
        return [tuple(e[:3]) for e in d.get_toc()]
    finally:
        d.close()


def test_bookmarks_edit_round_trip_after_reorder_and_delete(app, spdf, tmp_path):
    c = canvas_for(spdf)
    c.show()
    panel = OutlinePanel(c)
    panel.rebuild()
    assert [m.title for m in panel.marks] == ["One", "One.A", "Two", "Three", "Web"]
    p0, p1, p2, p3 = c.doc.pages
    # add a bookmark at page 4, below the heading area: title from the nearest heading
    c.go_to(3)
    pump(app, 0.05)
    n = panel.add_here()
    assert panel.marks[n].uid == p3.uid and panel.marks[n].title == "Chapter 4"
    assert c.doc.outline is not None
    n = next(i for i, m in enumerate(panel.marks) if m.title == "Chapter 4")
    panel.rename(n, "Four")
    panel.indent(n, 1)  # under "Web"
    assert panel.marks[n].level == 2
    panel.move(next(i for i, m in enumerate(panel.marks) if m.title == "Three"), 0)
    assert [m.title for m in panel.marks][:2] == ["Three", "One"]
    # reorder pages (p3, p2, p0, p1) and delete p1 ("Two" has no children: left out)
    c.undo.push(C.SetPages(c, [(r, 0) for r in c.doc.pages], [(r, r.rotation) for r in (p3, p2, p0)], "Reorder"))
    assert OL.orphaned(c.doc) == 1
    out = tmp_path / "bm.pdf"
    save(c, out)
    assert outline_rows(out) == [(1, "Three", 2), (1, "One", 3), (2, "One.A", 3), (1, "Web", -1), (2, "Four", 1)]
    d = pymupdf.open(str(out))
    web = d.get_toc(simple=False)[3][3]
    assert web["kind"] == pymupdf.LINK_URI and web["uri"] == "https://toc.example/"
    d.close()
    # reopen: the panel reads the saved outline the same way
    c2 = canvas_for(str(out))
    p2 = OutlinePanel(c2)
    p2.rebuild()
    assert [(m.level, m.title) for m in p2.marks][-1] == (2, "Four")
    # undo all: back to the untouched outline
    while c.undo.canUndo():
        c.undo.undo()
    assert c.doc.outline is None and [m.title for m in panel.marks][0] == "One"


def test_bookmark_destination_point_round_trips_on_rotated_page(app, spdf, tmp_path):
    c = canvas_for(spdf)
    c.show()
    panel = OutlinePanel(c)
    panel.rebuild()
    p1 = c.doc.pages[1]  # rotated 90: page.rect is 800 x 600
    marks = list(panel.marks) + [OL.Bookmark("Rot", 1, p1.uid, (0.0, 300.0))]
    panel._push(marks, "Add")
    out = tmp_path / "rot.pdf"
    save(c, out)
    c2 = canvas_for(str(out))
    q = OutlinePanel(c2)
    q.rebuild()
    m = q.marks[-1]
    assert m.title == "Rot" and m.point is not None and abs(m.point[1] - 300) < 1
    assert q.navigate(len(q.marks) - 1) and c2.current_index() == 1


def test_untouched_outline_survives_incremental_save(app, spdf, tmp_path):
    c = canvas_for(spdf)
    panel = OutlinePanel(c)
    panel.rebuild()
    n = 0
    panel.rename(n, "Changed")
    c.undo.undo()  # back to untouched
    assert c.doc.outline is None
    LK.add_links(c, c.doc.pages[0], [LK.Link((300, 500, 400, 520), "uri", "https://x.example/")])
    out = tmp_path / "inc.pdf"
    assert save(c, out)
    orig = open(spdf, "rb").read()
    new = open(out, "rb").read()
    assert new.startswith(orig)
    a, b = pymupdf.open(spdf), pymupdf.open(str(out))
    assert a.get_toc(simple=False) == b.get_toc(simple=False)
    assert all(a.xref_object(x) == b.xref_object(x) for x in a.get_outline_xrefs())
    a.close()
    b.close()


def test_bookmark_panel_tree_drag_result_and_rename_item(app, spdf):
    c = canvas_for(spdf)
    panel = OutlinePanel(c)
    panel.rebuild()
    t = panel.tree
    # simulate what an internal drag does: move "Three" under "Two"
    three = t.takeTopLevelItem(2)
    t.topLevelItem(1).addChild(three)
    panel._tree_changed()
    assert [(m.level, m.title) for m in panel.marks] == [(1, "One"), (2, "One.A"), (1, "Two"), (2, "Three"), (1, "Web")]
    item = panel.entries[0][0]
    item.setText(0, "Renamed")
    pump(app, 0.05)
    assert panel.marks[0].title == "Renamed"
    panel.delete(0)  # with its child
    assert [m.title for m in panel.marks] == ["Two", "Three", "Web"]
    c.undo.undo()
    c.undo.undo()
    c.undo.undo()
    assert c.doc.outline is None


# ------------------------------------------------------------------------------------------- attachments
def test_attachments_add_rename_remove_round_trip(app, tmp_path):
    path = str(tmp_path / "f.pdf")
    doc_pdf(path, files=True)
    c = canvas_for(path)
    panel = AT.AttachmentsPanel(c)
    panel.refresh()
    assert [r[1].name for r in panel.rows] == ["notes.txt", "data.csv"]
    assert panel.rows[0][1].desc == "Some notes"
    extra = tmp_path / "extra.bin"
    extra.write_bytes(b"\x00\x01payload")
    AT.add_files(c, [str(extra)], panel.refresh)
    assert [r[1].name for r in panel.rows] == ["notes.txt", "data.csv", "extra.bin"]
    AT.edit_file(c, panel.rows[0][1].id, "renamed.txt", "New description", panel.refresh)
    AT.remove_file(c, panel.rows[1][1].id, panel.refresh)
    assert [r[1].name for r in panel.rows] == ["renamed.txt", "extra.bin"]
    out = tmp_path / "fo.pdf"
    assert save(c, out)
    d = pymupdf.open(str(out))
    names = d.embfile_names()
    assert sorted(names) == ["extra.bin", "notes.txt"]
    info = d.embfile_info("notes.txt")
    assert info["ufilename"] == "renamed.txt" and info["description"] == "New description"
    assert d.embfile_get("notes.txt") == b"hello notes" and d.embfile_get("extra.bin") == b"\x00\x01payload"
    d.close()
    while c.undo.canUndo():
        c.undo.undo()
    assert c.doc.attachments is None
    assert [r[1].name for r in panel.rows] == ["notes.txt", "data.csv"]


def test_attachments_open_warns_for_programs_and_lists_annotations(app, tmp_path, monkeypatch):
    path = str(tmp_path / "a.pdf")
    doc_pdf(path, files=False, toc=False)
    d = pymupdf.open(path)
    d[1].add_file_annot(pymupdf.Point(100, 100), b"annot data", "on-page.txt", desc="pinned")
    d.embfile_add("tool.exe", b"MZ")
    d.saveIncr()
    d.close()
    c = canvas_for(path)
    panel = AT.AttachmentsPanel(c)
    panel.refresh()
    assert [(r[0], r[1].name if r[0] == "file" else r[1][2]) for r in panel.rows] == [("file", "tool.exe"),
                                                                                    ("annot", "on-page.txt")]
    asked, opened = [], []
    monkeypatch.setattr(AT, "confirm_risky", lambda parent, name: asked.append(name) or False)
    monkeypatch.setattr(AT, "open_url", lambda url: opened.append(url.toLocalFile()) or True)
    assert panel.open_row(panel.rows[0]) is None and asked == ["tool.exe"] and not opened
    p = panel.open_row(panel.rows[1])
    assert p and open(p, "rb").read() == b"annot data" and [os.path.normpath(x) for x in opened] == [os.path.normpath(p)]
    target = tmp_path / "saved.txt"
    monkeypatch.setattr(AT, "ask_save_path", lambda parent, name: str(target))
    panel.save_row(panel.rows[1])
    assert target.read_bytes() == b"annot data"
    # removing the annotation attachment deletes the annotation (undoable)
    panel.remove_row(panel.rows[1])
    assert [r[0] for r in panel.rows] == ["file"]
    out = tmp_path / "ao.pdf"
    save(c, out)
    d = pymupdf.open(str(out))
    assert not list(d[1].annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT]))
    d.close()
    c.undo.undo()
    panel.refresh()
    assert [r[0] for r in panel.rows] == ["file", "annot"]


# ------------------------------------------------------------------------------------------------ window
def test_window_link_tool_insert_link_bookmark_and_attachments(app, tmp_path, monkeypatch):
    from tests.test_viewing import close_win, isolated, open_win  # noqa: F401

    from leaf import recovery
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    old = getattr(recovery, "OVERRIDE_DIR", None), getattr(recovery, "_offered", None)
    recovery.OVERRIDE_DIR = str(tmp_path / "recovery")
    recovery._offered = True
    path = str(tmp_path / "w.pdf")
    doc_pdf(path, files=True)
    w = open_win(app, path)
    try:
        cv = w.canvas
        for name in ("a_link_tool", "a_insert_link", "a_add_bookmark", "a_attachments", "a_attach_file"):
            assert w.findChild(type(w.a_link_tool), name) is getattr(w, name)
        assert cv.tool_keys[Qt.Key.Key_K] == "link"
        assert w.structure.dock.isVisible()  # the file has attachments
        pump(app, 0.2)
        assert w.attachments.count() == 2
        # Link tool: drag a rectangle on page 1, the dialog (stubbed) answers with a URL
        monkeypatch.setattr(LK, "ask", lambda canvas, link, title, picked=None: LK.Link((0, 0, 0, 0), "uri",
                                                                                         "https://drag.example/"))
        cv.setFocus()
        QTest.keyClick(cv, Qt.Key.Key_K)
        assert cv.tool == "link" and w.a_link_tool.isChecked()
        pg = cv.order[0]
        a = cv.mapFromScene(pg.content.mapToScene(QPointF(300, 450)))
        b = cv.mapFromScene(pg.content.mapToScene(QPointF(420, 480)))
        vp = cv.viewport()
        QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        QTest.mouseMove(vp, b)
        QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
        added = [ln for _, ln in cv.links_on(pg) if ln.get("leaf_link")]
        assert len(added) == 1 and added[0]["uri"] == "https://drag.example/"
        assert w.structure.tool.boxes  # outlines shown while the tool is active
        # click it, Delete removes it
        mid = cv.mapFromScene(pg.content.mapToScene(QPointF(360, 465)))
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, mid)
        assert w.structure.tool.selected is not None
        QTest.keyClick(cv, Qt.Key.Key_Delete)
        assert not [ln for _, ln in cv.links_on(pg) if ln.get("leaf_link")]
        cv.set_tool("select")
        assert not w.structure.tool.boxes
        # Insert › Link on selected text: one link per selected line
        cv._update_text_selection(pg, QPointF(80, 85), QPointF(300, 140))
        assert len(cv._sel_rects) == 2
        w.a_insert_link.trigger()
        assert len([ln for _, ln in cv.links_on(pg) if ln.get("leaf_link")]) == 2
        # bookmark via the action
        w.a_add_bookmark.trigger()
        pump(app, 0.05)
        assert w.viewing.dock.isVisible() and w.a_outline.isEnabled()
        assert len(w.outline.marks) == 6
    finally:
        close_win(app, w)
        recovery.OVERRIDE_DIR, recovery._offered = old
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_link_dialog_kinds_and_pick_flow(app, spdf, monkeypatch):
    c = canvas_for(spdf)
    c.show()
    dlg = LK.LinkDialog(c, None, "Add link")
    dlg.url.setText("example.org/x")
    r = dlg.result_link()
    assert (r.kind, r.target) == ("uri", "https://example.org/x")
    dlg.kind.setCurrentIndex(LK.KINDS.index("email"))
    dlg.email.setText("a@b.c")
    assert dlg.result_link().target == "mailto:a@b.c"
    p2 = c.doc.pages[2]
    dlg.set_target(p2.uid, (0, 250))
    r = dlg.result_link()
    assert (r.kind, r.uid, r.point) == ("page", p2.uid, (0, 250))
    dlg.top.setChecked(True)
    assert dlg.result_link().point is None
    dlg.deleteLater()

    # the tool: drag -> "Pick on page…" -> click on page 3 -> the dialog comes back with that spot -> OK
    from PySide6.QtWidgets import QMainWindow
    win = QMainWindow()
    win.canvas = c
    tool = LK.LinkTool(win)
    answers = []

    def ask(canvas, link, title, picked=None):
        answers.append(picked)
        if picked is None:
            return "pick", LK.Link((0, 0, 0, 0), "page")
        return LK.Link((0, 0, 0, 0), "page", uid=picked[0], point=picked[1])

    monkeypatch.setattr(LK, "ask", ask)
    c.set_tool("link")
    from PySide6.QtCore import QRectF
    tool.create(c.doc.pages[0], QRectF(300, 450, 100, 30))
    assert tool.pending is not None
    c.go_to(2)
    pump(app, 0.05)
    pg = c.order[2]
    pt = c.mapFromScene(pg.content.mapToScene(QPointF(200, 300)))
    QTest.mouseClick(c.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)
    assert tool.pending is None and answers[-1][0] == pg.ref.uid
    link = c.doc.links[c.doc.pages[0].uid].added[0]
    assert link.uid == pg.ref.uid and abs(link.point[1] - 300) < 2
    win.deleteLater()
