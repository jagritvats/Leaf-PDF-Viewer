"""Redaction: marks that truly remove content at save, search & redact, and document sanitizing.

Marks are overlay items (`RedactItem`) like any other: movable, resizable, deletable, undoable. They draw a red
outline while editing; nothing is removed from the open sources. At save, `PdfDocument._apply` hands each page's
marks to `redact_page` (after the other overlays, before the crop box): it removes annotations, form widgets
(and their fields) and links touching a mark, then adds a Redact annotation per mark and applies them all, which
deletes the text, blanks image pixels and removes vector graphics under them. A document with marks is never
saved incrementally (the old bytes would stay in the file): `save` forces a full rewrite with garbage=4.

Sanitizing (`Sanitize`, a document-level setting in `PdfDocument.sanitize`) is applied by `apply_sanitize` to the
assembled output, also with a full rewrite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields as dc_fields

import pymupdf
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QUndoCommand
from PySide6.QtWidgets import QGraphicsView

from .document import SEARCH_FLAGS
from .items import DEFAULTS, Z_OVERLAY, BoxItem, pdf_color, to_rect

KIND = "redact"
MARK_FILL = QColor(220, 38, 38, 46)
MARK_LINE = QColor(220, 38, 38)


# ======================================================================= marks
class RedactItem(BoxItem):
    """A redaction mark (box). Text marks are one box per selected line, snapped to the words."""

    redact = True  # PdfDocument picks these up (has_redactions, the redaction step in _apply)

    def __init__(self, kind: str = KIND, rect: QRectF = QRectF(), props: dict | None = None, text: str = ""):
        super().__init__(KIND, rect, props)
        self.marked_text = text  # the words under a text mark (review list); empty for a drawn box
        self.setZValue(Z_OVERLAY + 1)

    def clone_overlay(self) -> "RedactItem":
        c = RedactItem(KIND, QRectF(), dict(self.props), self.marked_text)
        c.set_geometry(self.geometry())
        return c

    clone = clone_overlay  # Duplicate (Ctrl+D)

    def is_trivial(self) -> bool:
        return self._r.width() < 2 or self._r.height() < 2

    def paint_content(self, p: QPainter) -> None:
        p.fillRect(self._r, MARK_FILL)
        pen = QPen(MARK_LINE, 1.5)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self._r)
        label = str(self.props.get("text") or "")
        if label and self._r.height() >= 4:
            f = QFont()
            f.setPointSizeF(max(2.0, min(11.0, self._r.height() * 0.6)))
            p.setFont(f)
            p.setPen(MARK_LINE.darker(140))
            p.drawText(self._r, int(Qt.AlignmentFlag.AlignCenter), label)

    def apply(self, page: pymupdf.Page, m: pymupdf.Matrix, rotate: int) -> None:
        pass  # written by redact_page() after all other overlays of the page

    def pdf_rect(self, to_pdf: pymupdf.Matrix) -> pymupdf.Rect:
        """The mark in unrotated PDF page coordinates."""
        return to_rect(self.scene_rect_in_parent(), to_pdf)


def is_mark(item) -> bool:
    return isinstance(item, RedactItem)


def marks_of(canvas) -> list[tuple[int, RedactItem]]:
    """(page index, mark) for every mark in the current page order."""
    out = []
    for i, pg in enumerate(getattr(canvas, "order", [])):
        out += [(i, it) for it in pg.overlays() if is_mark(it)]
    return out


def _defaults(canvas) -> dict:
    d = canvas.defaults.get(KIND)
    if d is None:
        d = canvas.defaults[KIND] = dict(DEFAULTS[KIND])
    return d


# ---------------------------------------------------------- canvas tool hooks
def _mark_at(canvas, e):
    it = canvas.itemAt(e.position().toPoint())
    while it is not None and not is_mark(it):
        it = it.parentItem()
    return it


def canvas_press(canvas, pg, lp: QPointF, e) -> None:
    """Redact tool press: on an existing mark -> select/move it; on a word -> word-snapped text selection
    (finished by canvas_text_done); elsewhere, or with Shift/Alt held -> drag a box."""
    hit = _mark_at(canvas, e)
    if hit is not None:
        QGraphicsView.mousePressEvent(canvas, e)
        canvas._move_snapshot = {i: QPointF(i.pos()) for i in canvas.scene().selectedItems() if is_mark(i)}
        return
    canvas.scene().clearSelection()
    mods = e.modifiers()
    force_box = bool(mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier))
    x, y = lp.x(), lp.y()
    words = [] if force_box else pg.ref.src.words(pg.ref.index, pg.ref.uid)
    if any(w[0] - 1 <= x <= w[2] + 1 and w[1] - 1 <= y <= w[3] + 1 for w in words):
        canvas._end_text_selection()
        canvas._sel = (pg, lp, KIND)
        canvas._update_text_selection(pg, lp, lp)  # a click alone marks the word under it
        return
    item = RedactItem(KIND, QRectF(lp, QSizeF(0, 0)), _defaults(canvas))
    item.setParentItem(pg.content)
    canvas._create = (pg, item, lp)


def text_marks(content, rects: list[QRectF], text: str, props: dict) -> list[RedactItem]:
    lines = text.split("\n") if text else []
    items = []
    for k, r in enumerate(rects):
        it = RedactItem(KIND, QRectF(r), props, lines[k] if k < len(lines) else "")
        it.setParentItem(content)
        items.append(it)
    return items


def canvas_text_done(canvas, pg, rects: list[QRectF], text: str) -> None:
    from . import commands as C

    if not rects:
        return
    items = text_marks(pg.content, rects, text, _defaults(canvas))
    canvas.undo.push(C.AddItems(items, "Redact text"))


def redact_selection(canvas) -> bool:
    """Mark the current text selection (select tool) for redaction. One undo step."""
    from . import commands as C

    rects = list(getattr(canvas, "_sel_rects", []))
    sel_item = getattr(canvas, "_sel_item", None)
    if not rects or sel_item is None or sel_item.parentItem() is None:
        return False
    items = text_marks(sel_item.parentItem(), rects, canvas.selected_text(), _defaults(canvas))
    canvas.undo.push(C.AddItems(items, "Redact text"))
    canvas._end_text_selection()
    return True


# ===================================================================== saving
def _contrast(fill: tuple) -> tuple:
    lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return (1, 1, 1) if lum < 0.5 else (0, 0, 0)


def _hits(r: pymupdf.Rect, rects: list[pymupdf.Rect]) -> bool:
    return any(not (r & m).is_empty for m in rects)


def remove_under(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> None:
    """Delete annotations (with their replies/popups), form widgets (and fields left without widgets) and links
    that intersect any of `rects` (unrotated page coordinates). Pending Redact annotations stay (applied next)."""
    doc = page.parent
    drop = set()
    irt = {}
    for a in page.annots():
        if a.type[0] == pymupdf.PDF_ANNOT_REDACT:
            continue
        t, v = doc.xref_get_key(a.xref, "IRT")
        if t == "xref":
            irt[a.xref] = int(v.split()[0])
        if a.type[0] != pymupdf.PDF_ANNOT_POPUP and _hits(pymupdf.Rect(a.rect), rects):
            drop.add(a.xref)
    changed = True
    while changed:  # replies to a removed comment go with it (they quote or discuss what was under it)
        changed = False
        for x, parent in irt.items():
            if parent in drop and x not in drop:
                drop.add(x)
                changed = True
    for x in drop:
        a = page.load_annot(x)
        if a is not None:
            page.delete_annot(a)
    parents = set()
    for w in list(page.widgets()):
        if _hits(pymupdf.Rect(w.rect), rects):
            t, v = doc.xref_get_key(w.xref, "Parent")
            if t == "xref":
                parents.add(int(v.split()[0]))
            page.delete_widget(w)
    _drop_empty_fields(doc, parents)
    derot = page.derotation_matrix
    for ln in page.get_links():
        if _hits(pymupdf.Rect(ln["from"]) * derot, rects):
            page.delete_link(ln)


def _drop_empty_fields(doc: pymupdf.Document, parents: set[int]) -> None:
    """Parent fields whose last widget was removed: out of /AcroForm /Fields (their /V may hold the text)."""
    from .document import PdfDocument

    dead = set()
    for x in parents:
        t, v = doc.xref_get_key(x, "Kids")
        if t != "array" or not re.search(r"\d+\s+\d+\s+R", v):
            doc.xref_set_key(x, "V", "null")
            dead.add(x)
    PdfDocument._drop_fields(doc, dead)


def redact_page(page: pymupdf.Page, marks, to_pdf: pymupdf.Matrix) -> None:
    """Remove everything under `marks` from `page` (see the module docstring)."""
    pairs = [(m.pdf_rect(to_pdf), m) for m in marks]
    pairs = [(r, m) for r, m in pairs if not r.is_empty]
    if not pairs:
        return
    remove_under(page, [r for r, _ in pairs])
    for r, m in pairs:
        fill = pdf_color(m.props.get("color") or "#000000")
        text = str(m.props.get("text") or "").strip() or None
        page.add_redact_annot(r, text=text, fill=fill, text_color=_contrast(fill), cross_out=False,
                              fontsize=max(4.0, min(11.0, r.height * 0.7)), align=pymupdf.TEXT_ALIGN_CENTER)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


def signed_fields(doc: pymupdf.Document) -> int:
    """Number of signed signature fields (a full rewrite invalidates them)."""
    try:
        if doc.get_sigflags() < 1:
            return 0
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for page in doc:
        for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE]):
            x = w.xref
            for _ in range(32):
                if doc.xref_get_key(x, "V")[0] != "null":
                    n += 1
                    break
                t, v = doc.xref_get_key(x, "Parent")
                if t != "xref":
                    break
                x = int(v.split()[0])
    return n


# ============================================================ search & redact
@dataclass(frozen=True)
class Preset:
    name: str
    pattern: str
    check: object = None  # extra validation of a match (e.g. Luhn checksum)
    case_sensitive: bool | None = None  # None: follow the dialog's option


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _luhn(s: str) -> bool:
    d = _digits(s)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, c in enumerate(reversed(d)):
        v = int(c)
        if i % 2:
            v = v * 2 - 9 if v > 4 else v * 2
        total += v
    return total % 10 == 0


def _iban(s: str) -> bool:
    t = re.sub(r"\s", "", s).upper()
    if not 15 <= len(t) <= 34:
        return False
    t = t[4:] + t[:4]
    try:
        return int("".join(str(int(c, 36)) for c in t)) % 97 == 1
    except ValueError:
        return False


def _phone(s: str) -> bool:
    return 7 <= len(_digits(s)) <= 15 and not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s.strip())


_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
PRESETS = [
    Preset("Text", ""),
    Preset("Email addresses", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    Preset("Phone numbers", r"(?<![\w+])\+?(?:\(\d{1,4}\)[ .\-]?)?\d[\d .\-()]{5,18}\d(?!\w)", _phone),
    Preset("Credit card numbers", r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)", _luhn),
    Preset("IBANs", r"\b[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]){11,30}\b", _iban),
    Preset("Dates", r"\b(?:\d{1,2}[./\-]\d{1,2}[./\-](?:\d{4}|\d{2})|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"
                    rf"|\d{{1,2}}\.?\s+{_MONTHS},?\s+\d{{4}}|{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})\b",
           None, False),
    Preset("Custom regular expression", ""),
]


def build_regex(text: str, preset: Preset, case: bool = False, whole: bool = False) -> re.Pattern:
    """Compile the search: plain text (escaped), a preset pattern, or a custom regex. Raises re.error."""
    if preset.name == "Text":
        pat = r"\s+".join(re.escape(p) for p in text.split())
        if not pat:
            raise re.error("empty search")
    elif preset.name == "Custom regular expression":
        pat = text
        if not pat:
            raise re.error("empty pattern")
    else:
        pat = preset.pattern
    if whole:
        pat = rf"(?<!\w)(?:{pat})(?!\w)"
    cs = case if preset.case_sensitive is None else preset.case_sensitive
    return re.compile(pat, 0 if cs else re.IGNORECASE)


@dataclass
class Hit:
    page: int  # position in the page list
    text: str
    rects: list  # QRectF per line, in the page's display (page.rect) space
    context: str = ""


def page_text(words) -> tuple[str, list[tuple[int, int]]]:
    """Words joined (space within a line, newline between lines) and each word's (start, end) in that string."""
    parts, spans, pos, prev = [], [], 0, None
    for w in words:
        key = (w[5], w[6])
        if prev is not None:
            sep = " " if key == prev else "\n"
            parts.append(sep)
            pos += 1
        prev = key
        spans.append((pos, pos + len(w[4])))
        parts.append(w[4])
        pos += len(w[4])
    return "".join(parts), spans


def _sub_rect(src, index: int, word, piece: str):
    """Exact box of `piece` inside a word (partial match), via MuPDF's search; None if not found."""
    page = src.page(index)
    r = pymupdf.Rect(word[:4])
    clip = (r * page.derotation_matrix).normalize()
    clip = pymupdf.Rect(clip.x0 - 1, clip.y0 - 1, clip.x1 + 1, clip.y1 + 1)
    try:
        found = page.search_for(piece, clip=clip, flags=SEARCH_FLAGS)
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    out = pymupdf.Rect(found[0])
    for f in found[1:]:
        out |= f
    return (out * page.rotation_matrix).normalize()


def search_page(doc, i: int, rx: re.Pattern, check=None) -> list[Hit]:
    """Matches of `rx` on page `i` of the page list (uses the ligature-expanded words of the source)."""
    ref = doc.pages[i]
    words = ref.src.words(ref.index, ref.uid)  # text edits: the page as shown
    if not words:
        return []
    text, spans = page_text(words)
    hits = []
    for m in rx.finditer(text):
        s, e = m.span()
        if e <= s or (check is not None and not check(m.group(0))):
            continue
        lines: dict = {}
        for k, (ws, we) in enumerate(spans):
            if we <= s or ws >= e:
                continue
            w = words[k]
            r = None
            if ws < s or we > e:  # match covers part of this word: find its exact box, else take the whole word
                r = _sub_rect(ref.src, ref.index, w, w[4][max(s, ws) - ws:min(e, we) - ws])
            if r is None:
                r = pymupdf.Rect(w[:4])
            key = (w[5], w[6])
            lines[key] = lines[key] | r if key in lines else pymupdf.Rect(r)
        rects = [QRectF(QPointF(r.x0, r.y0), QPointF(r.x1, r.y1)) for r in lines.values()]
        if rects:
            ctx = text[max(0, s - 24):min(len(text), e + 24)].replace("\n", " ")
            hits.append(Hit(i, m.group(0).replace("\n", " "), rects, ctx))
    return hits


def marks_for_hits(canvas, hits: list[Hit]) -> list[RedactItem]:
    props = _defaults(canvas)
    items = []
    for h in hits:
        content = canvas.page_item(canvas.doc.pages[h.page]).content
        items += text_marks(content, h.rects, "\n".join([h.text] * len(h.rects)), props)
    return items


def mark_hits(canvas, hits: list[Hit]) -> int:
    """Turn search hits into marks; one undo step. Returns the number of marks added."""
    from . import commands as C

    items = marks_for_hits(canvas, hits)
    if items:
        canvas.undo.push(C.AddItems(items, f"Search & redact ({len(hits)})"))
    return len(items)


# ================================================================== sanitize
@dataclass(frozen=True)
class Sanitize:
    metadata: bool = False
    javascript: bool = False
    attachments: bool = False
    comments: bool = False
    hidden_text: bool = False
    hidden_layers: bool = False
    forms: bool = False
    links: bool = False
    thumbnails: bool = False
    page_labels: bool = False

    def __bool__(self) -> bool:
        return any(getattr(self, f.name) for f in dc_fields(self))

    def chosen(self) -> list[str]:
        return [LABELS[f.name][0] for f in dc_fields(self) if getattr(self, f.name)]


LABELS = {
    "metadata": ("Metadata", "Document properties (title, author, ...), XMP metadata, private application data"),
    "javascript": ("JavaScript and actions", "Scripts, open actions, additional actions, launch/submit actions"),
    "attachments": ("Attachments", "Embedded files and file-attachment annotations"),
    "comments": ("Comments and annotations", "Notes, highlights, drawings and every other annotation"),
    "hidden_text": ("Hidden text", "Invisible text (render mode 3, OCR layers, fully transparent text)"),
    "hidden_layers": ("Hidden layers", "Content in optional-content layers that are turned off"),
    "forms": ("Form fields (flatten)", "Field values become page content; the fields are removed"),
    "links": ("Links", "Hyperlinks and internal links"),
    "thumbnails": ("Page thumbnails", "Embedded page preview images"),
    "page_labels": ("Page labels", "Custom page numbering (i, ii, A-1, ...)"),
}
DEFAULT_SANITIZE = Sanitize(metadata=True, javascript=True, attachments=True, hidden_text=True, hidden_layers=True)
DANGEROUS_ACTIONS = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/ResetForm", "/Rendition", "/RichMediaExecute"}


class SetSanitize(QUndoCommand):
    def __init__(self, doc, new, text: str = "Sanitize document"):
        super().__init__(text)
        self.doc, self.old, self.new = doc, doc.sanitize, new

    def redo(self) -> None:
        self.doc.sanitize = self.new

    def undo(self) -> None:
        self.doc.sanitize = self.old


def _xrefs(doc: pymupdf.Document):
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_is_stream(x) or doc.xref_object(x, compressed=True).startswith("<<"):
                yield x
        except Exception:  # noqa: BLE001  (free or broken entry)
            continue


def _key(doc, x: int, k: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(x, k)
    except Exception:  # noqa: BLE001
        return ("null", "null")


def _remove_actions(doc: pymupdf.Document) -> None:
    cat = doc.pdf_catalog()
    t, v = _key(doc, cat, "Names")
    if t == "xref":
        doc.xref_set_key(int(v.split()[0]), "JavaScript", "null")
    elif t == "dict":
        doc.xref_set_key(cat, "Names/JavaScript", "null")
    t, v = _key(doc, cat, "OpenAction")
    if t in ("dict", "xref") and _key(doc, cat, "OpenAction/S")[1] != "/GoTo":
        doc.xref_set_key(cat, "OpenAction", "null")
    if _key(doc, cat, "AcroForm/XFA")[0] != "null":
        doc.xref_set_key(cat, "AcroForm/XFA", "null")
    for x in _xrefs(doc):
        if _key(doc, x, "AA")[0] != "null":
            doc.xref_set_key(x, "AA", "null")
        if _key(doc, x, "A/S")[1] in DANGEROUS_ACTIONS:
            doc.xref_set_key(x, "A", "null")
        if _key(doc, x, "Next")[0] != "null" and _key(doc, x, "S")[0] == "name":
            doc.xref_set_key(x, "Next", "null")
        if _key(doc, x, "S")[1] in DANGEROUS_ACTIONS:
            doc.update_object(x, "<<>>")  # an action object (now unreferenced or inert)


def _remove_metadata(doc: pymupdf.Document) -> None:
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:  # noqa: BLE001
        pass
    for x in _xrefs(doc):
        if _key(doc, x, "Type")[1] == "/Metadata":
            doc.update_object(x, "<<>>")
            doc.update_stream(x, b"", new=True)
            continue
        for k in ("Metadata", "PieceInfo"):
            if _key(doc, x, k)[0] != "null":
                doc.xref_set_key(x, k, "null")
    t, _ = _key(doc, -1, "Info")
    if t != "null":
        try:
            doc.xref_set_key(-1, "Info", "null")
        except Exception:  # noqa: BLE001
            pass


def _remove_attachments(doc: pymupdf.Document) -> None:
    for name in list(doc.embfile_names()):
        doc.embfile_del(name)
    cat = doc.pdf_catalog()
    if _key(doc, cat, "AF")[0] != "null":
        doc.xref_set_key(cat, "AF", "null")
    for page in doc:
        for x in [a.xref for a in page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT])]:
            page.delete_annot(page.load_annot(x))


def _remove_annotations(doc: pymupdf.Document) -> None:
    for page in doc:
        xs = [a.xref for a in page.annots() if a.type[0] != pymupdf.PDF_ANNOT_POPUP]
        for x in xs:
            a = page.load_annot(x)
            if a is not None:
                page.delete_annot(a)


def _remove_links(doc: pymupdf.Document) -> None:
    for page in doc:
        for ln in page.get_links():
            page.delete_link(ln)


def _remove_hidden_text(doc: pymupdf.Document) -> None:
    """Invisible text (render mode 3 / no fill and no stroke / alpha 0): MuPDF's own invisible-text redaction over
    the whole page. Pages with pending third-party Redact annotations are left alone (they would be applied)."""
    for page in doc:
        if any(True for _ in page.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
            continue
        if not page.get_text("text", flags=SEARCH_FLAGS).strip():
            continue
        links = page.get_links()
        page.add_redact_annot(page.rect * page.derotation_matrix, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE_INVISIBLE)
        # MuPDF drops the links inside a redaction area; this one covers the page, so put them back
        kept = {tuple(round(v, 1) for v in ln["from"]) for ln in page.get_links()}
        for ln in links:
            if tuple(round(v, 1) for v in ln["from"]) not in kept:
                try:
                    page.insert_link(ln)
                except Exception:  # noqa: BLE001
                    pass


# ---- hidden layers: content stream filter for /OC ... BDC ... EMC blocks of layers that are off
_WS = b" \t\r\n\x0c\x00"
_DELIMS = b"()<>[]{}/%"


def _skip_string(data: bytes, i: int) -> int:
    depth, n = 0, len(data)
    while i < n:
        c = data[i]
        if c == 0x5C:  # backslash
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_nested(data: bytes, i: int, open_: bytes, close: bytes) -> int:
    """Skip a dict (<< >>) or array ([ ]) starting at i, honoring strings inside."""
    depth, n = 0, len(data)
    while i < n:
        if data.startswith(open_, i):
            depth += 1
            i += len(open_)
            continue
        if data.startswith(close, i):
            depth -= 1
            i += len(close)
            if depth == 0:
                return i
            continue
        c = data[i]
        if c == 0x28:
            i = _skip_string(data, i)
        elif c == 0x3C and not data.startswith(b"<<", i):  # hex string
            j = data.find(b">", i)
            i = n if j < 0 else j + 1
        else:
            i += 1
    return n


def content_ops(data: bytes):
    """Tokenize a content stream into (operator, operands, start, end): `start` is where the first operand
    begins (or the operator, without operands), `end` is after the operator. Operands are raw bytes."""
    i, n = 0, len(data)
    operands, start = [], None
    while i < n:
        c = data[i]
        if c in _WS:
            i += 1
            continue
        if c == 0x25:  # % comment
            while i < n and data[i] not in b"\r\n":
                i += 1
            continue
        s = i
        if c == 0x28:
            i = _skip_string(data, i)
        elif data.startswith(b"<<", i):
            i = _skip_nested(data, i, b"<<", b">>")
        elif c == 0x3C:
            j = data.find(b">", i)
            i = n if j < 0 else j + 1
        elif c == 0x5B:
            i = _skip_nested(data, i, b"[", b"]")
        elif c == 0x2F:
            i += 1
            while i < n and data[i] not in _WS and data[i] not in _DELIMS:
                i += 1
        elif c in b")>]}{":  # stray delimiter
            i += 1
            continue
        else:
            while i < n and data[i] not in _WS and data[i] not in _DELIMS:
                i += 1
            tok = data[s:i]
            if not re.fullmatch(rb"[+\-]?(?:\d+\.?\d*|\.\d+)|true|false|null", tok):
                if tok == b"BI":  # inline image: up to EI after the binary data
                    m = re.compile(rb"\sID[\s]").search(data, i)
                    j = m.end() if m else n
                    m2 = re.compile(rb"\sEI(?=[\s]|$)").search(data, j)
                    i = m2.end() if m2 else n
                    tok = b"BI"
                yield tok, operands, s if start is None else start, i
                operands, start = [], None
                continue
        if start is None:
            start = s
        operands.append(data[s:i])
    return


def _props_map(doc: pymupdf.Document, xref: int) -> dict[bytes, int]:
    """/Resources /Properties of a page or form XObject: name -> xref."""
    t, v = _key(doc, xref, "Resources")
    if t == "xref":
        t, v = _key(doc, int(v.split()[0]), "Properties")
    elif t == "dict":
        t, v = _key(doc, xref, "Resources/Properties")
    else:
        return {}
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]), compressed=True)
    elif t != "dict":
        return {}
    return {m.group(1).encode(): int(m.group(2)) for m in re.finditer(r"/([^\s/<>\[\]()]+)\s*(\d+)\s+\d+\s+R", v)}


def _off_layers(doc: pymupdf.Document) -> tuple[set[int], callable]:
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:  # noqa: BLE001
        ocgs = {}
    off = {x for x, info in ocgs.items() if not info.get("on", True)}

    def hidden(x: int) -> bool:
        if x in off:
            return True
        if _key(doc, x, "Type")[1] != "/OCMD":
            return False
        t, v = _key(doc, x, "OCGs")
        members = [int(m) for m in re.findall(r"(\d+)\s+\d+\s+R", v)] if t in ("array", "xref") else []
        if not members:
            return False
        states = [m not in off for m in members]  # True = on
        pol = _key(doc, x, "P")[1]
        visible = {"/AllOn": all(states), "/AnyOff": not all(states), "/AllOff": not any(states)}.get(pol, any(states))
        return not visible

    return off, hidden


def strip_hidden_content(data: bytes, names: dict[bytes, int], hidden) -> tuple[bytes, bool]:
    """Remove `/OC /Name BDC ... EMC` blocks whose layer is hidden. Returns (new data, changed)."""
    cut, stack, active = [], [], None
    for op, operands, s, e in content_ops(data):
        if op in (b"BDC", b"BMC"):
            off = False
            if active is None and op == b"BDC" and len(operands) == 2 and operands[0] == b"/OC":
                name = operands[1][1:] if operands[1].startswith(b"/") else None
                off = name is not None and name in names and hidden(names[name])
            stack.append(off)
            if off:
                active = s
        elif op == b"EMC" and stack:
            if stack.pop() and active is not None:
                cut.append((active, e))
                active = None
    if active is not None:
        cut.append((active, len(data)))
    if not cut:
        return data, False
    out, pos = [], 0
    for s, e in cut:
        out.append(data[pos:s])
        out.append(b" ")
        pos = e
    out.append(data[pos:])
    return b"".join(out), True


def _remove_hidden_layers(doc: pymupdf.Document) -> None:
    off, hidden = _off_layers(doc)
    if not off:
        return
    # form XObjects and images that are themselves in a hidden layer: emptied (hidden wherever they are used)
    for x in _xrefs(doc):
        if not doc.xref_is_stream(x):
            continue
        t, v = _key(doc, x, "OC")
        if t == "xref" and hidden(int(v.split()[0])):
            if _key(doc, x, "Subtype")[1] == "/Image":
                for k in ("Filter", "DecodeParms", "SMask", "Mask", "Decode", "ColorSpace"):
                    doc.xref_set_key(x, k, "null")
                doc.xref_set_key(x, "ColorSpace", "/DeviceGray")
                doc.xref_set_key(x, "Width", "1")
                doc.xref_set_key(x, "Height", "1")
                doc.xref_set_key(x, "BitsPerComponent", "8")
                doc.update_stream(x, b"\xff")
            else:
                doc.update_stream(x, b"")
            continue
        if _key(doc, x, "Subtype")[1] == "/Form":
            names = _props_map(doc, x)
            if names:
                new, changed = strip_hidden_content(doc.xref_stream(x) or b"", names, hidden)
                if changed:
                    doc.update_stream(x, new)
    for page in doc:
        for a in list(page.annots()):
            t, v = _key(doc, a.xref, "OC")
            if t == "xref" and hidden(int(v.split()[0])):
                page.delete_annot(a)
        names = _props_map(doc, page.xref)
        if not names:
            continue
        new, changed = strip_hidden_content(page.read_contents(), names, hidden)
        if changed:
            x = doc.get_new_xref()
            doc.update_object(x, "<<>>")
            doc.update_stream(x, new, new=True)
            doc.xref_set_key(page.xref, "Contents", f"{x} 0 R")
    # the removed layers leave the layer list
    cat = doc.pdf_catalog()
    t, v = _key(doc, cat, "OCProperties")
    if t in ("dict", "xref"):
        target = int(v.split()[0]) if t == "xref" else None
        src = doc.xref_object(target, compressed=True) if target else v
        for x in off:
            src = re.sub(rf"(?<!\d){x}\s+0\s+R", "", src)
        if target:
            doc.update_object(target, src)
        else:
            doc.xref_set_key(cat, "OCProperties", src)


def _flatten_forms(doc: pymupdf.Document) -> None:
    if not doc.is_form_pdf:
        return
    doc.bake(annots=False, widgets=True)
    cat = doc.pdf_catalog()
    if _key(doc, cat, "AcroForm")[0] != "null":
        doc.xref_set_key(cat, "AcroForm", "null")


def apply_sanitize(doc: pymupdf.Document, spec: Sanitize) -> None:
    """Apply a Sanitize spec to an output document (before metadata edits and decorations are written)."""
    if not spec:
        return
    if spec.javascript:
        _remove_actions(doc)
    if spec.attachments:
        _remove_attachments(doc)
    if spec.comments:
        _remove_annotations(doc)
    if spec.links:
        _remove_links(doc)
    if spec.hidden_layers:
        _remove_hidden_layers(doc)
    if spec.hidden_text:
        _remove_hidden_text(doc)
    if spec.forms:
        _flatten_forms(doc)
    if spec.thumbnails:
        for page in doc:
            if _key(doc, page.xref, "Thumb")[0] != "null":
                doc.xref_set_key(page.xref, "Thumb", "null")
    if spec.page_labels:
        cat = doc.pdf_catalog()
        if _key(doc, cat, "PageLabels")[0] != "null":
            doc.xref_set_key(cat, "PageLabels", "null")
    if spec.metadata:
        _remove_metadata(doc)

