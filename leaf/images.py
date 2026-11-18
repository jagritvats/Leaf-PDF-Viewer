"""Edit images already in the PDF: the engine. Qt-free.

An image *draw* is one `/Name Do` of an image XObject in the page's content (Form XObjects included). The same
image object (xref) may be drawn several times, on this page or others; an edit changes exactly one draw.

    draws(src, index) -> [ImageDraw]                  every image draw of a source page (page content in z order)
    draw_at(doc, ref, x, y, edits) -> ImageDraw|None  hit-test in page.rect space, topmost first, as shown
    check(src, index, draw)                           raises Refuse when the draw can't be edited
    shown_quad(draw, edit) / shown_rect(draw, edit)   where the draw is shown with an edit

An edit is stored as `doc.image_edits[ref.uid][draw.key] = ImageEdit(...)` (features/images.py, undoable) and
replayed with `apply_image_edits(page, edits)` on a fresh copy of the source page: at save (document.PdfDocument
._apply, right after the page's text edits, before overlays) and on the page's slot of the display copy that
screen, thumbnails and search use (document.Source.set_text_edits, after the text edits).

How a page is rewritten: MuPDF's sanitize content filter visits every image draw (image filter callback with the
draw's full matrix). Deleted draws return no image, so the filter drops exactly that `Do`. A draw that moves (or
is replaced) gets a tiny unique placeholder image instead, which the filter writes at the same spot of the
rewritten stream (the page's own, or this page's private copy of the Form XObject that holds it). The
placeholder's `/Name Do` is then rewritten to `q <M> cm /LeafImg<xref> Do Q`, reusing the ORIGINAL image object
(or the replacement), with M = new matrix x inverse(current matrix): the draw keeps its place in the paint order
(an image under text stays under it) and other uses of the same xref (this page or others) are untouched.
`page.delete_image` / `replace_image` are never used: they change every use of the xref.

Draws are identified by their matrix, pixel size and occurrence among identical ones (`ImageDraw.key`, matched
with a small tolerance), so an edit is found again on any copy of the same source page, also after text edits
rewrote the page (those never touch images).
Refused: inline images (BI ... EI), draws whose image object can't be identified, pages MuPDF's filter can't
rewrite exactly (same guard as text edits: a no-op pass must render identically).
"""
from __future__ import annotations

import hashlib
import os
import re
import weakref
from collections import OrderedDict
from dataclasses import dataclass

import pymupdf

m = pymupdf.mupdf

EPS = 0.02  # matrix matching tolerance (pt)
FULL_PAGE = 0.8  # an image covering this much of the page is a "background" (see features/images.py)


class Refuse(Exception):
    """The image can't be edited; the message says why (shown to the user)."""


@dataclass(frozen=True)
class ImageDraw:
    index: int  # position among the page's image draws (content order, Form XObjects included)
    ctm: tuple  # (a, b, c, d, e, f): image unit square -> PDF space
    quad: tuple  # unit square corners (0,0) (1,0) (1,1) (0,1) in page.rect space
    bbox: tuple  # page.rect space
    width: int  # pixels
    height: int
    xref: int  # image XObject, 0 if unknown (inline image, ambiguous name)
    name: str
    in_form: bool
    inline: bool
    occ: int = 0  # how many draws with the same matrix and size come before it (normally 0)

    @property
    def group(self) -> tuple:
        return _group(self.ctm, (self.width, self.height))

    @property
    def key(self) -> tuple:
        """Identifies the draw on any copy of the source page: its matrix, size and occurrence (not its position,
        which MuPDF's filter reports in a different order for Form XObjects when it only reads)."""
        return (self.occ,) + self.group

    @property
    def disp(self) -> pymupdf.Matrix:
        """Unit square -> page.rect space, as originally drawn."""
        (x0, y0), (x1, y1), _, (x3, y3) = self.quad
        # (0,0)->q0, (1,0)->q1, (0,1)->q3
        return pymupdf.Matrix(x1 - x0, y1 - y0, x3 - x0, y3 - y0, x0, y0)


@dataclass(frozen=True)
class ImageEdit:
    """One draw's edit. `rect` is the axis-aligned box the image is shown in (page.rect space), `turns` quarter
    turns clockwise on top of the original orientation (the replacement's upright one), `data` a replacement
    image file (PNG, JPEG, ...)."""
    delete: bool = False
    rect: tuple | None = None
    turns: int = 0
    data: bytes | None = None

    @property
    def is_noop(self) -> bool:
        return not self.delete and self.rect is None and self.turns % 4 == 0 and self.data is None


# ---------------------------------------------------------------------------------------------- geometry
def page_transform(page: pymupdf.Page) -> pymupdf.Matrix:
    """PDF space -> page.rect space (rotation and CropBox included), as MuPDF renders the page."""
    mb, ctm = m.FzRect(), m.FzMatrix()
    m.pdf_page_transform(pymupdf._as_pdf_page(page), mb, ctm)
    return pymupdf.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)


_UNIT = [pymupdf.Point(0, 0), pymupdf.Point(1, 0), pymupdf.Point(1, 1), pymupdf.Point(0, 1)]
_UPRIGHT = pymupdf.Matrix(1, 0, 0, -1, 0, 1)  # image space (row 0 at y=1) -> y-down unit square


def _quad(mat: pymupdf.Matrix) -> tuple:
    return tuple(((p * mat).x, (p * mat).y) for p in _UNIT)


def _bbox(quad) -> pymupdf.Rect:
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def placement(draw: ImageDraw, edit: ImageEdit | None) -> pymupdf.Matrix:
    """Unit square -> page.rect space for the draw as shown with `edit` (not for deletions)."""
    if edit is None:
        return draw.disp
    base = _UPRIGHT if edit.data is not None else draw.disp
    if edit.rect is None and edit.data is None and edit.turns % 4 == 0:
        return base
    b0 = _bbox(_quad(base))
    target = pymupdf.Rect(edit.rect if edit.rect is not None else draw.bbox)
    c0 = (b0.tl + b0.br) / 2
    rot = pymupdf.Matrix(1, 0, 0, 1, -c0.x, -c0.y) * pymupdf.Matrix(90 * (edit.turns % 4))
    b1 = b0 * rot
    if b1.width < 1e-6 or b1.height < 1e-6 or target.is_empty:
        raise Refuse("The image would have no size.")
    sc = pymupdf.Matrix(target.width / b1.width, 0, 0, target.height / b1.height, 0, 0)
    ct = (target.tl + target.br) / 2
    return base * rot * sc * pymupdf.Matrix(1, 0, 0, 1, ct.x, ct.y)


def shown_quad(draw: ImageDraw, edit: ImageEdit | None) -> tuple:
    return _quad(placement(draw, edit))


def shown_rect(draw: ImageDraw, edit: ImageEdit | None) -> pymupdf.Rect:
    return _bbox(shown_quad(draw, edit))


def _inside(quad, x: float, y: float) -> bool:
    """Point in a convex quad (either winding)."""
    sign = 0
    for i in range(4):
        (ax, ay), (bx, by) = quad[i], quad[(i + 1) % 4]
        c = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if abs(c) < 1e-9:
            continue
        s = 1 if c > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


# ---------------------------------------------------------------------------------------------- scanning
class _Filter(m.PdfSanitizeFilterOptions2):
    def __init__(self, decide):
        super().__init__()
        self.use_virtual_image_filter()
        self.decide = decide

    def image_filter(self, ctx, ctm, name, image, scissor):  # noqa: ARG002
        try:
            mat = (ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)
            r = self.decide(mat, name or "", image)
        except Exception:  # noqa: BLE001  (never let an exception cross the C boundary)
            return image
        return image if r is True else r


def _run(page: pymupdf.Page, decide, update: bool, forms: bool) -> None:
    f = _Filter(decide)
    fo = pymupdf._make_PdfFilterOptions(recurse=1 if forms else 0, instance_forms=1 if forms and update else 0,
                                        ascii=1, no_update=0 if update else 1, sanitize=1, sopts=f)
    m.pdf_filter_page_contents(pymupdf._as_pdf_document(page.parent), pymupdf._as_pdf_page(page), fo)


def _image_size(image) -> tuple[int, int]:
    w, h = image.w, image.h
    return (int(w() if callable(w) else w), int(h() if callable(h) else h))


def _sequence(page: pymupdf.Page, forms: bool) -> list[tuple]:
    out: list[tuple] = []

    def rec(mat, name, image):
        out.append((mat, name, _image_size(image)))
        return True

    _run(page, rec, update=False, forms=forms)
    return out


def _group(mat, size) -> tuple:
    return (tuple(round(v, 2) for v in mat), tuple(size))


def _close(a, b, eps: float = EPS) -> bool:
    return all(abs(x - y) <= eps * max(1.0, abs(y) * 1e-3) for x, y in zip(a, b))


def scan(page: pymupdf.Page) -> list[ImageDraw]:
    """Every image draw of `page` (read-only)."""
    full = _sequence(page, forms=True)
    top = _sequence(page, forms=False)
    # the page-level draws are a subsequence of all draws (a read-only pass may visit Form XObjects after the
    # page's own content, so positions are not paint order there; ImageDraw.key doesn't depend on them)
    j, flags = 0, []
    for mat, name, size in full:
        if j < len(top) and top[j][1] == name and top[j][2] == size and _close(top[j][0], mat, 1e-4):
            flags.append(False)
            j += 1
        else:
            flags.append(True)
    try:
        imgs = page.get_images(full=True)  # (xref, smask, w, h, bpc, cs, alt cs, name, filter, referencer)
    except Exception:  # noqa: BLE001
        imgs = []
    t = page_transform(page)
    draws = []
    seen: dict[tuple, int] = {}
    for i, ((mat, name, (w, h)), in_form) in enumerate(zip(full, flags)):
        g = _group(mat, (w, h))
        occ = seen.get(g, 0)
        seen[g] = occ + 1
        inline = not name or name == "<inline>"
        xref = 0
        if not inline:
            if in_form:
                cand = {e[0] for e in imgs if e[7] == name and e[9] != 0 and (e[2], e[3]) == (w, h)}
            else:
                cand = {e[0] for e in imgs if e[7] == name and e[9] == 0}
            xref = cand.pop() if len(cand) == 1 else 0
        q = _quad(pymupdf.Matrix(*mat) * t)
        b = _bbox(q)
        draws.append(ImageDraw(i, tuple(mat), q, (b.x0, b.y0, b.x1, b.y1), w, h, xref, name, in_form, inline, occ))
    return draws


_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # Source -> {index: [ImageDraw]}


def draws(src, index: int) -> list[ImageDraw]:
    """The image draws of a source page (cached: sources never change)."""
    per = _CACHE.get(src)
    if per is None:
        per = _CACHE[src] = OrderedDict()
    d = per.get(index)
    if d is None:
        d = per[index] = scan(src.page(index))
        if len(per) > 64:
            per.popitem(last=False)
    else:
        per.move_to_end(index)
    return d


def find(src, index: int, key: tuple) -> ImageDraw | None:
    for d in draws(src, index):
        if d.key == key:
            return d
    return None


def draw_at(doc, ref, x: float, y: float, edits: dict | None = None, skip_full: bool = False) -> ImageDraw | None:
    """The topmost image draw shown at (x, y) (page.rect space) with these edits (default: doc.image_edits).
    `skip_full` ignores page-covering images (backgrounds, scans)."""
    if edits is None:
        edits = (getattr(doc, "image_edits", None) or {}).get(ref.uid, {})
    pr = doc.page(ref).rect
    area = max(pr.width * pr.height, 1.0)
    for d in reversed(draws(ref.src, ref.index)):
        e = edits.get(d.key)
        if e is not None and e.delete:
            continue
        try:
            q = shown_quad(d, e)
        except Refuse:
            continue
        b = _bbox(q)
        if b.width < 1 or b.height < 1:
            continue
        if skip_full and (b & pr).get_area() >= FULL_PAGE * area:
            continue
        if _inside(q, x, y):
            return d
    return None


# ---------------------------------------------------------------------------------------------- checks
_LOSSLESS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # Source -> {index: bool}


def _lossless(src, index: int) -> bool:
    """Whether MuPDF's content filter reproduces this page exactly (a no-op pass over a one-page copy renders the
    same). Cached per source page."""
    per = _LOSSLESS.setdefault(src, {})
    v = per.get(index)
    if v is None:
        tmp = pymupdf.open()
        try:
            tmp.insert_pdf(src.doc, from_page=index, to_page=index, links=False, annots=False, widgets=False)
            _run(tmp[0], lambda *a: True, update=True, forms=True)
            a = src.page(index).get_pixmap(colorspace=pymupdf.csGRAY, annots=False, alpha=False)
            b = tmp[0].get_pixmap(colorspace=pymupdf.csGRAY, annots=False, alpha=False)
            v = a.samples == b.samples
        except Exception:  # noqa: BLE001
            v = False
        finally:
            tmp.close()
        per[index] = v
    return v


def check(src, index: int, draw: ImageDraw) -> None:
    """Raises Refuse (with the reason) when this draw can't be moved, replaced or deleted."""
    if draw.inline:
        raise Refuse("This is an inline image (stored inside the page's drawing commands); Leaf can't edit it. "
                     "You can still save a copy of it.")
    if not draw.xref:
        raise Refuse("Leaf can't tell which image object this is (several images share its name).")
    a, b, c, d, _, _ = draw.ctm
    if abs(a * d - b * c) < 1e-9:
        raise Refuse("This image has no size on the page.")
    if not _lossless(src, index):
        raise Refuse("MuPDF can't rewrite this page's drawing commands exactly, so its images can't be edited.")


# ---------------------------------------------------------------------------------------------- writing


def _graft(doc: pymupdf.Document, src: pymupdf.Document, xref: int) -> int:
    gm = m.pdf_new_graft_map(pymupdf._as_pdf_document(doc))
    obj = m.pdf_new_indirect(pymupdf._as_pdf_document(src), xref, 0)
    return m.pdf_to_num(m.pdf_graft_mapped_object(gm, obj))


def image_info(data: bytes) -> tuple[int, int]:
    """Pixel size of an image file (raises Refuse if MuPDF can't read it)."""
    try:
        img = pymupdf.open(stream=data)
        try:
            r = img[0].rect
            pix_doc = img
            w, h = r.width, r.height
            # an image document page is sized at 72 dpi from the resolution; the pixel size is what we want
            info = pix_doc[0].get_images(full=True)
            if info:
                w, h = info[0][2], info[0][3]
            return int(w), int(h)
        finally:
            img.close()
    except Exception as e:  # noqa: BLE001
        raise Refuse(f"That file isn't an image Leaf can read ({e}).") from e


def _replacement_xref(doc: pymupdf.Document, data: bytes) -> int:
    per = getattr(doc, "_leaf_replacements", None)  # sha1 -> xref, per document (display copies reuse them)
    if per is None:
        per = {}
        try:
            doc._leaf_replacements = per
        except AttributeError:
            pass
    h = hashlib.sha1(data).hexdigest()
    x = per.get(h)
    if x is None:
        tmp = pymupdf.open()
        try:
            p = tmp.new_page(width=100, height=100)
            fx = p.insert_image(p.rect, stream=data, keep_proportion=False)
            x = per[h] = _graft(doc, tmp, fx)
        except Exception as e:  # noqa: BLE001
            raise Refuse(f"The replacement image couldn't be read ({e}).") from e
        finally:
            tmp.close()
    return x


def _placeholder(width: int):
    """A 1-pixel-high image with random pixels: MuPDF stores identical images once per document, and every
    placeholder has to be a new object of its own."""
    data = os.urandom(width)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, width, 1, data, False)
    return m.fz_new_image_from_pixmap(pix.this, m.FzImage())


_REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")


def _resources(doc: pymupdf.Document, holder: int) -> tuple[int, str]:
    """(object, key prefix) where `holder`'s /Resources dictionary can be changed."""
    t, v = doc.xref_get_key(holder, "Resources")
    if t == "xref":
        return int(v.split()[0]), ""
    if t == "null":
        doc.xref_set_key(holder, "Resources", "<<>>")
    return holder, "Resources/"


def _xobjects(doc: pymupdf.Document, holder: int) -> dict[str, int]:
    obj, pre = _resources(doc, holder)
    t, v = doc.xref_get_key(obj, pre + "XObject")
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]), compressed=True)
    elif t != "dict":
        return {}
    return {n: int(x) for n, x in _REF.findall(v)}


def _set_xobject(doc: pymupdf.Document, holder: int, name: str, value: str) -> None:
    obj, pre = _resources(doc, holder)
    t, v = doc.xref_get_key(obj, pre + "XObject")
    if t == "xref":  # an XObject dictionary of its own: written by the filter for this page, but keep it private
        doc.xref_set_key(obj, pre + "XObject", doc.xref_object(int(v.split()[0]), compressed=True))
    doc.xref_set_key(obj, pre + f"XObject/{name}", value)


def _holders(doc: pymupdf.Document, page: pymupdf.Page) -> list[tuple[int, list[int]]]:
    """(resource holder, its content streams): the page, then every Form XObject reachable from it."""
    out = [(page.xref, list(page.get_contents()))]
    seen, todo = set(), [page.xref]
    while todo:
        h = todo.pop()
        for x in _xobjects(doc, h).values():
            if x in seen:
                continue
            seen.add(x)
            if doc.xref_get_key(x, "Subtype") == ("name", "/Form"):
                out.append((x, [x]))
                todo.append(x)
    return out


def _fmt(mat: pymupdf.Matrix) -> str:
    return " ".join(f"{v:.6f}".rstrip("0").rstrip(".") or "0" for v in (mat.a, mat.b, mat.c, mat.d, mat.e, mat.f))


def _matching(draws: list, mat, size) -> list:
    return [d for d in draws if (d.width, d.height) == tuple(size) and _close(d.ctm, mat)]


def _pass(page: pymupdf.Page, plan: list, forms: bool) -> None:
    """One filter pass. plan: [(matrix, size, occurrence among the matching draws this pass visits,
    (ImageEdit, new matrix in PDF space, image xref to draw))]."""
    doc = page.parent
    base = doc.xref_length()
    keep = []  # placeholders (Python refs alive until the pass is done)
    widths: dict[int, tuple] = {}  # placeholder width -> (target xref, cm matrix)
    counts = [0] * len(plan)
    hit = set()

    def decide(mat, name, image):
        size = _image_size(image)
        p = None
        for k, (pm, ps, occ, payload) in enumerate(plan):
            if ps == size and _close(pm, mat):
                if counts[k] == occ:
                    p = payload
                    hit.add(k)
                counts[k] += 1
        if p is None:
            return True
        edit, new, target = p
        if edit.delete:
            return None
        w = len(keep) + 2  # a distinct width per placeholder (identical images could be merged)
        img = _placeholder(w)
        keep.append(img)
        cm = new * ~pymupdf.Matrix(*mat)  # inserted `cm` x current matrix = new matrix
        widths[w] = (target, cm)
        return m.ll_fz_keep_image(img.m_internal)

    _run(page, decide, update=True, forms=forms)
    if len(hit) < len(plan):
        raise Refuse("An edited image is no longer on the page.")
    if not widths:
        return
    ph: dict[int, tuple] = {}
    for x in range(base, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Subtype") != ("name", "/Image") or doc.xref_get_key(x, "Height")[1] != "1":
                continue
            w = int(doc.xref_get_key(x, "Width")[1])
        except Exception:  # noqa: BLE001
            continue
        if w in widths:
            ph[x] = widths[w]
    done = 0
    for holder, streams in _holders(doc, page):
        for name, x in list(_xobjects(doc, holder).items()):
            if x not in ph:
                continue
            target, cm = ph[x]
            mine = f"LeafImg{target}"
            pat = re.compile(rb"/" + re.escape(name.encode()) + rb"(?=\s)\s*Do\b")
            rep = f"q {_fmt(cm)} cm /{mine} Do Q".encode()
            for s in streams:
                data = doc.xref_stream(s) or b""
                new, n = pat.subn(rep, data)
                if n:
                    doc.update_stream(s, new)
                    done += n
            _set_xobject(doc, holder, mine, f"{target} 0 R")
            _set_xobject(doc, holder, name, "null")
    if done < len(widths):
        raise Refuse("An image could not be placed again.")


def apply_image_edits(page: pymupdf.Page, edits: dict) -> None:
    """Replay image edits ({draw key: ImageEdit}) on `page`, a fresh copy of the source page they were made on
    (text edits may already be applied). Raises Refuse if one can't be applied."""
    edits = {k: e for k, e in edits.items() if e is not None and not e.is_noop}
    if not edits:
        return
    doc = page.parent
    found = scan(page)
    top = [d for d in found if not d.in_form]
    t = page_transform(page)
    ti = ~t
    form_plan, top_plan = [], []
    for key, e in edits.items():
        occ, mat, size = key
        cand = _matching(found, mat, size)  # (matched with a tolerance: a rewritten stream may round numbers)
        if occ >= len(cand):
            raise Refuse("An edited image is no longer on the page.")
        d = cand[occ]
        if d.inline or not d.xref:
            raise Refuse("An edited image can't be identified on the page.")
        if e.delete:
            new = None
            target = d.xref
        else:
            new = placement(d, e) * ti  # unit square -> PDF space
            target = _replacement_xref(doc, e.data) if e.data is not None else d.xref
        if d.in_form:
            form_plan.append((d.ctm, (d.width, d.height), occ, (e, new, target)))
        else:  # the page-level pass only visits page-level draws
            top_plan.append((d.ctm, (d.width, d.height), _matching(top, d.ctm, size).index(d), (e, new, target)))
    if form_plan:  # private copies of the page's Form XObjects (the page's own stream is rewritten too)
        _pass(page, form_plan, True)
    if top_plan:
        _pass(page, top_plan, False)


# ---------------------------------------------------------------------------------------------- reading
def extract(src, draw: ImageDraw, edit: ImageEdit | None = None) -> tuple[bytes, str]:
    """(file bytes, extension) of the image as stored: the original stream when it is a whole image file (JPEG,
    JPEG 2000, ...), else a lossless PNG; a replacement's own file. Soft masks are not part of the bytes."""
    if edit is not None and edit.data is not None:
        return edit.data, _ext_of(edit.data)
    if draw.inline or not draw.xref:
        raise Refuse("Leaf can't read this inline image's data.")
    info = src.doc.extract_image(draw.xref)
    if not info:
        raise Refuse("The image data couldn't be read.")
    return info["image"], info["ext"]


def _ext_of(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] in (b"II", b"MM"):
        return "tif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "png"


def pixmap(src, draw: ImageDraw, edit: ImageEdit | None = None) -> pymupdf.Pixmap:
    """The image's pixels (RGB or gray, with alpha when it has a soft mask), unrotated."""
    if edit is not None and edit.data is not None:
        pix = pymupdf.Pixmap(edit.data)
    elif draw.xref and not draw.inline:
        pix = pymupdf.Pixmap(src.doc, draw.xref)
        try:
            smask = src.doc.xref_get_key(draw.xref, "SMask")
            if smask[0] == "xref":
                base = pymupdf.Pixmap(pix, 0) if pix.alpha else pix
                if base.colorspace is not None and base.colorspace.n > 3:
                    base = pymupdf.Pixmap(pymupdf.csRGB, base)
                mask = pymupdf.Pixmap(src.doc, int(smask[1].split()[0]))
                if (mask.width, mask.height) == (base.width, base.height):
                    pix = pymupdf.Pixmap(base, mask)
        except Exception:  # noqa: BLE001  (show it without transparency)
            pass
    else:
        raise Refuse("Leaf can't read this inline image's pixels.")
    if pix.colorspace is not None and pix.colorspace.n > 3:  # CMYK, ...
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix


def save_bytes(src, draw: ImageDraw, edit: ImageEdit | None, fmt: str) -> bytes:
    """The image in the asked format: "original" (as stored), "png" (lossless, keeps transparency) or "jpg"."""
    if fmt == "original":
        return extract(src, draw, edit)[0]
    pix = pixmap(src, draw, edit)
    if fmt in ("jpg", "jpeg"):
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        return pix.tobytes("jpeg", jpg_quality=92)
    return pix.tobytes("png")
