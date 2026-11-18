"""Compare two PDFs (Qt-free core): page alignment, word-level text diff, visual (pixel) diff.

`compare_documents(a, b)` is the pure entry point (bytes, a path or an open pymupdf.Document on each side) and
returns a list of `Change`s in reading order. The UI (features/compare.py) drives the same `Comparison` object in
small steps instead, so big documents stay interactive:

1. `Comparison.prepare()` is a generator: it extracts each page's words (and a tiny thumbnail for pages without
   text), yielding progress, then aligns the pages.
2. Alignment: identical pages anchor the match (difflib on page fingerprints); the stretches between anchors are
   aligned by text similarity (Jaccard on word sets, thumbnail likeness for textless pages) with a small dynamic
   program, so inserted and deleted pages show up as gaps instead of shifting every later page.
3. `Comparison.row_changes(row)` diffs one aligned row on demand (cached): words with positions through difflib
   (insert / delete / replace, rectangles merged per line), then a pixel diff of both pages rendered at the same
   scale for everything that isn't text (images, vector graphics, annotations), minus the areas the text diff
   already reports.

Coordinates are in each page's display space (`page.rect`, i.e. after its /Rotate), the space `get_pixmap` renders.
"""
from __future__ import annotations

import difflib
import hashlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field

import pymupdf

KINDS = {
    "insert": "Inserted text", "delete": "Deleted text", "replace": "Changed text",
    "page_insert": "Inserted page", "page_delete": "Deleted page", "visual": "Graphics changed",
    "page_size": "Page size changed",
}
MATCH_BONUS = 0.25  # a page pair must be at least this similar to be aligned rather than shown as delete + insert
DP_LIMIT = 250_000  # largest unanchored stretch (pages A x pages B) aligned by similarity; beyond: by position
VISUAL_DPI = 50
VISUAL_CELL = 6  # px at VISUAL_DPI
VISUAL_TOLERANCE = 40  # per-channel difference that counts as changed (ignores anti-aliasing noise)
THUMB = 24  # px, the long side of the fingerprint thumbnail of a textless page

Rect = tuple[float, float, float, float]


@dataclass
class Word:
    text: str
    rect: Rect


@dataclass
class Change:
    kind: str  # see KINDS
    row: int  # aligned row (page pair) index
    a_page: int | None  # 0-based page in A; None when the row has no page there
    b_page: int | None
    a_text: str = ""
    b_text: str = ""
    a_rects: list[Rect] = field(default_factory=list)
    b_rects: list[Rect] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, self.kind)

    def snippet(self, n: int = 80) -> str:
        if self.kind == "replace":
            s = f"{self.a_text} → {self.b_text}"
        else:
            s = self.b_text or self.a_text
        s = " ".join(s.split())
        return s if len(s) <= n else s[: n - 1] + "…"

    def page_label(self) -> str:
        a = "–" if self.a_page is None else str(self.a_page + 1)
        b = "–" if self.b_page is None else str(self.b_page + 1)
        return a if a == b else f"{a} → {b}"

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------------------------- text
def page_words(page: pymupdf.Page) -> list[Word]:
    """The page's words in a stable reading order: lines sorted top to bottom (lines sharing a baseline left to
    right), words left to right. Content-stream order isn't used on purpose: Leaf (and other editors) append
    edited text at the end of the stream, which would make an untouched line look moved."""
    m = page.rotation_matrix
    lines: dict[tuple[int, int], list[Word]] = {}
    for x0, y0, x1, y1, t, b, ln, _w in page.get_text("words", sort=False):
        r = pymupdf.Rect(x0, y0, x1, y1) * m
        lines.setdefault((b, ln), []).append(Word(t, (r.x0, r.y0, r.x1, r.y1)))
    if not lines:
        return []
    boxes = []
    for ws in lines.values():
        y0 = min(w.rect[1] for w in ws)
        y1 = max(w.rect[3] for w in ws)
        boxes.append(((y0 + y1) / 2, max(1.0, y1 - y0), min(w.rect[0] for w in ws), ws))
    boxes.sort(key=lambda t: (t[0], t[2]))
    rows: list[list] = []
    for yc, h, x0, ws in boxes:  # cluster lines whose centers are within half a line height
        if rows and abs(yc - rows[-1][0]) < 0.5 * min(h, rows[-1][1]):
            rows[-1][2].append((x0, ws))
        else:
            rows.append([yc, h, [(x0, ws)]])
    out: list[Word] = []
    for _yc, _h, items in rows:
        for _x0, ws in sorted(items, key=lambda t: t[0]):
            out.extend(sorted(ws, key=lambda w: w.rect[0]))
    return out


def merge_rects(rects: list[Rect], gap: float = 2.0) -> list[Rect]:
    """Join word rectangles that sit on the same line next to each other into one rectangle per line run."""
    out: list[list[float]] = []
    for x0, y0, x1, y1 in rects:
        if out:
            a = out[-1]
            h = min(y1 - y0, a[3] - a[1])
            overlap = min(y1, a[3]) - max(y0, a[1])
            if overlap > 0.5 * h and x0 >= a[0] - gap and x0 - a[2] < max(gap, 0.8 * h):
                a[0], a[1], a[2], a[3] = min(a[0], x0), min(a[1], y0), max(a[2], x1), max(a[3], y1)
                continue
        out.append([x0, y0, x1, y1])
    return [tuple(r) for r in out]


def text_changes(wa: list[Word], wb: list[Word], row: int, pa: int | None, pb: int | None) -> list[Change]:
    """Word-level diff of two pages' words (difflib on the word texts)."""
    ta, tb = [w.text for w in wa], [w.text for w in wb]
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    out = []
    for op, i0, i1, j0, j1 in sm.get_opcodes():
        if op == "equal":
            continue
        out.append(Change(op, row, pa, pb, " ".join(ta[i0:i1]), " ".join(tb[j0:j1]),
                          merge_rects([w.rect for w in wa[i0:i1]]), merge_rects([w.rect for w in wb[j0:j1]])))
    return out


# --------------------------------------------------------------------------------------------- visual
def _pix(page: pymupdf.Page, dpi: int) -> pymupdf.Pixmap:
    z = dpi / 72
    return page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csRGB, alpha=False, annots=True)


def visual_regions(pa: pymupdf.Page, pb: pymupdf.Page, dpi: int = VISUAL_DPI, cell: int = VISUAL_CELL,
                   tolerance: int = VISUAL_TOLERANCE) -> list[Rect]:
    """Rectangles (page.rect space, pt) where the two pages, rendered at the same scale and aligned at the top
    left, differ. Rows are compared as byte strings first, so identical areas cost next to nothing."""
    xa, xb = _pix(pa, dpi), _pix(pb, dpi)
    w, h = min(xa.width, xb.width), min(xa.height, xb.height)
    sa, sb, ra, rb, n = xa.samples, xb.samples, xa.stride, xb.stride, xa.n
    cells: set[tuple[int, int]] = set()
    span = cell * n
    for y in range(h):
        la, lb = sa[y * ra: y * ra + w * n], sb[y * rb: y * rb + w * n]
        if la == lb:
            continue
        cy = y // cell
        for x in range(0, w * n, span):
            ca, cb = la[x: x + span], lb[x: x + span]
            if ca != cb and (cy, x // span) not in cells and any(abs(p - q) > tolerance for p, q in zip(ca, cb)):
                cells.add((cy, x // span))
    z = dpi / 72
    out = [(c[0][1] * cell / z, c[0][0] * cell / z, (c[1][1] + 1) * cell / z, (c[1][0] + 1) * cell / z)
           for c in _components(cells)]
    # area one page has and the other doesn't (different sizes)
    if xa.width != xb.width or xa.height != xb.height:
        big = xa if xa.width * xa.height >= xb.width * xb.height else xb
        if big.width > w:
            out.append((w / z, 0.0, big.width / z, big.height / z))
        if big.height > h:
            out.append((0.0, h / z, big.width / z, big.height / z))
    return out


def _components(cells: set[tuple[int, int]]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Bounding boxes ((row0, col0), (row1, col1)) of 8-connected groups of cells; groups one cell apart join."""
    left = set(cells)
    boxes = []
    while left:
        start = left.pop()
        stack = [start]
        r0 = r1 = start[0]
        c0 = c1 = start[1]
        while stack:
            r, c = stack.pop()
            r0, r1, c0, c1 = min(r0, r), max(r1, r), min(c0, c), max(c1, c)
            for dr in (-2, -1, 0, 1, 2):
                for dc in (-2, -1, 0, 1, 2):
                    k = (r + dr, c + dc)
                    if k in left:
                        left.remove(k)
                        stack.append(k)
        boxes.append(((r0, c0), (r1, c1)))
    boxes.sort()
    return boxes


def _touches(r: Rect, others: list[Rect], pad: float = 3.0) -> bool:
    return any(r[0] - pad < o[2] and o[0] < r[2] + pad and r[1] - pad < o[3] and o[1] < r[3] + pad for o in others)


# ------------------------------------------------------------------------------------------ alignment
@dataclass
class PageInfo:
    words: list[Word]
    key: str  # identical pages share a key (text + thumbnail)
    vocab: frozenset
    thumb: bytes = b""


def page_info(page: pymupdf.Page) -> PageInfo:
    words = page_words(page)
    thumb = b""
    if len(words) < 3:  # textless page (scan, image, blank): a tiny gray thumbnail stands in for the text
        r = page.rect
        z = THUMB / max(r.width, r.height, 1)
        thumb = page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False).samples
    h = hashlib.blake2b(digest_size=16)
    h.update("\x00".join(w.text for w in words).encode("utf-8", "surrogatepass"))
    h.update(thumb)
    return PageInfo(words, h.hexdigest(), frozenset(w.text.lower() for w in words), thumb)


def similarity(a: PageInfo, b: PageInfo) -> float:
    if a.key == b.key:
        return 1.0
    if a.vocab or b.vocab:
        if not (a.vocab and b.vocab):
            return 0.0
        return len(a.vocab & b.vocab) / len(a.vocab | b.vocab)
    if a.thumb and len(a.thumb) == len(b.thumb):
        return 1.0 - sum(abs(p - q) for p, q in zip(a.thumb, b.thumb)) / (255 * len(a.thumb))
    return 0.3


def _dp(ia: list[PageInfo], ib: list[PageInfo]) -> list[tuple[int | None, int | None]]:
    """Needleman-Wunsch: maximize the summed (similarity - MATCH_BONUS) of matched pairs; gaps cost nothing."""
    n, m = len(ia), len(ib)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]  # 0 diag, 1 up (delete a), 2 left (insert b)
    for i in range(1, n + 1):
        move[i][0] = 1
    for j in range(1, m + 1):
        move[0][j] = 2
    for i in range(1, n + 1):
        si, pi, row = score[i], score[i - 1], move[i]
        for j in range(1, m + 1):
            d = pi[j - 1] + similarity(ia[i - 1], ib[j - 1]) - MATCH_BONUS
            u, lf = pi[j], si[j - 1]
            if d >= u and d >= lf:
                si[j], row[j] = d, 0
            elif u >= lf:
                si[j], row[j] = u, 1
            else:
                si[j], row[j] = lf, 2
    out: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i or j:
        mv = move[i][j]
        if mv == 0:
            out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif mv == 1:
            out.append((i - 1, None))
            i -= 1
        else:
            out.append((None, j - 1))
            j -= 1
    out.reverse()
    return out


def align(ia: list[PageInfo], ib: list[PageInfo]) -> list[tuple[int | None, int | None]]:
    """Rows (page in A or None, page in B or None) in order: identical pages anchor, the rest by similarity."""
    sm = difflib.SequenceMatcher(None, [p.key for p in ia], [p.key for p in ib], autojunk=False)
    rows: list[tuple[int | None, int | None]] = []
    i = j = 0
    for bi, bj, size in sm.get_matching_blocks():
        if bi > i or bj > j:
            sa, sb = ia[i:bi], ib[j:bj]
            if len(sa) * len(sb) <= DP_LIMIT:
                rows += [(None if x is None else x + i, None if y is None else y + j) for x, y in _dp(sa, sb)]
            else:  # too big to align by similarity: pair by position
                k = min(len(sa), len(sb))
                rows += [(i + t, j + t) for t in range(k)]
                rows += [(i + t, None) for t in range(k, len(sa))] + [(None, j + t) for t in range(k, len(sb))]
        rows += [(bi + t, bj + t) for t in range(size)]
        i, j = bi + size, bj + size
    return rows


# ----------------------------------------------------------------------------------------- comparison
def _open(x) -> tuple[pymupdf.Document, bool]:
    if isinstance(x, pymupdf.Document):
        return x, False
    if isinstance(x, (bytes, bytearray, memoryview)):
        return pymupdf.open(stream=bytes(x), filetype="pdf"), True
    return pymupdf.open(x), True


class Comparison:
    """A comparison of documents A (old) and B (new), computed in steps. The documents stay owned by the caller
    unless they were opened here (close() closes those)."""

    def __init__(self, a, b, visual: bool = True):
        self.a, self._own_a = _open(a)
        self.b, self._own_b = _open(b)
        self.visual = visual
        self.info_a: list[PageInfo] = []
        self.info_b: list[PageInfo] = []
        self.rows: list[tuple[int | None, int | None]] = []
        self.ready = False
        self._cache: dict[int, list[Change]] = {}

    def close(self) -> None:
        for d, own in ((self.a, self._own_a), (self.b, self._own_b)):
            if own and not d.is_closed:
                d.close()

    def prepare(self) -> Iterator[tuple[int, int]]:
        """Extract page fingerprints (yields (done, total) after each page) and align the pages."""
        total = self.a.page_count + self.b.page_count
        done = 0
        for d, info in ((self.a, self.info_a), (self.b, self.info_b)):
            for i in range(len(info), d.page_count):
                info.append(page_info(d[i]))
                done += 1
                yield done, total
        self.rows = align(self.info_a, self.info_b)
        self.ready = True

    def run_prepare(self) -> None:
        for _ in self.prepare():
            pass

    def computed(self, row: int) -> bool:
        return row in self._cache

    def row_changes(self, row: int) -> list[Change]:
        hit = self._cache.get(row)
        if hit is not None:
            return hit
        i, j = self.rows[row]
        out: list[Change] = []
        if j is None:
            r = self.a[i].rect
            out.append(Change("page_delete", row, i, None, a_text=" ".join(w.text for w in self.info_a[i].words[:40]),
                              a_rects=[(r.x0, r.y0, r.x1, r.y1)]))
        elif i is None:
            r = self.b[j].rect
            out.append(Change("page_insert", row, None, j, b_text=" ".join(w.text for w in self.info_b[j].words[:40]),
                              b_rects=[(r.x0, r.y0, r.x1, r.y1)]))
        elif self.info_a[i].key != self.info_b[j].key or self.visual:
            pa, pb = self.a[i], self.b[j]
            out = text_changes(self.info_a[i].words, self.info_b[j].words, row, i, j)
            ra, rb = pa.rect, pb.rect
            if abs(ra.width - rb.width) > 0.5 or abs(ra.height - rb.height) > 0.5:
                out.append(Change("page_size", row, i, j, f"{ra.width:.0f} × {ra.height:.0f} pt",
                                  f"{rb.width:.0f} × {rb.height:.0f} pt"))
            if self.visual:
                texty = [r for c in out for r in c.a_rects + c.b_rects]
                for r in visual_regions(pa, pb):
                    if not _touches(r, texty):
                        out.append(Change("visual", row, i, j, a_rects=[r], b_rects=[r]))
        self._cache[row] = out
        return out

    def changes(self) -> list[Change]:
        if not self.ready:
            self.run_prepare()
        return [c for row in range(len(self.rows)) for c in self.row_changes(row)]


def compare_documents(a, b, visual: bool = True) -> list[Change]:
    """All changes from A to B (bytes, path or pymupdf.Document each), in row order. Pure: no Qt, no state."""
    cmp = Comparison(a, b, visual)
    try:
        return cmp.changes()
    finally:
        cmp.close()


def summarize(changes: list[Change]) -> dict[str, int]:
    """Counts per kind plus words inserted/deleted (a replace counts both)."""
    c = Counter(ch.kind for ch in changes)
    out = {k: c.get(k, 0) for k in KINDS}
    out["words_inserted"] = sum(len(ch.b_text.split()) for ch in changes if ch.kind in ("insert", "replace"))
    out["words_deleted"] = sum(len(ch.a_text.split()) for ch in changes if ch.kind in ("delete", "replace"))
    out["total"] = len(changes)
    return out


def summary_text(changes: list[Change]) -> str:
    s = summarize(changes)
    if not s["total"]:
        return "No differences"
    parts = []
    text = s["insert"] + s["delete"] + s["replace"]
    if text:
        parts.append(f"{text} text change{'s' if text != 1 else ''} (+{s['words_inserted']} / −{s['words_deleted']} words)")
    if s["page_insert"]:
        parts.append(f"{s['page_insert']} page{'s' if s['page_insert'] != 1 else ''} inserted")
    if s["page_delete"]:
        parts.append(f"{s['page_delete']} page{'s' if s['page_delete'] != 1 else ''} deleted")
    if s["visual"]:
        parts.append(f"{s['visual']} graphics change{'s' if s['visual'] != 1 else ''}")
    if s["page_size"]:
        parts.append(f"{s['page_size']} page size change{'s' if s['page_size'] != 1 else ''}")
    return " · ".join(parts)
