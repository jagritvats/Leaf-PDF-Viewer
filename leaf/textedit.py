"""Edit existing text (phase 3): the engine. Qt-free.

Contract (the UI codes against this):

    unit_at(doc, ref, x, y, para=False) -> TextUnit | None     hit-test a line / paragraph (page.rect space)
    check(doc, ref, unit, new_text=None) -> list[str]           raises Refuse; returns warnings for the user

An edit is stored as `doc.text_edits[ref.uid][unit.key] = new_text` (commands.EditPageText) and replayed with
`apply_edits(page, edits)` on a fresh copy of the source page: at save (document.PdfDocument._apply, after form
values and before overlays), and on the per-page slot of the display copy that the screen, thumbnails, search and
selection use (document.Source.set_text_edits).

How one edit works (ported from the phase-3 spike; fidelity numbers in tests/test_textedit.py):

1. PageText(page)   capture every painted glyph with MuPDF's sanitize filter (read-only): exact text matrix,
                    advance, render mode, alpha; join with rawdict chars (text, font, colour, synthetic spaces);
                    resolve each span to its font object (xref) and build a codec (unicode -> code, code -> advance).
2. units            lines (rawdict lines) and paragraphs (own grouping: columns, alignment, leading). A unit's key
                    is its kind plus the PDF-space origin and unicode of each of its glyphs, so it is found again
                    on any copy of the same source page.
3. plan             diff old/new text; new chars inherit the style of the char they replace; unchanged chars keep
                    their exact original positions (kerning included); paragraphs re-break greedily within their
                    original edges and baselines, and re-justify.
4. remove           remove exactly the unit's glyphs (sanitize-filter callback matching origin + unicode; no
                    rectangle, so neighbours on overlapping lines survive).
5. write            one appended content stream: the ORIGINAL font object (same xref, nothing re-embedded) with TJ
                    adjustments so each glyph lands where the plan put it. Characters that font cannot show use a
                    fallback font (same family installed on the system, else a generic one by style, else a script
                    font); fallback fonts are subset in a scratch document and grafted into the page resources
                    (not placed as a Form XObject, which split words in text extraction).

Safety: check() refuses units it can't reproduce (no reliable Unicode, shaped scripts, vertical, invisible/OCR,
outlined or clipping text, glyphs it can't locate, a font it can't reuse for the untouched characters) and pages
where MuPDF's content filter isn't lossless (a no-op pass over a one-page copy must render identically; it loses
e.g. the line width of stroked text in MuPDF 1.28).

Display: document.Source keeps one "slot" page per edited page uid in its display copy (shares the original
page's resources and annotations, own content stream), so duplicated pages never show each other's edits. The
render worker never runs this engine: it receives the slot's page keys plus the objects Leaf created for it
(Source.upage_payload, a few KB per edited page) and renumbers them into its own copy (render_worker).
Measured on a 70-line page (6.6k glyphs): first unit_at ~60 ms (PageText, cached per source page), then ~2 ms;
first check ~60 ms (lossless guard, cached), then ~1 ms per keystroke for a line; display re-apply ~30 ms.
"""
from __future__ import annotations

import ctypes
import difflib
import math
import re
import statistics
import unicodedata
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field

import pymupdf

m = pymupdf.mupdf

FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_MEDIABOX_CLIP
EPS = 0.02  # pt, origin matching tolerance


class Refuse(Exception):
    """The edit cannot be done faithfully; str(e) is a short reason meant for the user."""


@dataclass(frozen=True)
class TextUnit:
    page_uid: int
    kind: str  # "line" | "para"
    key: tuple  # stable, hashable id of the original glyphs: (kind, ((x, y, unicode), ...)) in PDF space
    text: str
    bbox: tuple  # (x0, y0, x1, y1) in the source page.rect space (display space incl. the page's own /Rotate)
    line_boxes: tuple  # per-line bboxes, same space
    baseline: tuple  # (x, y) origin of the first glyph, same space
    angle: float  # text direction in degrees in that space, clockwise (y down) as QGraphicsItem.setRotation
    size: float
    color: str  # "#rrggbb"
    family: str  # Qt-displayable family: matched system family if installed, else Times New Roman/Arial/Courier New
    bold: bool
    italic: bool
    align: str  # "left" | "right" | "center" | "justify"


# ---------------------------------------------------------------------------------------------- glyph capture
class Glyph:
    """One painted glyph. `mt`: glyph space -> PDF user space (font size, Tz, Ts, Tm, CTM folded in) as a tuple;
    (x, y) its origin in PDF user space (y up)."""
    __slots__ = ("mt", "x", "y", "adv", "ucs", "tr", "ca", "used")

    def __init__(self, mt: tuple, adv: float, ucs: tuple, tr: int, ca: float):
        self.mt, self.x, self.y = mt, mt[4], mt[5]
        self.adv, self.ucs, self.tr, self.ca = adv, ucs, tr, ca  # adv: em, as MuPDF uses it
        self.used = False

    @property
    def origin(self) -> pymupdf.Point:
        return pymupdf.Point(self.x, self.y)

    @property
    def m(self) -> pymupdf.Matrix:
        return pymupdf.Matrix(*self.mt)

    @property
    def key(self) -> tuple:
        return (round(self.x, 2), round(self.y, 2), self.ucs)


class _Sanitize(m.PdfSanitizeFilterOptions2):
    def __init__(self, decide):
        super().__init__()
        self.use_virtual_text_filter()
        self.decide = decide

    def text_filter(self, ctx, ucsbuf, ucslen, trm, ctm, bbox, tr, ca, CA):  # noqa: N803
        try:
            ucs = tuple((ctypes.c_int * ucslen).from_address(int(ucsbuf))) if ucslen else ()
            t, c = trm, ctm  # trm x ctm, inlined (this runs once per glyph)
            ta, tb, tc, td, te, tf = t.a, t.b, t.c, t.d, t.e, t.f
            ca_, cb, cc, cd = c.a, c.b, c.c, c.d
            g = (ta * ca_ + tb * cc, ta * cb + tb * cd, tc * ca_ + td * cc, tc * cb + td * cd,
                 te * ca_ + tf * cc + c.e, te * cb + tf * cd + c.f)
            return 1 if self.decide(g, bbox.x1 - bbox.x0, ucs, tr, ca) else 0
        except Exception:  # noqa: BLE001  (never let an exception cross the C boundary)
            return 0


def _run_filter(page: pymupdf.Page, decide, update: bool, forms: bool) -> None:
    s = _Sanitize(decide)
    fo = pymupdf._make_PdfFilterOptions(recurse=1 if forms or not update else 0, instance_forms=1 if forms else 0,
                                        ascii=1, no_update=0 if update else 1, sanitize=1, sopts=s)
    m.pdf_filter_page_contents(pymupdf._as_pdf_document(page.parent), pymupdf._as_pdf_page(page), fo)


def capture_glyphs(page: pymupdf.Page) -> list[Glyph]:
    """Every glyph the page paints (Form XObjects included), read-only."""
    out: list[Glyph] = []

    def rec(g, adv, ucs, tr, ca):
        out.append(Glyph(g, adv, ucs, tr, ca))
        return False

    _run_filter(page, rec, update=False, forms=True)
    return out


def _wanted(targets) -> dict:
    want: dict[tuple, int] = {}
    for g in targets:
        k = (round(g.x / EPS), round(g.y / EPS), g.ucs)
        want[k] = want.get(k, 0) + 1
    return want


def remove_glyphs(page: pymupdf.Page, targets: list[Glyph]) -> int:
    """Remove exactly the glyphs whose origin (PDF space) and unicode match `targets`. Returns how many."""
    left = _wanted(targets)
    total = sum(left.values())

    def decide(g, adv, ucs, tr, ca):
        k = (round(g[4] / EPS), round(g[5] / EPS), ucs)
        if left.get(k):
            left[k] -= 1
            return True
        return False

    _run_filter(page, decide, update=True, forms=False)
    done = total - sum(left.values())
    if done < total:  # some glyphs live in Form XObjects: filter those too, on per-page copies of the forms
        _run_filter(page, decide, update=True, forms=True)
        done = total - sum(left.values())
    return done


# ---------------------------------------------------------------------------------------------- font codecs
def _resolve(doc, xref: int, key: str):
    t, v = doc.xref_get_key(xref, key)
    if t == "xref":
        return "xref", int(v.split()[0])
    return t, v


def _nums(s: str) -> list:
    """Tokenise a PDF array string into numbers and nested lists (one level)."""
    out, stack = [], []
    for tok in re.findall(r"\[|\]|-?\d*\.?\d+", s):
        if tok == "[":
            stack.append(out)
            out = []
        elif tok == "]":
            if not stack:
                break
            inner = out
            out = stack.pop()
            out.append(inner)
        else:
            out.append(float(tok))
    return out[0] if len(out) == 1 and isinstance(out[0], list) else out


def _array(doc, xref, key) -> list:
    t, v = _resolve(doc, xref, key)
    if t == "xref":
        v = doc.xref_object(v, compressed=True)
    elif t != "array":
        return []
    return _nums(v)


def _parse_tounicode(data: bytes) -> tuple[dict[int, str], int]:
    """ToUnicode CMap -> ({code: text}, code length in bytes)."""
    s = data.decode("latin-1")
    nbytes = 1
    for lo in re.findall(r"begincodespacerange\s*<([0-9A-Fa-f]+)>", s):
        nbytes = max(nbytes, len(lo) // 2)
    out: dict[int, str] = {}

    def txt(h):
        h = h if len(h) % 4 == 0 else h.rjust((len(h) + 3) // 4 * 4, "0")
        try:
            return bytes.fromhex(h).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return "�"

    for body in re.findall(r"beginbfchar(.*?)endbfchar", s, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", body):
            out[int(a, 16)] = txt(b)
    for body in re.findall(r"beginbfrange(.*?)endbfrange", s, re.S):
        for a, b, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", body):
            lo, hi = int(a, 16), int(b, 16)
            if rest.startswith("["):
                for i, h in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    out[lo + i] = txt(h)
            else:
                base = bytes.fromhex(rest[1:-1].rjust((len(rest) - 2 + 3) // 4 * 4, "0"))
                for i in range(min(hi - lo + 1, 65536)):
                    v = int.from_bytes(base, "big") + i
                    out[lo + i] = txt(f"{v:0{len(base) * 2}x}")
    return out, nbytes


_STD_FIX = {0x27: "’", 0x60: "‘"}  # StandardEncoding differs from Latin-1 in printable ASCII here
BASE14 = {
    "Helvetica": "helv", "Helvetica-Bold": "hebo", "Helvetica-Oblique": "heit", "Helvetica-BoldOblique": "hebi",
    "Times-Roman": "tiro", "Times-Bold": "tibo", "Times-Italic": "tiit", "Times-BoldItalic": "tibi",
    "Courier": "cour", "Courier-Bold": "cobo", "Courier-Oblique": "coit", "Courier-BoldOblique": "cobi",
    "Symbol": "symb", "ZapfDingbats": "zadb",
}


@dataclass(eq=False)
class FontCodec:
    xref: int
    subtype: str
    basefont: str
    nbytes: int
    code_of: dict  # text -> code
    widths: dict  # code -> advance (em)
    default_w: float
    embedded: bool
    program: pymupdf.Font | None
    names: set
    identity: bool = False  # Type0 Identity-H with identity CID->GID: any glyph of the program is reachable
    problems: list = field(default_factory=list)
    program_ok: bool = False  # program cmap agrees with the glyphs seen on the page

    @property
    def subset(self) -> bool:
        return bool(re.match(r"^[A-Z]{6}\+", self.basefont))

    def encode(self, text: str, seen: set) -> int | None:
        code = self.code_of.get(text)
        if code is None and self.identity and self.program is not None and len(text) == 1:
            gid = self.program.has_glyph(ord(text))
            if gid:
                code = gid
        if code is None:
            return None
        if text in seen or not self.embedded:
            return code
        if self.program is not None and self.program_ok and len(text) == 1:
            return code if self.program.has_glyph(ord(text)) else None
        if self.subtype == "Type3":
            return code
        return None if self.subset else code

    def advance(self, code: int, text: str = "") -> float:
        w = self.widths.get(code)
        if w is None and self.program is not None and text:
            w = self.program.glyph_advance(ord(text[0]))
        return self.default_w if w is None else w


def build_codec(doc: pymupdf.Document, xref: int) -> FontCodec:
    subtype = doc.xref_get_key(xref, "Subtype")[1].lstrip("/")
    basefont = doc.xref_get_key(xref, "BaseFont")[1].lstrip("/")
    problems = []
    try:
        _, _ext, _, buf = doc.extract_font(xref)
    except Exception:  # noqa: BLE001
        buf = b""
    program = None
    names = {re.sub(r"^[A-Z]{6}\+", "", basefont)}
    if buf:
        try:
            program = pymupdf.Font(fontbuffer=buf)
            names.add(program.name)
        except Exception:  # noqa: BLE001
            problems.append("embedded font program could not be loaded")
    code_of: dict = {}
    widths: dict = {}
    default_w = 0.0
    nbytes = 1
    identity = False
    diffs: list = []
    table: dict = {}
    t, tu = _resolve(doc, xref, "ToUnicode")
    tumap: dict = {}
    if t == "xref":
        try:
            tumap, _nb = _parse_tounicode(doc.xref_stream(tu))
        except Exception:  # noqa: BLE001
            problems.append("unreadable ToUnicode")
    if subtype == "Type0":
        enc = doc.xref_get_key(xref, "Encoding")[1]
        t, v = _resolve(doc, xref, "DescendantFonts")
        if t == "xref":
            v = doc.xref_object(v, compressed=True)
        mt = re.search(r"(\d+)\s+0\s+R", v or "")
        if not mt:  # descendant given inline is rare; accept only indirect
            raise Refuse("unsupported font structure")
        dx = int(mt.group(1))
        nbytes = 2
        cid2gid = doc.xref_get_key(dx, "CIDToGIDMap")[1]
        if enc in ("/Identity-H", "/Identity-V"):
            identity = cid2gid in ("/Identity", "null") and program is not None
            for code, txt in tumap.items():
                code_of.setdefault(txt, code)
        elif re.match(r"/Uni\w+-(UCS2|UTF16)-[HV]", enc):
            for cp in range(0x20, 0x10000):  # code = UTF-16 code unit
                if 0xD800 <= cp < 0xE000:
                    continue
                code_of[chr(cp)] = cp
        else:
            problems.append(f"predefined CMap {enc} not supported")
        if enc.endswith("-V"):
            problems.append("vertical writing mode")
        tw, wv = _resolve(doc, dx, "DW")
        default_w = (float(wv) if tw in ("int", "real") else 1000.0) / 1000
        w = _array(doc, dx, "W")
        i = 0
        while i < len(w):
            if i + 1 < len(w) and isinstance(w[i + 1], list):
                for k, val in enumerate(w[i + 1]):
                    widths[int(w[i]) + k] = val / 1000
                i += 2
            elif i + 2 < len(w) and not isinstance(w[i + 2], list):
                for c in range(int(w[i]), int(w[i + 1]) + 1):
                    widths[c] = w[i + 2] / 1000
                i += 3
            else:
                break
        embedded = bool(buf)
    else:
        scale = 1 / 1000
        if subtype == "Type3":
            fm = _array(doc, xref, "FontMatrix")
            scale = fm[0] if fm else 0.001
        first = doc.xref_get_key(xref, "FirstChar")[1]
        ws = _array(doc, xref, "Widths")
        if ws and first.isdigit():
            for k, val in enumerate(ws):
                if not isinstance(val, list):
                    widths[int(first) + k] = val * scale
        elif basefont in BASE14:  # standard 14 without /Widths: use MuPDF's metrics
            f = _font(("builtin", BASE14[basefont]))
            for code in range(32, 256):
                ch = bytes([code]).decode("cp1252", "replace")
                widths[code] = f.text_length(ch, 1)
        # unicode via ToUnicode first, then /Encoding (base + Differences)
        for code, txt in tumap.items():
            code_of.setdefault(txt, code)
        et, ev = _resolve(doc, xref, "Encoding")
        base = None
        d = ""
        if et == "name":
            base = ev
        elif et in ("xref", "dict"):
            if et == "xref":
                base = doc.xref_get_key(ev, "BaseEncoding")[1]
                d = doc.xref_get_key(ev, "Differences")[1]
            else:
                bm = re.search(r"/BaseEncoding\s*/(\w+)", ev)
                base = "/" + bm.group(1) if bm else None
                dm = re.search(r"/Differences\s*\[(.*?)\]", ev, re.S)
                d = "[" + dm.group(1) + "]" if dm else ""
            code = 0
            for tok in re.findall(r"/[^\s/\[\]]+|\d+", d or ""):
                if tok[0] == "/":
                    diffs.append((code, tok[1:]))
                    code += 1
                else:
                    code = int(tok)
        if base == "/WinAnsiEncoding" or (base in (None, "null") and basefont in BASE14 and subtype != "Type3"):
            codec_name = "cp1252" if base == "/WinAnsiEncoding" else "latin-1"
            for code in range(32, 256):
                try:
                    table[code] = bytes([code]).decode(codec_name)
                except UnicodeDecodeError:
                    pass
            if base != "/WinAnsiEncoding":
                table.update(_STD_FIX)
        elif base == "/MacRomanEncoding":
            for code in range(32, 256):
                table[code] = bytes([code]).decode("mac_roman")
        for code, name in diffs:
            u = m.fz_unicode_from_glyph_name(name)  # (pymupdf.glyph_name_to_unicode returns U+FFFD here)
            if u not in (0, 0xFFFD):
                table[code] = chr(u)
            else:
                table.pop(code, None)
        for code, ch in table.items():
            if code not in tumap:
                code_of.setdefault(ch, code)
        if basefont in ("Symbol", "ZapfDingbats"):
            problems.append("symbolic font")
        if subtype == "Type3":
            procs = doc.xref_get_key(xref, "CharProcs")[1]
            have = set(re.findall(r"/([^\s/<>\[\]()]+)\s+\d+\s+\d+\s+R", procs))
            dmap = dict(diffs)
            code_of = {k: v for k, v in code_of.items() if dmap.get(v) in have}
        embedded = bool(buf) or subtype == "Type3"
    # several codes may map to the same text (pdfTeX: 45 and 173 both "-", only 45 has a glyph and a width):
    # prefer a code with a non-zero width that the encoding actually defines
    enc_codes = {c for c, _ in diffs} if subtype != "Type0" and diffs else None
    alts: dict = {}
    for code, txt in tumap.items():
        alts.setdefault(txt, []).append(code)
    for txt, codes in alts.items():
        if len(codes) > 1:
            def rank(c):
                return (widths.get(c, 0) <= 0, enc_codes is not None and c not in enc_codes and c not in table, c)
            code_of[txt] = min(codes, key=rank)
    # NBSP and space usually share one glyph; ToUnicode may name either
    for a, b in ((" ", " "), (" ", " ")):
        if a not in code_of and b in code_of:
            code_of[a] = code_of[b]
    return FontCodec(xref, subtype, basefont, nbytes, code_of, widths, default_w, embedded, program, names,
                     identity, problems)


# ---------------------------------------------------------------------------------------------- page text model
@dataclass(eq=False)
class Run:  # a rawdict span
    font: str
    size: float
    flags: int
    color: int
    alpha: int
    bbox: pymupdf.Rect
    codec: FontCodec | None = None
    tried: bool = False


@dataclass(eq=False)
class Char:
    c: str
    origin: pymupdf.Point  # page space (unrotated, y down)
    bbox: pymupdf.Rect
    synthetic: bool
    run: Run
    glyph: Glyph | None = None
    extra: list = field(default_factory=list)  # duplicate glyphs at (almost) the same spot: fake bold, shadows


@dataclass(eq=False)
class Line:
    chars: list[Char]
    dir: tuple
    wmode: int
    bbox: pymupdf.Rect
    orphans: int = 0  # unmatched visible glyphs inside the line's box

    @property
    def text(self) -> str:
        return "".join(c.c for c in self.chars)

    @property
    def glyphs(self) -> list[Glyph]:
        return [c.glyph for c in self.chars if c.glyph is not None]

    @property
    def all_glyphs(self) -> list[Glyph]:
        return [g for c in self.chars for g in ([c.glyph] if c.glyph is not None else []) + c.extra]

    @property
    def keys(self) -> tuple:
        return tuple(g.key for g in self.glyphs)

    @property
    def size(self) -> float:
        return statistics.median(c.run.size for c in self.chars) if self.chars else 0

    @property
    def lines(self) -> list["Line"]:
        return [self]


@dataclass(eq=False)
class Para:
    lines: list[Line]
    align: str  # left | right | center | justify

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.lines[0].bbox)
        for ln in self.lines[1:]:
            r |= ln.bbox
        return r

    @property
    def keys(self) -> tuple:
        return tuple(k for ln in self.lines for k in ln.keys)

    @property
    def text(self) -> str:
        out = ""
        for ln in self.lines:
            t = ln.text.rstrip()
            if out.endswith("-") and not out.endswith(" -"):  # hyphen at line end: keep, the user decides
                out += t.lstrip()
            else:
                out += (" " if out else "") + t.lstrip()
        return out


class PageText:
    """Glyphs, lines and paragraphs of one page (read-only: building it never modifies the document)."""

    def __init__(self, page: pymupdf.Page):
        self.page = page
        self.doc = page.parent
        self.to_pdf = ~page.transformation_matrix
        self.glyphs = capture_glyphs(page)
        idx: dict[tuple, list[Glyph]] = {}
        for g in self.glyphs:
            idx.setdefault((round(g.x), round(g.y)), []).append(g)
        self._idx = idx
        self.codecs: dict[int, FontCodec] = {}
        self._fonts = None
        self.lines: list[Line] = []
        self._run_chars: dict[int, list[Char]] = {}
        self._runs: list[Run] = []
        raw = page.get_text("rawdict", flags=FLAGS)
        ta, tb, tc, td, te, tf = self.to_pdf
        for b in raw["blocks"]:
            for ln in b.get("lines", []):
                chars = []
                for s in ln["spans"]:
                    run = Run(s["font"], s["size"], s["flags"], s["color"], s.get("alpha", 255), pymupdf.Rect(s["bbox"]))
                    run_chars = self._run_chars[id(run)] = []
                    self._runs.append(run)  # (keeps id(run) unique)
                    for ch in s["chars"]:
                        o = pymupdf.Point(ch["origin"])
                        c = Char(ch["c"] if ch["c"] != " " else " ", o, pymupdf.Rect(ch["bbox"]),
                                 bool(ch.get("synthetic")), run)
                        run_chars.append(c)
                        if not c.synthetic:
                            ox, oy = ch["origin"]
                            c.glyph = self._match(ox * ta + oy * tc + te, ox * tb + oy * td + tf, ch["c"])
                        chars.append(c)
                if chars:
                    self.lines.append(Line(chars, tuple(ln["dir"]), ln["wmode"], pymupdf.Rect(ln["bbox"])))
        for ln in self.lines:
            _drop_spacing_artifacts(ln)
        self._attach_leftovers()
        self.seen: dict[int, set] = {}
        self._seen_fonts: set[str] = set()
        self._lossless: bool | None = None
        self._paras: list[Para] | None = None
        self._by_key: dict[tuple, Line | Para] = {}
        self.boxes: dict[tuple, pymupdf.Rect] = {}  # (key, text) -> page-space area of the edited unit

    def _match(self, px: float, py: float, c: str) -> Glyph | None:
        best = None
        rx, ry = round(px), round(py)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for g in self._idx.get((rx + dx, ry + dy), ()):
                    if g.used or abs(g.x - px) > 0.05 or abs(g.y - py) > 0.05:
                        continue
                    if "".join(map(chr, g.ucs)) == c or best is None:
                        best = g
        if best is not None:
            best.used = True
        return best

    def _attach_leftovers(self) -> None:
        """Glyphs no char claimed: duplicates of a char at (almost) the same spot (fake bold) go with that char;
        other visible ones inside a line's box make that line refuse (they would stay behind after an edit)."""
        left = [g for g in self.glyphs if not g.used and g.tr != 3 and g.ucs not in ((32,), (160,))]
        if not left:
            return
        chars = [c for ln in self.lines for c in ln.chars if c.glyph is not None]
        near: dict[tuple, list[Char]] = {}
        for c in chars:
            near.setdefault((round(c.glyph.x), round(c.glyph.y)), []).append(c)
        rest = []
        for g in left:
            hit = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for c in near.get((round(g.x) + dx, round(g.y) + dy), ()):
                        if c.glyph.ucs == g.ucs and abs(c.glyph.x - g.x) < 1.0 and abs(c.glyph.y - g.y) < 1.0:
                            hit = c
            if hit is not None:
                hit.extra.append(g)
                g.used = True
            else:
                rest.append(g)
        for g in rest:
            p = g.origin * self.page.transformation_matrix
            for ln in self.lines:
                if p in ln.bbox:
                    ln.orphans += 1

    # -- fonts
    def codec_for(self, run: Run, chars: list[Char]) -> FontCodec | None:
        if run.codec is not None or run.tried:
            return run.codec
        run.tried = True
        if self._fonts is None:
            self._fonts = self.page.get_fonts(full=True)
        mt = re.match(r"Type3 \((\d+) 0 R\)", run.font)
        cands = [int(mt.group(1))] if mt else [f[0] for f in self._fonts]
        # score against every glyph of this run on the page: one or two glyphs often fit several fonts
        chars = self._run_chars.get(id(run)) or chars
        best, best_score = None, -1.0
        for x in cands:
            if x not in self.codecs:
                try:
                    self.codecs[x] = build_codec(self.doc, x)
                except Refuse:
                    continue
                except Exception:  # noqa: BLE001  (malformed font dictionaries)
                    continue
            cd = self.codecs[x]
            score = 2.0 if run.font in cd.names else 0.0
            hits = n = 0
            for ch in chars:
                if ch.glyph is None or ch.run is not run:
                    continue
                n += 1
                code = cd.code_of.get(ch.c)
                if code is not None and abs(cd.advance(code, ch.c) - ch.glyph.adv) < 0.002:
                    hits += 1
            score += 3.0 * hits / n if n else 0
            if score > best_score:
                best, best_score = cd, score
        if best is None or best_score < 2.5:
            return None
        run.codec = best
        s = self.seen.setdefault(best.xref, set())
        if run.font not in self._seen_fonts:  # every char shown in this font is in the font (subset or not)
            self._seen_fonts.add(run.font)
            s.update(ch.c for ln in self.lines for ch in ln.chars if ch.run.font == run.font and not ch.synthetic)
        if best.program is not None and not best.program_ok:
            probe = [c for c in s if len(c) == 1][:40]
            best.program_ok = bool(probe) and all(best.program.has_glyph(ord(c)) for c in probe)
        return best

    def rewrite_is_lossless(self) -> bool:
        """Whether MuPDF's content filter reproduces this page exactly. Removing glyphs rewrites the whole content
        stream, and the filter loses some state (e.g. line width and dash of stroked text, MuPDF 1.28): a no-op
        pass over a one-page copy must render the same, else editing on this page is refused. Cached; ~5 ms.

        Both renders are of the same one-page copy, before and after the pass: the page itself differs from the copy
        when it has annotations even with annots=False (MuPDF renders a page whose annotations use blending, e.g. a
        Highlight's Multiply, inside a transparency group: anti-aliased edges come out one grey level off)."""
        if self._lossless is None:
            tmp = pymupdf.open()
            try:
                tmp.insert_pdf(self.doc, from_page=self.page.number, to_page=self.page.number, links=False,
                               annots=False, widgets=False)
                a = tmp[0].get_pixmap(colorspace=pymupdf.csGRAY, annots=False, alpha=False)
                _run_filter(tmp[0], lambda *a: False, update=True, forms=True)
                b = tmp[0].get_pixmap(colorspace=pymupdf.csGRAY, annots=False, alpha=False)
                self._lossless = a.samples == b.samples
            except Exception:  # noqa: BLE001
                self._lossless = False
            finally:
                tmp.close()
        return self._lossless

    # -- units
    def line_at(self, p: pymupdf.Point) -> Line | None:
        """The line under `p` (page space); among overlapping boxes (tight leading) the one whose middle is nearest."""
        best, dist = None, None
        for ln in self.lines:
            if not ln.glyphs or not ln.text.strip():
                continue
            r = ln.bbox
            if not (r.x0 - 0.5 <= p.x <= r.x1 + 0.5 and r.y0 - 0.5 <= p.y <= r.y1 + 0.5):
                continue
            d = abs(_across(ln, p) - _across(ln, (r.tl + r.br) / 2))
            if dist is None or d < dist:
                best, dist = ln, d
        return best

    def paragraphs(self) -> list[Para]:
        if self._paras is None:
            self._paras = group_paragraphs(self.lines)
        return self._paras

    def para_of(self, ln: Line) -> Para:
        for p in self.paragraphs():
            if any(x is ln for x in p.lines):
                return p
        return Para([ln], "left")

    def find(self, key: tuple) -> Line | Para:
        """The unit with this key (see unit_key); Refuse if this page has no such text."""
        u = self._by_key.get(key)
        if u is not None:
            return u
        kind, gk = key
        idx = self.__dict__.setdefault("_unit_index", {})
        if kind not in idx:  # one pass per kind instead of one per lookup (Replace all looks up hundreds)
            units = self.lines if kind == "line" else self.paragraphs()
            idx[kind] = {}
            for un in units:
                idx[kind].setdefault(frozenset(un.keys), un)
        un = idx[kind].get(frozenset(gk))
        if un is None:
            raise Refuse("The original text was not found on the page.")
        self._by_key[key] = un
        return un


def unit_key(unit: Line | Para) -> tuple:
    return ("para" if isinstance(unit, Para) else "line", unit.keys)


def _drop_spacing_artifacts(ln: Line) -> None:
    """MuPDF inserts synthetic spaces when glyphs are spread (Tc, Tz < 100). Drop synthetic spaces whose gap is
    no wider than the line's usual letter gap."""
    real = [c for c in ln.chars if not c.synthetic]
    if len(real) < 3:
        return
    gaps = []
    for a, b in zip(ln.chars, ln.chars[1:]):
        if b.synthetic or a.synthetic or a.c == " " or b.c == " ":
            continue
        gaps.append(_along(ln, b.origin) - _along(ln, a.bbox.br if ln.dir[0] >= 0 else a.bbox.tl))
    if not gaps:  # every pair separated by a synthetic space: letter-spaced line
        gaps = [_along(ln, b.origin) - _along(ln, a.origin) for a, b in zip(real, real[1:])]
        gaps = [g - real[0].run.size * 0.5 for g in gaps]
    gmed = statistics.median(gaps) if gaps else 0
    if gmed < 0.08 * ln.size:
        return
    keep = []
    for i, c in enumerate(ln.chars):
        if c.synthetic and c.c == " " and 0 < i < len(ln.chars) - 1:
            a, b = ln.chars[i - 1], ln.chars[i + 1]
            gap = _along(ln, b.origin) - _along(ln, a.origin) - (a.bbox.width if ln.dir[0] else a.bbox.height)
            if gap < gmed + 0.15 * ln.size:
                continue
        keep.append(c)
    ln.chars = keep


def _along(ln: Line, p) -> float:
    return p.x * ln.dir[0] + p.y * ln.dir[1]


def _across(ln: Line, p) -> float:
    return -p.x * ln.dir[1] + p.y * ln.dir[0]


# ---------------------------------------------------------------------------------------------- paragraphs
def group_paragraphs(lines: list[Line]) -> list[Para]:
    """Greedy grouping of lines into paragraphs: same direction, similar size, consistent baseline step,
    horizontally overlapping (so columns separate), not separated by a larger gap."""
    usable = [ln for ln in lines if ln.text.strip() and ln.glyphs]
    hz = [ln for ln in usable if ln.dir == (1.0, 0.0)]
    rest = [ln for ln in usable if ln.dir != (1.0, 0.0)]
    hz.sort(key=lambda ln: (ln.chars[0].origin.y, ln.bbox.x0))
    paras: list[list[Line]] = []
    for ln in hz:
        base = ln.chars[0].origin.y
        best = None
        for p in paras:
            last = p[-1]
            lb = last.chars[0].origin.y
            step = base - lb
            sz = max(ln.size, last.size)
            if abs(ln.size - last.size) > 0.15 * sz or not (0.8 * sz <= step <= 1.9 * sz):
                continue
            if len(p) >= 2:
                prev = p[-1].chars[0].origin.y - p[-2].chars[0].origin.y
                if abs(step - prev) > 0.12 * sz:
                    continue
            ov = min(ln.bbox.x1, last.bbox.x1) - max(ln.bbox.x0, last.bbox.x0)
            if ov < 0.3 * min(ln.bbox.width, last.bbox.width) or abs(ln.bbox.x0 - last.bbox.x0) > 4 * sz:
                continue
            best = p
            break
        if best is None:
            paras.append([ln])
        else:
            best.append(ln)
    out = []
    for p in paras:
        out.extend(_split_short_lines(p))
    return out + [Para([ln], "left") for ln in rest]


def _split_short_lines(p: list[Line]) -> list[Para]:
    """A justified/left paragraph ends at a line that stops well short of the right edge."""
    if len(p) < 3:
        return [Para(p, _alignment(p))]
    right = statistics.median(ln.bbox.x1 for ln in p)
    out, cur = [], []
    for ln in p:
        cur.append(ln)
        if ln is not p[-1] and right - ln.bbox.x1 > 3 * ln.size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [Para(q, _alignment(q)) for q in out]


def _alignment(p: list[Line]) -> str:
    if len(p) < 2:
        return "left"
    sz = p[0].size
    tol = 0.25 * sz
    lefts = [ln.bbox.x0 for ln in p]
    rights = [ln.bbox.x1 for ln in p[:-1]]
    centers = [(ln.bbox.x0 + ln.bbox.x1) / 2 for ln in p]
    l_ok = max(lefts[1:]) - min(lefts[1:]) < tol if len(p) > 2 else abs(lefts[0] - lefts[1]) < tol
    r_ok = max(rights) - min(rights) < tol if len(rights) > 1 else False
    if l_ok and r_ok:
        return "justify"
    if l_ok:
        return "left"
    if max(centers) - min(centers) < tol:
        return "center"
    if max(ln.bbox.x1 for ln in p) - min(ln.bbox.x1 for ln in p) < tol:
        return "right"
    return "left"


# ---------------------------------------------------------------------------------------------- fonts
_FONTS: OrderedDict = OrderedDict()  # ("file", path) | ("builtin", name) -> pymupdf.Font


def _font(key: tuple) -> pymupdf.Font:
    f = _FONTS.get(key)
    if f is None:
        f = pymupdf.Font(fontfile=key[1]) if key[0] == "file" else pymupdf.Font(key[1])
        _FONTS[key] = f
        if len(_FONTS) > 24:
            _FONTS.popitem(last=False)
    else:
        _FONTS.move_to_end(key)
    return f


def style_of(run: Run) -> tuple[str, bool, bool]:
    n = run.font.lower()
    mono = bool(run.flags & 8) or any(k in n for k in ("mono", "courier", "consol", "code"))
    serif = bool(run.flags & 4) or any(k in n for k in ("times", "serif", "roman", "georgia", "cmr", "garamond",
                                                        "minion", "cambria", "palatino", "book"))
    serif = serif and "sans" not in n
    bold = bool(run.flags & 16) or any(k in n for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(run.flags & 2) or any(k in n for k in ("italic", "oblique", "-it", "slant"))
    return ("mono" if mono else "serif" if serif else "sans"), bold, italic


GENERIC = {
    "sans": ["Arial", "Liberation Sans", "Arimo", "Helvetica", "DejaVu Sans"],
    "serif": ["Times New Roman", "Liberation Serif", "Tinos", "Times", "DejaVu Serif"],
    "mono": ["Courier New", "Liberation Mono", "Cousine", "Courier", "DejaVu Sans Mono"],
}
_BUILTIN = {"sans": ("helv", "hebo", "heit", "hebi"), "serif": ("tiro", "tibo", "tiit", "tibi"),
            "mono": ("cour", "cobo", "coit", "cobi")}
_SCRIPTS = [  # (predicate, system families, MuPDF built-in) for characters the style fonts lack
    (lambda cp: 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF,
     ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP", "Noto Sans JP", "Hiragino Sans"], "japan"),
    (lambda cp: 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F,
     ["Malgun Gothic", "Noto Sans CJK KR", "Noto Sans KR", "AppleGothic"], "korea"),
    (lambda cp: 0x2E80 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF or 0x20000 <= cp,
     ["Microsoft YaHei", "SimSun", "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Source Han Sans SC"],
     "china-s"),
    (lambda cp: True, ["Segoe UI Symbol", "Segoe UI", "DejaVu Sans", "Noto Sans Symbols", "Noto Sans Symbols2",
                       "Symbola", "Arial Unicode MS"], None),
]
_GENERIC_QT = {"sans": "Arial", "serif": "Times New Roman", "mono": "Courier New"}


def _index():
    try:
        from . import fonts
        return fonts.index()
    except Exception:  # noqa: BLE001
        return None


def _face_key(family: str, bold: bool, italic: bool) -> tuple | None:
    """("file", path) of an installed face of `family` that MuPDF can load (first face of a collection)."""
    ix = _index()
    if ix is None:
        return None
    try:
        face = ix.find(family, bold, italic)
        if face is not None and face["index"] != 0 and face["path"].lower().endswith((".ttc", ".otc")):
            face = next((f for f in ix.faces_of(family) if f["index"] == 0), None)
    except Exception:  # noqa: BLE001
        return None
    return ("file", face["path"]) if face else None


def _family_candidates(fontname: str) -> list[str]:
    base = re.sub(r"^[A-Z]{6}\+", "", fontname or "").replace("#20", " ")
    head = re.split(r"[-,]", base)[0]
    out = []
    for c in (base, head):
        c2 = re.sub(r"(PSMT|MT|PS|Std|Pro|LT|Regular|Roman|Bold|Italic|Oblique)+$", "", c).strip()
        for x in (c, c2):
            if x:
                out += [x, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", x)]
    return list(dict.fromkeys(out))


def qt_family(fontname: str, style: tuple) -> str:
    """A family Qt can show for a PDF font name: the installed family it names, else a generic one by style."""
    ix = _index()
    if ix is not None:
        try:
            for c in _family_candidates(fontname):
                faces = ix.faces_of(c)
                if faces:
                    return next((f for f in faces[0]["families"] if f.casefold() == c.casefold()), c)
        except Exception:  # noqa: BLE001
            pass
    return _GENERIC_QT[style[0]]


def _label(key: tuple) -> str:
    try:
        return _font(key).name
    except Exception:  # noqa: BLE001
        return key[1]


class Fallbacks:
    """Fallback fonts for characters the original font cannot show. Choosing one only loads the font; embedding
    happens in `embed` (at apply time): the needed glyphs go through a scratch document whose fonts are subset,
    then the subset font objects are grafted into the target document and named in the page resources."""

    def __init__(self):
        self._pick: dict[tuple, tuple | None] = {}

    def for_char(self, ch: str, run: Run, family: str | None) -> tuple | None:
        """-> (font key, gid, advance em) or None."""
        cp = ord(ch[0])
        style, bold, italic = style_of(run)
        sig = (cp, style, bold, italic, family)
        if sig in self._pick:
            return self._pick[sig]
        keys: list[tuple] = []
        if family:
            k = _face_key(family, bold, italic)
            if k:
                keys.append(k)
        for fam in GENERIC[style]:
            k = _face_key(fam, bold, italic)
            if k:
                keys.append(k)
                break
        keys.append(("builtin", _BUILTIN[style][(1 if bold else 0) + (2 if italic else 0)]))
        for pred, fams, builtin in _SCRIPTS:
            if not pred(cp):
                continue
            for fam in fams:
                k = _face_key(fam, False, False)
                if k:
                    keys.append(k)
            if builtin:
                keys.append(("builtin", builtin))
        res = None
        for k in dict.fromkeys(keys):
            try:
                f = _font(k)
                gid = f.has_glyph(cp)
            except Exception:  # noqa: BLE001
                continue
            if gid:
                res = (k, gid, f.glyph_advance(cp))
                break
        self._pick[sig] = res
        return res


_EMBEDS: OrderedDict = OrderedDict()  # (font key, gids) -> (scratch document, font xref there)


def _embed_source(key: tuple, glyphs: dict) -> tuple:
    """A scratch document holding `key` subset to `glyphs` ({gid: (text, advance em)}), with its own small
    ToUnicode and widths. Cached: the display copy replays the same edits often."""
    sig = (key, tuple(sorted((g, t) for g, (t, _) in glyphs.items())))
    hit = _EMBEDS.get(sig)
    if hit is not None:
        _EMBEDS.move_to_end(sig)
        return hit
    tmp = pymupdf.open()
    sp = tmp.new_page()
    if key[0] == "file":
        fx = sp.insert_font(fontname="F0", fontfile=key[1])
    else:
        fx = sp.insert_font(fontname="F0", fontbuffer=_font(key).buffer)
    gids = sorted(glyphs)
    cx = tmp.get_new_xref()
    tmp.update_object(cx, "<<>>")
    tmp.update_stream(cx, ("BT /F0 10 Tf <" + "".join(f"{g:04X}" for g in gids) + "> Tj ET").encode())
    tmp.xref_set_key(sp.xref, "Contents", f"{cx} 0 R")
    try:
        tmp.subset_fonts()
    except Exception:  # noqa: BLE001  (keeps the whole font: larger, still correct)
        pass
    # small ToUnicode (only these glyphs) and widths that match the advances the layout used
    lines = []
    for g in gids:
        t = glyphs[g][0]
        lines.append(f"<{g:04X}> <{t.encode('utf-16-be').hex().upper()}>")
    body = []
    for i in range(0, len(lines), 100):
        chunk = lines[i:i + 100]
        body.append(f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar")
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo\n<< /Registry (Adobe) "
            "/Ordering (UCS) /Supplement 0 >> def\n/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + "\n".join(body) +
            "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    tu = tmp.get_new_xref()
    tmp.update_object(tu, "<<>>")
    tmp.update_stream(tu, cmap.encode())
    tmp.xref_set_key(fx, "ToUnicode", f"{tu} 0 R")
    t, v = tmp.xref_get_key(fx, "DescendantFonts")
    if t == "xref":
        v = tmp.xref_object(int(v.split()[0]), compressed=True)
    mt = re.search(r"(\d+)\s+0\s+R", v or "")
    if mt:
        tmp.xref_set_key(int(mt.group(1)), "W", "[" + " ".join(f"{g} [{glyphs[g][1] * 1000:.3f}]" for g in gids) + "]")
    _EMBEDS[sig] = (tmp, fx)
    if len(_EMBEDS) > 16:
        _EMBEDS.popitem(last=False)
    return tmp, fx


def _graft(doc: pymupdf.Document, src: pymupdf.Document, xref: int) -> int:
    gm = m.pdf_new_graft_map(pymupdf._as_pdf_document(doc))
    obj = m.pdf_new_indirect(pymupdf._as_pdf_document(src), xref, 0)
    return m.pdf_to_num(m.pdf_graft_mapped_object(gm, obj))


# ---------------------------------------------------------------------------------------------- layout
@dataclass(eq=False)
class PGlyph:
    """A glyph to write: PDF-space matrix (incl. size), font, code, colour, render mode."""
    text: str
    m: pymupdf.Matrix
    font_xref: int | None  # original font object
    fb: tuple | None  # fallback font key (font_xref is None)
    code: int
    nbytes: int
    adv: float  # em
    color: int
    tr: int
    ca: float


@dataclass(eq=False)
class Plan:
    unit_glyphs: list[Glyph]  # to remove
    glyphs: list[PGlyph]  # to write
    warnings: list[str]
    bbox: pymupdf.Rect  # area the edit touches (page space)
    lines_out: int = 1


def _style_chars(old: list[Char], new_text: str) -> list[tuple[str, Char, int | None]]:
    """Map new text onto old chars: (char, style source, index of the unchanged old char or None)."""
    a = [c.c for c in old]
    b = list(new_text)
    out: list[tuple[str, Char, int | None]] = []
    # common prefix/suffix first: difflib is quadratic, and a typical edit touches one spot of a long paragraph
    pre = 0
    while pre < min(len(a), len(b)) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < min(len(a), len(b)) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1
    ops = [("equal", 0, pre, 0, pre)] if pre else []
    sm = difflib.SequenceMatcher(a=a[pre:len(a) - suf], b=b[pre:len(b) - suf], autojunk=False)
    ops += [(op, i1 + pre, i2 + pre, j1 + pre, j2 + pre) for op, i1, i2, j1, j2 in sm.get_opcodes()]
    if suf:
        ops.append(("equal", len(a) - suf, len(a), len(b) - suf, len(b)))
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            out += [(b[j1 + k], old[i1 + k], i1 + k) for k in range(i2 - i1)]
        elif op in ("replace", "insert"):
            for k in range(j2 - j1):
                if op == "replace":
                    src = old[min(i1 + k, i2 - 1)]
                else:
                    src = old[i1 - 1] if i1 > 0 else old[0]
                out.append((b[j1 + k], src, None))
    return out


def _spacing(ln: Line) -> tuple[float, float, float]:
    """Estimate extra letter spacing (Tc) and word spacing (Tw) in em, and the width of a space, for a line."""
    tcs, tws = [], []
    for a, b in zip(ln.chars, ln.chars[1:]):
        if a.glyph is None or b.glyph is None:
            continue
        ga, gb = a.glyph, b.glyph
        xa = pymupdf.Point(ga.m.a, ga.m.b)
        n2 = xa.x * xa.x + xa.y * xa.y
        if n2 <= 0:
            continue
        d = gb.origin - ga.origin
        u = (d.x * xa.x + d.y * xa.y) / n2 - ga.adv
        (tws if a.c == " " else tcs).append(u)
    tc = statistics.median(tcs) if tcs else 0.0
    tw = (statistics.median(tws) - tc) if tws else 0.0
    sp = None
    for c in ln.chars:  # synthetic spaces: width of the gap in em
        if c.synthetic and c.c == " " and c.bbox.width > 0:
            sp = c.bbox.width / max(c.run.size, 1e-6)
            break
    if sp is None:
        for c in ln.chars:
            if c.c == " " and c.glyph is not None:
                sp = c.glyph.adv
                break
    return tc, tw, sp if sp is not None else 0.25


class _Ctx:
    """Per-plan state: fallbacks and what they were used for (for the warning)."""

    def __init__(self, pt: PageText, fb: Fallbacks):
        self.pt, self.fb = pt, fb
        self.missing: dict[tuple, set] = {}  # (original font, fallback label) -> chars
        self.families: dict[str, str | None] = {}

    def family(self, run: Run) -> str | None:
        if run.font not in self.families:
            self.families[run.font] = None
            for c in _family_candidates(run.font):
                if _face_key(c, False, False):
                    self.families[run.font] = c
                    break
        return self.families[run.font]

    def encode(self, ch: str, src: Char, unchanged: bool = False) -> tuple:
        """-> (font_xref, fallback key, code, nbytes, adv_em). An `unchanged` character (same as in the original)
        must stay in its own font: if Leaf can't reuse that font, it refuses rather than silently changing the
        look of text the user didn't touch."""
        pt = self.pt
        codec = pt.codec_for(src.run, [src])
        seen = pt.seen.get(codec.xref, set()) if codec else set()
        if codec is not None and not codec.problems:
            code = codec.encode(ch, seen)
            if code is not None:
                return codec.xref, None, code, codec.nbytes, codec.advance(code, ch)
        if unchanged and not src.synthetic:
            name = re.sub(r"^[A-Z]{6}\+", "", src.run.font)
            why = "; ".join(codec.problems) if codec is not None and codec.problems else "it can’t be identified"
            raise Refuse(f"Leaf can’t reuse this text’s font ({name}: {why}), so editing would change the look "
                         "of the whole line.")
        r = self.fb.for_char(ch, src.run, self.family(src.run))
        if r is None:
            raise Refuse(f"No font on this computer can show “{ch}” (U+{ord(ch[0]):04X}).")
        key, gid, adv = r
        name = re.sub(r"^[A-Z]{6}\+", "", src.run.font)
        self.missing.setdefault((name, _label(key)), set()).add(ch)
        return None, key, gid, 2, adv

    def warnings(self) -> list[str]:
        return fallback_warnings(self.missing)


def fallback_warnings(missing: dict) -> list[str]:
    """User-facing notes for {(document font, fallback font label): chars} (characters drawn from another font)."""
    out = []
    for (orig, fbl), chars in missing.items():
        shown = "".join(sorted(chars))[:20]
        one = len(chars) == 1
        if fbl.split()[0].casefold() == re.split(r"[\s,-]", orig)[0].casefold():
            out.append(f"“{shown}” {'wasn’t' if one else 'weren’t'} embedded with the document’s font {orig}; "
                       f"Leaf adds {'it' if one else 'them'} from the installed {fbl}.")
        else:
            out.append(f"“{shown}” {'isn’t' if one else 'aren’t'} in the document’s font {orig}; Leaf uses "
                       f"{fbl} for {'it' if one else 'them'}.")
    return out


def _glyph_matrix(src: Char, pt: PageText) -> tuple[pymupdf.Matrix, int, float]:
    if src.glyph is not None:
        return pymupdf.Matrix(src.glyph.m), src.glyph.tr, src.glyph.ca
    # synthetic char: borrow the matrix of a real glyph of the same run
    for ln in pt.lines:
        for c in ln.chars:
            if c.run is src.run and c.glyph is not None:
                g = pymupdf.Matrix(c.glyph.m)
                o = src.origin * pt.to_pdf
                g.e, g.f = o.x, o.y
                return g, c.glyph.tr, c.glyph.ca
    s = src.run.size
    o = src.origin * pt.to_pdf
    return pymupdf.Matrix(s, 0, 0, s, o.x, o.y), 0, 1.0


def _complex(ch: str) -> bool:
    cp = ord(ch[0])
    return (unicodedata.bidirectional(ch[0]) in ("R", "AL") or 0x0900 <= cp <= 0x0DFF or 0x0E00 <= cp <= 0x0EFF
            or 0x1000 <= cp <= 0x109F or 0x1780 <= cp <= 0x17FF)


GARBLED = "This text has no reliable character mapping in the PDF, so it can’t be edited safely."
UNMATCHED = "Some characters of this text couldn’t be located in the page content."
INVISIBLE = ("This is invisible text (an OCR layer under a scan). Run Recognize text (OCR) again to change it "
             "instead.")
STROKED = "Outlined text can’t be edited (its outline style can’t be reproduced)."
CLIPPING = "This text is used as a clipping mask and can’t be edited."
COMPLEX = "Text in this script (Arabic, Hebrew, Indic, Thai, …) needs shaping Leaf can’t redo, so it can’t be edited."
VERTICAL = "Vertical writing can’t be edited."
OVERLAP = ("This overlaps text you already edited. Click that text to change it again, or undo that edit first.")
LOSSY = ("This page uses drawing settings Leaf can’t keep exactly when it rewrites the page (such as outlined text "
         "with its own line width), so its text can’t be edited.")


def check_unit(pt: PageText, lines: list[Line]) -> list[str]:
    """Reasons to refuse editing these lines at all (user-facing, most important first)."""
    why = []
    chars = [c for ln in lines for c in ln.chars]
    text = "".join(c.c for c in chars)
    if any(c.glyph is not None and c.glyph.tr == 3 for c in chars):
        why.append(INVISIBLE)
    if any(c == "�" or (ord(c) < 32 and c != "\t") or 0xE000 <= ord(c) <= 0xF8FF for c in text):
        why.append(GARBLED)
    if any(_complex(c) for c in text if c.strip()):
        why.append(COMPLEX)
    if any(ln.wmode for ln in lines):
        why.append(VERTICAL)
    if any(c.glyph is None and not c.synthetic for c in chars) or any(ln.orphans for ln in lines):
        why.append(UNMATCHED)
    if any(c.glyph is not None and c.glyph.tr in (1, 2, 5, 6) for c in chars):
        why.append(STROKED)
    elif any(c.glyph is not None and c.glyph.tr >= 4 for c in chars):
        why.append(CLIPPING)
    return why


def _check_new_text(text: str, kind: str) -> None:
    if kind == "line" and ("\n" in text or "\r" in text):
        raise Refuse("A line can’t contain line breaks; edit the paragraph instead.")
    if any(_complex(c) for c in text if c.strip()):
        raise Refuse(COMPLEX)
    if any(unicodedata.category(c) in ("Cc", "Cs", "Co") and c not in "\n\t" for c in text):
        raise Refuse("The new text contains characters that can’t be drawn.")


def _glyphs_bbox(glyphs: list[PGlyph], pt: PageText, start: pymupdf.Rect) -> pymupdf.Rect:
    """`start` joined with the page-space boxes of `glyphs` (em box: 0..adv along, -0.25..1 across)."""
    ta, tb, tc, td, te, tf = pt.page.transformation_matrix
    x0, y0, x1, y1 = start
    for g in glyphs:
        a, b, c, d, e, f = g.m
        for u, v in ((0, -0.25), (g.adv, -0.25), (0, 1.0), (g.adv, 1.0)):
            px, py = u * a + v * c + e, u * b + v * d + f  # glyph -> PDF space
            qx, qy = px * ta + py * tc + te, px * tb + py * td + tf  # -> page space
            x0, x1 = min(x0, qx), max(x1, qx)
            y0, y1 = min(y0, qy), max(y1, qy)
    return pymupdf.Rect(x0, y0, x1, y1)


def plan_line_edit(pt: PageText, ln: Line, new_text: str, ctx: _Ctx) -> Plan:
    why = check_unit(pt, [ln])
    if why:
        raise Refuse(why[0])
    _check_new_text(new_text, "line")
    new_text = new_text.replace("\t", " ")
    tc, tw, sp = _spacing(ln)
    styled = _style_chars(ln.chars, new_text) if new_text else []
    out: list[PGlyph] = []
    pen = None  # PDF-space point where the next glyph goes if it is not an unchanged one
    shift = pymupdf.Point(0, 0)
    last_old = -2
    for ch, src, oi in styled:
        mtx, tr, ca = _glyph_matrix(src, pt)
        xv = pymupdf.Point(mtx.a, mtx.b)  # one em along the baseline
        if oi is not None:
            orig = src.glyph.origin if src.glyph is not None else src.origin * pt.to_pdf
            if pen is not None and oi != last_old + 1:
                shift = pen - orig  # first unchanged char after an edit: continue where the edit ended
            o = orig + shift
            last_old = oi
        else:
            o = pen if pen is not None else (src.glyph.origin if src.glyph is not None else src.origin * pt.to_pdf)
            last_old = -2
        mtx.e, mtx.f = o.x, o.y
        if oi is not None and src.synthetic:  # unchanged synthetic space: a gap, no glyph
            pen = o + xv * (src.bbox.width / max(src.run.size, 1e-6))
            continue
        if ch == " " and oi is None:
            codec = pt.codec_for(src.run, [src])
            code = codec.encode(" ", pt.seen.get(codec.xref, set())) if codec and not codec.problems else None
            if code is None:  # no space glyph in the font (TeX fonts): a gap
                pen = o + xv * (sp + tc + tw)
                continue
        fx, fbk, code, nb, adv = ctx.encode(ch, src, unchanged=oi is not None and ch != " ")
        out.append(PGlyph(ch, mtx, fx, fbk, code, nb, adv, src.run.color, tr, ca))
        pen = o + xv * (adv + tc + (tw if ch == " " else 0))
    return Plan(ln.all_glyphs, out, [], _glyphs_bbox(out, pt, ln.bbox))


def plan_para_edit(pt: PageText, para: Para, new_text: str, ctx: _Ctx, keep_unchanged_lines: bool = True) -> Plan:
    """Greedy re-break of the paragraph within its original left/right edges and baselines (a newline in the new
    text forces a break). Output lines whose words equal the original line's words are left untouched (original
    glyphs and positions) when `keep_unchanged_lines`: re-laying them out would drift from the producer's own
    rounding."""
    why = check_unit(pt, para.lines)
    if why:
        raise Refuse(why[0])
    _check_new_text(new_text, "para")
    new_text = new_text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    chars: list[Char] = []
    for k, ln in enumerate(para.lines):
        lc = list(ln.chars)
        while lc and lc[-1].c == " ":
            lc.pop()
        while lc and lc[0].c == " " and k:
            lc.pop(0)
        if chars and lc:
            joiner = Char(" ", lc[0].origin, pymupdf.Rect(lc[0].bbox.x0, lc[0].bbox.y0, lc[0].bbox.x0, lc[0].bbox.y1),
                          True, lc[0].run)
            chars.append(joiner)
        chars += lc
    styled = _style_chars(chars, new_text) if new_text else []
    first = para.lines[0]
    tc, tw, sp = _spacing(first)
    items: list = []  # PGlyph | None (space) | "\n"
    for ch, src, oi in styled:
        if ch == " ":
            items.append(None)
            continue
        if ch == "\n":
            items.append("\n")
            continue
        fx, fbk, code, nb, adv = ctx.encode(ch, src, unchanged=oi is not None)
        mtx, tr, ca = _glyph_matrix(src, pt)
        items.append(PGlyph(ch, mtx, fx, fbk, code, nb, adv, src.run.color, tr, ca))
    words: list = []  # list of glyph lists, "\n" markers between
    cur: list = []
    for it in items:
        if it is None or it == "\n":
            if cur:
                words.append(cur)
                cur = []
            if it == "\n":
                words.append("\n")
        else:
            cur.append(it)
    if cur:
        words.append(cur)

    def scale(g):  # em -> pt along the baseline
        return abs(pymupdf.Point(g.m.a, g.m.b))

    def wwidth(w):
        return sum((g.adv + tc) * scale(g) for g in w) - tc * scale(w[-1])

    # natural inter-word space; measured Tw is a justification artefact in justified text
    space_pt = (sp + tc + (tw if para.align != "justify" else 0)) * first.size
    x0s = [ln.chars[0].origin.x for ln in para.lines]
    left = statistics.median(x0s[1:]) if len(x0s) > 1 else x0s[0]
    indent = x0s[0] - left
    right = max(ln.bbox.x1 for ln in para.lines)
    base = [ln.chars[0].origin.y for ln in para.lines]
    step = (base[-1] - base[0]) / (len(base) - 1) if len(base) > 1 else first.size * 1.2
    lines_out: list[list] = [[]]
    hard: set[int] = set()  # output lines ended by a newline (not justified)
    width = indent
    for w in words:
        if w == "\n":
            hard.add(len(lines_out) - 1)
            lines_out.append([])
            width = 0
            continue
        ww = wwidth(w)
        avail = right - left
        need = (width + ww) if not lines_out[-1] else width + space_pt + ww
        if lines_out[-1] and need > avail + 0.01:
            lines_out.append([w])
            width = ww
        else:
            lines_out[-1].append(w)
            width = need
    warnings: list[str] = []
    if len(lines_out) > len(para.lines):
        warnings.append(f"The paragraph now needs {len(lines_out)} lines instead of {len(para.lines)}; it extends "
                        "below its original area and may overlap what follows.")
    kept = set()
    if keep_unchanged_lines:
        for li, ws in enumerate(lines_out):
            if li < len(para.lines) and li not in hard and ws \
                    and ["".join(g.text for g in w) for w in ws] == para.lines[li].text.split() \
                    and all(g.fb is None for w in ws for g in w):
                kept.add(li)
    out: list[PGlyph] = []
    for li, ws in enumerate(lines_out):
        if li in kept or not ws:
            continue
        y = base[0] + li * step
        xl = left + (indent if li == 0 else 0)
        natural = sum(wwidth(w) for w in ws) + space_pt * (len(ws) - 1)
        gap = space_pt
        x = xl
        if para.align == "justify" and li < len(lines_out) - 1 and li not in hard and len(ws) > 1:
            gap = (right - xl - sum(wwidth(w) for w in ws)) / (len(ws) - 1)
        elif para.align == "center":
            x = xl + (right - xl - natural) / 2
        elif para.align == "right":
            x = right - natural
        for w in ws:
            for g in w:
                o = pymupdf.Point(x, y) * pt.to_pdf
                g.m = pymupdf.Matrix(g.m)
                g.m.e, g.m.f = o.x, o.y
                out.append(g)
                x += (g.adv + tc) * scale(g)
            x += gap - tc * scale(w[-1])
    glyphs = [g for li, ln in enumerate(para.lines) if li not in kept for g in ln.all_glyphs]
    return Plan(glyphs, out, warnings, _glyphs_bbox(out, pt, para.bbox), len(lines_out))


def plan_unit(pt: PageText, key: tuple, new_text: str, ctx: _Ctx) -> Plan:
    unit = pt.find(key)
    if key[0] == "para":
        return plan_para_edit(pt, unit, new_text, ctx)
    return plan_line_edit(pt, unit, new_text, ctx)


def plan_cached(pt: PageText, key: tuple, new_text: str, ctx: _Ctx) -> Plan:
    """plan_unit, memoized on `pt` by (key, text): check() plans an edit and the display replay plans it again
    (Find & replace checks hundreds of lines, then applies them). Plans are never modified after planning; the
    fallback fonts a plan used are merged into `ctx` so its warnings stay complete. Refusals aren't cached."""
    cache = pt.__dict__.setdefault("_plans", {})
    hit = cache.get((key, new_text))
    if hit is None:
        sub = _Ctx(pt, ctx.fb)
        sub.families = ctx.families
        plan = plan_unit(pt, key, new_text, sub)
        hit = (plan, {k: set(v) for k, v in sub.missing.items()})
        if len(cache) >= 2048:
            cache.clear()
        cache[(key, new_text)] = hit
    plan, missing = hit
    for k, v in missing.items():
        ctx.missing.setdefault(k, set()).update(v)
    return plan


@dataclass(eq=False)
class PagePlan:
    remove: list[Glyph]
    glyphs: list[PGlyph]
    warnings: list[str]
    boxes: dict  # key -> page-space Rect


def plan_page(pt: PageText, edits: dict, hidden: tuple | None = None) -> PagePlan:
    """All edits of one page planned against the same (unmodified) PageText, so they stay independent.
    `hidden`: a unit key whose glyphs are removed and not replaced (display only, while the editor is open)."""
    ctx = _Ctx(pt, Fallbacks())
    remove: list[Glyph] = []
    glyphs: list[PGlyph] = []
    warnings: list[str] = []
    boxes = {}
    for key, text in edits.items():
        if key == hidden:
            continue
        p = plan_cached(pt, key, text, ctx)
        remove += p.unit_glyphs
        glyphs += p.glyphs
        warnings += p.warnings
        boxes[key] = p.bbox
    if hidden is not None:
        remove += [g for ln in pt.find(hidden).lines for g in ln.all_glyphs]
    return PagePlan(remove, glyphs, warnings + ctx.warnings(), boxes)


# ---------------------------------------------------------------------------------------------- writing
def _own_resources(doc: pymupdf.Document, page_xref: int, cat: str) -> None:
    """Make the page's /Resources and its /`cat` subdictionary direct objects of this page, so adding a name never
    changes a dictionary that other pages share."""
    t, v = doc.xref_get_key(page_xref, "Resources")
    if t == "null":  # inherited from the page tree
        x = page_xref
        for _ in range(32):
            tp, vp = doc.xref_get_key(x, "Parent")
            if tp != "xref":
                break
            x = int(vp.split()[0])
            t2, v2 = doc.xref_get_key(x, "Resources")
            if t2 != "null":
                t, v = t2, v2
                break
        doc.xref_set_key(page_xref, "Resources", v if t == "dict" else
                         doc.xref_object(int(v.split()[0]), compressed=True) if t == "xref" else "<<>>")
    elif t == "xref":
        doc.xref_set_key(page_xref, "Resources", doc.xref_object(int(v.split()[0]), compressed=True))
    t, v = doc.xref_get_key(page_xref, f"Resources/{cat}")
    if t == "xref":
        doc.xref_set_key(page_xref, f"Resources/{cat}", doc.xref_object(int(v.split()[0]), compressed=True))


def write_glyphs(page: pymupdf.Page, glyphs: list[PGlyph], fb_xrefs: dict) -> None:
    doc = page.parent
    names: dict[int, str] = {}
    gstates: dict[float, str] = {}
    fonts_done = gs_done = False
    for g in glyphs:
        fx = g.font_xref if g.font_xref is not None else fb_xrefs[g.fb]
        if fx not in names:
            if not fonts_done:
                _own_resources(doc, page.xref, "Font")
                fonts_done = True
            names[fx] = f"LeafE{fx}"
            doc.xref_set_key(page.xref, f"Resources/Font/LeafE{fx}", f"{fx} 0 R")
        if g.ca < 0.999 and g.ca not in gstates:
            if not gs_done:
                _own_resources(doc, page.xref, "ExtGState")
                gs_done = True
            gx = doc.get_new_xref()
            doc.update_object(gx, f"<</Type/ExtGState/ca {g.ca:g}/CA {g.ca:g}>>")
            gstates[g.ca] = f"LeafA{gx}"
            doc.xref_set_key(page.xref, f"Resources/ExtGState/{gstates[g.ca]}", f"{gx} 0 R")

    def fid(g):
        return g.font_xref if g.font_xref is not None else fb_xrefs[g.fb]

    ops = ["q"]
    i = 0
    while i < len(glyphs):
        g = glyphs[i]
        font = names[fid(g)]
        r, gg, b = ((g.color >> 16) & 255) / 255, ((g.color >> 8) & 255) / 255, (g.color & 255) / 255
        col = f"{r:g} g" if r == gg == b else f"{r:g} {gg:g} {b:g} rg"
        ops.append(f"BT /{font} 1 Tf 0 Tc 0 Tw 100 Tz 0 Ts {g.tr} Tr {col}")
        if g.ca < 0.999:
            ops.append(f"/{gstates[g.ca]} gs")
        mt = g.m
        ops.append(f"{mt.a:.6f} {mt.b:.6f} {mt.c:.6f} {mt.d:.6f} {mt.e:.4f} {mt.f:.4f} Tm")
        tj = [f"<{g.code:0{g.nbytes * 2}X}>"]
        inv = ~pymupdf.Matrix(mt.a, mt.b, mt.c, mt.d, 0, 0)
        j = i + 1
        while j < len(glyphs):  # extend the run while font, colour, basis and baseline allow a TJ adjustment
            h, p = glyphs[j], glyphs[j - 1]
            if (fid(h), h.color, h.tr, h.ca) != (fid(p), p.color, p.tr, p.ca):
                break
            if any(abs(x - y) > 1e-4 for x, y in ((h.m.a, mt.a), (h.m.b, mt.b), (h.m.c, mt.c), (h.m.d, mt.d))):
                break
            d = (pymupdf.Point(h.m.e, h.m.f) - pymupdf.Point(p.m.e, p.m.f)) * inv  # text space displacement
            if abs(d.y) > 1e-3:
                break
            n = (p.adv - d.x) * 1000
            if abs(n) > 0.01:
                tj.append(f"{n:.2f}")
            tj.append(f"<{h.code:0{h.nbytes * 2}X}>")
            j += 1
        ops.append("[" + " ".join(tj) + "] TJ ET")
        i = j
    ops.append("Q")
    if not page.is_wrapped:
        page.wrap_contents()
    x = doc.get_new_xref()
    doc.update_object(x, "<<>>")
    doc.update_stream(x, ("\n".join(ops) + "\n").encode())
    t, v = doc.xref_get_key(page.xref, "Contents")
    if t == "array":
        doc.xref_set_key(page.xref, "Contents", v.rstrip().rstrip("]") + f" {x} 0 R]")
    elif t == "xref":
        doc.xref_set_key(page.xref, "Contents", f"[{v} {x} 0 R]")
    else:
        doc.xref_set_key(page.xref, "Contents", f"{x} 0 R")


def apply_plan(page: pymupdf.Page, plan: PagePlan) -> None:
    if plan.remove:
        n = remove_glyphs(page, plan.remove)
        if n < len(plan.remove):
            raise Refuse("Some of the original characters could not be removed from the page.")
    if not plan.glyphs:
        return
    used: dict[tuple, dict] = {}
    for g in plan.glyphs:
        if g.fb is not None:
            used.setdefault(g.fb, {})[g.code] = (g.text, g.adv)
    fb_xrefs = {}
    for key, gl in used.items():
        tmp, fx = _embed_source(key, gl)
        fb_xrefs[key] = _graft(page.parent, tmp, fx)
    write_glyphs(page, plan.glyphs, fb_xrefs)


def apply_edits(page: pymupdf.Page, edits: dict, hidden: tuple | None = None,
                pt: PageText | None = None) -> list[str]:
    """Replay text edits ({unit key: new text}) on `page`, a fresh copy of the source page they were made on.
    `pt` may be a PageText of an identical copy of that page (the display copy reuses its cached one).
    Raises Refuse if an edit cannot be applied. Returns warnings."""
    if not edits and hidden is None:
        return []
    plan = plan_page(pt if pt is not None else PageText(page), edits, hidden)
    apply_plan(page, plan)
    return plan.warnings


# ---------------------------------------------------------------------------------------------- public API
_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # Source -> {index: PageText}


def page_text(src, index: int) -> PageText:
    """The PageText of a source page (cached; sources never change, so neither does this)."""
    per = _CACHE.get(src)
    if per is None:
        per = _CACHE[src] = OrderedDict()
    pt = per.get(index)
    if pt is None:
        pt = per[index] = PageText(src.page(index))
        if len(per) > 32:
            per.popitem(last=False)
    else:
        per.move_to_end(index)
    return pt


def _edits(doc, ref) -> dict:
    return (getattr(doc, "text_edits", None) or {}).get(ref.uid, {})


def edited_box(pt: PageText, key: tuple, text: str) -> pymupdf.Rect:
    """Page-space area of a unit after its edit (original box joined with the new layout)."""
    b = pt.boxes.get((key, text))
    if b is None:
        unit = pt.find(key)
        try:
            b = plan_cached(pt, key, text, _Ctx(pt, Fallbacks())).bbox
        except Refuse:
            b = pymupdf.Rect(unit.bbox)
        b = b | unit.bbox
        pt.boxes[(key, text)] = b
    return b


def original_text(doc, ref, key: tuple) -> str:
    """The unit's text in the source (before any edit)."""
    return _unit_text(page_text(ref.src, ref.index).find(key))


def _unit_text(u) -> str:
    return (u.text if isinstance(u, Para) else u.text).rstrip()


def _hex(c: int) -> str:
    return f"#{(c >> 16) & 255:02x}{(c >> 8) & 255:02x}{c & 255:02x}"


def _make_unit(ref, pt: PageText, key: tuple, text: str | None) -> TextUnit:
    u = pt.find(key)
    lines = u.lines
    rot = pt.page.rotation_matrix
    first = next(c for c in lines[0].chars if c.glyph is not None)
    run = first.run
    style = style_of(run)
    real = [c for ln in lines for c in ln.chars if c.glyph is not None]
    size = statistics.median(c.run.size for c in real)
    box = pymupdf.Rect(u.bbox)
    if text is not None:
        box = edited_box(pt, key, text)
    v = pymupdf.Point(lines[0].dir) * pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
    o = first.origin * rot
    return TextUnit(
        page_uid=ref.uid, kind=key[0], key=key, text=_unit_text(u) if text is None else text,
        bbox=tuple((box * rot).normalize()), line_boxes=tuple(tuple((pymupdf.Rect(ln.bbox) * rot).normalize())
                                                             for ln in lines),
        baseline=(o.x, o.y), angle=round(math.degrees(math.atan2(v.y, v.x)), 3) % 360, size=round(size, 3),
        color=_hex(run.color), family=qt_family(run.font, style), bold=style[1], italic=style[2],
        align=u.align if isinstance(u, Para) else "left")


def unit_at(doc, ref, x: float, y: float, para: bool = False) -> TextUnit | None:
    """The line (or with `para`, the paragraph) under (x, y) in the source page.rect space, or None.

    An already edited unit (of either kind) is returned with its current text and the same key, so editing it
    again replaces that edit. Cheap after the first call per page (the PageText is cached)."""
    pt = page_text(ref.src, ref.index)
    p = pymupdf.Point(x, y) * pt.page.derotation_matrix
    edits = _edits(doc, ref)
    for key, text in edits.items():
        try:
            if p in edited_box(pt, key, text) + (-0.5, -0.5, 0.5, 0.5):
                return _make_unit(ref, pt, key, text)
        except Refuse:
            continue
    ln = pt.line_at(p)
    if ln is None:
        return None
    unit = pt.para_of(ln) if para else ln
    return _make_unit(ref, pt, unit_key(unit), None)


_SIGNED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def is_signed(src) -> bool:
    v = _SIGNED.get(src)
    if v is None:
        v = False
        d = src.doc
        try:
            if d.get_sigflags() >= 1:
                for page in d:
                    for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE]):
                        if d.xref_get_key(w.xref, "V")[0] != "null" or \
                                d.xref_get_key(w.xref, "Parent/V")[0] not in ("null",):
                            v = True
                            break
                    if v:
                        break
        except Exception:  # noqa: BLE001
            v = False
        _SIGNED[src] = v
    return v


def _page_warnings(pt: PageText, src) -> list[str]:
    out = []
    if is_signed(src):
        out.append("This document is digitally signed: changing its text makes the signature show the document "
                   "as modified.")
    if "_pw" not in pt.__dict__:
        pt.__dict__["_pw"] = _page_warnings_uncached(pt)
    return out + pt.__dict__["_pw"]


def _page_warnings_uncached(pt: PageText) -> list[str]:
    out = []
    d = pt.doc
    try:
        if d.xref_get_key(d.pdf_catalog(), "StructTreeRoot")[0] != "null":
            out.append("This PDF is tagged for accessibility; the edited text isn’t added to its structure, so "
                       "screen readers and reflow may read it out of order.")
    except Exception:  # noqa: BLE001
        pass
    try:
        content = pt.page.read_contents()
        res = d.xref_object(pt.page.xref, compressed=True)
        cs = d.xref_get_key(pt.page.xref, "Resources/ColorSpace")
        if re.search(rb"(?:[\d.]+\s+){4}k\b", content) or "/Separation" in res or "/DeviceN" in res or \
                (cs[0] != "null" and re.search(r"Separation|DeviceN|DeviceCMYK", d.xref_object(int(cs[1].split()[0]))
                                               if cs[0] == "xref" else cs[1])):
            out.append("This page uses CMYK or spot colors; edited text is written in RGB.")
    except Exception:  # noqa: BLE001
        pass
    return out


def check(doc, ref, unit: TextUnit, new_text: str | None = None, detail: dict | None = None) -> list[str]:
    """Raise Refuse if `unit` cannot be edited (or cannot take `new_text`); else return warnings for the user
    (fallback font used, paragraph grows past its lines, signed document, tagged structure, CMYK/spot colours).
    Only `unit.key` is used, so anything with a unit key works (Find & replace passes a `KeyUnit`).
    `detail` (optional dict) receives the same warnings by kind, for callers that aggregate many checks:
    "page" (document/page level), "unit" (this edit) and "fallback" ({(document font, fallback font): chars})."""
    pt = page_text(ref.src, ref.index)
    u = pt.find(unit.key)
    why = check_unit(pt, u.lines)
    if why:
        raise Refuse(why[0])
    if not pt.rewrite_is_lossless():
        raise Refuse(LOSSY)
    mine = frozenset(unit.key[1])
    for key in _edits(doc, ref):
        if key != unit.key and mine & frozenset(key[1]):
            raise Refuse(OVERLAP)
    if not _removable(pt, unit.key):
        raise Refuse(UNMATCHED)
    warnings: list[str] = []
    ctx = _Ctx(pt, Fallbacks())
    # without new text: a no-op plan, which refuses if the unit's own font can't be reused
    plan = plan_cached(pt, unit.key, _unit_text(u) if new_text is None else new_text, ctx)
    if new_text is not None:
        warnings += plan.warnings + ctx.warnings()
    page = _page_warnings(pt, ref.src)
    if detail is not None:
        detail["page"] = list(page)
        detail["unit"] = list(plan.warnings) if new_text is not None else []
        detail["fallback"] = {k: set(v) for k, v in ctx.missing.items()} if new_text is not None else {}
    return page + warnings


def _removable(pt: PageText, key: tuple) -> bool:
    """Whether the content filter meets every glyph of the unit (dry run, read-only; cached per unit)."""
    cache = pt.__dict__.setdefault("_removable", {})
    if key not in cache:
        left = _wanted([g for ln in pt.find(key).lines for g in ln.all_glyphs])

        def decide(g, adv, ucs, tr, ca):
            k = (round(g[4] / EPS), round(g[5] / EPS), ucs)
            if left.get(k):
                left[k] -= 1
            return False

        _run_filter(pt.page, decide, update=False, forms=True)
        cache[key] = not sum(left.values())
    return cache[key]


# ---------------------------------------------------------------------------------------------- find & replace
# The page's text as shown, unit by unit, so Find & replace (leaf/replace.py) matches what the user sees and turns
# each hit into an ordinary edit of the unit that holds it.
@dataclass(frozen=True)
class KeyUnit:
    """The minimum `check()` and commands.EditPageTexts need: a unit key."""
    key: tuple


@dataclass(frozen=True)
class Segment:
    """One unit of a page's text as shown: an edited unit (line or paragraph: its current text) or an unedited
    line (its own text, trailing spaces stripped). An edit of `key` replaces exactly this text."""
    key: tuple
    text: str
    edited: bool
    bbox: tuple  # source page.rect space
    line: object = field(default=None, compare=False, repr=False)  # the engine's Line (unedited segments)


def shown_segments(doc, ref) -> list[Segment]:
    """The page's editable text as shown, in content order (an edited unit sits where its first line was).
    Lines without located glyphs or without visible text are left out (nothing to edit there)."""
    pt = page_text(ref.src, ref.index)
    edits = _edits(doc, ref)
    rot = pt.page.rotation_matrix
    cover: dict[tuple, tuple] = {}
    for key in edits:
        for gk in key[1]:
            cover[gk] = key
    out: list[Segment] = []
    done: set = set()

    def edited(key) -> Segment:
        done.add(key)
        try:
            box = pymupdf.Rect(edited_box(pt, key, edits[key]))
        except Refuse:
            box = pymupdf.Rect()
        return Segment(key, edits[key], True, tuple((box * rot).normalize()))

    for ln in pt.lines:
        if not ln.glyphs or not ln.text.strip():
            continue
        ek = next((cover[k] for k in ln.keys if k in cover), None) if cover else None
        if ek is not None:
            if ek not in done:
                out.append(edited(ek))
            continue
        out.append(Segment(unit_key(ln), _unit_text(ln), False, tuple((pymupdf.Rect(ln.bbox) * rot).normalize()), ln))
    out += [edited(k) for k in edits if k not in done]  # (not expected: its glyphs are on the page)
    return out


def segment_rect(ref, seg: Segment, start: int, end: int) -> tuple | None:
    """page.rect-space box of characters [start, end) of an unedited segment's text; None for an edited one
    (its glyphs are new: search the shown page instead)."""
    ln = seg.line
    if ln is None or end <= start:
        return None
    rot = page_text(ref.src, ref.index).page.rotation_matrix
    r = None
    i = 0
    for c in ln.chars:
        n = len(c.c)
        if i + n > start and i < end:
            r = pymupdf.Rect(c.bbox) if r is None else r | c.bbox
        i += n
        if i >= end:
            break
    return tuple((r * rot).normalize()) if r is not None else None


def warm(doc, ref, keys) -> None:
    """Run check()'s per-unit content scan for many units of one page in one pass (Find & replace checks dozens of
    lines per page; one filter pass each would dominate). Results land in the same cache check() reads."""
    pt = page_text(ref.src, ref.index)
    cache = pt.__dict__.setdefault("_removable", {})
    left: dict[tuple, dict] = {}
    index: dict[tuple, list] = {}
    for key in dict.fromkeys(keys):
        if key in cache:
            continue
        try:
            unit = pt.find(key)
        except Refuse:
            continue
        w = left[key] = _wanted([g for ln in unit.lines for g in ln.all_glyphs])
        for wk in w:
            index.setdefault(wk, []).append(key)
    if not left:
        return

    def decide(g, adv, ucs, tr, ca):
        for key in index.get((round(g[4] / EPS), round(g[5] / EPS), ucs), ()):
            w = left[key]
            k = (round(g[4] / EPS), round(g[5] / EPS), ucs)
            if w[k]:
                w[k] -= 1
        return False

    _run_filter(pt.page, decide, update=False, forms=True)
    for key, w in left.items():
        cache[key] = not sum(w.values())
