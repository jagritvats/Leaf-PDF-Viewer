"""End-to-end workflows through the real MainWindow (see tests/e2e_kit.py): what a person does, start to finish,
then the saved file is reopened and checked."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from tests.e2e_kit import (  # noqa: F401
    CTRL, SHIFT, accept_with, diag, find_count, labelled_pdf, page_labels, save_keys, choose, click, drag, driver, find_button, key, open_in, page_point, pump, sandbox,
    shortcut, shot, type_text, wait_until, windows, word_rect,
)
from tests.test_editor import app, make_pdf  # noqa: F401


def _sig_png(path: str) -> str:
    img = QImage(400, 140, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(QPen(QColor("#102a80"), 6))
    p.drawLine(20, 100, 120, 30)
    p.drawLine(120, 30, 200, 110)
    p.drawLine(200, 110, 380, 40)
    p.end()
    img.save(path)
    return path


def _save_as(win, driver, out: str) -> None:
    driver.on("file", lambda _: (out, "PDF files (*.pdf)"))
    shortcut(win, "Ctrl+Shift+S")
    wait_until(lambda: os.path.exists(out), msg=f"saved file; popups {driver.seen}, unexpected {driver.unexpected}, "
               f"focus {QApplication.focusWidget()}, active {QApplication.activeWindow() is win}, "
               f"item {win.canvas.scene().focusItem()}, enabled {win.a_save_as.isEnabled()}")
    pump(0.1)


# ---------------------------------------------------------------------------- a
def test_a_form_signature_note(app, windows, driver, tmp_path):
    src = str(tmp_path / "form.pdf")
    make_pdf(src)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()

    # text field: click, type, Tab moves on
    click(vp, page_point(win, 0, 150, 210))
    type_text(QApplication.focusWidget() or cv, "Ada Lovelace")
    key(QApplication.focusWidget() or cv, Qt.Key.Key_Tab)
    # checkbox and dropdown by mouse
    click(vp, page_point(win, 0, 79, 247))
    driver.on("menu", choose("Green"))
    click(vp, page_point(win, 0, 130, 280))
    shot(win, "a1_form_filled")

    # image signature: Insert > Signature > Image tab > Choose image… > Save & place
    png = _sig_png(str(tmp_path / "sig.png"))

    def fill_sig(dlg):
        dlg.tabs.setCurrentIndex(2)
        driver.on("file", lambda _: (png, ""))
        find_button(dlg, "Choose image").click()
        shot(dlg, "a2_signature_dialog")
        dlg.place_new.click()
        return QDialog_Accepted()

    driver.on("dialog", fill_sig)
    win.a_sign.trigger()
    pump(0.2)
    sig = [i for i in cv.selected_overlays() if getattr(i, "kind", "") == "signature"]
    assert len(sig) == 1, cv.selected_overlays()
    sig = sig[0]
    # drag it into place
    start = cv.mapFromScene(sig.sceneBoundingRect().center())
    target = page_point(win, 0, 400, 600)
    start = cv.mapFromScene(sig.sceneBoundingRect().center())
    drag(vp, start, target)
    c = sig.mapRectToItem(sig.parentItem(), sig.boundingRect()).center()
    assert abs(c.x() - 400) < 6 and abs(c.y() - 600) < 6, c

    # sticky note: N, click, type the comment
    cv.setFocus()
    key(cv, Qt.Key.Key_N)
    assert cv.tool == "note"

    def note(dlg):
        dlg.edit.setPlainText("Please countersign")
        shot(win, "a3_note_dialog")

    driver.on("dialog", accept_with(note))
    click(vp, page_point(win, 0, 480, 120))
    key(cv, Qt.Key.Key_Escape)
    shot(win, "a4_before_save")

    out = str(tmp_path / "a-out.pdf")
    _save_as(win, driver, out)
    assert cv.undo.isClean() and not win.isWindowModified()
    driver.check()

    d = pymupdf.open(out)
    vals = {w.field_name: w.field_value for w in d[0].widgets()}
    assert vals["name"] == "Ada Lovelace", vals
    assert vals["agree"] not in (False, "Off", ""), vals
    assert vals["color"] == "Green", vals
    imgs = d[0].get_image_info()
    assert any(abs((im["bbox"][0] + im["bbox"][2]) / 2 - 400) < 8 for im in imgs), imgs
    notes = [a for a in d[0].annots() if a.type[1] == "Text"]
    assert notes and notes[0].info["content"] == "Please countersign"
    d.close()

    # reopen in a fresh window: values show, nothing dirty
    win2 = windows()
    open_in(win2, out)
    shot(win2, "a5_reopened")
    assert not win2.isWindowModified()
    f = [f for f in win2.canvas._all_fields() if f.name == "name"]
    assert f and f[0].value() == "Ada Lovelace"


def QDialog_Accepted():
    from PySide6.QtWidgets import QDialog

    return QDialog.DialogCode.Accepted


# ---------------------------------------------------------------------------- b
def test_b_annotate(app, windows, driver, tmp_path):
    from leaf.items import HighlightItem, ShapeItem, TextItem

    src = str(tmp_path / "annot.pdf")
    make_pdf(src, rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()

    # highlight "quick brown fox" by dragging with H
    key(cv, Qt.Key.Key_H)
    assert cv.tool == "highlight"
    q, f = word_rect(win, 0, "quick"), word_rect(win, 0, "fox")
    my = (q[1] + q[3]) / 2
    drag(vp, page_point(win, 0, q[0] + 1, my), page_point(win, 0, f[2] - 1, my))
    hl = [i for i in cv.order[0].overlays() if isinstance(i, HighlightItem)]
    assert len(hl) == 1 and hl[0].marked_text == "quick brown fox", [getattr(h, "marked_text", "") for h in hl]

    # text box: T, click, type, select all + Ctrl+B, finish, then pick a font in the Style panel
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 100, 400))
    ed = cv.scene().focusItem()
    assert isinstance(ed, TextItem), ed
    type_text(cv, "Bold words")
    shortcut(win, "Ctrl+A")
    shortcut(win, "Ctrl+B")
    assert win.a_text_bold.isEnabled()
    key(cv, Qt.Key.Key_Escape)  # finish editing
    pump(0.1)
    assert not cv._editing_text()
    boxes = [i for i in cv.order[0].overlays() if isinstance(i, TextItem)]
    assert len(boxes) == 1 and boxes[0].toPlainText() == "Bold words"
    box = boxes[0]
    cv.scene().clearSelection()
    box.setSelected(True)
    pump(0.1)
    fb = win.props.font_box
    assert fb.isVisible(), "Style panel shows no font picker for a selected text box"
    fb.setFocus()
    fb.lineEdit().selectAll()
    type_text(fb.lineEdit(), "Serif (Times)")
    key(fb.lineEdit(), Qt.Key.Key_Return)
    assert box.props.get("font") == "Times", box.props
    shot(win, "b1_textbox")

    # rectangle: R, drag; then V and move it with the mouse
    cv.setFocus()
    key(cv, Qt.Key.Key_R)
    drag(vp, page_point(win, 0, 300, 500), page_point(win, 0, 420, 580))
    rects = [i for i in cv.order[0].overlays() if isinstance(i, ShapeItem) and i.kind == "rect"]
    assert len(rects) == 1
    rect = rects[0]

    def rect_pos():
        r = rect.mapRectToItem(rect.parentItem(), rect.boundingRect())
        return r.center()

    c0 = rect_pos()
    key(cv, Qt.Key.Key_V)
    grab = page_point(win, 0, 360, 540)  # inside the rectangle
    drag(vp, grab, page_point(win, 0, 460, 640, scroll=False))
    c1 = rect_pos()
    assert abs((c1 - c0).x() - 100) < 4 and abs((c1 - c0).y() - 100) < 4, (c0, c1)

    # undo / redo the move with the keyboard
    shortcut(win, "Ctrl+Z")
    assert abs((rect_pos() - c0).x()) < 1, rect_pos()
    shortcut(win, "Ctrl+Y")
    assert abs((rect_pos() - c1).x()) < 1, rect_pos()

    # colour from the floating mini toolbar
    cv.scene().clearSelection()
    rect.setSelected(True)
    pump(0.2)
    mb = win.minibar
    assert mb.isVisible()
    shot(win, "b2_minibar")
    blue = next(s for s in mb.swatches if QColor(s.color).name() == "#1f4fd1")
    click(blue, blue.rect().center())
    assert rect.props["color"] == "#1f4fd1"

    save_keys(win, driver)
    driver.check()

    d = pymupdf.open(src)
    p = d[0]
    kinds = {a.type[1]: a for a in p.annots()}
    assert "Highlight" in kinds and "Square" in kinds, kinds
    sq = kinds["Square"]
    assert abs(sq.rect.x0 - 400) < 4 and abs(sq.rect.y0 - 600) < 4, sq.rect
    assert QColor.fromRgbF(*sq.colors["stroke"]).name() == "#1f4fd1"
    spans = [s for b in p.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"] if "Bold" in s["text"]]
    assert spans, "text box not in the saved page"
    assert spans[0]["flags"] & 16 or "bold" in spans[0]["font"].lower(), spans[0]
    assert "times" in spans[0]["font"].lower() or spans[0]["flags"] & 4, spans[0]  # serif
    d.close()


# ---------------------------------------------------------------------------- c
_labelled_pdf = labelled_pdf
_labels = page_labels


def test_c_pages(app, windows, driver, tmp_path, monkeypatch):
    from tests.e2e_kit import drag_and_drop, thumb_point

    a = _labelled_pdf(str(tmp_path / "a.pdf"), ["A1", "A2", "A3"])
    b = _labelled_pdf(str(tmp_path / "b.pdf"), ["B1", "B2"])
    win = windows()
    open_in(win, a)
    sb = win.sidebar
    wait_until(lambda: sb.count() == 3, msg="thumbnails")

    # drag A3 to the top
    drag_and_drop(monkeypatch, sb.viewport(), thumb_point(sb, 2), sb.viewport(), thumb_point(sb, 0) - QPoint(0, 20))
    pump(0.1)
    assert _labels(win) == ["A3", "A1", "A2"], _labels(win)

    # rotate A1: click its thumbnail, Ctrl+Shift+R
    click(sb.viewport(), thumb_point(sb, 1))
    assert win.canvas.current_index() == 1, (sb.currentRow(), sb.selected_rows(), win.canvas.zoom,
                                             win.canvas.verticalScrollBar().value(), win.canvas.verticalScrollBar().maximum(),
                                             win.canvas.viewport().size(), sb.viewport().size())
    shortcut(win, "Ctrl+Shift+R")
    assert win.canvas.doc.pages[1].rotation == 90, [r.rotation for r in win.canvas.doc.pages]
    # delete A2 with the Delete key in the page list
    click(sb.viewport(), thumb_point(sb, 2))
    key(sb, Qt.Key.Key_Delete)
    assert _labels(win) == ["A3", "A1"], _labels(win)
    # blank page after A3
    click(sb.viewport(), thumb_point(sb, 0))
    win.a_blank_after.trigger()
    assert _labels(win) == ["A3", "", "A1"], _labels(win)

    # copy B2 in a second window, paste after A1
    win2 = windows()
    open_in(win2, b)
    sb2 = win2.sidebar
    wait_until(lambda: sb2.count() == 2, msg="thumbnails 2")
    click(sb2.viewport(), thumb_point(sb2, 1))
    key(sb2, Qt.Key.Key_C, CTRL)
    from tests.e2e_kit import activate
    activate(win)
    click(sb.viewport(), thumb_point(sb, 2))
    key(sb, Qt.Key.Key_V, CTRL)
    assert _labels(win) == ["A3", "", "A1", "B2"], _labels(win)
    shot(win, "c1_pages")

    # crop A3 with the crop tool
    cv = win.canvas
    cv.setFocus()
    key(cv, Qt.Key.Key_C)
    assert cv.tool == "crop", cv.tool
    driver.on("menu", choose("Crop this page"))
    drag(cv.viewport(), page_point(win, 0, 50, 60), page_point(win, 0, 400, 300, scroll=False))
    assert cv.doc.pages[0].crop is not None
    shot(win, "c2_cropped")

    save_keys(win, driver)
    d = pymupdf.open(a)
    assert [p.get_text().split()[0] if p.get_text().strip() else "" for p in d] == ["A3", "", "A1", "B2"]
    assert d[2].rotation == 90
    cb = d[0].cropbox
    assert abs(cb.x0 - 50) < 2 and abs(cb.width - 350) < 2, cb
    d.close()

    # extract pages 3-4
    out = str(tmp_path / "extract.pdf")
    driver.on("dialog", accept_with(lambda dlg: (dlg.range.setText("3-4"), None)[1]))
    driver.on("file", lambda _: (out, ""))
    win.a_extract.trigger()
    wait_until(lambda: os.path.exists(out), msg="extracted file")
    driver.check()
    d = pymupdf.open(out)
    assert [p.get_text().split()[0] for p in d] == ["A1", "B2"] and d[0].rotation == 90
    d.close()


# ---------------------------------------------------------------------------- d
def test_d_edit_text(app, windows, driver, tmp_path):
    from PySide6.QtGui import QTextCursor

    src = str(tmp_path / "text.pdf")
    make_pdf(src, rotate_second=False, form=False)
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    tool = win.textedit_tool

    key(cv, Qt.Key.Key_I)
    assert cv.tool == "edittext", cv.tool
    s = word_rect(win, 0, "Second")
    at = page_point(win, 0, s[0] + 4, (s[1] + s[3]) / 2)
    from tests.e2e_kit import move
    move(vp, at, held=False)
    wait_until(lambda: tool.hover is not None and tool.hover.isVisible(), msg="hover outline")
    shot(win, "d1_hover")
    click(vp, at)
    assert tool.editing(), "the click didn't open the editor"
    ed = tool.editor
    assert ed.plain_text().startswith("Second line"), ed.plain_text()
    c = ed.textCursor()
    c.movePosition(QTextCursor.MoveOperation.End)
    ed.setTextCursor(c)
    key(cv, Qt.Key.Key_Backspace)  # drop the full stop
    type_text(cv, " EDITED")
    shot(win, "d2_editing")
    key(cv, Qt.Key.Key_Return)
    wait_until(lambda: not tool.editing(), msg="editor closed")
    assert cv.undo.canUndo()

    assert find_count(win, "EDITED") == 1
    shot(win, "d3_found")
    cv.setFocus()
    shortcut(win, "Ctrl+Z")
    assert find_count(win, "EDITED") == 0
    assert find_count(win, "selection tests") == 3
    cv.setFocus()
    shortcut(win, "Ctrl+Y")
    assert find_count(win, "EDITED") == 1
    save_keys(win, driver)
    driver.check()
    d = pymupdf.open(src)
    t = d[0].get_text()
    assert "Second line of text for selection tests EDITED" in t, t
    assert "tests." not in t, t  # the old glyphs are gone
    assert "selection tests." in d[1].get_text()
    d.close()


# ---------------------------------------------------------------------------- e
def test_e_redact_decorate_protect(app, windows, driver, tmp_path):
    src = str(tmp_path / "secret.pdf")
    make_pdf(src, rotate_second=True, form=False)
    win = windows()
    open_in(win, src)
    cv = win.canvas

    # Search & redact "fox" (non-modal dialog)
    win.a_search_redact.trigger()
    dlg = win._search_redact_dlg
    wait_until(dlg.isVisible, msg="search & redact dialog")
    dlg.query.setFocus()
    type_text(dlg.query, "fox")
    click(dlg.b_search, dlg.b_search.rect().center())
    wait_until(lambda: dlg._job is None and dlg.tree.topLevelItemCount() > 0, msg="redaction hits")
    assert dlg.tree.topLevelItemCount() == 3
    shot(dlg, "e1_search_redact")
    click(dlg.b_mark, dlg.b_mark.rect().center())
    assert not dlg.isVisible()
    from tests.e2e_kit import activate
    activate(win)

    # watermark
    def wm(d):
        d.text.selectAll()
        type_text(d.text, "CONFIDENTIAL")
        shot(d, "e2_watermark")
    driver.on("dialog", accept_with(wm))
    win.a_watermark.trigger()
    assert driver.queue == []

    # page numbers via the header/footer dialog's "Page numbers" menu
    def hf(d):
        btn = find_button(d, "Page numbers")
        driver.on("menu", choose("Page {page} of {pages}"))
        d.slot_edits[5].setFocus()
        click(btn, btn.rect().center())
        pump(0.1)
        assert d.slot_edits[5].text() == "Page {page} of {pages}", [e.text() for e in d.slot_edits]
        d.slot_edits[4].clear()
        shot(d, "e3_header_footer")
    driver.on("dialog", accept_with(hf))
    win.a_header_footer.trigger()

    # password
    def pw(d):
        for e in (d.user_pw, d.user_pw2):
            e.setFocus()
            type_text(e, "s3cret")
        shot(d, "e4_protect")
    driver.on("dialog", accept_with(pw, button="Protect"))
    win.a_protect.trigger()
    assert cv.doc.security is not None and cv.doc.security.encrypted
    shot(win, "e5_before_save")

    # save: the redaction warning comes first
    driver.on("dialog", accept_with(lambda b: shot(b, "e6_redact_confirm"), button="Save and remove"))
    save_keys(win, driver)
    driver.check()

    d = pymupdf.open(src)
    assert d.needs_pass and d.authenticate("s3cret")
    for i, p in enumerate(d):
        t = p.get_text()
        assert "fox" not in t and "quick brown" in t, t
        assert "CONFIDENTIAL" in t, t
        assert f"Page {i + 1} of 3" in t, t
    d.close()

    # reopen in a fresh window: asks for the password
    win2 = windows()
    driver.on("input", lambda _: ("s3cret", True))
    open_in(win2, src)
    assert len(win2.canvas.doc.pages) == 3
    shot(win2, "e7_reopened")
    driver.check()


# ---------------------------------------------------------------------------- g
def test_g_digital_signature(app, windows, driver, tmp_path):
    src = str(tmp_path / "contract.pdf")
    make_pdf(src, rotate_second=False, form=True)
    signed = str(tmp_path / "contract-signed.pdf")
    p12 = str(tmp_path / "me.p12")
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()

    def create(d):
        type_text(d.name, "Test Signer")
        d.pw.setFocus()
        type_text(d.pw, "pw123")
        d.pw2.setFocus()
        type_text(d.pw2, "pw123")
        driver.on("file", lambda _: (p12, ""))
        shot(d, "g1_create_identity")

    def sign(d):
        driver.on("dialog", accept_with(create, button="Create"))
        click(find_button(d, "Create a self-signed"), find_button(d, "Create a self-signed").rect().center())
        assert d.ident.currentData() == os.path.abspath(p12) and d.pw.text() == "pw123"
        assert d.place.currentData() == "invisible" or d.place.findData("invisible") >= 0
        d.place.setCurrentIndex(d.place.findData("invisible"))
        d.reason.setText("Approved")
        shot(d, "g2_sign_dialog")
        driver.on("file", lambda _: (signed, ""))  # then: where to save the signed copy
    driver.on("dialog", accept_with(sign, button="Sign"))
    win.a_sign_digital.trigger()
    wait_until(lambda: cv.doc is not None and cv.doc.path == os.path.abspath(signed), msg="signed file reopened " + diag(win, driver))

    panel = win.signing.panel
    shortcut(win, "F12")
    wait_until(lambda: panel.statuses, secs=30, msg="verification")
    st = panel.statuses[0]
    assert st.intact and st.valid and st.signer.startswith("Test Signer"), st
    assert st.state == "untrusted", st.state  # self-signed: intact, not yet trusted
    driver.on("msg", lambda _: QMessageBox_Yes())
    click(panel.trust_btn, panel.trust_btn.rect().center())
    wait_until(lambda: panel.statuses and panel.statuses[0].state == "valid", secs=30,
               msg=f"valid after trusting: {[(s.state, s.trust, s.trust_note, s.error) for s in panel.statuses]} "
                   f"trusted={len(win.settings.value('signing/trusted') or [])} job={win.signing._job} "
                   f"summary={panel.summary.text()!r} {driver.seen}")
    shot(win, "g3_signatures_panel")

    # fill a field, save in place: incremental, signature still valid with an allowed change
    activate_canvas = cv.setFocus
    activate_canvas()
    click(vp, page_point(win, 0, 150, 210))
    type_text(cv, "Filled later")
    key(cv, Qt.Key.Key_Escape) if False else click(vp, page_point(win, 0, 450, 700))
    assert not cv.undo.isClean()
    before = os.path.getsize(signed)
    with open(signed, "rb") as f:
        head = f.read()
    save_keys(win, driver)
    with open(signed, "rb") as f:
        after = f.read()
    assert after.startswith(head) and len(after) > before, "not an incremental save"
    wait_until(lambda: panel.statuses and panel.summary.text() != "Checking signatures…", secs=30, msg="re-verify")
    st = panel.statuses[0]
    assert st.state == "valid" and st.changes == "form" and st.changes_ok, (st.state, st.changes, st.changes_ok)
    shot(win, "g4_after_fill")
    driver.check()
    d = pymupdf.open(signed)
    assert {w.field_name: w.field_value for w in d[0].widgets()}["name"] == "Filled later"
    d.close()


def QMessageBox_Yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


# ---------------------------------------------------------------------------- f
def _scanned_pdf(path: str) -> str:
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 140), "Invoice number 4711", fontsize=28)
    p.insert_text((72, 200), "Total amount due today", fontsize=28)
    pix = p.get_pixmap(dpi=200)
    out = pymupdf.open()
    out.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
    out.save(path)
    out.close()
    d.close()
    return path


def test_f_ocr(app, windows, driver, tmp_path):
    import pytest

    from leaf import ocr as O

    if not O.available_engines():
        pytest.skip("no OCR engine installed")
    src = _scanned_pdf(str(tmp_path / "scan.pdf"))
    win = windows()
    open_in(win, src)
    cv, vp = win.canvas, win.canvas.viewport()
    assert not cv.doc.primary.doc[0].get_text().strip()
    pump(0.8)  # the "looks scanned" offer is a toast, not a dialog

    driver.on("dialog", accept_with(lambda d: shot(d, "f1_ocr_dialog")))
    win.a_ocr.trigger()
    st = win._ocr
    wait_until(lambda: not st.running, secs=120, msg="OCR finished")
    assert cv.doc.ocr, "no recognized text"
    shot(win, "f2_recognized")

    # select the recognized words with the mouse
    entry = next(iter(cv.doc.ocr.values()))
    words = O.search_words(getattr(entry, "words", entry), "Invoice")
    assert words, "OCR missed 'Invoice'"
    r = words[0]
    y = r.y0 + r.height / 2
    drag(vp, page_point(win, 0, r.x0 + 2, y), page_point(win, 0, r.x0 + 300, y, scroll=False))
    assert "Invoice" in cv.selected_text(), cv.selected_text()
    # the find bar finds recognized (not yet saved) text
    assert find_count(win, "4711") == 1

    save_keys(win, driver)
    driver.check()
    d = pymupdf.open(src)
    assert d[0].search_for("Invoice") and d[0].search_for("4711"), d[0].get_text()
    d.close()


# ---------------------------------------------------------------------------- h
def test_h_palette_shortcuts_history(app, windows, driver, tmp_path):
    from PySide6.QtWidgets import QUndoView

    from leaf.palette import CommandPalette

    src = _labelled_pdf(str(tmp_path / "pal.pdf"), ["P1", "P2", "P3"])
    win = windows()
    open_in(win, src)
    cv = win.canvas

    def palette(query: str):
        shortcut(win, "Ctrl+K")
        pal = win.findChild(CommandPalette)
        assert pal is not None, "Ctrl+K didn't open the palette"
        if not pal.isVisible() and QApplication.platformName() != "offscreen":
            import pytest
            # Windows closes popups of an app that isn't in the foreground (a test runner in a terminal)
            pytest.skip("popup closed: the test process isn't the foreground app")
        assert pal.isVisible(), "Ctrl+K didn't open the palette"
        assert pal.edit.hasFocus()
        type_text(pal.edit, query)
        shot(pal, "h1_palette")
        first = pal.current_entry()
        key(pal.edit, Qt.Key.Key_Return)
        pump(0.1)
        assert not pal.isVisible()
        from tests.e2e_kit import activate
        activate(win)
        return first

    e = palette("rotate right")
    assert e is not None and "Rotate right" in e.title, e
    assert cv.doc.pages[0].rotation == 90
    e = palette("3")
    assert cv.current_index() == 2, (e.title if e else None, cv.current_index())
    e = palette("night")
    assert win.a_night_mode.isChecked() and cv.night, e.title if e else None
    palette("night")  # back to normal

    # rebind Rotate left to Alt+Shift+L in Edit › Keyboard shortcuts (Ctrl+, opens Settings, which links to it)
    def rebind(d):
        ed = d.edits[win.a_rot_l]
        d.tree.scrollToItem(d.rows[win.a_rot_l])
        ed.setFocus()
        QTest_keyClick(ed, Qt.Key.Key_L, Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier)
        wait_until(lambda: d.pending[win.a_rot_l] and d.pending[win.a_rot_l][0].toString() == "Alt+Shift+L",
                   secs=4, msg=f"pending shortcut {[s.toString() for s in d.pending[win.a_rot_l]]}")
        shot(d, "h2_shortcuts")
    driver.on("dialog", accept_with(rebind))
    win.a_shortcut_prefs.trigger()
    assert [s.toString() for s in win.a_rot_l.shortcuts()][:1] == ["Alt+Shift+L"], win.a_rot_l.shortcuts()
    cv.setFocus()
    cv.go_to(0)
    pump(0.1)
    shortcut(win, "Alt+Shift+L")
    assert cv.doc.pages[0].rotation == 0, cv.doc.pages[0].rotation

    # History panel: jump back to the opened file, then forward again
    shortcut(win, "F7")
    view = win.history_dock.widget()
    assert isinstance(view, QUndoView) and win.history_dock.isVisible()
    stack = cv.undo
    assert stack.count() == 2, [stack.text(i) for i in range(stack.count())]
    m = view.model()
    click(view.viewport(), view.visualRect(m.index(0, 0)).center())
    assert stack.index() == 0 and cv.doc.pages[0].rotation == 0
    click(view.viewport(), view.visualRect(m.index(1, 0)).center())
    assert stack.index() == 1 and cv.doc.pages[0].rotation == 90
    shot(win, "h3_history")
    driver.check()


def QTest_keyClick(w, k, mods):
    from PySide6.QtTest import QTest

    QTest.keyClick(w, k, mods)


# ---------------------------------------------------------------------------- i
def test_i_welcome_recent_close_prompts(app, windows, driver, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox

    from tests.e2e_kit import double_click

    src = _labelled_pdf(str(tmp_path / "recent.pdf"), ["R1", "R2"])
    QSettings().setValue("recent", [os.path.abspath(src)])
    win = windows()
    assert win.current_view() == "welcome"
    wv = win.welcome
    wait_until(lambda: wv.recent.count() == 1, msg="recent list")
    pump(0.3)
    shot(win, "i1_welcome")
    it = wv.recent.item(0)
    double_click(wv.recent.viewport(), wv.recent.visualItemRect(it).center())
    wait_until(lambda: win.canvas.doc is not None, msg="recent file opened")
    assert win.current_view() == "document"
    win.canvas.setFocus()

    def dirty():
        shortcut(win, "Ctrl+Shift+R")
        assert not win.canvas.undo.isClean() and win.isWindowModified()

    asked = []

    def answer(button):
        def h(call):
            name, args, kw = call
            asked.append(args[2] if len(args) > 2 else "")
            return button
        return h

    # Cancel: the window stays, the edit stays
    dirty()
    driver.on("msg", answer(QMessageBox.StandardButton.Cancel))
    shortcut(win, "Ctrl+F4")
    assert win.isVisible() and not win.canvas.undo.isClean()
    assert asked and "recent.pdf" in asked[-1], asked
    # Discard: closes without writing
    mtime = os.path.getmtime(src)
    driver.on("msg", answer(QMessageBox.StandardButton.Discard))
    shortcut(win, "Ctrl+F4")
    wait_until(lambda: _closed(win), msg="closed after Discard")
    assert os.path.getmtime(src) == mtime
    d = pymupdf.open(src)
    assert d[0].rotation == 0
    d.close()

    # Save: writes, then closes
    win2 = windows()
    assert win2.current_view() == "welcome"
    wait_until(lambda: win2.welcome.recent.count() == 1, msg="recent list 2")
    it = win2.welcome.recent.item(0)
    double_click(win2.welcome.recent.viewport(), win2.welcome.recent.visualItemRect(it).center())
    wait_until(lambda: win2.canvas.doc is not None, msg="recent file opened 2")
    win = win2
    win.canvas.setFocus()
    dirty()
    driver.on("msg", answer(QMessageBox.StandardButton.Save))
    shortcut(win, "Ctrl+F4")
    wait_until(lambda: _closed(win), msg="closed after Save")
    d = pymupdf.open(src)
    assert d[0].rotation == 90
    d.close()
    driver.check()


def _closed(w) -> bool:
    try:
        return not w.isVisible()
    except RuntimeError:  # WA_DeleteOnClose: already deleted
        return True
