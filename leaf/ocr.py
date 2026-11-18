"""OCR: engines, scanned-page detection and the invisible text layer written at save.

Qt-free (the OCR worker process imports it). Engines are imported lazily, only when a page is actually
recognized, so Leaf starts (and works) without the optional `ocr` extra:

- RapidOCR (`rapidocr` + `onnxruntime`, the `ocr` extra): default when importable. Its bundled PP-OCRv6
  model is multilingual (Latin-script languages, Chinese, Japanese) and needs no download.
- Tesseract, through MuPDF's built-in Tesseract (`Page.get_textpage_ocr`): always available, since Leaf ships
  English language data (leaf/data/tessdata, see leaf.ocrdata). More languages (Hindi, Arabic, Cyrillic, ...)
  come from the user's downloads (AppLocalDataLocation/tessdata), a Tesseract install or TESSDATA_PREFIX.

`choose_engine` picks the engine for a language: RapidOCR when installed and the language is one it reads,
otherwise Tesseract with a folder that has every language file of the combination (e.g. "eng+hin").

Recognized words are kept as tuples in the source page's `page.rect` (display) space, like `Source.words`:
    (x0, y0, x1, y1, text, line, word, confidence)      confidence 0..1, or -1 when the engine has none

At save each word becomes invisible text (render mode 3) in a Form XObject, scaled to fill the word's box.
The font is a non-embedded "glyphless" Type0/Identity-H font: every character code is its Unicode value
(BMP), every glyph is 0.5 em wide, and a ToUnicode CMap makes copy/search return the text. One font
covers every script, nothing has to be embedded (PDF/A allows non-embedded fonts for render mode 3), and
it adds only a few hundred bytes per page. The same approach Tesseract's own PDF renderer uses.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import shutil
import sys
import time
from dataclasses import dataclass, field

import pymupdf

from . import ocrdata

DEFAULT_DPI = 300
OCR_BLOCK = 100_000  # block number of OCR words in Source.words (kept apart from the page's own blocks)
NOT_AVAILABLE = (
    "Text recognition (OCR) isn't available: Leaf's English language data is missing.\n\n"
    "Reinstall Leaf, install the OCR add-on (Leaf's optional \"ocr\" extra: RapidOCR + ONNX Runtime), "
    "or install Tesseract with its language files (or set TESSDATA_PREFIX to its tessdata folder)."
)

# Tesseract language codes -> names (the dialog shows the code for anything not listed)
TESS_NAMES = ocrdata.NAMES
# Languages RapidOCR's bundled model reads (Tesseract codes): Latin script with Western European accents,
# Chinese and Japanese. Everything else goes to Tesseract.
RAPID_LANGS = frozenset({
    "eng", "deu", "fra", "spa", "ita", "por", "nld", "dan", "swe", "nor", "fin", "isl", "cat", "glg", "eus",
    "afr", "ind", "msa", "lat", "gle", "cym", "ltz", "swa", "fil", "chi_sim", "chi_tra", "jpn",
})


# ------------------------------------------------------------------ detection
def text_chars(page: pymupdf.Page) -> int:
    """Non-whitespace characters of extractable text on the page."""
    return sum(not c.isspace() for c in page.get_text("text", flags=0))


def image_coverage(page: pymupdf.Page) -> float:
    """Fraction of the page covered by images (0..1)."""
    area = page.rect * page.derotation_matrix  # image boxes come in unrotated page space
    total = abs(area) or 1.0
    covered = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & area
        if not r.is_empty:
            covered += abs(r)
    return min(1.0, covered / total)


def needs_ocr(page: pymupdf.Page, max_chars: int = 20, min_cover: float = 0.5) -> bool:
    """Scanned-looking page: (almost) no extractable text and an image covering most of it."""
    try:
        return text_chars(page) < max_chars and image_coverage(page) >= min_cover
    except Exception:  # noqa: BLE001 - a damaged page just isn't offered for OCR
        return False


# -------------------------------------------------------------------- engines
@dataclass
class EngineInfo:
    key: str  # "rapidocr" | "tesseract"
    label: str
    languages: list[tuple[str, str]]  # (code, label)
    default_language: str
    tessdata: str | None = None  # Tesseract: the first folder (see `folders` and `tessdata_for`)
    free_language: bool = False  # the language box accepts combinations like "eng+hin"
    notes: str = ""
    folders: list[str] = field(default_factory=list)  # Tesseract: tessdata folders, preferred first

    def supports(self, language: str) -> bool:
        parts = split_language(language)
        if not parts:
            return False
        if self.key == "rapidocr":
            return parts == ["auto"] or all(p in RAPID_LANGS for p in parts)
        return self.tessdata_for(language) is not None

    def tessdata_for(self, language: str) -> str | None:
        """Tesseract: the first folder with every language file of `language` (e.g. "eng+hin")."""
        if self.key != "tesseract":
            return None
        parts = split_language(language)
        for d in self.folders or ([self.tessdata] if self.tessdata else []):
            have = set(tesseract_languages(d))
            if parts and all(p in have for p in parts):
                return d
        return None


def split_language(language: str) -> list[str]:
    return [p.strip() for p in str(language or "").split("+") if p.strip()]


def rapidocr_available() -> bool:
    """Whether the OCR extra is installed (checked without importing it)."""
    try:
        return importlib.util.find_spec("rapidocr") is not None and importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def find_tessdata() -> str | None:
    """A Tesseract install's (or TESSDATA_PREFIX's) folder with *.traineddata files, or None. No subprocess."""
    cands = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        cands += [env, os.path.join(env, "tessdata")]
    exe = shutil.which("tesseract")
    if exe:
        d = os.path.dirname(os.path.realpath(exe))
        cands += [os.path.join(d, "tessdata"), os.path.join(d, "..", "share", "tessdata")]
        cands += glob.glob(os.path.join(d, "..", "share", "tesseract-ocr", "*", "tessdata"))
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                cands.append(os.path.join(base, "Tesseract-OCR", "tessdata"))
    else:
        cands += sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"), reverse=True)
        cands += ["/usr/share/tessdata", "/usr/local/share/tessdata", "/opt/homebrew/share/tessdata",
                  "/opt/local/share/tessdata", "/usr/share/tesseract/tessdata"]
    for c in cands:
        if c and os.path.isdir(c) and glob.glob(os.path.join(c, "*.traineddata")):
            return os.path.abspath(c)
    return None


def tessdata_folders() -> list[str]:
    """Folders with Tesseract language files, preferred first: the user's downloads, a Tesseract install
    (or TESSDATA_PREFIX), the English data shipped with Leaf."""
    out: list[str] = []
    try:
        user = ocrdata.user_dir()
    except Exception:  # noqa: BLE001 - no Qt or no writable location
        user = None
    for d in (user, find_tessdata(), ocrdata.bundled_dir()):
        if d and tesseract_languages(d):
            d = os.path.abspath(d)
            if all(os.path.normcase(d) != os.path.normcase(x) for x in out):
                out.append(d)
    return out


def tesseract_languages(tessdata: str) -> list[str]:
    return ocrdata.languages_in(tessdata)


def _lang_order(c: str):
    return (c != "eng", TESS_NAMES.get(c, c).casefold())


def available_engines() -> list[EngineInfo]:
    """Engines usable on this machine, best first. Cheap: nothing heavy is imported."""
    out = []
    if rapidocr_available():
        out.append(EngineInfo("rapidocr", "RapidOCR (built-in model)",
                              [(c, ocrdata.label(c)) for c in sorted(RAPID_LANGS, key=_lang_order)], "eng",
                              notes="Reads Latin-script languages, Chinese and Japanese."))
    folders = tessdata_folders()
    langs = sorted({c for d in folders for c in tesseract_languages(d)}, key=_lang_order)
    if langs:
        default = "eng" if "eng" in langs else langs[0]
        out.append(EngineInfo("tesseract", "Tesseract", [(c, ocrdata.label(c)) for c in langs], default,
                              tessdata=folders[0], free_language=True,
                              notes="Combine languages with +, e.g. eng+hin.", folders=folders))
    return out


def choose_engine(engines: list[EngineInfo], language: str, preferred: str = "auto") -> EngineInfo | None:
    """The engine for `language`: the preferred one when it reads that language, else RapidOCR when it does
    (installed + supported), else Tesseract when its language files are there. None: nothing reads it."""
    if preferred and preferred != "auto":
        e = next((e for e in engines if e.key == preferred), None)
        if e is not None and e.supports(language):
            return e
    for key in ("rapidocr", "tesseract"):
        e = next((e for e in engines if e.key == key), None)
        if e is not None and e.supports(language):
            return e
    return None


def language_choices(engines: list[EngineInfo]) -> list[tuple[str, str]]:
    """Every language some engine reads, English first, then by name."""
    seen: dict[str, str] = {}
    for e in engines:
        for c, lab in e.languages:
            if c != "auto":
                seen.setdefault(c, lab)
    return sorted(seen.items(), key=lambda kv: _lang_order(kv[0]))


class Engine:
    """Recognizes one page at a time. Results are word tuples in `page.rect` space (see module doc)."""

    key = ""

    def recognize(self, page: pymupdf.Page, language: str, dpi: int = DEFAULT_DPI) -> list[tuple]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class RapidEngine(Engine):
    key = "rapidocr"

    def __init__(self) -> None:
        self._ocr = None

    def _load(self):
        if self._ocr is None:
            from rapidocr import RapidOCR  # the optional extra: imported only here

            # Recognition reads line crops at full resolution (the default caps the longer side at 2000 px, losing
            # small print); detection runs on a copy at most 2200 px long: on a 300 dpi A4 page that keeps the peak
            # memory near 0.8 GB instead of 1.8 GB with about the same results.
            self._ocr = RapidOCR(params={"Global.log_level": "error", "Global.max_side_len": 4200,
                                         "Det.limit_type": "max", "Det.limit_side_len": 2200})
        return self._ocr

    def recognize(self, page, language="auto", dpi=DEFAULT_DPI):
        import numpy as np

        ocr = self._load()
        pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB, annots=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()  # BGR
        del pix
        res = ocr(img, return_word_box=True)
        del img
        k = 72.0 / dpi
        txts = res.txts or ()
        boxes = res.boxes if res.boxes is not None else ()
        scores = res.scores or ()
        word_res = res.word_results or ()
        out: list[tuple] = []
        for ln, text in enumerate(txts):
            box = _bbox(boxes[ln]) if ln < len(boxes) else None
            ws = word_res[ln] if ln < len(word_res) and word_res[ln] else None
            if ws and all(w and len(w) >= 3 and w[2] is not None for w in ws):
                parts = [(str(w[0]), float(w[1]), _bbox(w[2])) for w in ws]
                parts = _merge_cjk(parts)
            elif box is not None:
                parts = _split_line(str(text), float(scores[ln]) if ln < len(scores) else -1.0, box)
            else:
                continue
            n = 0
            for t, conf, (x0, y0, x1, y1) in parts:
                t = t.strip()
                if not t or x1 <= x0 or y1 <= y0:
                    continue
                out.append((x0 * k, y0 * k, x1 * k, y1 * k, t, ln, n, round(conf, 4)))
                n += 1
        return out

    def close(self) -> None:
        self._ocr = None


class TesseractEngine(Engine):
    key = "tesseract"

    def __init__(self, tessdata: str | None) -> None:
        self.tessdata = tessdata or find_tessdata() or ocrdata.bundled_dir()

    def recognize(self, page, language="eng", dpi=DEFAULT_DPI):
        tp = page.get_textpage_ocr(flags=0, language=language or "eng", dpi=dpi, full=True, tessdata=self.tessdata)
        m = page.rotation_matrix  # words come unrotated; Leaf keeps page.rect (display) coordinates
        out = []
        ln = -1
        # Words from the characters: MuPDF's OCR device puts each Tesseract word in its own span but writes no
        # space between words of scripts like Devanagari, so get_text("words") would glue a whole line together.
        for block in page.get_text("rawdict", textpage=tp, flags=0)["blocks"]:
            for line in block.get("lines", ()):
                ln += 1
                n = 0
                for span in line.get("spans", ()):
                    word, box = "", None
                    for ch in span.get("chars", ()) + [{"c": " ", "bbox": None}]:
                        if ch["c"].isspace():
                            if word.strip() and box is not None and not box.is_empty:
                                r = box * m
                                out.append((r.x0, r.y0, r.x1, r.y1, word, ln, n, -1.0))
                                n += 1
                            word, box = "", None
                            continue
                        word += ch["c"]
                        cb = pymupdf.Rect(ch["bbox"])
                        box = cb if box is None else box | cb
        return out


def create_engine(key: str, tessdata: str | None = None) -> Engine:
    if key == "tesseract":
        return TesseractEngine(tessdata)
    if key == "rapidocr":
        return RapidEngine()
    raise ValueError(f"unknown OCR engine {key!r}")


def _bbox(quad) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _split_line(text: str, conf: float, box) -> list[tuple]:
    """A line without word boxes: split at spaces, widths proportional to the characters."""
    x0, y0, x1, y1 = box
    words = text.split()
    total = sum(len(w) for w in words) + max(0, len(words) - 1)
    if not total:
        return []
    per = (x1 - x0) / total
    out, x = [], x0
    for w in words:
        out.append((w, conf, (x, y0, x + per * len(w), y1)))
        x += per * (len(w) + 1)
    return out


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF


def _merge_cjk(parts: list[tuple]) -> list[tuple]:
    """RapidOCR returns CJK text one character per 'word': join neighbouring CJK characters into runs, so
    selection and copy don't put spaces between them."""
    out: list[tuple] = []
    for t, c, b in parts:
        if out and t and all(_is_cjk(ch) for ch in t) and all(_is_cjk(ch) for ch in out[-1][0]):
            pt, pc, pb = out[-1]
            out[-1] = (pt + t, min(pc, c), (min(pb[0], b[0]), min(pb[1], b[1]), max(pb[2], b[2]), max(pb[3], b[3])))
        else:
            out.append((t, c, b))
    return out


def recognize_document(pdf_bytes: bytes, indexes, engine: Engine, language: str, dpi: int = DEFAULT_DPI,
                       password: str | None = None, only_if_needed: bool = False):
    """Recognize pages of a PDF (in-process). Yields (index, words or None if skipped, seconds)."""
    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        if d.needs_pass:
            d.authenticate(password or "")
        for i in indexes:
            page = d[i]
            if only_if_needed and text_chars(page) >= 20:
                yield i, None, 0.0
                continue
            t = time.perf_counter()
            words = engine.recognize(page, language, dpi)
            yield i, words, time.perf_counter() - t
    finally:
        d.close()


# ------------------------------------------------------------- Leaf model use
def as_source_words(words: list[tuple]) -> list[tuple]:
    """OCR words in Source.words format (x0, y0, x1, y1, text, block, line, word)."""
    return [(w[0], w[1], w[2], w[3], w[4], OCR_BLOCK, w[5], w[6]) for w in words]


def search_words(words: list[tuple], query: str) -> list[pymupdf.Rect]:
    """Case-insensitive matches of `query` in OCR words (may span words of a line). One rect per match and
    line, in the words' coordinate space."""
    q = " ".join(query.split()).casefold()
    if not q or not words:
        return []
    lines: dict = {}
    for w in words:
        lines.setdefault((w[5], w[6]) if len(w) > 7 and w[5] == OCR_BLOCK else w[5], []).append(w)
    out = []
    for ws in lines.values():
        text, spans = "", []
        for w in ws:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w[4]), w))
            text += w[4]
        low = text.casefold()
        if len(low) != len(text):  # casefold changed lengths (ß -> ss): fall back to per-character lower()
            low = text.lower()
            if len(low) != len(text):
                continue
        start = low.find(q)
        while start >= 0:
            end = start + len(q)
            r = pymupdf.Rect()
            for a, b, w in spans:
                if b <= start or a >= end:
                    continue
                wr = pymupdf.Rect(w[:4])
                n = max(1, b - a)  # partial word: interpolate its box by characters
                lo, hi = max(start, a) - a, min(end, b) - a
                part = pymupdf.Rect(wr.x0 + wr.width * lo / n, wr.y0, wr.x0 + wr.width * hi / n, wr.y1)
                r = part if r.is_empty else r | part
            if not r.is_empty:
                out.append(r)
            start = low.find(q, end)
    return out


# ------------------------------------------------------------ text layer (save)
_FONT = "LeafOCR"


def _code(ch: str) -> int:
    o = ord(ch)
    return o if o <= 0xFFFF and not 0xD800 <= o <= 0xDFFF else 0xFFFD


def _to_unicode(codes) -> bytes:
    codes = sorted(set(codes))
    out = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
           "/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def",
           "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
           "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar")
        out += [f"<{c:04X}> <{c:04X}>" for c in chunk]
        out.append("endbfchar")
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(out).encode("ascii")


def _new_stream(doc: pymupdf.Document, data: bytes, compress: bool = True) -> int:
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, data, compress=compress)
    return x


def _glyphless_font(doc: pymupdf.Document, codes) -> int:
    fd = doc.get_new_xref()
    doc.update_object(fd, f"<</Type/FontDescriptor/FontName/{_FONT}/Flags 4/FontBBox[0 -200 500 800]"
                          "/ItalicAngle 0/Ascent 800/Descent -200/CapHeight 700/StemV 80>>")
    cid = doc.get_new_xref()
    doc.update_object(cid, f"<</Type/Font/Subtype/CIDFontType2/BaseFont/{_FONT}"
                           "/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>"
                           f"/FontDescriptor {fd} 0 R/DW 500/CIDToGIDMap/Identity>>")
    tu = _new_stream(doc, _to_unicode(codes))
    f = doc.get_new_xref()
    doc.update_object(f, f"<</Type/Font/Subtype/Type0/BaseFont/{_FONT}/Encoding/Identity-H"
                         f"/DescendantFonts[{cid} 0 R]/ToUnicode {tu} 0 R>>")
    return f


def text_layer_ops(words: list[tuple], to_user: pymupdf.Matrix) -> tuple[bytes, set[int]]:
    """Content stream drawing `words` (page.rect space) as invisible text; `to_user` maps page.rect space to
    PDF user space. Returns (stream, character codes used)."""
    o0 = pymupdf.Point(0, 0) * to_user
    ops = ["q", "BT", "3 Tr", f"/{_FONT} 1 Tf"]
    used: set[int] = set()
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        codes = [_code(c) for c in str(text)]
        if not codes or x1 <= x0 or y1 <= y0:
            continue
        used.update(codes)
        h, wd = y1 - y0, x1 - x0
        sx = 2.0 * wd / len(codes)  # every glyph is 0.5 em wide: n glyphs fill the box width
        d = pymupdf.Point(1, 0) * to_user - o0  # reading direction in user space
        u = pymupdf.Point(0, -1) * to_user - o0  # "up" in user space
        o = pymupdf.Point(x0, y1 - 0.2 * h) * to_user  # baseline origin (descent 0.2 em)
        tm = (sx * d.x, sx * d.y, h * u.x, h * u.y, o.x, o.y)
        ops.append(" ".join(f"{v:.3f}" for v in tm) + " Tm <" + "".join(f"{c:04X}" for c in codes) + "> Tj")
    ops += ["ET", "Q"]
    return "\n".join(ops).encode("ascii"), used


def write_text_layer(page: pymupdf.Page, words: list[tuple], to_pdf: pymupdf.Matrix) -> None:
    """Add `words` (in the source page's page.rect space; `to_pdf` = its derotation matrix) to `page` as an
    invisible text layer. Built on a scratch page with the same geometry and placed as one Form XObject, so
    the page's own resources and content are left alone (incremental saves just append)."""
    if not words:
        return
    tmp = pymupdf.open()
    try:
        sp = tmp.new_page(width=page.mediabox.width, height=page.mediabox.height)
        sp.set_mediabox(page.mediabox)
        sp.set_cropbox(page.cropbox)
        # page.rect space -> unrotated page space -> PDF user space. The scratch page stays unrotated: PyMuPDF's
        # transformation_matrix drops the crop box offset on rotated pages.
        to_user = to_pdf * ~sp.transformation_matrix
        data, used = text_layer_ops(words, to_user)
        font = _glyphless_font(tmp, used)
        tmp.xref_set_key(sp.xref, "Resources", f"<</Font<</{_FONT} {font} 0 R>>>>")
        tmp.xref_set_key(sp.xref, "Contents", f"{_new_stream(tmp, data)} 0 R")
        rot = page.rotation
        # identical unrotated geometry on both pages -> identity placement (see document._flush_font_layer)
        page.set_rotation(0)
        try:
            page.show_pdf_page(page.rect, tmp, 0)
        finally:
            page.set_rotation(rot)
    finally:
        tmp.close()


@dataclass
class OcrPage:
    """OCR result for one page of the page list (the layer is keyed by PageRef.uid)."""
    src: object  # document.Source
    index: int
    words: list = field(default_factory=list)
    engine: str = ""
    language: str = ""
