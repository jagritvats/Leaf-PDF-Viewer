"""Viewing: links, outline, page layouts, history, worker rendering, position memory."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent

from leaf.canvas import Canvas
from leaf.document import PdfDocument
from leaf.features.viewing import position_key, restore_position, save_position
from leaf.outline import OutlinePanel
from leaf.render_worker import RenderWorker
from tests.test_editor import app, make_pdf, pump  # noqa: F401


def link_pdf(path, n=4):
    """Page 1 has a word with a GoTo link to page 3 and a URI link; page 1 is rotated 90."""
    d = pymupdf.open()
    for i in range(n):
        p = d.new_page(width=600, height=800)
        p.insert_text((72, 100 + 40 * i), f"PAGE{i + 1}", fontsize=14)
    p = d[0]
    p.insert_text((100, 200), "LINKHERE", fontsize=20)
    p.insert_text((100, 300), "WEBLINK", fontsize=20)
    r = p.search_for("LINKHERE")[0]
    p.insert_link({"kind": pymupdf.LINK_GOTO, "from": r, "page": 2, "to": pymupdf.Point(0, 500)})
    p.insert_link({"kind": pymupdf.LINK_URI, "from": p.search_for("WEBLINK")[0], "uri": "https://example.com/x"})
    p.set_rotation(90)
    d.set_toc([[1, "Intro", 1, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(0, 0)}],
               [1, "Part two", 3, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(0, 500)}],
               [2, "Details", 4, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(0, 100)}]])
    d.save(path)
    d.close()


@pytest.fixture
def lpdf(tmp_path):
    path = str(tmp_path / "links.pdf")
    link_pdf(path)
    return path


def make_canvas(path, w=900, h=700):
    c = Canvas()
    c.resize(w, h)
    c.show()
    c.set_document(PdfDocument.open(path))
    return c


def _word(c, page, text):
    return next(w for w in c.order[page].ref.src.words(c.order[page].ref.index) if w[4] == text)


def test_link_rects_map_onto_rotated_page(app, lpdf):
    c = make_canvas(lpdf)
    pg = c.order[0]
    assert c.doc.page(pg.ref).rotation == 90
    word = _word(c, 0, "LINKHERE")  # display space (rotation-corrected)
    links = c.links_on(pg)
    goto = next(r for r, ln in links if ln["kind"] == pymupdf.LINK_GOTO)
    for a, b in zip((goto.left(), goto.top(), goto.right(), goto.bottom()), word[:4]):
        assert abs(a - b) < 1.5
    # hit testing through the scene, from the word's center
    center = pg.content.mapToScene(QPointF((word[0] + word[2]) / 2, (word[1] + word[3]) / 2))
    hit = c.link_at(center)
    assert hit is not None and hit[1]["kind"] == pymupdf.LINK_GOTO
    assert c.link_target_text(*hit) == "Go to page 3"


def test_internal_link_follows_page_list_and_history(app, tmp_path):
    path = str(tmp_path / "six.pdf")
    link_pdf(path, n=6)
    c = make_canvas(path)
    # move source page 3 behind page 4: the link must follow it
    c.doc.pages = [c.doc.pages[i] for i in (0, 1, 3, 2, 4, 5)]
    c.sync_pages()
    pg = c.order[0]
    word = _word(c, 0, "LINKHERE")
    hit = c.link_at(pg.content.mapToScene(QPointF((word[0] + word[2]) / 2, (word[1] + word[3]) / 2)))
    assert c.link_target_text(*hit) == "Go to page 4"
    c.follow_link(*hit)
    assert c.current_index() == 3
    target = c.order[3].content.mapToScene(QPointF(0, 500))
    top = c.mapToScene(c.viewport().rect()).boundingRect().top()
    assert 0 <= target.y() - top < 60 / c.zoom
    assert c.can_go_back()
    c.nav_back()
    assert c.current_index() == 0
    c.nav_forward()
    assert c.current_index() == 3


def test_uri_link_needs_confirmation(app, lpdf, monkeypatch):
    c = make_canvas(lpdf)
    asked, opened = [], []
    c.confirm_external = lambda t: asked.append(t) or False
    monkeypatch.setattr("leaf.canvas.QDesktopServices.openUrl", lambda url: opened.append(url))
    pg = c.order[0]
    uri = (pg, next(ln for _, ln in c.links_on(pg) if ln["kind"] == pymupdf.LINK_URI))
    c.follow_link(*uri)
    assert asked == ["https://example.com/x"] and not opened
    c.confirm_external = lambda t: True
    c.follow_link(*uri)
    assert opened and opened[0].toString() == "https://example.com/x"


def _malicious_links_pdf(path, folder):
    """One page, one link per action: (label, PDF action dictionary)."""
    exe = os.path.join(folder, "payload.exe")
    open(exe, "wb").write(b"MZ")
    open(os.path.join(folder, "notes.txt"), "w").write("hi")
    open(os.path.join(folder, "data.xyz"), "w").write("hi")
    fwd = exe.replace("\\", "/")
    acts = {
        "web": "<</S/URI/URI(https://example.com/a)>>",
        "mail": "<</S/URI/URI(mailto:a@example.com)>>",
        "file-uri-exe": f"<</S/URI/URI(file:///{fwd})>>",
        "launch-exe": "<</S/Launch/F(payload.exe)>>",
        "launch-exe-dot": "<</S/Launch/F(payload.exe.)>>",
        "launch-abs-exe": f"<</S/Launch/F({pymupdf.get_pdf_str(exe)[1:-1]})>>",
        "launch-bat": "<</S/Launch/F(run.bat)>>",
        "launch-unc": "<</S/Launch/F<</Type/Filespec/F(//attacker/share/x.pdf)/UF(//attacker/share/x.pdf)>>>>",
        "uri-unc": "<</S/URI/URI(file://attacker/share/x.pdf)>>",
        "msdt": "<</S/URI/URI(ms-msdt:/id PCWDiagnostic)>>",
        "search-ms": "<</S/URI/URI(search-ms:query=x&crumb=location:\\\\\\\\attacker\\\\s)>>",
        "js": "<</S/URI/URI(javascript:alert(1))>>",
        "custom": "<</S/URI/URI(zoommtg://join?x=1)>>",
        "missing": "<</S/GoToR/F(nothere.pdf)/D[0/Fit]>>",
        "txt": "<</S/Launch/F(notes.txt)>>",
        "unknown": "<</S/Launch/F(data.xyz)>>",
    }
    d = pymupdf.open()
    p = d.new_page()
    xrefs = []
    for i, a in enumerate(acts.values()):
        x = d.get_new_xref()
        d.update_object(x, f"<</Type/Annot/Subtype/Link/Rect[10 {10 + i * 20} 100 {25 + i * 20}]/Border[0 0 0]/A{a}>>")
        xrefs.append(x)
    d.xref_set_key(p.xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in xrefs) + "]")
    d.save(path)
    d.close()
    return list(acts)


def test_links_to_programs_network_and_handlers_are_refused(app, tmp_path, monkeypatch):
    path = str(tmp_path / "evil.pdf")
    labels = _malicious_links_pdf(path, str(tmp_path))
    c = make_canvas(path)
    asked, refused, opened = [], [], []
    c.confirm_external = lambda t, warning="": asked.append((t, warning)) or True
    c.refuse_external = lambda msg: refused.append(msg)
    monkeypatch.setattr("leaf.canvas.QDesktopServices.openUrl", lambda url: opened.append(url))
    pg = c.order[0]
    links = sorted(c.links_on(pg), key=lambda rl: -rl[0].top())  # PDF y grows upwards: the first action is lowest
    assert len(links) == len(labels)
    seen = {}
    for label, (_, ln) in zip(labels, links):
        asked.clear(), refused.clear(), opened.clear()
        c.follow_link(pg, ln)
        seen[label] = ("block" if refused else "warn" if asked and asked[0][1] else "open" if asked else "none",
                       [u.toString() for u in opened])
    for label in ("file-uri-exe", "launch-exe", "launch-exe-dot", "launch-abs-exe", "launch-bat", "launch-unc",
                  "uri-unc", "msdt", "search-ms", "js", "missing"):
        assert seen[label] == ("block", []), (label, seen[label])
    assert seen["web"] == ("open", ["https://example.com/a"])
    assert seen["mail"] == ("open", ["mailto:a@example.com"])
    assert seen["custom"][0] == "warn" and seen["custom"][1] == ["zoommtg://join?x=1"]
    assert seen["txt"][0] == "open" and seen["txt"][1][0].endswith("/notes.txt")
    assert seen["unknown"][0] == "warn"


def test_safeopen_names_and_paths():
    from leaf import safeopen as S

    assert S.is_risky("a.exe") and S.is_risky("a.EXE. ") and S.is_risky("a.ex\x00e") and S.is_risky("x.chm")
    assert S.is_risky("invoice\u202efdp.exe") and not S.is_risky("report.pdf")
    assert S.safe_filename("..\\..\\a.exe.") == "a.exe" and S.safe_filename("a\x01.pdf") == "a.pdf"
    assert S.safe_filename("evil\u202egpj.exe") == "evilgpj.exe" and S.safe_filename("...") == "attachment"
    assert S.shown("a\u202eb") == "a\\u202eb"
    if os.name == "nt":
        for p in (r"\\srv\s\x.pdf", r"\/srv/s/x.pdf", "//srv/s/x.pdf", r"\\?\UNC\srv\s\x.pdf", r"\??\UNC\srv\s\x"):
            assert S.is_network_path(p), p
        assert S.is_risky("a.exe::$DATA") and not S.is_network_path(r"C:\x.pdf")
        assert S.is_network_path(r"\\.\pipe\x")
        for p in (r"\\?\C:\a very long\path\x.pdf", r"\\.\D:\x.pdf"):  # long-path form of a local drive: still local
            assert not S.is_network_path(p), p


def test_go_to_history_only_for_jumps(app, lpdf):
    c = make_canvas(lpdf)
    c.go_to(1)
    assert not c.can_go_back()  # neighbouring page: not a jump
    c.go_to(3)
    assert c.can_go_back()
    c.nav_back()
    assert c.current_index() == 1


def test_outline_navigation_and_tracking(app, lpdf):
    c = make_canvas(lpdf)
    panel = OutlinePanel(c)
    panel.rebuild()
    assert panel.has_outline()
    assert panel.tree.topLevelItemCount() == 2
    assert panel.tree.topLevelItem(1).child(0).text(0) == "Details"
    n = next(i for i, e in enumerate(panel.entries) if e[0].text(0) == "Part two")
    assert panel.navigate(n)
    assert c.current_index() == 2
    target = c.order[2].content.mapToScene(QPointF(0, 500))
    top = c.mapToScene(c.viewport().rect()).boundingRect().top()
    assert 0 <= target.y() - top < 60 / c.zoom
    assert panel.current_entry() == n
    panel.filter.setText("deta")
    assert not panel.tree.topLevelItem(1).child(0).isHidden()
    assert panel.tree.topLevelItem(0).isHidden()


def test_outline_destination_on_rotated_page(app, tmp_path):
    path = str(tmp_path / "rot.pdf")
    d = pymupdf.open()
    for _ in range(2):
        d.new_page(width=600, height=800)
    d[1].set_rotation(90)
    d.set_toc([[1, "R", 2, {"kind": pymupdf.LINK_GOTO, "to": pymupdf.Point(100, 200)}]])
    d.save(path)
    c = make_canvas(path)
    panel = OutlinePanel(c)
    panel.rebuild()
    _, idx, pt, _ = panel.entries[0]
    assert idx == 1
    # set_toc wrote /XYZ 200 100; on a 600x800 page shown at 90 degrees that is (100, 200) of page.rect (get_toc
    # reports it there, as get_links does for link destinations)
    assert abs(pt.x() - 100) < 0.5 and abs(pt.y() - 200) < 0.5


def test_no_outline(app, pdf):
    c = make_canvas(pdf)
    panel = OutlinePanel(c)
    panel.rebuild()
    assert not panel.has_outline()


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path, rotate_second=False, form=False)
    return path


def test_spread_layout_cover_alone(app, lpdf):
    c = make_canvas(lpdf)
    c.set_view_mode("spread")
    a, b, d, e = c.order  # page 1 is rotated (wider)
    assert a.x() > 0  # cover on the right
    assert b.y() == pytest.approx(d.y())
    assert b.x() + b.w <= 0 <= d.x()  # page 2 left of the gutter, page 3 right
    assert e.x() + e.w <= 0  # page 4 starts the next spread on the left
    assert c.current_index() == 0
    c.next_page()
    assert c.current_index() == 1
    c.next_page()
    assert c.current_index() == 3
    c.next_page(-1)
    assert c.current_index() == 1
    c.set_view_mode("spread", cover_alone=False)
    assert a.y() + a.h / 2 == pytest.approx(b.y() + b.h / 2) and a.x() + a.w <= 0 <= b.x()


def test_single_page_mode(app, lpdf):
    c = make_canvas(lpdf)
    c.go_to(2)
    c.set_view_mode("single")
    assert c.current_index() == 2
    assert [it.isVisible() for it in c.order] == [False, False, True, False]
    assert c.visible_pages() == [c.order[2]]
    c.next_page()
    assert c.current_index() == 3 and c.order[3].isVisible() and not c.order[2].isVisible()
    c.next_page()
    assert c.current_index() == 3  # clamps at the end
    c.set_view_mode("continuous")
    assert all(it.isVisible() for it in c.order)
    assert c.current_index() == 3


def test_ctrl_wheel_zoom_keeps_point_under_cursor(app, lpdf):
    c = make_canvas(lpdf)
    c.zoom_to(1.0)
    pos = QPointF(300, 250)
    before = c.mapToScene(pos.toPoint())
    ev = QWheelEvent(pos, c.viewport().mapToGlobal(pos.toPoint()).toPointF(), QPoint(0, 0), QPoint(0, 240),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
    c.wheelEvent(ev)
    assert c.zoom > 1.2
    after = c.mapToScene(pos.toPoint())
    assert abs(after.x() - before.x()) * c.zoom < 2 and abs(after.y() - before.y()) * c.zoom < 2


def test_night_mode_inverts_pixels(app, pdf):
    c = make_canvas(pdf)
    pump(app, 0.5)
    pg = c.order[0]
    assert pg.img is not None
    assert pg.img.pixelColor(2, 2).lightness() > 240
    c.set_night(True)
    assert pg.img.pixelColor(2, 2).lightness() < 15
    c.set_night(False)
    assert pg.img.pixelColor(2, 2).lightness() > 240


def test_worker_render_matches_in_process(app, pdf):
    doc = PdfDocument.open(pdf)
    ref = doc.pages[0]
    w = RenderWorker()
    try:
        import time

        t0 = time.time()
        while not w.usable and time.time() - t0 < 20:
            time.sleep(0.01)
        assert w.usable
        clip_disp = (50, 60, 300, 200)
        ref.rotation = 90
        pclip = tuple(pymupdf.Rect(clip_disp) * ~doc.extra_matrix(ref))
        ref.rotation = 0
        rids = {w.submit(ref.src, 0, 0.5, 0): None, w.submit(ref.src, 0, 1.25, 90, pclip): pclip}
        got = {}
        while len(got) < 2 and time.time() - t0 < 30:
            for res in w.poll():
                got[res[1]] = res
            time.sleep(0.005)
        assert set(got) == set(rids)
        full = got[min(rids)]
        pix, _ = doc.render(ref, 0.5)
        assert (full[4], full[5]) == (pix.width, pix.height) and full[7] == pix.samples
        ref.rotation = 90
        pix, _ = doc.render(ref, 1.25, clip=clip_disp)
        tile = got[max(rids)]
        assert (tile[2], tile[3], tile[4], tile[5]) == (pix.x, pix.y, pix.width, pix.height) and tile[7] == pix.samples
    finally:
        w.close()


def test_canvas_renders_through_worker(app, pdf, monkeypatch):
    monkeypatch.delenv("LEAF_INPROCESS_RENDER", raising=False)
    c = make_canvas(pdf)
    try:
        assert c.renderer.enable_worker(True)
        import time

        t0 = time.time()
        while not c.renderer.worker.usable and time.time() - t0 < 20:
            app.processEvents()
        for it in c.order:
            it.evict()
        c.zoom_to(1.3)
        used_worker = False
        t0 = time.time()
        while time.time() - t0 < 10 and (c.order[0].img is None or abs(c.order[0].img_scale - c.order[0].full_scale()) > 1e-3):
            used_worker |= bool(c.renderer.inflight)
            app.processEvents()
        assert used_worker
        assert c.order[0].img is not None and abs(c.order[0].img_scale - c.order[0].full_scale()) < 1e-3
        expected, _ = c.doc.render(c.order[0].ref, c.order[0].img_scale)
        img = c.order[0].img
        assert (img.width(), img.height()) == (expected.width, expected.height)
    finally:
        c.renderer.enable_worker(False)


def test_position_memory(app, lpdf, tmp_path):
    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    c = make_canvas(lpdf)
    c.zoom_to(1.5)
    c.go_to(2)
    save_position(s, c)
    assert s.value(position_key(lpdf))
    c2 = make_canvas(lpdf)
    assert c2.current_index() == 0
    assert restore_position(s, c2)
    assert c2.current_index() == 2 and abs(c2.zoom - 1.5) < 1e-3
    c3 = make_canvas(lpdf)
    c3.fit_page()
    c3.go_to(1)
    save_position(s, c3)
    c4 = make_canvas(lpdf)
    c4.zoom_to(3.0)
    restore_position(s, c4)
    assert c4.fit_mode == "page" and c4.current_index() == 1


def test_position_memory_is_capped(app, lpdf, tmp_path, monkeypatch):
    import leaf.features.viewing as V

    monkeypatch.setattr(V, "POS_CAP", 3)
    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    c = make_canvas(lpdf)
    for k in range(5):
        c.doc.path = str(tmp_path / f"f{k}.pdf")
        save_position(s, c)
    keys = [k for k in s.allKeys() if k.startswith("viewpos/") and not k.endswith("_order")]
    assert len(keys) == 3
    assert not s.value(position_key(str(tmp_path / "f0.pdf")))


# ------------------------------------------------------------------ window
@pytest.fixture
def isolated(tmp_path):
    from leaf import recovery

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    old = getattr(recovery, "OVERRIDE_DIR", None), getattr(recovery, "_offered", None)
    recovery.OVERRIDE_DIR = str(tmp_path / "recovery")
    recovery._offered = True
    yield tmp_path
    recovery.OVERRIDE_DIR, recovery._offered = old
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def open_win(app, path):
    from leaf.window import MainWindow

    w = MainWindow()
    w.show()
    w.open_path(path)
    pump(app, 0.15)
    return w


def close_win(app, w):
    w.canvas.undo.setClean()
    w.close()
    pump(app, 0.05)


def test_window_outline_dock_and_layout_actions(app, isolated, lpdf, pdf):
    w = open_win(app, lpdf)
    try:
        assert w.viewing.dock.isVisible() and w.a_outline.isEnabled()
        for name in ("a_nav_back", "a_nav_forward", "a_view_single", "a_view_spread", "a_night_mode", "a_full_screen",
                     "a_presentation", "a_outline"):
            assert w.findChild(type(w.a_outline), name) is getattr(w, name)
        w.a_view_spread.trigger()
        assert w.canvas.view_mode == "spread" and w.settings.value("view/mode") == "spread"
        w.a_view_continuous.trigger()
        w.a_next.trigger()
        assert w.canvas.current_index() == 1
    finally:
        close_win(app, w)
    w2 = open_win(app, pdf)  # no outline -> dock hidden, action disabled
    try:
        assert not w2.viewing.dock.isVisible() and not w2.a_outline.isEnabled()
    finally:
        close_win(app, w2)


def test_window_presentation_round_trip(app, isolated, lpdf):
    w = open_win(app, lpdf)
    try:
        cv = w.canvas
        cv.go_to(1)
        w.a_presentation.trigger()
        pump(app, 0.1)
        assert cv.presenting and cv.view_mode == "single" and not w.menuBar().isVisible()
        assert cv.current_index() == 1
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent

        cv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " "))
        assert cv.current_index() == 2
        cv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        pump(app, 0.05)
        assert not cv.presenting and cv.view_mode == "continuous" and w.menuBar().isVisible()
        assert cv.current_index() == 2
        w.a_full_screen.trigger()
        assert not w.menuBar().isVisible()
        w.a_full_screen.trigger()
        assert w.menuBar().isVisible()
    finally:
        close_win(app, w)


def test_window_remembers_position(app, isolated, lpdf):
    w = open_win(app, lpdf)
    w.canvas.zoom_to(1.75)
    w.canvas.go_to(3)
    close_win(app, w)
    w2 = open_win(app, lpdf)
    try:
        assert w2.canvas.current_index() == 3 and abs(w2.canvas.zoom - 1.75) < 1e-3
    finally:
        close_win(app, w2)


def test_keyboard_and_shift_wheel_navigation(app, lpdf):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    c = make_canvas(lpdf)
    c.zoom_to(3.0)  # wider than the viewport -> horizontal scrolling possible
    sb, hb = c.verticalScrollBar(), c.horizontalScrollBar()

    def key(k):
        c.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier))

    v0 = sb.value()
    key(Qt.Key.Key_PageDown)
    assert sb.value() - v0 == c.viewport().height() - 40
    key(Qt.Key.Key_End)
    assert sb.value() == sb.maximum() and c.current_index() == len(c.order) - 1
    key(Qt.Key.Key_Home)
    assert sb.value() == 0 and c.current_index() == 0
    assert c.can_go_back()  # Home/End are jumps
    h0 = hb.value()
    pos = QPointF(200, 200)
    ev = QWheelEvent(pos, c.viewport().mapToGlobal(pos.toPoint()).toPointF(), QPoint(0, 0), QPoint(0, -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.ShiftModifier, Qt.ScrollPhase.NoScrollPhase, False)
    c.wheelEvent(ev)
    assert hb.value() > h0 and sb.value() == 0
