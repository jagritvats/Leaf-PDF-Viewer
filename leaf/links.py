"""Links: add, edit and delete link annotations; the Link tool and its dialog.

Model: `PdfDocument.links` maps a page uid to a `PageLinks` (absent = the page's links are untouched). A PageLinks names
the page's ORIGINAL link annotations (by xref) that were deleted or edited, and the links made in Leaf (`Link` specs;
an edited original becomes a removed xref plus a new spec). Specs keep their rectangle in the source page.rect space (the
space `page.get_links()` reports and overlays use) and a page target as a page UID, so a target follows the page through
reordering, merging and duplication; it is resolved to the final page number only at save.

Save: `remove_links` runs per page in `PdfDocument._apply` (before the page is merged, while xrefs still match the
source); `apply_structure` runs once the page list is final (after `_assemble`, or after `_apply` for an incremental
save, and before Sanitize) and writes the new links, then the edited outline (leaf/outline.py) and attachments
(leaf/attachments.py). Links whose target page isn't in the output are dropped (`dropped_targets` tells the UI first).

Coordinates: link rectangles and destinations are written in PDF user space (page.rect -> derotate -> PDF), which
doesn't depend on the output page's rotation or crop. (`insert_link` itself treats its points as unrotated, which
puts links on rotated pages in the wrong place.)"""
from __future__ import annotations

import itertools
import os
import urllib.parse
from dataclasses import dataclass, field, replace

import pymupdf
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPalette, QPen, QUndoCommand
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGraphicsItem, QGraphicsRectItem, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QRadioButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

_ids = itertools.count(1)
KINDS = ("uri", "page", "email", "file")
KIND_NAMES = {"uri": "Web address", "page": "Page in this document", "email": "Email address", "file": "File"}


@dataclass(frozen=True)
class Link:
    rect: tuple  # (x0, y0, x1, y1) in the source page.rect space of the page that carries the link
    kind: str  # "uri" | "page" | "email" | "file"
    target: str = ""  # uri: the address; email: a mailto: URI; file: the path
    uid: int | None = None  # page: the target page's PageRef.uid
    point: tuple | None = None  # page: (x, y) in the target page's page.rect space; None = top of the page
    id: int = field(default_factory=lambda: next(_ids))


@dataclass(frozen=True)
class PageLinks:
    removed: frozenset = frozenset()  # xrefs of the page's original link annotations that are gone (deleted or edited)
    added: tuple = ()  # Link specs made in Leaf, in drawing order


# ----------------------------------------------------------------------------------------------- model helpers
def ref_for_uid(doc, uid):
    return next((r for r in doc.pages if r.uid == uid), None)


def uid_for(doc, src, index: int):
    """The uid of the first page in the page list that shows page `index` of `src` (None if it isn't there)."""
    return next((r.uid for r in doc.pages if r.src is src and r.index == index), None)


def mailto(address: str, subject: str = "") -> str:
    a = address.strip()
    if a.lower().startswith("mailto:"):
        a = a[7:]
    q = "?subject=" + urllib.parse.quote(subject.strip()) if subject.strip() else ""
    return "mailto:" + urllib.parse.quote(a, safe="@.+-_~!$&'*=,;") + q


def parse_mailto(uri: str) -> tuple[str, str]:
    u = uri[7:] if uri.lower().startswith("mailto:") else uri
    addr, _, query = u.partition("?")
    subject = urllib.parse.parse_qs(query).get("subject", [""])[0]
    return urllib.parse.unquote(addr), subject


def normalize_url(text: str) -> str:
    t = text.strip()
    if not t:
        return t
    low = t.lower()
    if "://" not in t and not low.startswith(("mailto:", "tel:", "urn:", "news:", "data:")):
        t = "https://" + t
    return t


def from_link_dict(doc, ref, ln: dict) -> Link | None:
    """An original link (a `page.get_links()` dict of page `ref`) as an editable spec, or None if it can't be edited."""
    r = ln["from"]
    rect = (r.x0, r.y0, r.x1, r.y1)
    kind = ln.get("kind")
    if kind == pymupdf.LINK_URI:
        uri = ln.get("uri") or ""
        return Link(rect, "email" if uri.lower().startswith("mailto:") else "uri", uri)
    if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and ln.get("page", -1) >= 0:
        uid = uid_for(doc, ref.src, ln["page"])
        to = ln.get("to")
        pt = (to.x, to.y) if to is not None and (to.x or to.y) else None
        return Link(rect, "page", uid=uid, point=pt)
    if kind in (pymupdf.LINK_GOTOR, pymupdf.LINK_LAUNCH) and ln.get("file"):
        return Link(rect, "file", ln["file"])
    return None


def display_dict(link: Link) -> dict:
    """A spec as a link dict like `get_links()` gives (what the canvas hit-tests and follows)."""
    r = pymupdf.Rect(link.rect)
    if link.kind == "page":
        to = pymupdf.Point(*link.point) if link.point else pymupdf.Point(0, 0)
        d = {"kind": pymupdf.LINK_GOTO, "page": 0, "to": to, "target_uid": link.uid}
    elif link.kind in ("uri", "email"):
        d = {"kind": pymupdf.LINK_URI, "uri": link.target}
    else:
        d = {"kind": pymupdf.LINK_LAUNCH, "file": link.target}
    d.update({"from": r, "xref": 0, "leaf_link": link})
    return d


_shown: dict = {}  # (id of the originals list, state) -> (originals, result): the same dicts while nothing changes


def shown_links(originals: list, state: PageLinks) -> list:
    """canvas.links_on for an edited page: the originals that remain, then the links made in Leaf."""
    key = (id(originals), state)
    hit = _shown.get(key)
    if hit is not None and hit[0] is originals:
        return hit[1]
    out = [(r, ln) for r, ln in originals if ln.get("xref") not in state.removed]
    for link in state.added:
        x0, y0, x1, y1 = link.rect
        out.append((QRectF(x0, y0, x1 - x0, y1 - y0), display_dict(link)))
    if len(_shown) > 64:
        _shown.clear()
    _shown[key] = (originals, out)
    return out


def describe(doc, link_or_dict) -> str:
    """Short text for a link target ('Page 3', 'https://…', 'mail@x', 'report.pdf')."""
    link = link_or_dict.get("leaf_link") if isinstance(link_or_dict, dict) else link_or_dict
    if link is None:
        return ""
    if link.kind == "page":
        i = next((k for k, r in enumerate(doc.pages) if r.uid == link.uid), None)
        return f"Page {i + 1}" if i is not None else "A deleted page"
    if link.kind == "email":
        return parse_mailto(link.target)[0]
    if link.kind == "file":
        return os.path.basename(link.target) or link.target
    return link.target


def dropped_targets(doc) -> int:
    """Links made in Leaf whose target page is no longer in the document (they are left out when saving)."""
    have = {r.uid for r in doc.pages}
    return sum(1 for uid, st in doc.links.items() if uid in have
               for link in st.added if link.kind == "page" and link.uid not in have)


# --------------------------------------------------------------------------------------------------- commands
class SetLinks(QUndoCommand):
    def __init__(self, canvas, uid: int, old: PageLinks | None, new: PageLinks | None, text: str):
        super().__init__(text)
        self.canvas, self.uid, self.old, self.new = canvas, uid, old, new

    def _set(self, st) -> None:
        links = self.canvas.doc.links
        if st is None:
            links.pop(self.uid, None)
        else:
            links[self.uid] = st
        notify(self.canvas)

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def notify(canvas) -> None:
    canvas._link_hover = None
    tool = getattr(canvas, "link_tool", None)
    if tool is not None:
        tool.refresh()
    canvas.viewport().update()


def _state(doc, uid) -> PageLinks:
    return doc.links.get(uid) or PageLinks()


def _push(canvas, ref, new: PageLinks, text: str) -> None:
    old = canvas.doc.links.get(ref.uid)
    if not new.removed and not new.added:
        new = None
    if new == old:
        return
    canvas.undo.push(SetLinks(canvas, ref.uid, old, new, text))


def add_links(canvas, ref, links) -> None:
    """Add links (specs) to page `ref` as one undo step."""
    links = list(links)
    if not links:
        return
    st = _state(canvas.doc, ref.uid)
    _push(canvas, ref, replace(st, added=st.added + tuple(links)), "Add link" if len(links) == 1 else f"Add {len(links)} links")


def delete_link(canvas, ref, ln: dict) -> None:
    """Delete a shown link (a dict from canvas.links_on)."""
    st = _state(canvas.doc, ref.uid)
    link = ln.get("leaf_link")
    if link is not None:
        new = replace(st, added=tuple(x for x in st.added if x.id != link.id))
    elif ln.get("xref"):
        new = replace(st, removed=st.removed | {ln["xref"]})
    else:
        return
    _push(canvas, ref, new, "Delete link")


def update_link(canvas, ref, ln: dict, new_link: Link) -> None:
    """Replace a shown link by `new_link` (keeps its place in the page's link order when it was made in Leaf)."""
    st = _state(canvas.doc, ref.uid)
    old = ln.get("leaf_link")
    if old is not None:
        if new_link == old:
            return
        new = replace(st, added=tuple(new_link if x.id == old.id else x for x in st.added))
    elif ln.get("xref"):
        new = PageLinks(st.removed | {ln["xref"]}, st.added + (new_link,))
    else:
        return
    _push(canvas, ref, new, "Edit link")


# ------------------------------------------------------------------------------------------------------- save
def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def to_pdf_point(page: pymupdf.Page, pt) -> pymupdf.Point:
    """page.rect space -> PDF user space (independent of the output page's rotation and crop)."""
    return pymupdf.Point(pt) * page.derotation_matrix * ~page.transformation_matrix


def to_pdf_rect(page: pymupdf.Page, r) -> pymupdf.Rect:
    q = pymupdf.Rect(r) * page.derotation_matrix * ~page.transformation_matrix
    return q.normalize()


def _ascii_uri(uri: str) -> str:
    return "".join(c if 32 < ord(c) < 127 else urllib.parse.quote(c) for c in uri)


def goto_action(doc, out, target_ref, out_pno: int, point) -> str:
    """GoTo action for page `out_pno` of `out`; `point` in the target's source page.rect space or None (top)."""
    sp = doc.page(target_ref)
    if point is None:
        v = doc.view_rect(target_ref)
        point = (v.x0, v.y0)
    p = to_pdf_point(sp, point)
    return f"<</S/GoTo/D[{out.page_xref(out_pno)} 0 R/XYZ {_num(p.x)} {_num(p.y)} 0]>>"


def file_action(path: str) -> str:
    spec = f"<</Type/Filespec/F{pymupdf.get_pdf_str(path)}/UF{pymupdf.get_pdf_str(path)}>>"
    if path.lower().endswith(".pdf"):
        return f"<</S/GoToR/F{spec}/D[0/Fit]>>"
    return f"<</S/Launch/F{spec}>>"


def link_action(doc, out, link: Link, pos: dict, pages: list) -> str | None:
    if link.kind == "page":
        i = pos.get(link.uid)
        if i is None:
            return None
        return goto_action(doc, out, pages[i], i, link.point)
    if link.kind in ("uri", "email"):
        if not link.target:
            return None
        return f"<</S/URI/URI{pymupdf.get_pdf_str(_ascii_uri(link.target))}>>"
    if link.kind == "file" and link.target:
        return file_action(link.target)
    return None


def remove_links(page: pymupdf.Page, st: PageLinks) -> None:
    """Per page, before it is merged (xrefs still those of its source): delete the removed original links."""
    if not st.removed:
        return
    for ln in page.get_links():
        if ln.get("xref") in st.removed:
            page.delete_link(ln)


def insert_link_annot(page: pymupdf.Page, pdf_rect: pymupdf.Rect, action: str) -> int:
    """Add a border-less link annotation with this PDF-space /Rect and action; returns its xref."""
    out = page.parent
    before = {x for x, t, _ in page.annot_xrefs() if t == pymupdf.PDF_ANNOT_LINK}
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(0, 0, 1, 1), "uri": "about:blank"})
    new = [x for x, t, _ in page.annot_xrefs() if t == pymupdf.PDF_ANNOT_LINK and x not in before]
    if not new:
        raise RuntimeError("Couldn't add a link annotation.")
    x = new[-1]
    out.xref_set_key(x, "Rect", "[" + " ".join(_num(v) for v in (pdf_rect.x0, pdf_rect.y0, pdf_rect.x1, pdf_rect.y1)) + "]")
    out.xref_set_key(x, "A", action)
    return x


def write_links(doc, out, pages: list, overlays_for) -> int:
    """Write the links made in Leaf on the final pages. Returns how many were left out (target page not saved)."""
    pos: dict = {}
    for i, r in enumerate(pages):
        pos.setdefault(r.uid, i)
    dropped = 0
    for i, ref in enumerate(pages):
        st = doc.links.get(ref.uid)
        if st is None or not st.added:
            continue
        sp = doc.page(ref)
        # redact: a mark removes the links under it, new ones included
        marks = [m.pdf_rect(sp.derotation_matrix) for m in overlays_for(ref) if getattr(m, "redact", False)]
        page = out[i]
        for link in st.added:
            action = link_action(doc, out, link, pos, pages)
            if action is None:
                dropped += 1
                continue
            if marks:
                unrot = (pymupdf.Rect(link.rect) * sp.derotation_matrix).normalize()
                if any(unrot.intersects(m) for m in marks):
                    continue
            insert_link_annot(page, to_pdf_rect(sp, link.rect), action)
        del page
    return dropped


def apply_structure(doc, out, pages: list, overlays_for) -> None:
    """Document-level structure steps on the output, once its page list is final (see the module docstring)."""
    if doc.links:
        write_links(doc, out, pages, overlays_for)
    if doc.outline is not None:
        from .outline import write_outline
        write_outline(doc, out, pages)
    if doc.attachments is not None:
        from .attachments import write_attachments
        write_attachments(doc, out)


# ----------------------------------------------------------------------------------------------------- dialog
class LinkDialog(QDialog):
    """Choose a link target. `result_link()` returns the spec (rect left empty); "Pick on page…" closes the dialog
    with `picking = True` so the tool can wait for a click on the target page."""

    def __init__(self, canvas, link: Link | None = None, title: str = "Link", parent=None):
        super().__init__(parent or canvas)
        self.setWindowTitle(title)
        self.canvas = canvas
        self.doc = canvas.doc
        self.picking = False
        link = link or Link((0, 0, 0, 0), "uri")
        self._uid = link.uid
        self._point = link.point
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for k in KINDS:
            self.kind.addItem(KIND_NAMES[k], k)
        form.addRow("Link to:", self.kind)
        lay.addLayout(form)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack)

        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit(link.target if link.kind == "uri" else "")
        self.url.setPlaceholderText("https://example.com")
        f.addRow("Address:", self.url)
        self.stack.addWidget(w)

        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        self.page = QSpinBox()
        n = len(self.doc.pages)
        self.page.setRange(1, max(1, n))
        cur = next((k for k, r in enumerate(self.doc.pages) if r.uid == link.uid), None)
        self.page.setValue((cur if cur is not None else max(0, canvas.current_index())) + 1)
        self.page.setSuffix(f" of {n}")
        f.addRow("Page:", self.page)
        self.top = QRadioButton("Top of the page")
        self.at = QRadioButton("The position picked on the page")
        grp = QButtonGroup(self)
        grp.addButton(self.top)
        grp.addButton(self.at)
        (self.at if link.point is not None and cur is not None else self.top).setChecked(True)
        self.at.setEnabled(link.point is not None and cur is not None)
        pick = QPushButton("Pick on page…")
        pick.setToolTip("Close this window, then click the spot the link should go to")
        pick.clicked.connect(self._pick)
        row = QHBoxLayout()
        row.addWidget(self.at)
        row.addStretch(1)
        row.addWidget(pick)
        f.addRow("Show:", self.top)
        f.addRow("", row)
        self.page.valueChanged.connect(self._page_changed)
        self.stack.addWidget(w)

        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        addr, subj = parse_mailto(link.target) if link.kind == "email" else ("", "")
        self.email = QLineEdit(addr)
        self.email.setPlaceholderText("name@example.com")
        self.subject = QLineEdit(subj)
        f.addRow("Address:", self.email)
        f.addRow("Subject:", self.subject)
        self.stack.addWidget(w)

        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(0, 0, 0, 0)
        self.file = QLineEdit(link.target if link.kind == "file" else "")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.file, 1)
        row.addWidget(browse)
        f.addRow("File:", row)
        note = QLabel("Other apps may ask before opening files from a link.")
        note.setEnabled(False)
        f.addRow("", note)
        self.stack.addWidget(w)

        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.kind.setCurrentIndex(KINDS.index(link.kind) if link.kind in KINDS else 0)
        self._kind_changed()
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self._accept)
        self.bb.rejected.connect(self.reject)
        lay.addWidget(self.bb)
        for e in (self.url, self.email, self.subject, self.file):
            e.textChanged.connect(self._validate)
        self._validate()
        self.resize(440, self.sizeHint().height())

    def _kind_changed(self, *_) -> None:
        self.stack.setCurrentIndex(self.kind.currentIndex())
        self._validate()

    def _page_changed(self, *_) -> None:
        ref = self.doc.pages[self.page.value() - 1] if self.doc.pages else None
        same = ref is not None and ref.uid == self._uid and self._point is not None
        self.at.setEnabled(same)
        if not same:
            self.top.setChecked(True)

    def _browse(self) -> None:
        start = os.path.dirname(self.doc.path) if self.doc.path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Link to file", start)
        if path:
            base = os.path.dirname(self.doc.path) if self.doc.path else ""
            try:  # relative to the document when it sits next to it (keeps working when both are moved)
                rel = os.path.relpath(path, base) if base else path
                path = rel if not rel.startswith("..") else path
            except ValueError:
                pass
            self.file.setText(path)

    def _pick(self) -> None:
        self.picking = True
        self.kind.setCurrentIndex(KINDS.index("page"))
        self.done(QDialog.DialogCode.Rejected)

    def _validate(self, *_) -> None:
        if getattr(self, "bb", None) is None:  # still building
            return
        k = self.kind.currentData()
        ok = {"uri": bool(self.url.text().strip()), "page": bool(self.doc.pages),
              "email": "@" in self.email.text(), "file": bool(self.file.text().strip())}[k]
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _accept(self) -> None:
        self.accept()

    def set_target(self, uid: int, point) -> None:
        """After picking: the clicked page and spot."""
        self._uid, self._point = uid, point
        self.kind.setCurrentIndex(KINDS.index("page"))
        i = next((k for k, r in enumerate(self.doc.pages) if r.uid == uid), 0)
        self.page.blockSignals(True)
        self.page.setValue(i + 1)
        self.page.blockSignals(False)
        self.at.setEnabled(point is not None)
        (self.at if point is not None else self.top).setChecked(True)

    def result_link(self, rect=(0, 0, 0, 0)) -> Link:
        k = self.kind.currentData()
        if k == "uri":
            return Link(rect, "uri", normalize_url(self.url.text()))
        if k == "email":
            return Link(rect, "email", mailto(self.email.text(), self.subject.text()))
        if k == "file":
            return Link(rect, "file", self.file.text().strip())
        ref = self.doc.pages[self.page.value() - 1]
        pt = self._point if self.at.isChecked() and ref.uid == self._uid else None
        return Link(rect, "page", uid=ref.uid, point=pt)


def _run_dialog(canvas, link: Link | None, title: str, picked=None):
    """Show the dialog: returns a Link (rect unset), ("pick", draft Link) or None. A seam for tests (`links.ask`)."""
    dlg = LinkDialog(canvas, link, title)
    if picked is not None:
        dlg.set_target(*picked)
    ok = dlg.exec() == QDialog.DialogCode.Accepted
    if dlg.picking:
        return "pick", dlg.result_link()
    return dlg.result_link() if ok else None


ask = _run_dialog


def _ask(canvas, link, title, picked=None):
    return ask(canvas, link, title, picked)


# ------------------------------------------------------------------------------------------------------- tool
class LinkTool(QObject):
    """The "link" canvas tool: drag a rectangle to add a link; click a link to select it, double-click (or Enter) to
    edit, Delete to remove, right-click for a menu. While it is active every link is outlined.

    An event filter on the canvas viewport (and the canvas for keys), so the canvas needs no link-tool code."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        cv = self.cv = win.canvas
        cv.link_tool = self
        self.drag = None  # (page item, start point)
        self.band: QGraphicsRectItem | None = None
        self.boxes: list = []
        self.selected = None  # (page uid, link dict)
        self.pending = None  # (page ref, rect, Link spec draft, original link dict or None) while picking a target
        self._refresh = QTimer(self, singleShot=True, interval=40, timeout=self.refresh)
        cv.viewport().installEventFilter(self)
        cv.installEventFilter(self)
        cv.toolChanged.connect(self._tool_changed)
        cv.verticalScrollBar().valueChanged.connect(self._scrolled)
        cv.horizontalScrollBar().valueChanged.connect(self._scrolled)
        cv.pagesChanged.connect(self._scrolled)
        cv.layoutChanged.connect(self._scrolled)
        cv.documentChanged.connect(self._reset)

    @property
    def active(self) -> bool:
        return self.cv.tool == "link" and self.cv.doc is not None

    def _reset(self) -> None:
        self.selected = None
        self.pending = None
        self.drag = None
        self.boxes = []
        self.band = None

    def _tool_changed(self, tool: str) -> None:
        if tool == "link":
            self.cv.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.drag = None
            self.pending = None
            self.selected = None
            self._drop_band()
        self.refresh()

    def _scrolled(self, *_) -> None:
        if self.active:
            self._refresh.start()

    # ------------------------------------------------------------ outlines
    def _pen(self, color: QColor, width: float, dash: bool) -> QPen:
        pen = QPen(color, width)
        pen.setCosmetic(True)
        if dash:
            pen.setStyle(Qt.PenStyle.DashLine)
        return pen

    def refresh(self) -> None:
        for b in self.boxes:
            if b.scene() is not None:
                b.scene().removeItem(b)
        self.boxes = []
        if not self.active:
            return
        cv = self.cv
        accent = cv.palette().color(QPalette.ColorRole.Highlight)
        have = {r.uid for r in cv.doc.pages}
        sel = self.selected
        for pg in cv.visible_pages():
            for r, ln in cv.links_on(pg):
                link = ln.get("leaf_link")
                missing = link is not None and link.kind == "page" and link.uid not in have
                is_sel = sel is not None and sel[0] == pg.ref.uid and _same(sel[1], ln)
                color = QColor("#d93025") if missing else accent
                box = QGraphicsRectItem(r, pg.content)
                box.setPen(self._pen(color, 2.0 if is_sel else 1.2, not is_sel))
                fill = QColor(color)
                fill.setAlpha(60 if is_sel else 22)
                box.setBrush(fill)
                box.setZValue(95)
                box.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                box.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                tip = describe(cv.doc, ln) or cv.link_target_text(pg, ln)
                box.setToolTip(tip + ("  (its target page was deleted: left out when saving)" if missing else ""))
                self.boxes.append(box)

    # --------------------------------------------------------------- band
    def _set_band(self, pg, rect: QRectF) -> None:
        if self.band is None or self.band.parentItem() is not pg.content:
            self._drop_band()
            self.band = QGraphicsRectItem(pg.content)
            self.band.setZValue(96)
            self.band.setPen(self._pen(self.cv.palette().color(QPalette.ColorRole.Highlight), 1.5, True))
            self.band.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.band.setRect(rect)

    def _drop_band(self) -> None:
        if self.band is not None and self.band.scene() is not None:
            self.band.scene().removeItem(self.band)
        self.band = None

    @staticmethod
    def _clamped(pg, p: QPointF) -> QPointF:
        r = pg.content.rect()
        return QPointF(min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom()))

    def hit(self, sp: QPointF):
        """(page item, link dict) under scene point `sp` (the smallest link, so nested ones can be picked)."""
        cv = self.cv
        pg = cv.page_at(sp)
        if pg is None:
            return None
        lp = pg.content.mapFromScene(sp)
        best = None
        for r, ln in cv.links_on(pg):
            if r.adjusted(-2, -2, 2, 2).contains(lp):
                a = r.width() * r.height()
                if best is None or a < best[0]:
                    best = (a, ln)
        return (pg, best[1]) if best else None

    # -------------------------------------------------------------- events
    def eventFilter(self, obj, e) -> bool:
        cv = self.cv
        if not self.active or getattr(cv, "_space", False):
            return False
        t = e.type()
        if obj is cv:
            if t == QEvent.Type.KeyPress:
                return self._key(e)
            return False
        if t == QEvent.Type.MouseButtonPress:
            sp = cv.mapToScene(e.position().toPoint())
            if e.button() == Qt.MouseButton.RightButton:
                h = self.hit(sp)
                if h is not None:
                    self.select(h[0], h[1])
                    self._context(h[0], h[1], e.globalPosition().toPoint())
                return True
            if e.button() != Qt.MouseButton.LeftButton:
                return False
            cv.setFocus()
            pg = cv.page_at(sp)
            if self.pending is not None:
                if pg is not None:
                    self._picked(pg, pg.content.mapFromScene(sp))
                return True
            h = self.hit(sp)
            if h is not None:
                self.select(*h)
                return True
            self.select(None, None)
            if pg is not None:
                self.drag = (pg, self._clamped(pg, pg.content.mapFromScene(sp)))
                self._set_band(pg, QRectF(self.drag[1], self.drag[1]))
            return True
        if t == QEvent.Type.MouseMove:
            if self.drag is not None:
                pg, start = self.drag
                p = self._clamped(pg, pg.content.mapFromScene(cv.mapToScene(e.position().toPoint())))
                self._set_band(pg, QRectF(start, p).normalized())
                return True
            shape = Qt.CursorShape.PointingHandCursor if self.pending is not None else Qt.CursorShape.CrossCursor
            if self.pending is None and self.hit(cv.mapToScene(e.position().toPoint())) is not None:
                shape = Qt.CursorShape.ArrowCursor
            if cv.viewport().cursor().shape() != shape:
                cv.viewport().setCursor(shape)
            return True
        if t == QEvent.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton:
            if self.drag is not None:
                pg, start = self.drag
                self.drag = None
                p = self._clamped(pg, pg.content.mapFromScene(cv.mapToScene(e.position().toPoint())))
                rect = QRectF(start, p).normalized()
                self._drop_band()
                if rect.width() >= 4 and rect.height() >= 4:
                    self.create(pg.ref, rect)
            return True
        if t == QEvent.Type.MouseButtonDblClick and e.button() == Qt.MouseButton.LeftButton:
            h = self.hit(cv.mapToScene(e.position().toPoint()))
            if h is not None:
                self.edit(h[0].ref, h[1])
            return True
        return False

    def _key(self, e) -> bool:
        k = e.key()
        if k == Qt.Key.Key_Escape:
            if self.pending is not None:
                self.pending = None
                self._message("Picking a link target cancelled.")
                return True
            if self.selected is not None:
                self.select(None, None)
                return True
            return False
        if self.selected is None:
            return False
        uid, ln = self.selected
        ref = next((r for r in self.cv.doc.pages if r.uid == uid), None)
        if ref is None:
            return False
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.selected = None
            delete_link(self.cv, ref, ln)
            return True
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.edit(ref, ln)
            return True
        return False

    def _message(self, text: str) -> None:
        self.cv.message.emit(text)

    def select(self, pg, ln) -> None:
        self.selected = (pg.ref.uid, ln) if pg is not None and ln is not None else None
        self.refresh()

    def _context(self, pg, ln, at) -> None:
        m = QMenu(self.cv)
        a_edit = m.addAction("Edit link…")
        a_go = m.addAction("Go to target")
        m.addSeparator()
        a_del = m.addAction("Delete link")
        a_del.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        chosen = exec_menu(m, at)
        if chosen is a_edit:
            self.edit(pg.ref, ln)
        elif chosen is a_go:
            self.cv.follow_link(pg, ln)
        elif chosen is a_del:
            self.selected = None
            delete_link(self.cv, pg.ref, ln)

    # ----------------------------------------------------------- actions
    def create(self, ref, rect: QRectF, draft: Link | None = None, picked=None) -> None:
        r = (rect.left(), rect.top(), rect.right(), rect.bottom())
        res = _ask(self.cv, draft, "Add link", picked)
        self._handle(ref, r, res, None)

    def edit(self, ref, ln: dict) -> None:
        link = ln.get("leaf_link") or from_link_dict(self.cv.doc, ref, ln)
        if link is None:
            self._message("This link's target can't be edited in Leaf; you can delete it.")
            return
        res = _ask(self.cv, link, "Edit link")
        self._handle(ref, link.rect, res, ln)

    def _handle(self, ref, rect, res, ln) -> None:
        if res is None:
            return
        if isinstance(res, tuple):  # ("pick", draft)
            self.pending = (ref, rect, res[1], ln)
            self.cv.set_tool("link")
            self._message("Click the spot the link should go to (Esc cancels).")
            self.cv.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            return
        spec = replace(res, rect=tuple(rect))
        if ln is None:
            add_links(self.cv, ref, [spec])
        else:
            update_link(self.cv, ref, ln, replace(spec, id=ln["leaf_link"].id) if ln.get("leaf_link") else spec)
        self.selected = None
        self.refresh()

    def _picked(self, pg, lp: QPointF) -> None:
        ref, rect, draft, ln = self.pending
        self.pending = None
        top = lp.y() <= pg.content.rect().top() + 4
        point = None if top else (pg.content.rect().left(), lp.y())
        picked = (pg.ref.uid, point)
        self.cv.viewport().setCursor(Qt.CursorShape.CrossCursor)
        res = _ask(self.cv, replace(draft, kind="page", uid=pg.ref.uid, point=point), "Edit link" if ln else "Add link",
                   picked)
        self._handle(ref, rect, res, ln)

    def link_selection(self) -> bool:
        """Insert › Link: links on the selected text (one per line), else start the tool. Returns True if added."""
        cv = self.cv
        rects = list(getattr(cv, "_sel_rects", []) or [])
        sel_item = getattr(cv, "_sel_item", None)
        pg = None
        if rects and sel_item is not None:
            content = sel_item.parentItem()
            pg = next((p for p in cv.order if p.content is content), None)
        if pg is None:
            cv.set_tool("link")
            self._message("Drag a rectangle where the link should be.")
            return False
        res = _ask(cv, None, "Link selected text")
        if res is None or isinstance(res, tuple):
            if isinstance(res, tuple):
                self._message("Pick the target page in the link's settings after making it (Link tool).")
            return False
        cv._end_text_selection()
        add_links(cv, pg.ref, [replace(res, rect=(r.left(), r.top(), r.right(), r.bottom()), id=next(_ids))
                                for r in rects])
        return True


def _same(a: dict, b: dict) -> bool:
    la, lb = a.get("leaf_link"), b.get("leaf_link")
    if la is not None or lb is not None:
        return la is not None and lb is not None and la.id == lb.id
    return a.get("xref") == b.get("xref") and a.get("xref", 0) != 0


def exec_menu(menu: QMenu, at):
    """A seam for tests (a modal menu would block them)."""
    return menu.exec(at)
