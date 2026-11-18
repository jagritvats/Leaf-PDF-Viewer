"""Document model.

Sources are opened from bytes and never mutated, so they are always the pristine
originals. Edits live in memory as a page list (order/rotation), form values and
overlay items, and are applied to a fresh copy at save time. That makes saving
repeatable, keeps undo working after save, and never locks the file on disk.
"""
from __future__ import annotations

import itertools
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

import pymupdf

_uid = itertools.count(1)
# expand ligature glyphs (fi, ffi, ...) so search and copy see plain letters, as other viewers do
WORD_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES
SEARCH_FLAGS = pymupdf.TEXTFLAGS_SEARCH & ~pymupdf.TEXT_PRESERVE_LIGATURES
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".jxr", ".pnm"}


class PasswordRequired(Exception):
    pass


@dataclass(eq=False)
class Source:
    id: int
    name: str
    data: bytes
    doc: pymupdf.Document
    password: str | None = None
    _pages: OrderedDict = field(default_factory=OrderedDict)
    _words: dict = field(default_factory=dict)
    # ocr: recognized words per page index (Source.words format), merged into words(); see PdfDocument.ocr
    ocr: dict = field(default_factory=dict)
    # display copy: see render_page()
    _display: pymupdf.Document | None = None
    _dpages: OrderedDict = field(default_factory=OrderedDict)
    _dorig: dict = field(default_factory=dict)  # xref -> original /F
    _dflags: dict = field(default_factory=dict)  # xref -> /F in the display copy
    _hidden: frozenset = frozenset()
    display_rev: int = 0
    # text edits (leaf/textedit.py): a page uid with edits is shown from its own "slot", a page appended to the
    # display copy that shares the original page's resources and annotations but gets its own content. Per uid,
    # not per source page, so two copies of one source page (duplicate) never show each other's edits.
    _slots: dict = field(default_factory=dict)  # page uid -> (slot page number in the display copy, source index)
    _live: dict = field(default_factory=dict)  # page uid -> state shown in its slot (only uids that use their slot)
    _urev: dict = field(default_factory=dict)  # page uid -> revision of its slot (render worker sync)
    _dbase: int = 0  # xref count of the display copy as opened: objects at or above it were made by Leaf
    # images (leaf/images.py): page uid -> {draw key: ImageEdit}, replayed in the same slot after the text edits
    _imgs: dict = field(default_factory=dict)

    def page(self, index: int) -> pymupdf.Page:
        p = self._pages.get(index)
        if p is None:
            p = self.doc[index]
            self._pages[index] = p
            if len(self._pages) > 96:
                self._pages.popitem(last=False)
        else:
            self._pages.move_to_end(index)
        return p

    def words(self, index: int, uid: int | None = None) -> list[tuple]:
        """Words in page.rect (display) coordinates: (x0, y0, x1, y1, text, block, line, word).
        With the page uid, words of that page as shown (its text edits included)."""
        ck = ("u", uid, index) if uid is not None and uid in self._live else index
        w = self._words.get(ck)
        if w is None:
            page = self.text_page(index, uid)
            m = page.rotation_matrix
            w = []
            for x0, y0, x1, y1, text, b, l, n in page.get_text("words", sort=False, flags=WORD_FLAGS):
                r = pymupdf.Rect(x0, y0, x1, y1) * m
                w.append((r.x0, r.y0, r.x1, r.y1, text, b, l, n))
            if self.ocr.get(index):  # ocr: recognized words follow the page's own (selection, copy, search)
                w += self.ocr[index]
            self._words[ck] = w
        return w

    def text_page(self, index: int, uid: int | None = None) -> pymupdf.Page:
        """The page whose text is shown for page `uid` (its text-edit slot), else the source page."""
        if uid is not None and uid in self._live:
            return self.render_page(index, uid)
        return self.page(index)

    def set_ocr_words(self, index: int, words) -> None:
        """ocr: recognized words for a page (Source.words format), or None to drop them."""
        if words:
            self.ocr[index] = list(words)
        else:
            self.ocr.pop(index, None)
        self._words.pop(index, None)
        for k in [k for k in self._words if isinstance(k, tuple) and k[2] == index]:  # text-edit slots of it
            del self._words[k]

    # -------------------------------------------------- display copy (annots)
    # Annotations the user changed or deleted are hidden from the page image (the overlay item draws them
    # instead). They are hidden in a separate document opened from the same bytes ("display copy"), never in
    # `doc`, and never used for saving. See leaf/annots.py.
    def render_page(self, index: int, uid: int | None = None) -> pymupdf.Page:
        """The page to rasterize: from the display copy once any annotation is hidden, else from `doc`.
        With the page uid: that page's text-edit slot if it has one in use."""
        if self._display is None:
            return self.page(index)
        if uid is not None and uid in self._live:
            index = self._slots[uid][0]
        p = self._dpages.get(index)
        if p is None:
            p = self._dpages[index] = self._display[index]
            if len(self._dpages) > 48:
                self._dpages.popitem(last=False)
        else:
            self._dpages.move_to_end(index)
        return p

    def annot_flags(self) -> dict[int, int]:
        """xref -> /F value in the display copy, for every annotation ever hidden (restored ones included)."""
        return dict(self._dflags)

    @property
    def hidden_annots(self) -> frozenset:
        return self._hidden

    def set_hidden_annots(self, xrefs) -> set[int]:
        """Hide exactly these annotation xrefs in the display copy (showing all others again).
        Returns the xrefs whose visibility changed."""
        xrefs = frozenset(xrefs)
        changed = set(xrefs ^ self._hidden)
        if not changed:
            return changed
        self._ensure_display()
        for x in changed:
            if x not in self._dorig:
                t, v = self.doc.xref_get_key(x, "F")
                self._dorig[x] = int(float(v)) if t in ("int", "real") else 0
            f = self._dorig[x] | 2 if x in xrefs else self._dorig[x]  # bit 2: Hidden
            self._dflags[x] = f
            try:
                self._display.xref_set_key(x, "F", str(f))
            except Exception:  # noqa: BLE001  (not an object of this file)
                pass
        self._hidden = xrefs
        self.display_rev += 1
        return changed

    def _ensure_display(self) -> pymupdf.Document:
        if self._display is None:
            disp = pymupdf.open(stream=self.data, filetype="pdf")
            if disp.needs_pass:
                disp.authenticate(self.password or "")
            self._display = disp
            self._dbase = disp.xref_length()
        return self._display

    # -------------------------------------------------- display copy (text edits)
    _SLOT_KEYS = ("MediaBox", "CropBox", "BleedBox", "TrimBox", "ArtBox", "Rotate", "Resources", "Contents",
                  "Annots", "Group", "UserUnit")

    def _fill_slot(self, pno: int, index: int) -> None:
        """Point the slot page at the original page's content, resources and annotations again."""
        disp = self._display
        ox, px = disp.page_xref(index), disp.page_xref(pno)
        for k in self._SLOT_KEYS:
            t, v = disp.xref_get_key(ox, k)
            x = ox
            for _ in range(32):  # inheritable keys come from the page tree
                if t != "null" or k not in ("MediaBox", "CropBox", "Rotate", "Resources"):
                    break
                tp, vp = disp.xref_get_key(x, "Parent")
                if tp != "xref":
                    break
                x = int(vp.split()[0])
                t, v = disp.xref_get_key(x, k)
            disp.xref_set_key(px, k, "null" if t == "null" else v)
        self._dpages.pop(pno, None)

    def set_text_edits(self, uid: int, index: int, edits: dict, hidden: tuple | None = None) -> list[str]:
        """Show page `uid` (source page `index`) with these text edits ({unit key: new text}) and, while the inline
        editor is open, without the glyphs of unit `hidden`. Returns the edits' warnings."""
        from .textedit import apply_edits, page_text

        imgs = self._imgs.get(uid) or {}  # images: shown in the same slot (set_image_edits)
        state = (index, frozenset(edits.items()), hidden, frozenset(imgs.items()))
        if self._live.get(uid) == state:
            return []
        warnings: list[str] = []
        if not edits and hidden is None and not imgs:
            if self._live.pop(uid, None) is None:
                return []
        else:
            disp = self._ensure_display()
            slot = self._slots.get(uid)
            if slot is None or slot[1] != index:
                r = disp[index].mediabox
                disp.new_page(-1, width=max(r.width, 1), height=max(r.height, 1))  # (the Page is dropped at once)
                self._dpages.clear()  # adding a page invalidates every Page object of the document
                slot = self._slots[uid] = (disp.page_count - 1, index)
            # MuPDF reads a page's annotations when it loads the page: the keys are set before the first load
            self._fill_slot(slot[0], index)
            page = disp[slot[0]]
            try:
                warnings = apply_edits(page, edits, hidden, pt=page_text(self, index))
                if imgs:  # images: after the text edits, as at save
                    from .images import apply_image_edits
                    apply_image_edits(page, imgs)
            except Exception:
                self._fill_slot(slot[0], index)
                self._live.pop(uid, None)
                raise
            finally:
                self._dpages.pop(slot[0], None)
            self._live[uid] = state
        for k in [k for k in self._words if isinstance(k, tuple) and k[1] == uid]:
            del self._words[k]
        self._urev[uid] = self._urev.get(uid, 0) + 1
        self.display_rev += 1
        return warnings

    def set_image_edits(self, uid: int, index: int, imgs: dict | None) -> None:
        """images: remember the image edits ({draw key: ImageEdit}) page `uid` is shown with; the next
        set_text_edits for it (canvas.sync_text) shows them, after its text edits."""
        if imgs:
            self._imgs[uid] = dict(imgs)
        else:
            self._imgs.pop(uid, None)

    def upage_revs(self) -> dict[int, int]:
        """page uid -> revision, for every uid whose slot was ever used (render worker sync)."""
        return dict(self._urev)

    def upage_payload(self, uid: int):
        """What the render worker needs to show page `uid` like the display copy: None (show the source page), or
        (source index, slot page keys, [(xref, object, raw stream or None)]) for every object Leaf created that the
        slot uses. Objects of the original file are referenced as they are (the worker has the same bytes)."""
        if uid not in self._live:
            return None
        disp = self._display
        pno, index = self._slots[uid]
        px = disp.page_xref(pno)
        keys = {}
        for k in self._SLOT_KEYS:
            t, v = disp.xref_get_key(px, k)
            if t != "null":
                keys[k] = v
        objs, seen = [], set()
        todo = [int(n) for v in keys.values() for n in re.findall(r"\b(\d+) 0 R\b", v)]
        while todo:
            x = todo.pop()
            if x < self._dbase or x in seen:
                continue
            seen.add(x)
            src = disp.xref_object(x, compressed=True)
            raw = disp.xref_stream_raw(x) if disp.xref_is_stream(x) else None
            objs.append((x, src, raw))
            todo += [int(n) for n in re.findall(r"\b(\d+) 0 R\b", src)]
        return index, keys, objs


@dataclass(eq=False)
class PageRef:
    src: Source
    index: int
    rotation: int = 0  # extra rotation on top of the page's own /Rotate (multiple of 90)
    uid: int = field(default_factory=lambda: next(_uid))
    # optimize: visible area as (x0, y0, x1, y1) in the source page.rect space, or None; written as the
    # CropBox at save (leaf/crop.py). Overlays keep their page.rect coordinates either way.
    crop: tuple | None = None


class Overlay(Protocol):
    def apply(self, page: pymupdf.Page, to_pdf: pymupdf.Matrix, rotate: int) -> None: ...


def _read_as_pdf(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        img = pymupdf.open(stream=data, filetype=ext[1:])
        data = img.convert_to_pdf()
        img.close()
    return data


def rect_key(r) -> tuple:
    return tuple(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1))


class PdfDocument:
    def __init__(self) -> None:
        self.path: str | None = None
        self.sources: list[Source] = []
        self.pages: list[PageRef] = []
        # (page uid, field name, rect key) -> value
        self.form_values: dict[tuple, object] = {}
        # output: Info keys to change ("title", "author", "subject", "keywords"); applied at save (docprops.py)
        self.metadata_edits: dict[str, str] = {}
        # output: None keeps the primary's encryption as it is; a docprops.Security re-encrypts (or removes it)
        self.security = None
        # decorate: watermarks, headers/footers, Bates (decorations.Decoration specs), drawn at save in final order
        self.decorations: list = []
        # redact: a redact.Sanitize spec (remove metadata, JavaScript, attachments, ...); applied at save, full rewrite
        self.sanitize = None
        # ocr: recognized text, page uid -> ocr.OcrPage (source page + words); written as invisible text at save.
        # Change it only through set_ocr() (keeps Source.words in sync). Duplicates of a page share its result.
        self.ocr: dict = {}
        # text edits (leaf/textedit.py): page uid -> {TextUnit.key: new text}; replayed at save after form values and
        # before overlays. Change through commands.EditPageText (keeps the display copy in sync).
        self.text_edits: dict[int, dict[tuple, str]] = {}
        # images (leaf/images.py): page uid -> {ImageDraw.key: ImageEdit} (move/resize/rotate/replace/delete one draw
        # of an image already in the page); replayed at save right after the text edits. Change through
        # features/images.py (keeps the display copy in sync).
        self.image_edits: dict[int, dict[tuple, object]] = {}
        # text edits: an incremental save keeps the old text in the file's previous revision. None = rewrite the
        # file when it has text edits unless it is digitally signed; True/False = always/never rewrite for them.
        self.prefer_rewrite: bool | None = None
        # settings (Saving › Always rewrite the whole file): never append an incremental update
        self.force_rewrite = False
        # structure: links edited per page (page uid -> links.PageLinks; absent = untouched), the outline (None =
        # untouched, else a tuple of outline.Bookmark) and attachments (None = untouched, else a tuple of
        # attachments.Attachment). Page targets are uids, resolved to final page numbers at save (links.apply_structure)
        self.links: dict = {}
        self.outline = None
        self.attachments = None

    # ---------------------------------------------------------------- loading
    @classmethod
    def open(cls, path: str, password: str | None = None) -> "PdfDocument":
        self = cls()
        self.path = os.path.abspath(path)
        src = self.add_source(path, password)
        self.pages = [PageRef(src, i) for i in range(src.doc.page_count)]
        return self

    def add_source(self, path: str, password: str | None = None) -> Source:
        return self._load(os.path.basename(path), _read_as_pdf(path), password)

    def add_blank_source(self, width: float, height: float) -> Source:
        d = pymupdf.open()
        d.new_page(width=width, height=height)
        data = d.tobytes()
        d.close()
        return self._load("blank", data)

    def add_pdf_bytes(self, name: str, data: bytes, password: str | None = None) -> Source:
        return self._load(name, data, password)

    def import_source(self, src: Source) -> Source:
        """A Source of this document with the same bytes as `src` (which may belong to another document).

        Re-uses `src` itself or an earlier import of it; never re-reads the disk."""
        if any(s is src for s in self.sources):
            return src
        imported = self.__dict__.setdefault("_imported", {})
        mine = imported.get(src)
        if mine is None:
            mine = imported[src] = self._load(src.name, src.data, src.password)
        return mine

    def _load(self, name: str, data: bytes, password: str | None = None) -> Source:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass and not (password and doc.authenticate(password)):
            doc.close()
            raise PasswordRequired(name)
        src = Source(len(self.sources), name, data, doc, password)
        self.sources.append(src)
        return src

    @property
    def primary(self) -> Source:
        return self.sources[0]

    # --------------------------------------------------------------- geometry
    def page(self, ref: PageRef) -> pymupdf.Page:
        return ref.src.page(ref.index)

    def text_page(self, ref: PageRef) -> pymupdf.Page:
        """The page to search/extract text from: shows the page's text edits (same geometry as page())."""
        return ref.src.text_page(ref.index, ref.uid)

    def page_rect(self, ref: PageRef) -> pymupdf.Rect:
        """Page rect as displayed by the source (includes its own /Rotate)."""
        return self.page(ref).rect

    def view_rect(self, ref: PageRef) -> pymupdf.Rect:
        """The part of page.rect that is shown: the crop (optimize) or the whole page."""
        return pymupdf.Rect(ref.crop) if ref.crop is not None else self.page_rect(ref)

    def extra_matrix(self, ref: PageRef, scale: float = 1.0) -> pymupdf.Matrix:
        """Maps page.rect space -> display space (extra rotation, origin at 0,0 = top left of the crop)."""
        m = pymupdf.Matrix(scale, scale).prerotate(ref.rotation)
        r = self.view_rect(ref) * m
        return m * pymupdf.Matrix(1, 0, 0, 1, -r.x0, -r.y0)

    def display_size(self, ref: PageRef) -> tuple[float, float]:
        r = self.view_rect(ref)
        return (r.height, r.width) if ref.rotation % 180 else (r.width, r.height)

    def render(self, ref: PageRef, scale: float, clip=None):
        """Render at `scale`; `clip` is a display-space rect (unscaled points).

        Returns (pixmap, (x, y, w, h)) where the tuple is the display-space area covered.
        """
        # hides annotations the user changed (their items draw them); shows the page's text edits
        page = ref.src.render_page(ref.index, ref.uid)
        m = pymupdf.Matrix(scale, scale).prerotate(ref.rotation)
        base = self.view_rect(ref)  # crop (optimize) or page.rect
        if clip is not None:
            clip = pymupdf.Rect(clip) * ~self.extra_matrix(ref)
            if ref.crop is not None:
                clip &= base
        elif ref.crop is not None:
            clip = base
        pix = page.get_pixmap(matrix=m, clip=clip, alpha=False, annots=True)
        rs = base * m
        return pix, ((pix.x - rs.x0) / scale, (pix.y - rs.y0) / scale, pix.width / scale, pix.height / scale)

    # ------------------------------------------------------------------ state
    def is_identity(self, pages: list[PageRef]) -> bool:
        p = self.primary
        return (
            len(pages) == p.doc.page_count
            and all(r.src is p and r.index == i and r.rotation == 0 for i, r in enumerate(pages))
        )

    def has_text_edits(self, pages: list[PageRef] | None = None) -> bool:
        """text edits: any on these pages (default: the page list)."""
        return any(self.text_edits.get(r.uid) for r in (self.pages if pages is None else pages))

    def rewrite_for_text_edits(self) -> bool:
        """text edits: whether a save with text edits is a full rewrite (see prefer_rewrite)."""
        if self.prefer_rewrite is not None:
            return self.prefer_rewrite
        from .textedit import is_signed
        return not is_signed(self.primary)

    @staticmethod
    def has_redactions(pages: list[PageRef], overlays_for) -> bool:
        """redact: any redaction mark (an overlay with `redact = True`) on these pages."""
        return any(getattr(it, "redact", False) for r in pages for it in overlays_for(r))

    # ------------------------------------------------------------------- save
    def save(
        self,
        target: str,
        overlays_for: Callable[[PageRef], Iterable[Overlay]],
        pages: list[PageRef] | None = None,
        fast: bool = False,
    ) -> bool:
        """Write to `target` atomically. Returns True if saved incrementally.

        `fast` skips duplicate-object merging and compression on a full rewrite (crash-recovery snapshots)."""
        pages = list(self.pages if pages is None else pages)
        if not pages:
            raise ValueError("A PDF needs at least one page.")
        target = os.path.abspath(target)
        primary = self.primary
        fd, tmp = tempfile.mkstemp(prefix=".leaf-", suffix=".pdf", dir=os.path.dirname(target))
        os.close(fd)
        # a new password/permission set needs a full rewrite (it invalidates existing signatures)
        incremental = bool(self.is_identity(pages) and primary.doc.can_save_incrementally() and self.security is None)
        # redact: redactions and sanitizing are never appended (the old bytes would stay in the file); they force a
        # full rewrite that drops unreferenced objects (garbage=4) and cleans content streams
        scrub = bool(self.sanitize) or self.has_redactions(pages, overlays_for)
        incremental = incremental and not scrub and not getattr(self, "force_rewrite", False)
        if incremental and self.has_text_edits(pages) and self.rewrite_for_text_edits():
            incremental = False  # text edits: the previous revision would keep the old text
        try:
            if incremental:
                # Appending changes keeps the original bytes (and signatures) intact.
                with open(tmp, "wb") as f:
                    f.write(primary.data)
                out = pymupdf.open(tmp)
                self._auth(out, primary)
                self._apply(out, pages, overlays_for)
                self._apply_structure(out, pages, overlays_for)
                self._apply_metadata(out)
                self._decorate(out, target, fast)
                out.saveIncr()  # keeps the original encryption
            else:
                out = self._assemble(pages, overlays_for)
                self._apply_structure(out, pages, overlays_for)  # structure: before Sanitize (which may remove it)
                if self.sanitize:  # redact: before metadata, so properties set explicitly in Leaf still apply
                    from .redact import apply_sanitize
                    apply_sanitize(out, self.sanitize)
                self._apply_metadata(out)
                self._decorate(out, target, fast)
                if scrub and not fast:  # redact: drop every unreferenced object (old content streams, images, fonts)
                    out.save(tmp, garbage=4, clean=True, deflate=True, **self._encryption())
                else:
                    out.save(tmp, garbage=1 if fast else 3, deflate=not fast, **self._encryption())
            out.close()
            check = pymupdf.open(tmp)
            if check.needs_pass:
                check.authenticate(self.output_password() or "")
            ok = check.page_count == len(pages)
            check.close()
            if not ok:
                raise RuntimeError("Verification failed: page count mismatch after save.")
            os.replace(tmp, target)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return incremental

    def _assemble(self, pages: list[PageRef], overlays_for) -> pymupdf.Document:
        """Build the output for a full rewrite, starting from the primary (keeps its catalog, outline, metadata).

        Each part gets its form values and overlays *before* it is merged: merging renames clashing field
        names, and a page used twice (duplicate, paste) needs its own page object, not a second reference.
        Pages are arranged with move/delete rather than `select()`, which drops the AcroForm (MuPDF 1.28)."""
        primary = self.primary
        out = pymupdf.open(stream=primary.data, filetype="pdf")
        self._auth(out, primary)
        groups: dict[tuple, list[PageRef]] = {}  # (source, copy number) -> refs, in page order
        seen: dict[tuple, int] = {}
        for r in pages:
            n = seen.get((r.src, r.index), 0)
            seen[(r.src, r.index)] = n + 1
            groups.setdefault((r.src, n), []).append(r)
        base = groups.pop((primary, 0), [])
        self._apply(out, self._aligned(primary, base, out.page_count), overlays_for)
        pos = {r.uid: r.index for r in base}  # page uid -> page number in `out`
        for (src, _), refs in groups.items():
            part = pymupdf.open(stream=src.data, filetype="pdf")
            self._auth(part, src)
            self._apply(part, self._aligned(src, refs, part.page_count), overlays_for)
            for r in refs:
                pos[r.uid] = out.page_count
                out.insert_pdf(part, from_page=r.index, to_page=r.index)
            part.close()
        order = [pos[r.uid] for r in pages]
        cur = list(range(out.page_count))  # cur[k]: original number of the page now at position k
        for t, want in enumerate(order):
            k = cur.index(want, t)
            if k != t:
                out.move_page(k, t)
                cur.insert(t, cur.pop(k))
        if len(cur) > len(order):
            dropped = list(range(len(order), len(cur)))
            dead = self._field_roots(out, dropped)
            out.delete_pages(dropped)
            self._drop_fields(out, dead - self._field_roots(out, range(out.page_count)))
        for i, r in enumerate(pages):
            if r.rotation:
                p = out[i]
                p.set_rotation((p.rotation + r.rotation) % 360)
        return out

    @staticmethod
    def _aligned(src: Source, refs: list[PageRef], n: int) -> list[PageRef]:
        """`refs` placed at their page numbers, with stand-ins (no values, no overlays) for the other pages."""
        at = {r.index: r for r in refs}
        return [at.get(i) or PageRef(src, i) for i in range(n)]

    @staticmethod
    def _field_roots(doc: pymupdf.Document, pnos) -> set[int]:
        roots = set()
        for pno in pnos:
            for xref, kind, _ in doc[pno].annot_xrefs():
                if kind != pymupdf.PDF_ANNOT_WIDGET:
                    continue
                for _ in range(32):  # follow /Parent up to the terminal field's root
                    t, v = doc.xref_get_key(xref, "Parent")
                    if t != "xref":
                        break
                    xref = int(v.split()[0])
                roots.add(xref)
        return roots

    @staticmethod
    def _drop_fields(doc: pymupdf.Document, xrefs: set[int]) -> None:
        """Remove fields whose widgets were all on deleted pages from /AcroForm /Fields."""
        if not xrefs:
            return
        cat = doc.pdf_catalog()
        t, v = doc.xref_get_key(cat, "AcroForm/Fields")
        if t != "array":
            return
        fields = [int(x) for x in re.findall(r"(\d+)\s+\d+\s+R", v)]
        keep = [f for f in fields if f not in xrefs]
        if keep != fields:
            doc.xref_set_key(cat, "AcroForm/Fields", "[" + " ".join(f"{f} 0 R" for f in keep) + "]")

    def _decorate(self, out: pymupdf.Document, target: str, fast: bool = False) -> None:
        if self.decorations:
            from .decorations import apply_all
            # recovery snapshots (fast) keep the document's own name for {filename}
            name = os.path.basename(self.path) if fast and self.path else os.path.basename(target)
            apply_all(out, self.decorations, name)

    def _apply_structure(self, out: pymupdf.Document, pages: list[PageRef], overlays_for) -> None:
        """structure: new/edited links, the edited outline and attachments, once the page list is final."""
        if self.links or self.outline is not None or self.attachments is not None:
            from .links import apply_structure
            apply_structure(self, out, pages, overlays_for)

    def _apply_metadata(self, out: pymupdf.Document) -> None:
        if self.metadata_edits:
            from .docprops import apply_metadata
            apply_metadata(out, self.metadata_edits)

    def _encryption(self) -> dict:
        """Keyword arguments for `Document.save` on a full rewrite. Without a new setting the primary's
        encryption is kept (MuPDF's default would silently write the file decrypted)."""
        sec = self.security
        if sec is None:
            enc = self.primary.doc.metadata.get("encryption")
            return {"encryption": pymupdf.PDF_ENCRYPT_KEEP if enc else pymupdf.PDF_ENCRYPT_NONE}
        return sec.save_kwargs()

    def output_password(self) -> str | None:
        """The password that opens what `save` writes (None if it opens without one)."""
        sec = self.security
        if sec is None:
            return self.primary.password
        return sec.user_pw or sec.owner_pw or None

    @staticmethod
    def _auth(doc: pymupdf.Document, src: Source) -> None:
        if doc.needs_pass:
            doc.authenticate(src.password or "")

    # ---------------------------------------------------------------- OCR
    def ocr_words(self, ref: PageRef) -> list:
        """ocr: recognized words of a page (page.rect space): its own result, else one for the same source page."""
        e = self.ocr.get(ref.uid)
        if e is None:
            e = next((o for o in self.ocr.values() if o.src is ref.src and o.index == ref.index), None)
        return e.words if e is not None else []

    def set_ocr(self, uid: int, entry) -> None:
        """ocr: set (or with None, remove) the OCR result of a page and refresh its source's words."""
        old = self.ocr.pop(uid, None)
        if entry is not None:
            self.ocr[uid] = entry
        from .ocr import as_source_words

        for e in {id(x): x for x in (old, entry) if x is not None}.values():
            cur = next((o for o in self.ocr.values() if o.src is e.src and o.index == e.index), None)
            e.src.set_ocr_words(e.index, as_source_words(cur.words) if cur is not None else None)

    def _apply(self, out, pages, overlays_for) -> None:
        values_by_page: dict[int, dict] = {}
        for (uid, name, rk), v in self.form_values.items():
            values_by_page.setdefault(uid, {})[(name, rk)] = v
        for i, ref in enumerate(pages):
            vals = values_by_page.get(ref.uid)
            items = list(overlays_for(ref))
            ocr_words = self.ocr_words(ref) if self.ocr else None
            tedits = self.text_edits.get(ref.uid)
            if self.links.get(ref.uid) is not None:  # structure: deleted/edited original links, by xref (leaf/links.py)
                from .links import remove_links
                remove_links(out[i], self.links[ref.uid])
            iedits = self.image_edits.get(ref.uid)  # images (leaf/images.py)
            if not vals and not items and not ocr_words and not tedits and not iedits:
                continue
            page = out[i]
            if vals:
                write_form_values(page, vals)
            if tedits:  # text edits: after form values, before overlays (a redaction over edited text removes the new)
                from .textedit import Refuse, apply_edits
                try:
                    apply_edits(page, tedits)
                except Refuse as e:
                    raise RuntimeError(f"A text edit on page {i + 1} could not be applied: {e}") from e
            if iedits:  # images: moved/resized/rotated/replaced/deleted draws; after text edits, before overlays
                from .images import Refuse as ImageRefuse, apply_image_edits
                try:
                    apply_image_edits(page, iedits)
                except ImageRefuse as e:
                    raise RuntimeError(f"An image edit on page {i + 1} could not be applied: {e}") from e
            src_page = self.page(ref)
            to_pdf = src_page.derotation_matrix  # overlay coords are in the source page.rect space
            rotate = src_page.rotation
            batch: list = []
            for it in items:
                if getattr(it, "font_layer", False):
                    batch.append(it)
                    continue
                _flush_font_layer(page, batch, to_pdf, rotate)
                it.apply(page, to_pdf, rotate)
            _flush_font_layer(page, batch, to_pdf, rotate)
            if ocr_words:  # ocr: invisible text layer (render mode 3), after overlays, before redactions
                from .ocr import write_text_layer
                write_text_layer(page, ocr_words, to_pdf)
            # redact: after overlays (and the OCR text layer), before the crop box; removes what lies under the marks
            marks = [it for it in items if getattr(it, "redact", False)]
            if marks:
                from .redact import redact_page
                redact_page(page, marks, to_pdf)
        # optimize: crop boxes LAST (keep this after every other per-page step): a new CropBox moves the page's
        # coordinate origin, which everything above (overlays, OCR text, redactions) relies on.
        if any(ref.crop is not None for ref in pages):
            from .crop import apply_crop
            for i, ref in enumerate(pages):
                if ref.crop is not None:
                    apply_crop(out[i], ref.crop)


def _flush_font_layer(page: pymupdf.Page, batch: list, to_pdf, rotate: int) -> None:
    """Write consecutive text boxes through a scratch page with the same geometry, subset its fonts, and place
    it as one Form XObject. Embedding directly would add each system font in full (~1 MB per box); subsetting
    the output itself isn't safe (fonts shared by other pages, signed content), but the scratch page only
    holds fonts Leaf added."""
    if not batch:
        return
    items = list(batch)
    batch.clear()
    tmp = pymupdf.open()
    try:
        sp = tmp.new_page(width=page.mediabox.width, height=page.mediabox.height)
        sp.set_mediabox(page.mediabox)
        sp.set_cropbox(page.cropbox)
        sp.set_rotation(page.rotation)
        for it in items:
            it.apply(sp, to_pdf, rotate)
        tmp.subset_fonts()
        rot = page.rotation
        # identical unrotated geometry on both pages -> identity placement (show_pdf_page misplaces content on
        # rotated pages whose boxes don't start at 0,0, so place unrotated and restore)
        sp.set_rotation(0)
        page.set_rotation(0)
        try:
            page.show_pdf_page(page.rect, tmp, 0)
        finally:
            page.set_rotation(rot)
    finally:
        tmp.close()


# ------------------------------------------------------------------ forms
_CHECKS = (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON)


def _xref_of(v: str) -> int:
    return int(v.split()[0])


def field_xref(doc: pymupdf.Document, xref: int) -> int:
    """The field dictionary a widget belongs to: the nearest object (itself or an ancestor) with /T."""
    x = xref
    for _ in range(32):
        if doc.xref_get_key(x, "T")[0] != "null":
            return x
        t, p = doc.xref_get_key(x, "Parent")
        if t != "xref":
            break
        x = _xref_of(p)
    return xref


def inherited_key(doc: pymupdf.Document, xref: int, key: str) -> tuple[str, str]:
    """(type, value) of an inheritable field key; falls back to the AcroForm dict (DA, Q)."""
    x = xref
    for _ in range(32):
        t, v = doc.xref_get_key(x, key)
        if t != "null":
            return t, v
        t, p = doc.xref_get_key(x, "Parent")
        if t != "xref":
            break
        x = _xref_of(p)
    return doc.xref_get_key(doc.pdf_catalog(), f"AcroForm/{key}")


def _decode_pdf_bytes(b: bytes) -> str:
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "replace")
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    return b.decode("latin-1")


def parse_pdf_strings(s: str) -> list[str]:
    """All literal/hex strings in a PDF object source, e.g. '[(A)(B)]' -> ['A', 'B']."""
    out, i, n = [], 0, len(s)
    esc = {"n": b"\n", "r": b"\r", "t": b"\t", "b": b"\b", "f": b"\f"}
    while i < n:
        c = s[i]
        if c == "(":
            depth, buf, i = 1, bytearray(), i + 1
            while i < n:
                c = s[i]
                if c == "\\" and i + 1 < n:
                    d = s[i + 1]
                    if d in esc:
                        buf += esc[d]
                        i += 2
                    elif d.isdigit():
                        j = i + 1
                        while j < n and j < i + 4 and s[j] in "01234567":
                            j += 1
                        buf.append(int(s[i + 1:j], 8) & 255)
                        i = j
                    else:
                        if d not in "\r\n":
                            buf += d.encode("latin-1", "replace")
                        i += 2
                    continue
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                buf += c.encode("latin-1", "replace")
                i += 1
            out.append(_decode_pdf_bytes(bytes(buf)))
        elif c == "<" and not s.startswith("<<", i):
            j = s.find(">", i)
            hx = "".join(s[i + 1:j].split())
            out.append(_decode_pdf_bytes(bytes.fromhex(hx + "0" * (len(hx) % 2))))
            i = j + 1
        else:
            i += 1
    return out


def choice_pairs(values) -> list[tuple[str, str]]:
    """Widget choice_values as (export value, display text) pairs."""
    out = []
    for c in values or []:
        if isinstance(c, (list, tuple)):
            out.append((str(c[0]), str(c[1] if len(c) > 1 else c[0])))
        else:
            out.append((str(c), str(c)))
    return out


def listbox_value(doc: pymupdf.Document, w: pymupdf.Widget) -> list[str]:
    """Selected export values of a list box (PyMuPDF only reads single selections)."""
    t, v = inherited_key(doc, w.xref, "V")
    if t == "array":
        return parse_pdf_strings(v)
    if t == "string":
        return [v] if v else []
    return [str(w.field_value)] if w.field_value not in (None, False, "") else []


def _pdf_lit(text: str) -> str:
    out = []
    for b in text.encode("cp1252", "replace"):
        if b in b"()\\":
            out.append("\\" + chr(b))
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append("\\%03o" % b)
    return "(" + "".join(out) + ")"


def da_font_size(doc: pymupdf.Document, xref: int) -> float:
    import re

    t, da = inherited_key(doc, xref, "DA")
    m = re.search(r"([\d.]+)\s+Tf", da) if t == "string" else None
    return float(m.group(1)) if m else 0.0


def write_listbox(page: pymupdf.Page, w: pymupdf.Widget, value) -> None:
    """Set a list box's /V and /I (single or multiple selection) and draw its appearance,
    which MuPDF doesn't generate for list boxes."""
    doc = page.parent
    fx = field_xref(doc, w.xref)
    opts = choice_pairs(w.choice_values)
    sel = [value] if isinstance(value, str) else [str(s) for s in (value or [])]
    sel = [s for s in sel if s]
    exports = [e for e, _ in opts]
    if not sel:
        doc.xref_set_key(fx, "V", "null")
        doc.xref_set_key(fx, "I", "null")
    else:
        doc.xref_set_key(fx, "V", pymupdf.get_pdf_str(sel[0]) if len(sel) == 1
                         else "[" + "".join(pymupdf.get_pdf_str(s) for s in sel) + "]")
        idx = sorted(exports.index(s) for s in sel if s in exports)
        doc.xref_set_key(fx, "I", "[" + " ".join(map(str, idx)) + "]" if idx else "null")
    W, H = w.rect.width, w.rect.height
    t, r = doc.xref_get_key(w.xref, "MK/R")
    rot = int(float(r)) % 360 if t in ("int", "real") else 0
    if rot in (90, 270):
        W, H = H, W
    m = pymupdf.Matrix(rot)
    fs = da_font_size(doc, w.xref) or min(12.0, max(6.0, (H - 2) / max(1, len(opts)) / 1.15))
    lh = fs * 1.15
    rows = max(1, int((H - 2) // lh))
    first = min((exports.index(s) for s in sel if s in exports), default=0)
    top_index = first - rows + 1 if first >= rows else 0
    ops = ["/Tx BMC", "q", f"1 1 {W - 2:.2f} {H - 2:.2f} re W n"]
    for row, (export, label) in enumerate(opts[top_index:]):
        top = H - 1 - row * lh
        if top <= 0:
            break
        if export in sel:
            ops.append(f"0.6 0.75 0.96 rg 1 {top - lh:.2f} {W - 2:.2f} {lh:.2f} re f")
        ops.append(f"BT /Helv {fs:.2f} Tf 0 g 2 {top - fs:.2f} Td {_pdf_lit(label)} Tj ET")
    ops += ["Q", "EMC"]
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, "\n".join(ops).encode("latin-1"), new=True)
    doc.xref_set_key(x, "Type", "/XObject")
    doc.xref_set_key(x, "Subtype", "/Form")
    doc.xref_set_key(x, "BBox", f"[0 0 {W:.2f} {H:.2f}]")
    doc.xref_set_key(x, "Matrix", f"[{m.a:g} {m.b:g} {m.c:g} {m.d:g} 0 0]")
    doc.xref_set_key(x, "Resources", "<</Font<</Helv<</Type/Font/Subtype/Type1/BaseFont/Helvetica"
                                     "/Encoding/WinAnsiEncoding>>>>>>")
    doc.xref_set_key(w.xref, "AP", f"<</N {x} 0 R>>")


def write_form_values(page: pymupdf.Page, vals: dict) -> None:
    """vals: (field name, rect key) -> value. Radio buttons go off before the chosen one goes on."""
    widgets = [w for w in page.widgets() if (w.field_name, rect_key(w.rect)) in vals]
    widgets.sort(key=lambda w: bool(vals[(w.field_name, rect_key(w.rect))]))
    before = _field_keys(page) if widgets else {}  # signing: see _restore_field_keys
    for w in widgets:
        v = vals[(w.field_name, rect_key(w.rect))]
        t = w.field_type
        if t == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
            continue  # never touch signature fields (keeps existing signatures valid)
        if t == pymupdf.PDF_WIDGET_TYPE_LISTBOX:
            write_listbox(page, w, v)
            continue
        if t in _CHECKS:
            v = w.on_state() if v else "Off"
        elif isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        w.field_value = "" if v is None else v
        w.update()
    if before:
        _restore_field_keys(page.parent, before)


def _field_keys(page: pymupdf.Page) -> dict[int, dict]:
    """Every widget on the page and its parent fields: xref -> {key: (type, value)}."""
    doc = page.parent
    out: dict[int, dict] = {}
    for x, kind, _ in page.annot_xrefs():
        if kind != pymupdf.PDF_ANNOT_WIDGET:
            continue
        for _ in range(32):
            if x in out:
                break
            out[x] = {k: doc.xref_get_key(x, k) for k in doc.xref_get_keys(x)}
            t, p = doc.xref_get_key(x, "Parent")
            if t != "xref":
                break
            x = _xref_of(p)
    return out


def _restore_field_keys(doc: pymupdf.Document, before: dict[int, dict]) -> None:
    """Widget.update() also rewrites things that didn't change (/BS /W, a /V on a widget kid, ...). Put back
    everything except the value and the appearance, so filling a form in a digitally signed document stays a
    permitted change (signature validators reject any other key changing; leaf/signing.py)."""
    for x, keys in before.items():
        allowed = {"AP", "AS", "V", "I"} if "T" in keys else {"AP", "AS"}
        for k in set(keys) | set(doc.xref_get_keys(x)):
            if k in allowed:
                continue
            old = keys.get(k)
            if doc.xref_get_key(x, k) == (old or ("null", "null")):
                continue
            if old is None:
                doc.xref_set_key(x, k, "null")
            else:
                doc.xref_set_key(x, k, pymupdf.get_pdf_str(old[1]) if old[0] == "string" else old[1])
