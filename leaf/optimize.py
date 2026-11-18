"""Reduce file size: presets, size breakdown, and the optimizer itself.

Qt-free on purpose: `run_worker` executes in a spawned child process (see `OptimizeJob` in
leaf/features/optimize.py), which imports only this module and PyMuPDF. The input is a file the save pipeline
wrote (exactly what Save would write); the output is a separate file. Encryption is kept as it is.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, replace

import pymupdf


@dataclass
class Options:
    images: bool = False  # recompress/downsample images
    dpi: int = 150  # target resolution for downsampled images
    threshold: int = 0  # only images above this resolution are downsampled (0: 1.5 x dpi)
    quality: int = 80  # JPEG quality
    lossless_images: bool = True  # also re-encode lossless (PNG-like) images as JPEG
    gray: bool = False  # convert the whole document to grayscale
    subset_fonts: bool = True
    strip_metadata: bool = False  # Info dictionary and XMP
    strip_thumbnails: bool = True  # embedded page thumbnails (/Thumb)
    strip_embedded: bool = False  # attached files
    clean: bool = True  # compact content streams, drop unused resources (fonts, images) per page

    def dpi_threshold(self) -> int:
        return self.threshold if self.threshold > self.dpi else int(self.dpi * 1.5)


PRESETS: dict[str, tuple[str, str, Options]] = {
    "lossless": ("Lossless", "Recompress streams, remove unused and duplicate objects, subset fonts. Nothing "
                 "visible changes.", Options()),
    "print": ("Print", "Images above 450 dpi down to 300 dpi, JPEG quality 90.",
              Options(images=True, dpi=300, quality=90)),
    "ebook": ("eBook", "Images down to 150 dpi, JPEG quality 80. Good on screens, fine for office printing.",
              Options(images=True, dpi=150, quality=80)),
    "screen": ("Screen", "Images down to 96 dpi, JPEG quality 65. Smallest; for viewing on screen only.",
               Options(images=True, dpi=96, quality=65)),
}


# ------------------------------------------------------------------ breakdown
def breakdown(doc: pymupdf.Document, total: int | None = None) -> dict[str, int]:
    """Approximate bytes per category ("images", "fonts", "content", "other") by scanning the xref table.

    Uses the stored (compressed) stream lengths and never decodes a stream. When `total` (the file size) is
    given, the rest (xref table, object streams overhead) is counted as "other"."""
    out = {"images": 0, "fonts": 0, "content": 0, "other": 0}
    n = doc.xref_length()
    fontfiles: set[int] = set()
    contents: set[int] = set()
    for pno in range(doc.page_count):
        try:
            contents.update(doc[pno].get_contents())
        except Exception:  # noqa: BLE001
            pass
    kind: dict[int, str] = {}
    for x in range(1, n):
        try:
            t = doc.xref_get_key(x, "Type")[1]
            st = doc.xref_get_key(x, "Subtype")[1]
        except Exception:  # noqa: BLE001
            continue
        if st == "/Image":
            kind[x] = "images"
        elif st == "/Form" or x in contents:
            kind[x] = "content"
        elif t == "/FontDescriptor":
            for key in ("FontFile", "FontFile2", "FontFile3"):
                tt, v = doc.xref_get_key(x, key)
                if tt == "xref":
                    fontfiles.add(int(v.split()[0]))
            kind[x] = "fonts"
        elif t == "/Font" or st in ("/Type1", "/TrueType", "/Type0", "/CIDFontType0", "/CIDFontType2", "/Type3"):
            kind[x] = "fonts"
    for x in fontfiles:
        kind[x] = "fonts"
    for x in range(1, n):
        try:
            size = 0
            if doc.xref_is_stream(x):
                lt, lv = doc.xref_get_key(x, "Length")
                if lt == "int":
                    size = int(lv)
                else:
                    size = len(doc.xref_stream_raw(x) or b"")
            size += len(doc.xref_object(x, compressed=True))
        except Exception:  # noqa: BLE001 - a broken object is just not counted
            continue
        out[kind.get(x, "other")] += size
    if total is not None:
        counted = sum(out.values())
        if counted > total:  # object streams compress dictionaries: scale down to the real size
            f = total / counted
            for k in out:
                out[k] = int(out[k] * f)
        else:
            out["other"] += total - counted
    return out


def human(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def has_signatures(doc: pymupdf.Document) -> bool:
    """True if the document carries digital signatures (they won't survive a rewrite)."""
    try:
        from .signing import has_signatures as shared  # the signing feature's helper, when it exists
    except ImportError:
        shared = None
    if shared is not None:
        try:
            return bool(shared(doc))
        except Exception:  # noqa: BLE001 - fall back to the local check
            pass
    try:
        flags = doc.get_sigflags()
        if flags > 0 and flags & 1:
            return True
        for page in doc:
            for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE]):
                if doc.xref_get_key(w.xref, "V")[0] == "xref":
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


# ------------------------------------------------------------------ optimizer
class Cancelled(Exception):
    pass


def optimize_file(src: str, dst: str, opts: Options, password: str | None = None, progress=None) -> int:
    """Write an optimized copy of `src` to `dst` (overwritten). Keeps the encryption of `src`.

    Returns the size of `dst`. Never makes the file bigger: if the result is larger than `src`, `src` is copied.
    `progress(fraction, text)` may raise to cancel."""
    def step(f: float, text: str) -> None:
        if progress is not None:
            progress(f, text)

    step(0.02, "Opening…")
    doc = pymupdf.open(src)
    try:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise ValueError("Wrong password for the document.")
        encrypted = bool(doc.metadata.get("encryption"))
        is_form = bool(doc.is_form_pdf)
        if opts.images or opts.gray:
            step(0.08, "Recompressing images…" if opts.images else "Converting to grayscale…")
            if opts.images:
                doc.rewrite_images(dpi_threshold=opts.dpi_threshold(), dpi_target=opts.dpi, quality=opts.quality,
                                   lossless=opts.lossless_images, bitonal=False, set_to_gray=opts.gray)
            else:  # grayscale only: convert, but leave image compression alone
                doc.rewrite_images(lossy=False, lossless=False, bitonal=False, color=False, gray=False,
                                   set_to_gray=True)
        if opts.subset_fonts and not is_form:  # form fields need full fonts to type new text
            step(0.55, "Subsetting fonts…")
            try:
                doc.subset_fonts()
            except Exception:  # noqa: BLE001 - subsetting is a bonus, never a reason to fail
                pass
        step(0.65, "Removing extras…")
        if opts.strip_thumbnails:
            for page in doc:
                if doc.xref_get_key(page.xref, "Thumb")[0] != "null":
                    doc.xref_set_key(page.xref, "Thumb", "null")
        if opts.strip_embedded:
            for name in list(doc.embfile_names()):
                doc.embfile_del(name)
        if opts.strip_metadata:
            doc.set_metadata({})
            doc.del_xml_metadata()
            cat = doc.pdf_catalog()
            if doc.xref_get_key(cat, "PieceInfo")[0] != "null":
                doc.xref_set_key(cat, "PieceInfo", "null")
        step(0.72, "Writing…")
        tmp = dst + ".part"
        try:
            doc.save(tmp, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1,
                     clean=opts.clean, encryption=pymupdf.PDF_ENCRYPT_KEEP if encrypted else pymupdf.PDF_ENCRYPT_NONE)
            step(0.94, "Checking…")
            check = pymupdf.open(tmp)
            try:
                if check.needs_pass and not check.authenticate(password or ""):
                    raise RuntimeError("Verification failed: the optimized file doesn't open.")
                if check.page_count != doc.page_count:
                    raise RuntimeError("Verification failed: page count mismatch.")
            finally:
                check.close()
            if os.path.getsize(tmp) >= os.path.getsize(src):
                shutil.copyfile(src, tmp)  # already as small as it gets: never grow the file
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    finally:
        doc.close()
    step(1.0, "Done")
    return os.path.getsize(dst)


def run_worker(conn, src: str, dst: str, opts: dict, password: str | None) -> None:
    """Child-process entry point: ("progress", f, text)... then ("done", size) or ("err", message)."""
    def progress(f: float, text: str) -> None:
        conn.send(("progress", f, text))

    try:
        size = optimize_file(src, dst, Options(**opts), password, progress)
        conn.send(("done", size))
    except BaseException as e:  # noqa: BLE001 - report everything to the GUI
        try:
            conn.send(("err", str(e) or type(e).__name__))
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def options_dict(opts: Options) -> dict:
    return asdict(opts)


def preset(name: str) -> Options:
    return replace(PRESETS[name][2])
