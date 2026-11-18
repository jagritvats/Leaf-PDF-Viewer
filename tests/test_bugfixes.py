"""Regression tests for specific bugs: test isolation of Qt settings/data, command palette selection colours,
signature fields signed with an image, Edit text on pages with a Highlight, comments panel wrapping, night mode
colours."""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette

from tests.test_editor import app, canvas_for, pump  # noqa: F401


# ------------------------------------------------- 1 + 2: shared settings store
def test_settings_and_app_data_are_private_to_the_test_session(app):  # noqa: F811
    """Concurrent test runs shared the native QSettings store (registry LeafTest/LeafE2E) and wiped each other's
    trust list / recent files / shortcut overrides mid-test with QSettings().clear() (test_g: trust lost; test_i:
    the rotation shortcut lost). conftest points QSettings and Qt's per-user data folders at a per-session folder."""
    from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths

    from tests import conftest

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafBugfix")
    try:
        s = QSettings()
        assert s.format() == QSettings.Format.IniFormat
        root = os.path.normcase(os.path.abspath(conftest._SESSION_DIR))
        assert os.path.normcase(os.path.abspath(s.fileName())).startswith(root), s.fileName()
        s.setValue("signing/trusted", ["x"])
        s.sync()
        assert QSettings().value("signing/trusted") in (["x"], "x")
        s.clear()
        for loc in (QStandardPaths.StandardLocation.AppDataLocation, QStandardPaths.StandardLocation.AppLocalDataLocation):
            p = QStandardPaths.writableLocation(loc)
            assert os.path.normcase(os.path.abspath(p)).startswith(root), p
        from leaf import agentlink

        assert os.path.normcase(os.path.abspath(agentlink.agent_dir())).startswith(root)
    finally:
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])


# ------------------------------------------------- 3: command palette colours
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_palette_selected_row_uses_highlight_colours(app, scheme):  # noqa: F811
    from PySide6.QtWidgets import QListWidget, QListWidgetItem

    from leaf.palette import Entry, _Delegate

    lw = QListWidget()
    pal = lw.palette()
    base, text = (QColor("white"), QColor("black")) if scheme == "light" else (QColor(30, 30, 30), QColor(230, 230, 230))
    hl, hl_text = QColor(0, 90, 200), QColor("white")
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.Base, base)
        pal.setColor(group, QPalette.ColorRole.Text, text)
        pal.setColor(group, QPalette.ColorRole.Highlight, hl)
        pal.setColor(group, QPalette.ColorRole.HighlightedText, hl_text)
    # an unfocused list (the palette's list never takes focus) gets a pale inactive highlight from many styles
    pal.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, QColor(220, 220, 220))
    lw.setPalette(pal)
    lw.setItemDelegate(_Delegate(lw))
    lw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    lw.resize(400, 200)
    for title in ("Rotate page", "Insert blank page"):
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, Entry(title, group="Pages › ", shortcut="Ctrl+R"))
        lw.addItem(it)
    lw.setCurrentRow(0)
    lw.show()
    pump(app, 0.05)
    img = lw.viewport().grab().toImage()
    row = lw.visualItemRect(lw.item(0))
    bg = img.pixelColor(row.left() + 6, row.top() + 2)
    assert (bg.red(), bg.green(), bg.blue()) == (hl.red(), hl.green(), hl.blue()), bg.name()
    # the text on it is drawn in HighlightedText (white), never on a pale background
    light = sum(1 for x in range(row.left(), row.right()) for y in range(row.top(), row.bottom())
                if img.pixelColor(x, y).lightness() > 200)
    assert light > 20
    pale = sum(1 for x in range(row.left() + 4, row.right() - 4) for y in range(row.top() + 3, row.bottom() - 3)
               if img.pixelColor(x, y).lightness() > 200 and img.pixelColor(x, y).saturation() < 30
               and img.pixelColor(x, y) != hl_text)
    assert pale < light // 2 + 10
    # an unselected row keeps normal Text on Base
    row2 = lw.visualItemRect(lw.item(1))
    bg2 = img.pixelColor(row2.left() + 6, row2.top() + 2)
    assert abs(bg2.lightness() - base.lightness()) < 20
    lw.close()


# ------------------------------------------------- 4: signature field signed with an image
def _sig_form(path):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    w = pymupdf.Widget()
    w.field_name = "sig"
    w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    w.rect = pymupdf.Rect(72, 400, 272, 450)
    w.field_flags = pymupdf.PDF_FIELD_IS_REQUIRED
    p.add_widget(w)
    d.save(path)
    d.close()
    return path


def _paint(item) -> QImage:
    r = item.boundingRect()
    img = QImage(int(r.width()) + 4, int(r.height()) + 4, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.translate(-r.left() + 2, -r.top() + 2)
    item.paint(p, None)
    p.end()
    return img


def _reddish(img: QImage) -> int:
    return sum(1 for y in range(img.height()) for x in range(img.width())
               if (c := img.pixelColor(x, y)).alpha() > 100 and c.red() > 150 and c.green() < 110 and c.blue() < 110)


def test_signature_field_with_placed_image_counts_as_signed(app, tmp_path):  # noqa: F811
    from leaf import forms

    c = canvas_for(_sig_form(str(tmp_path / "sig.pdf")))
    sig = next(f for f in forms.page_fields(c, c.order[0]) if f.name == "sig")
    assert sig.required and sig.is_empty()
    before = _paint(sig)
    assert _reddish(before) > 20  # required, empty: red outline (+ "Sign here")
    assert forms.next_required_empty(c) is sig
    img = QImage(400, 100, QImage.Format.Format_ARGB32)
    img.fill(QColor("navy"))
    item = forms.place_signature(c, sig, img)
    assert not sig.is_empty() and sig.has_signature_image()
    after = _paint(sig)
    assert _reddish(after) == 0 and after != before  # no required outline, no "Sign here" under the signature
    c.scene().setFocusItem(None)
    assert forms.next_required_empty(c) is None
    c.undo.undo()  # signature removed: empty again
    assert item.scene() is None and sig.is_empty()
    c.undo.redo()
    assert not sig.is_empty()
    # moved away from the field: no longer signs it
    item.setPos(item.pos() + QPointF(0, 300))
    assert sig.is_empty()


def test_authored_signature_field_with_image_drops_required_mark(app, tmp_path):  # noqa: F811
    from leaf import commands as C
    from leaf import fields as F
    from leaf.items import ImageItem

    c = canvas_for(_sig_form(str(tmp_path / "sig.pdf")))
    pg = c.order[0]
    f = F.FormFieldItem("signature", QRectF(100, 600, 200, 50), {"name": "Signature2", "required": True})
    f.setParentItem(pg.content)
    c.undo.push(C.AddItems([f]))
    assert not f.has_signature_image() and _reddish(_paint(f)) > 20
    img = QImage(400, 100, QImage.Format.Format_ARGB32)
    img.fill(QColor("navy"))
    sig = ImageItem("signature", QRectF(110, 605, 180, 40), img, {})
    sig.setParentItem(pg.content)
    c.undo.push(C.AddItems([sig]))
    assert f.has_signature_image() and _reddish(_paint(f)) == 0


# ------------------------------------------------- 5: Edit text next to a Highlight annotation
def test_edit_text_allowed_on_page_with_highlight(tmp_path):
    from leaf import textedit as T

    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 100), "Hello highlighted world", fontsize=14)
    p.insert_text((72, 140), "Second line here", fontsize=14)
    p.add_highlight_annot(p.search_for("highlighted")[0])
    doc = pymupdf.open("pdf", d.tobytes())
    assert T.PageText(doc[0]).rewrite_is_lossless()
    # stroked (outlined) text with its own line width is still refused, Highlight or not
    q = d.new_page(width=420, height=150)
    q.insert_text((40, 60), "Plain line", fontsize=11)
    xref = d.get_new_xref()
    d.update_object(xref, "<<>>")
    d.update_stream(xref, b"q BT /helv 16 Tf 1 Tr 0.5 w 40 40 Td (Outlined heading) Tj ET Q\n")
    cont = q.get_contents()
    d.xref_set_key(q.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in cont + [xref]) + "]")
    q.add_highlight_annot(q.search_for("Plain")[0])
    doc2 = pymupdf.open("pdf", d.tobytes())
    assert not T.PageText(doc2[1]).rewrite_is_lossless()


# ------------------------------------------------- 6: comments panel row height
def test_comments_row_shows_comment_when_date_wraps(app, tmp_path):  # noqa: F811
    from PySide6.QtGui import QFontMetrics

    from leaf.comments import CommentsPanel

    path = str(tmp_path / "c.pdf")
    d = pymupdf.open()
    p = d.new_page()
    a = p.add_text_annot((100, 100), "The comment text that must stay visible")
    a.set_info(title="Alexandra Longname-Example", modDate="D:20261118120000")
    a.update()
    d.save(path)
    d.close()
    c = canvas_for(path)
    panel = CommentsPanel(c)
    panel.resize(230, 500)
    panel.show()
    for _ in range(50):
        pump(app, 0.02)
        if not c.annots.busy():
            break
    panel.rebuild()
    pump(app, 0.1)
    tree = panel.tree
    node = tree.topLevelItem(0).child(0)
    text = node.text(0)
    assert "The comment text" in text and "\n" in text
    rect = tree.visualItemRect(node)
    fm = QFontMetrics(tree.font())
    width = rect.width() - tree.iconSize().width() - 12
    need = fm.boundingRect(QRect(0, 0, max(1, width), 10000), int(Qt.TextFlag.TextWordWrap), text).height()
    assert fm.horizontalAdvance(text.split("\n")[0]) > width  # the header line really wraps here
    assert rect.height() >= need, (rect.height(), need)
    # wider panel: the rows are measured again (fewer wrapped lines)
    h1 = rect.height()
    panel.resize(900, 500)
    pump(app, 0.2)
    assert tree.visualItemRect(node).height() < h1
    panel.close()


# ------------------------------------------------- 7: night mode keeps hues
def test_night_image_inverts_lightness_and_keeps_hue(app):  # noqa: F811
    from leaf.canvas import night_image

    img = QImage(1414, 1414, QImage.Format.Format_RGB888)
    img.fill(QColor("white"))
    p = QPainter(img)
    cases = [((0, 128, 0), (60, 60)), ((200, 30, 60), (300, 60)), ((0, 0, 0), (60, 300)), ((123, 45, 210), (300, 300)),
             ((10, 250, 128), (600, 600))]
    for rgb, (x, y) in cases:
        p.fillRect(x, y, 100, 100, QColor(*rgb))
    p.end()
    out = night_image(img)
    assert out.format() == img.format() and out.size() == img.size()
    for rgb, (x, y) in cases + [((255, 255, 255), (5, 5))]:
        k = 255 - max(rgb) - min(rgb)
        got = out.pixelColor(x + 50, y + 50)
        assert (got.red(), got.green(), got.blue()) == tuple(v + k for v in rgb), (rgb, got.name())
    g = out.pixelColor(110, 110)
    assert g.green() > g.red() and g.green() > g.blue()  # green stays green (plain inversion made it pink)
    assert night_image(out) == img  # its own inverse: night mode off restores the exact pixels
    t = time.perf_counter()
    night_image(img)
    assert time.perf_counter() - t < 0.2  # ~20 ms; loose bound for slow CI


def test_night_mode_on_canvas_keeps_green(app, tmp_path):  # noqa: F811
    path = str(tmp_path / "g.pdf")
    d = pymupdf.open()
    pg = d.new_page(width=300, height=300)
    pg.draw_rect(pymupdf.Rect(0, 0, 150, 300), color=None, fill=(0, 0.5, 0))
    d.save(path)
    d.close()
    c = canvas_for(path)
    c.show()
    for _ in range(100):
        pump(app, 0.02)
        if c.order and c.order[0].img is not None:
            break
    item = c.order[0]
    assert item.img is not None
    before = QImage(item.img)
    x, y = 10, item.img.height() // 2
    c.set_night(True)
    g = item.img.pixelColor(x, y)
    assert g.green() > g.red() + 60 and g.green() > g.blue() + 60, g.name()
    assert item.img.pixelColor(item.img.width() - 5, y).lightness() < 15  # white paper -> black
    c.set_night(False)
    assert item.img == before
