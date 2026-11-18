"""Page cropping.

Model: `PageRef.crop` is the visible area as (x0, y0, x1, y1) in the source page.rect space (the page as its own
/Rotate shows it, origin at its top left), or None. `PdfDocument.view_rect/extra_matrix/display_size/render`
honor it, so the page item, its content transform, thumbnails and hit-testing follow; overlays keep their
page.rect coordinates and therefore stay on the same content when a page is cropped or uncropped.

At save `apply_crop` writes the CropBox, as the very last per-page step (it moves the page's coordinate origin).
Crops are edited through `SetCrop`, one undo step per change.
"""
from __future__ import annotations

import pymupdf
from PySide6.QtGui import QUndoCommand

MIN_SIDE = 6.0  # points; smaller crops are refused


# ------------------------------------------------------------------ model
def as_crop(r) -> tuple | None:
    if r is None:
        return None
    r = pymupdf.Rect(r)
    r.normalize()
    return (round(r.x0, 3), round(r.y0, 3), round(r.x1, 3), round(r.y1, 3))


def clamp(page_rect: pymupdf.Rect, r) -> tuple | None:
    """`r` limited to the page; None when nothing sensible is left (or it is the whole page)."""
    r = pymupdf.Rect(r)
    r.normalize()
    r &= page_rect
    if r.is_empty or r.width < MIN_SIDE or r.height < MIN_SIDE:
        return None
    if all(abs(a - b) < 0.01 for a, b in zip(r, page_rect)):
        return None  # the whole page: no crop
    return as_crop(r)


def full_matrix(doc, ref) -> pymupdf.Matrix:
    """page.rect -> display space of the *uncropped* page (Leaf's extra rotation, origin at its top left)."""
    m = pymupdf.Matrix(ref.rotation)
    r = doc.page_rect(ref) * m
    return m * pymupdf.Matrix(1, 0, 0, 1, -r.x0, -r.y0)


def full_size(doc, ref) -> tuple[float, float]:
    r = doc.page_rect(ref)
    return (r.height, r.width) if ref.rotation % 180 else (r.width, r.height)


def margins_of(doc, ref, crop=None) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) of a crop (default: the page's own), in points as the page is displayed."""
    crop = ref.crop if crop is None else crop
    if crop is None:
        return (0.0, 0.0, 0.0, 0.0)
    W, H = full_size(doc, ref)
    d = pymupdf.Rect(crop) * full_matrix(doc, ref)
    d.normalize()
    return (max(0.0, d.x0), max(0.0, d.y0), max(0.0, W - d.x1), max(0.0, H - d.y1))


def crop_from_margins(doc, ref, margins) -> tuple | None:
    """The crop leaving `margins` (left, top, right, bottom; display orientation, points) off the page."""
    l, t, r, b = margins
    W, H = full_size(doc, ref)
    d = pymupdf.Rect(l, t, W - r, H - b)
    if d.is_empty:
        return None
    return clamp(doc.page_rect(ref), d * ~full_matrix(doc, ref))


def content_bbox(doc, ref, pad: float = 0.0, scale: float = 1.0, threshold: int = 248) -> pymupdf.Rect | None:
    """Bounding box of everything visible on the page (non-white pixels of a low-res render), in page.rect space,
    grown by `pad` points. None for a blank page."""
    page = ref.src.render_page(ref.index)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False, annots=True)
    w, h, stride = pix.width, pix.height, pix.stride
    mask = pix.samples.translate(bytes(1 if i < threshold else 0 for i in range(256)))
    x0, y0, x1, y1 = w, -1, -1, -1
    for y in range(h):
        row = mask[y * stride:y * stride + w]
        a = row.find(1)
        if a < 0:
            continue
        if y0 < 0:
            y0 = y
        y1 = y
        x0 = min(x0, a)
        x1 = max(x1, row.rfind(1))
    if y0 < 0:
        return None
    pr = page.rect
    r = pymupdf.Rect(x0 / scale, y0 / scale, (x1 + 1) / scale, (y1 + 1) / scale) + (pr.x0, pr.y0, pr.x0, pr.y0)
    return (r + (-pad, -pad, pad, pad)) & pr


def auto_crop(doc, ref, pad: float = 6.0) -> tuple | None:
    """A crop that trims the white margins of the page (None if there's nothing to trim)."""
    r = content_bbox(doc, ref, pad)
    return clamp(doc.page_rect(ref), r) if r is not None else None


def apply_crop(page: pymupdf.Page, crop) -> None:
    """Set the page's CropBox to `crop` (page.rect space). Called last per page at save.

    page.rect space -> unrotated MuPDF space (derotation_matrix, origin at the current CropBox's top left, y down)
    -> PDF user space (y up). Written directly: set_cropbox() mixes coordinate systems on pages whose MediaBox
    doesn't start at 0,0."""
    u = pymupdf.Rect(crop) * page.derotation_matrix
    u.normalize()
    cb, mb = page.cropbox, page.mediabox  # PyMuPDF: x as in the file, y measured down from the MediaBox top
    top = mb.y1 - cb.y0  # PDF y of the current visible top edge
    x0, x1 = cb.x0 + u.x0, cb.x0 + u.x1
    y0, y1 = top - u.y1, top - u.y0
    # never outside the MediaBox
    x0, x1 = max(x0, mb.x0), min(x1, mb.x1)
    y0, y1 = max(y0, mb.y0), min(y1, mb.y1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return
    doc = page.parent
    doc.xref_set_key(page.xref, "CropBox", f"[{x0:.4f} {y0:.4f} {x1:.4f} {y1:.4f}]")


# --------------------------------------------------------------- commands
class SetCrop(QUndoCommand):
    def __init__(self, canvas, changes: dict, text: str):
        """changes: PageRef -> (old crop, new crop)."""
        super().__init__(text)
        self.canvas, self.changes = canvas, changes

    def _apply(self, idx: int) -> None:
        for ref, pair in self.changes.items():
            ref.crop = pair[idx]
        self.canvas.sync_pages()

    def redo(self) -> None:
        self._apply(1)

    def undo(self) -> None:
        self._apply(0)


def set_crops(canvas, crops: dict, text: str | None = None) -> int:
    """crops: PageRef -> new crop (None removes it). One undo step; returns how many pages changed."""
    changes = {ref: (ref.crop, c) for ref, c in crops.items() if ref.crop != c}
    if not changes:
        return 0
    n = len(changes)
    if text is None:
        removing = all(c is None for _, c in changes.values())
        text = (f"Remove crop from {n} pages" if n > 1 else "Remove crop") if removing else \
            (f"Crop {n} pages" if n > 1 else "Crop page")
    canvas.undo.push(SetCrop(canvas, changes, text))
    return n
