"""File › Export: pages as images, every image of the document, text and Markdown-ish text.

Everything is exported from what Save would write: `snapshot()` runs the save pipeline into a temporary file
first, so text edits, image edits, overlays, form values, redactions, crops and decorations are included. The
exporters are generators that yield after each page or image (done, total, last file), so the UI can run them in
small steps with a progress dialog and a Cancel button (features/images.py). Qt-free except for the optional
WebP/TIFF encoders, which use Qt's image plugins when MuPDF has no encoder for the format.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

import pymupdf

PAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WebP", "tif": "TIFF"}  # extension -> label
MAX_PIXELS = 400_000_000  # per exported page image


def qt_formats() -> set[str]:
    """Extensions Qt can write (WebP/TIFF come from Qt's image format plugins, when installed)."""
    try:
        from PySide6.QtGui import QImageWriter

        have = {bytes(f).decode().lower() for f in QImageWriter.supportedImageFormats()}
    except Exception:  # noqa: BLE001
        return set()
    return {e for e, q in (("webp", "webp"), ("tif", "tiff")) if q in have}


def page_formats() -> dict[str, str]:
    """Formats the page export can write here: PNG and JPEG always (MuPDF), WebP/TIFF when Qt can."""
    qt = qt_formats()
    return {e: lbl for e, lbl in PAGE_FORMATS.items() if e in ("png", "jpg") or e in qt}


# ---------------------------------------------------------------------------------------------- snapshot
@dataclass
class Snapshot:
    """What Save would write, opened from a temporary file (deleted by close())."""
    path: str
    doc: pymupdf.Document
    _dir: str

    def close(self) -> None:
        try:
            self.doc.close()
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def snapshot(doc, overlays_for, pages=None) -> Snapshot:
    """Run the save pipeline (all edits) into a temporary file and open it."""
    d = tempfile.mkdtemp(prefix="leaf-export-")
    path = os.path.join(d, "export.pdf")
    try:
        doc.save(path, overlays_for, pages, fast=True)
        out = pymupdf.open(path)
        if out.needs_pass:
            out.authenticate(doc.output_password() or "")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return Snapshot(path, out, d)


def _names(folder: str, stem: str, n: int, ext: str) -> list[str]:
    w = max(3, len(str(n)))
    return [os.path.join(folder, f"{stem}-p{i + 1:0{w}d}.{ext}") for i in range(n)]


# ---------------------------------------------------------------------------------------------- pages
def export_pages(pdf: pymupdf.Document, rows: list[int], folder: str, stem: str, fmt: str = "png",
                 dpi: int = 150, quality: int = 90) -> Iterator[tuple[int, int, str]]:
    """One image file per page in `rows` (0-based). Yields (done, total, path) after each page."""
    fmt = fmt.lower().lstrip(".")
    fmt = "jpg" if fmt == "jpeg" else "tif" if fmt == "tiff" else fmt
    if fmt not in PAGE_FORMATS:
        raise ValueError(f"Unknown image format: {fmt}")
    os.makedirs(folder, exist_ok=True)
    names = _names(folder, stem, pdf.page_count, fmt)
    for k, i in enumerate(rows):
        page = pdf[i]
        r = page.rect
        if r.width * r.height * (dpi / 72) ** 2 > MAX_PIXELS:
            raise ValueError(f"Page {i + 1} would be too large at {dpi} dpi; choose a lower resolution.")
        pix = page.get_pixmap(dpi=dpi, alpha=False, annots=True)
        path = names[i]
        if fmt == "png":
            pix.save(path)
        elif fmt == "jpg":
            with open(path, "wb") as f:
                f.write(pix.tobytes("jpeg", jpg_quality=quality))
        else:
            _save_qt(pix, path, fmt, quality, dpi)
        yield k + 1, len(rows), path


def _save_qt(pix: pymupdf.Pixmap, path: str, fmt: str, quality: int, dpi: int) -> None:
    from PySide6.QtGui import QImage, QImageWriter

    img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                 QImage.Format.Format_RGB888 if pix.n == 3 else QImage.Format.Format_Grayscale8)
    dpm = int(round(dpi / 0.0254))
    img.setDotsPerMeterX(dpm)
    img.setDotsPerMeterY(dpm)
    w = QImageWriter(path, b"webp" if fmt == "webp" else b"tiff")
    w.setQuality(quality)
    if not w.write(img):
        raise OSError(f"Couldn't write {os.path.basename(path)}: {w.errorString()}")


# ---------------------------------------------------------------------------------------------- images
def _image_bytes(pdf: pymupdf.Document, xref: int, smask: int) -> tuple[bytes, str]:
    """The image file as stored (original format); one with a soft mask becomes a PNG with transparency."""
    info = pdf.extract_image(xref)
    if not info:
        raise ValueError("unreadable image")
    if smask:
        try:
            base = pymupdf.Pixmap(pdf, xref)
            if base.alpha:
                base = pymupdf.Pixmap(base, 0)
            if base.colorspace is not None and base.colorspace.n > 3:
                base = pymupdf.Pixmap(pymupdf.csRGB, base)
            mask = pymupdf.Pixmap(pdf, smask)
            if (mask.width, mask.height) == (base.width, base.height):
                return pymupdf.Pixmap(base, mask).tobytes("png"), "png"
        except Exception:  # noqa: BLE001  (fall back to the image without its mask)
            pass
    ext = info.get("ext") or "png"
    return info["image"], ext


def list_images(pdf: pymupdf.Document, rows: list[int] | None = None) -> list[tuple[int, int, int]]:
    """(page number, xref, smask) of every image object used on these pages, each xref once (first use)."""
    seen, out = set(), []
    for i in rows if rows is not None else range(pdf.page_count):
        for e in pdf[i].get_images(full=True):
            xref, smask = e[0], e[1]
            if xref in seen or xref <= 0:
                continue
            seen.add(xref)
            out.append((i, xref, smask))
    return out


def export_images(pdf: pymupdf.Document, folder: str, stem: str, rows: list[int] | None = None,
                  min_size: int = 0) -> Iterator[tuple[int, int, str | None]]:
    """Every image of the document (or of `rows`), once per image object and once per identical file content,
    in its original format. Yields (done, total, path or None when skipped)."""
    os.makedirs(folder, exist_ok=True)
    todo = list_images(pdf, rows)
    hashes: set[str] = set()
    w = max(3, len(str(pdf.page_count)))
    n = 0
    for k, (pno, xref, smask) in enumerate(todo):
        path = None
        try:
            ww = int(pdf.xref_get_key(xref, "Width")[1] or 0)
            hh = int(pdf.xref_get_key(xref, "Height")[1] or 0)
        except ValueError:
            ww = hh = 0
        if min(ww, hh) >= min_size:
            try:
                data, ext = _image_bytes(pdf, xref, smask)
            except Exception:  # noqa: BLE001  (unreadable image: skip it)
                data, ext = b"", ""
            h = hashlib.sha1(data).hexdigest()
            if data and h not in hashes:
                hashes.add(h)
                n += 1
                path = os.path.join(folder, f"{stem}-p{pno + 1:0{w}d}-img{n:03d}.{ext}")
                with open(path, "wb") as f:
                    f.write(data)
        yield k + 1, len(todo), path


# ---------------------------------------------------------------------------------------------- text
def page_text(page: pymupdf.Page) -> str:
    """Plain text of a page in reading order (top to bottom, left to right; ligatures expanded)."""
    flags = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    return page.get_text("text", sort=True, flags=flags).rstrip()


def _blocks(page: pymupdf.Page) -> list[list[tuple[str, float, bool]]]:
    """Text blocks in reading order, as lines of (text, size, bold)."""
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES & ~pymupdf.TEXT_PRESERVE_IMAGES
    out = []
    for b in page.get_text("dict", sort=True, flags=flags).get("blocks", []):
        lines = []
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in ln["spans"]).strip()
            n = sum(len(s["text"]) for s in spans)
            size = sum(s["size"] * len(s["text"]) for s in spans) / max(n, 1)
            bold = all(s.get("flags", 0) & 16 or "bold" in s.get("font", "").lower() for s in spans)
            lines.append((text, size, bold))
        if lines:
            out.append(lines)
    return out


def markdown_pages(pages: list[pymupdf.Page]) -> list[str]:
    """Markdown-ish text: headings from font size (the body size is the most used one), paragraphs from text
    blocks, bullet characters as list items."""
    all_blocks = [_blocks(p) for p in pages]
    sizes: Counter = Counter()
    for blocks in all_blocks:
        for b in blocks:
            for text, size, _ in b:
                sizes[round(size, 1)] += len(text)
    body = sizes.most_common(1)[0][0] if sizes else 11.0
    big = sorted({s for s in sizes if s >= body * 1.15}, reverse=True)
    level = {s: min(i + 1, 4) for i, s in enumerate(big)}
    out = []
    for blocks in all_blocks:
        parts = []
        for b in blocks:
            text = " ".join(t for t, _, _ in b)
            size = round(max(s for _, s, _ in b), 1)
            if size in level and len(text) <= 200:
                parts.append("#" * level[size] + " " + text)
                continue
            if all(bold for _, _, bold in b) and len(text) <= 80 and len(b) == 1:
                parts.append(f"**{text}**")
                continue
            items, para = [], []
            for t, _, _ in b:
                if len(t) > 1 and (t[0] in "•◦▪‣" or t[0] in "-*–" and t[1] in " \t"):
                    if para:
                        items.append(" ".join(para))
                        para = []
                    items.append("- " + t[1:].strip())
                elif items and not para and items[-1].startswith("- "):
                    items[-1] += " " + t
                else:
                    para.append(t)
            if para:
                items.append(" ".join(para))
            parts.append("\n".join(items))
        out.append("\n\n".join(parts))
    return out


def export_text(pdf: pymupdf.Document, rows: list[int], path: str, markdown: bool = False
                ) -> Iterator[tuple[int, int, str | None]]:
    """Text of the pages in `rows` into one UTF-8 file (written at the end). Yields (done, total, path or None)."""
    texts: list[str] = []
    for k, i in enumerate(rows):
        if not markdown:
            texts.append(page_text(pdf[i]))
        yield k + 1, len(rows) + (1 if markdown else 0), None
    if markdown:
        texts = markdown_pages([pdf[i] for i in rows])
        sep = "\n\n---\n\n"
    else:
        sep = "\n\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(sep.join(texts).rstrip() + "\n")
    yield len(rows) + (1 if markdown else 0), len(rows) + (1 if markdown else 0), path
