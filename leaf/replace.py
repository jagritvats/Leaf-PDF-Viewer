"""Find & replace over the page's own text: the Qt-free core (the UI is leaf/features/replace.py).

Matching runs over the text as shown (doc.text_edits applied), unit by unit (textedit.shown_segments): every unedited
line, and every edited line or paragraph with its current text. The units of a page are joined with "\\n" in content
order, so a regex can see line starts/ends (^ and $ are per line) and a match that crosses units is found, counted
and skipped ("spans lines") instead of silently missed. A plain query matches any run of whitespace where it has
spaces; ligature characters (ﬁ, ﬂ, …) match their letters, as in the find bar.

Every replacement becomes an ordinary text edit of the unit that holds the hit (doc.text_edits[uid][key]): all hits
of one line make one edit, an already edited line or paragraph gets its current text with the hits replaced. Each
edit is checked with textedit.check(); refused units are skipped and reported with the reason and where they are.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

import pymupdf

from . import textedit as T
from .document import SEARCH_FLAGS

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


class PatternError(ValueError):
    """Invalid search pattern or replacement template; str(e) is meant for the user."""


# ----------------------------------------------------------------------------------------------------- patterns
def compile_query(query: str, case: bool = False, word: bool = False, regex: bool = False) -> re.Pattern:
    if not query:
        raise PatternError("Type something to find.")
    pat = query if regex else r"\s+".join(re.escape(p) for p in query.split()) or re.escape(query)
    if word:
        pat = rf"(?<!\w)(?:{pat})(?!\w)"
    try:
        return re.compile(pat, re.MULTILINE | (0 if case else re.IGNORECASE))
    except re.error as ex:
        raise PatternError(f"Invalid regular expression: {ex}") from None


def _template(replacement: str) -> str:
    """$1 / ${name} / $$ (as in most editors) in addition to Python's \\1 and \\g<name>."""
    out = re.sub(r"\$(?:(\d+)|\{(\w+)\}|(\$))", lambda m: "$" if m.group(3) else rf"\g<{m.group(1) or m.group(2)}>",
                 replacement)
    return out


def expand(m: re.Match | None, replacement: str, regex: bool) -> str:
    """The text that replaces match `m`: the replacement as typed, or with regex its groups filled in."""
    if not regex or m is None:
        return replacement
    try:
        return m.expand(_template(replacement))
    except (re.error, IndexError) as ex:
        raise PatternError(f"Invalid replacement: {ex}") from None


def check_template(rx: re.Pattern, replacement: str, regex: bool) -> None:
    """Raise PatternError for a replacement that can't work with this pattern (unknown group, bad escape)."""
    if not regex:
        return
    tpl = _template(replacement)
    try:
        compile_tpl = getattr(re, "_compile_template", None)  # validates group references (CPython 3.12)
        if compile_tpl is not None:
            compile_tpl(rx, tpl)
        else:
            for g in re.findall(r"\\(\d+)|\\g<(\w+)>", tpl):
                ref = g[0] or g[1]
                if (int(ref) > rx.groups) if ref.isdigit() else ref not in rx.groupindex:
                    raise re.error(f"invalid group reference {ref}")
    except (re.error, IndexError) as ex:
        raise PatternError(f"Invalid replacement: {ex}") from None


def normalize(text: str) -> tuple[str, list[int]]:
    """Text as matched (ligatures spelled out) and, per character of it, the index of its source character."""
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        rep = LIGATURES.get(ch, ch)
        out.append(rep)
        pos.extend([i] * len(rep))
    return "".join(out), pos


# ------------------------------------------------------------------------------------------------------- hits
@dataclass(eq=False)
class Hit:
    kind: str  # "text": inside one unit | "span": crosses units (not replaceable) | "ocr": recognized text
    seg: int  # index into the page's segments (first one for "span"); -1 for "ocr"
    start: int  # source-text offsets in segs[seg].text ("text" only)
    end: int
    match: re.Match | None
    rect: tuple | None  # source page.rect space
    lead: str = ""  # parts of a ligature the match cuts through, kept around the replacement
    trail: str = ""

    @property
    def order(self) -> tuple:
        return (0, self.seg, self.start)


def _edited_rect(doc, ref, seg: T.Segment, norm: str, ns: int, ne: int) -> tuple | None:
    """Where a hit inside an edited unit is drawn: its glyphs are new, so search the shown page within the unit."""
    if not seg.bbox or pymupdf.Rect(seg.bbox).is_empty:
        return None
    page = doc.text_page(ref)
    needle = norm[ns:ne]
    if "\n" in needle:
        return seg.bbox
    clip = (pymupdf.Rect(seg.bbox) * page.derotation_matrix) + (-2, -2, 2, 2)
    try:
        found = page.search_for(needle, clip=clip, flags=SEARCH_FLAGS)
    except Exception:  # noqa: BLE001
        found = []
    if not found:
        return seg.bbox
    k = norm[:ns].lower().count(needle.lower())
    r = found[k] if k < len(found) else found[-1]
    return tuple((r * page.rotation_matrix).normalize())


def page_hits(doc, ref, rx: re.Pattern, ocr_query: str | None = None) -> tuple[list[T.Segment], list[Hit]]:
    """Matches of `rx` on page `ref` as shown, in content order. Cheap for pages without a match (one text
    extraction of the shown page, no engine work)."""
    page = doc.text_page(ref)
    segs: list[T.Segment] = []
    hits: list[Hit] = []
    if rx.search(page.get_text("text", flags=SEARCH_FLAGS)):
        segs = T.shown_segments(doc, ref)
        norms = [normalize(s.text) for s in segs]
        starts: list[int] = []
        o = 0
        for n, _ in norms:
            starts.append(o)
            o += len(n) + 1
        joined = "\n".join(n for n, _ in norms)
        for m in rx.finditer(joined):
            s, e = m.span()
            if s == e:
                continue
            i = bisect_right(starts, s) - 1
            j = bisect_right(starts, e - 1) - 1
            n, pos = norms[i]
            ns, ne = s - starts[i], e - starts[i]
            if i != j or ne > len(n) or ns >= len(n):  # crosses a unit boundary (or takes the line break)
                rect = None
                for k in range(i, min(j, len(segs) - 1) + 1):
                    a = max(s - starts[k], 0)
                    b = min(e - starts[k], len(norms[k][0]))
                    pk = norms[k][1]
                    r = T.segment_rect(ref, segs[k], pk[a], pk[b - 1] + 1) if b > a else None
                    r = r or segs[k].bbox
                    if r:
                        rect = r if rect is None else tuple(pymupdf.Rect(rect) | r)
                hits.append(Hit("span", i, -1, -1, m, rect))
                continue
            rs, re_ = pos[ns], pos[ne - 1] + 1
            first = ns
            while first > 0 and pos[first - 1] == rs:
                first -= 1
            last = ne
            while last < len(n) and pos[last] == pos[ne - 1]:
                last += 1
            seg = segs[i]
            rect = T.segment_rect(ref, seg, rs, re_) if not seg.edited else _edited_rect(doc, ref, seg, n, ns, ne)
            hits.append(Hit("text", i, rs, re_, m, rect or seg.bbox, n[first:ns], n[ne:last]))
    if ocr_query and ref.src.ocr.get(ref.index):
        from .ocr import search_words

        for r in search_words(ref.src.ocr[ref.index], ocr_query):
            hits.append(Hit("ocr", -1, -1, -1, None, tuple(r)))
    return segs, hits


def substitute(text: str, hits: list[Hit], replacement: str, regex: bool) -> str:
    """`text` (one unit's current text) with these hits replaced."""
    out: list[str] = []
    cur = 0
    for h in sorted(hits, key=lambda h: h.start):
        if h.start < cur:  # (two matches inside one ligature)
            continue
        out.append(text[cur:h.start])
        out.append(h.lead + expand(h.match, replacement, regex) + h.trail)
        cur = h.end
    out.append(text[cur:])
    return "".join(out)


# --------------------------------------------------------------------------------------------- replace all
_REASONS = [  # engine refusal -> short label for the summary
    (T.COMPLEX, "unsupported script"),
    (T.GARBLED, "no reliable character mapping"),
    (T.INVISIBLE, "invisible (OCR) text"),
    (T.STROKED, "outlined text"),
    (T.CLIPPING, "clipping text"),
    (T.VERTICAL, "vertical writing"),
    (T.UNMATCHED, "characters not located"),
    (T.LOSSY, "page can’t be rewritten exactly"),
    (T.OVERLAP, "overlaps an edit"),
    ("line breaks", "replacement contains a line break"),
    ("No font on this computer", "no font for a character"),
    ("can’t reuse this text’s font", "font can’t be reused"),
    ("characters that can’t be drawn", "replacement has undrawable characters"),
]
SPANS = "match spans lines"
OCR = "recognized (OCR) text"


def short_reason(reason: str) -> str:
    for needle, label in _REASONS:
        if needle in reason:
            return label
    return "can’t be edited"


@dataclass(eq=False)
class Skip:
    pno: int  # 1-based page number as shown
    uid: int
    rect: tuple | None  # source page.rect space
    reason: str  # full reason (tooltip)
    label: str  # short reason
    hits: int
    excerpt: str


@dataclass(eq=False)
class PageResult:
    changes: list = field(default_factory=list)  # (ref, key, new text)
    replaced: int = 0
    lines: int = 0
    skipped: list = field(default_factory=list)
    page_warnings: list = field(default_factory=list)
    unit_warnings: list = field(default_factory=list)
    fallback: dict = field(default_factory=dict)


def _excerpt(text: str, start: int = 0, end: int = 0, width: int = 60) -> str:
    text = text.replace("\n", " ")
    if len(text) <= width:
        return text
    a = max(0, min(start - 15, len(text) - width))
    return ("…" if a else "") + text[a:a + width] + ("…" if a + width < len(text) else "")


def plan_page(doc, ref, pno: int, rx: re.Pattern, replacement: str, regex: bool,
              ocr_query: str | None = None) -> PageResult:
    """Everything Replace all would do on one page, checked but not applied."""
    res = PageResult()
    segs, hits = page_hits(doc, ref, rx, ocr_query)
    groups: dict[int, list[Hit]] = {}
    for h in hits:
        if h.kind == "text":
            groups.setdefault(h.seg, []).append(h)
        elif h.kind == "span":
            res.skipped.append(Skip(pno, ref.uid, h.rect, "The match runs across two lines; replace it line by line.",
                                    SPANS, 1, _excerpt(h.match.group(0) if h.match else "")))
        else:
            res.skipped.append(Skip(pno, ref.uid, h.rect, "Recognized (OCR) text isn’t part of the page yet; run "
                                    "OCR again with the corrected text instead.", OCR, 1, ""))
    if groups:
        T.warm(doc, ref, [segs[i].key for i in groups])
    for i, hs in groups.items():
        seg = segs[i]
        new = substitute(seg.text, hs, replacement, regex)
        if new == seg.text:
            res.replaced += len(hs)
            continue
        detail: dict = {}
        try:
            T.check(doc, ref, T.KeyUnit(seg.key), new, detail=detail)
        except T.Refuse as ex:
            rect = hs[0].rect or seg.bbox
            res.skipped.append(Skip(pno, ref.uid, rect, str(ex), short_reason(str(ex)), len(hs),
                                    _excerpt(seg.text, hs[0].start)))
            continue
        res.changes.append((ref, seg.key, new))
        res.replaced += len(hs)
        res.lines += 1
        res.page_warnings += [w for w in detail.get("page", []) if w not in res.page_warnings]
        res.unit_warnings += detail.get("unit", [])
        for k, chars in detail.get("fallback", {}).items():
            res.fallback.setdefault(k, set()).update(chars)
    return res


@dataclass(eq=False)
class Totals:
    """Replace all over many pages (and text boxes / form fields), for the summary."""
    changes: list = field(default_factory=list)
    replaced: int = 0
    lines: int = 0
    skipped: list = field(default_factory=list)
    page_warnings: dict = field(default_factory=dict)  # message -> pages
    unit_warnings: dict = field(default_factory=dict)
    fallback: dict = field(default_factory=dict)  # (font, fallback) -> chars
    fallback_pages: dict = field(default_factory=dict)  # (font, fallback) -> pages
    boxes: int = 0  # text boxes changed
    box_hits: int = 0
    fields: int = 0  # form fields changed
    field_hits: int = 0

    def add(self, pno: int, r: PageResult) -> None:
        self.changes += r.changes
        self.replaced += r.replaced
        self.lines += r.lines
        self.skipped += r.skipped
        for w in r.page_warnings:
            self.page_warnings.setdefault(w, []).append(pno)
        for w in r.unit_warnings:
            pages = self.unit_warnings.setdefault(w, [])
            if pno not in pages:
                pages.append(pno)
        for k, chars in r.fallback.items():
            self.fallback.setdefault(k, set()).update(chars)
            pages = self.fallback_pages.setdefault(k, [])
            if pno not in pages:
                pages.append(pno)

    def fail_page(self, uid: int, pno: int, reason: str) -> None:
        """A page whose edits the command couldn't apply after all (they were checked): count them as skipped."""
        mine = [c for c in self.changes if c[0].uid == uid]
        self.changes = [c for c in self.changes if c[0].uid != uid]
        self.lines -= len(mine)
        self.skipped.append(Skip(pno, uid, None, reason, short_reason(reason), len(mine), ""))

    @property
    def total(self) -> int:
        return self.replaced + self.box_hits + self.field_hits

    def warnings(self) -> list[str]:
        out = list(self.page_warnings)
        out += [f"{w} ({pages_text(p)})" for w, p in self.unit_warnings.items()]
        for k, chars in self.fallback.items():
            out += [f"{w} ({pages_text(self.fallback_pages.get(k, []))})" for w in T.fallback_warnings({k: chars})]
        return out

    def headline(self) -> str:
        if not self.total and not self.skipped:
            return "No matches."
        parts = []
        where = [f"{self.lines} line{'s' if self.lines != 1 else ''}"]
        if self.boxes:
            where.append(f"{self.boxes} text box{'es' if self.boxes != 1 else ''}")
        if self.fields:
            where.append(f"{self.fields} form field{'s' if self.fields != 1 else ''}")
        parts.append(f"Replaced {self.total} in {_join(where)}" if self.total else "Nothing replaced")
        if self.skipped:
            n = sum(1 for s in self.skipped)
            by: dict[str, list[int]] = {}
            for s in self.skipped:
                pages = by.setdefault(s.label, [])
                if s.pno not in pages:
                    pages.append(s.pno)
            why = ", ".join(f"{label} ({pages_text(p)})" for label, p in by.items())
            parts.append(f"{n} {'line' if n == 1 else 'lines'} skipped: {why}")
        return "; ".join(parts) + "."


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def pages_text(pages: list[int], most: int = 6) -> str:
    ps = sorted(set(pages))
    shown = ", ".join(str(p) for p in ps[:most]) + (f", … (+{len(ps) - most})" if len(ps) > most else "")
    return ("p. " if len(ps) == 1 else "pp. ") + shown
