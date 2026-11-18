"""Font families for text boxes.

Three PDF base fonts come first (Helvetica / Times / Courier: MuPDF draws them with its built-in, metric-compatible
Nimbus fonts). Every installed family can be used too; the saved PDF then embeds the real font file: `faces()` maps a
Qt family + bold/italic to a file, using an index of the OS font folders. The index is built lazily on first use by
reading the files' `name`/`OS/2` tables (no full font load) and cached as JSON in the app data folder, keyed by the
folders' modification times so installing a font refreshes it.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading

BASE = {  # key -> (label, Qt families for the screen, CSS family that MuPDF maps to its built-in font)
    "Helvetica": ("Sans (Helvetica)", ["Arial", "Helvetica", "Liberation Sans", "Nimbus Sans", "Arimo"], "Helvetica"),
    "Times": ("Serif (Times)", ["Times New Roman", "Times", "Liberation Serif", "Nimbus Roman", "Tinos"], "Times"),
    "Courier": ("Mono (Courier)", ["Courier New", "Courier", "Liberation Mono", "Nimbus Mono PS", "Cousine"], "Courier"),
}
DEFAULT = "Helvetica"
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")
CACHE_VERSION = 2


def is_base(key: str) -> bool:
    return key in BASE


def label(key: str) -> str:
    return BASE[key][0] if key in BASE else key


def qt_families(key: str) -> list[str]:
    return list(BASE[key][1]) if key in BASE else [key]


# ------------------------------------------------------------------ folders
def font_dirs() -> list[str]:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        win = os.environ.get("WINDIR", r"C:\Windows")
        dirs = [os.path.join(win, "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        dirs = ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    else:
        data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.join(data, "fonts"), os.path.join(home, ".fonts")]
    return [d for d in dirs if os.path.isdir(d)]


def _walk(dirs: list[str]) -> tuple[dict[str, float], list[str]]:
    """(folder -> mtime for every folder incl. subfolders, font files)."""
    stamps, files = {}, []
    for root in dirs:
        for d, _, names in os.walk(root):
            try:
                stamps[d] = os.stat(d).st_mtime
            except OSError:
                continue
            files.extend(os.path.join(d, n) for n in names if n.lower().endswith(FONT_EXTS))
    return stamps, files


# ------------------------------------------------------------- font tables
def _names(rd, off: int) -> dict[int, str]:
    _, count, soff = struct.unpack(">HHH", rd(off, 6))
    recs = rd(off + 6, 12 * count)
    best: dict[int, tuple[int, str]] = {}
    for i in range(count):
        pid, eid, lang, nid, ln, so = struct.unpack_from(">HHHHHH", recs, 12 * i)
        if nid not in (1, 2, 16, 17):
            continue
        if pid == 3 and eid in (0, 1, 10):
            rank, enc = (0 if lang == 0x409 else 1), "utf-16-be"
        elif pid == 0:
            rank, enc = 2, "utf-16-be"
        elif pid == 1 and eid == 0:
            rank, enc = 3, "latin-1"
        else:
            continue
        if nid not in best or rank < best[nid][0]:
            best[nid] = (rank, rd(off + soff + so, ln).decode(enc, "replace").strip("\x00 "))
    return {k: v[1] for k, v in best.items()}


def _face(rd, off: int) -> dict | None:
    num = struct.unpack(">H", rd(off + 4, 2))[0]
    recs = rd(off + 12, 16 * num)
    tables = {}
    for i in range(num):
        tag, _, toff, tlen = struct.unpack_from(">4sIII", recs, 16 * i)
        tables[tag] = (toff, tlen)
    if b"name" not in tables:
        return None
    names = _names(rd, tables[b"name"][0])
    fams = [n for n in (names.get(16), names.get(1)) if n]
    if not fams:
        return None
    weight, stretch, italic, serif, mono = 400, 5, False, None, False
    if b"OS/2" in tables:
        os2 = rd(tables[b"OS/2"][0], 64)
        weight, stretch = struct.unpack_from(">HH", os2, 4)
        cls = struct.unpack_from(">h", os2, 30)[0] >> 8
        panose = os2[32:42]
        sel = struct.unpack_from(">H", os2, 62)[0]
        italic = bool(sel & 0x201)
        if sel & 0x20:
            weight = max(weight, 700)
        if panose[0] == 2:
            mono = panose[3] == 9
            if 2 <= panose[1] <= 10:
                serif = True
            elif 11 <= panose[1] <= 15:
                serif = False
        if serif is None and cls:
            serif = 1 <= cls <= 7
    if b"head" in tables:
        style = struct.unpack(">H", rd(tables[b"head"][0] + 44, 2))[0]
        italic = italic or bool(style & 2)
        if style & 1:
            weight = max(weight, 700)
    if b"post" in tables:
        mono = mono or struct.unpack(">I", rd(tables[b"post"][0] + 12, 4))[0] != 0
    sub = (names.get(17) or names.get(2) or "").lower()
    italic = italic or "italic" in sub or "oblique" in sub
    return {"families": list(dict.fromkeys(fams)), "legacy": names.get(1) or fams[0], "weight": int(weight),
            "stretch": int(stretch) if 1 <= stretch <= 9 else 5, "italic": italic, "serif": bool(serif), "mono": bool(mono)}


def scan_file(path: str) -> list[dict]:
    """Faces in a font file: [{path, index, families, legacy, weight, stretch, italic, serif, mono}]; [] if unreadable.
    Reads only the table directory and the few small tables it needs."""
    try:
        with open(path, "rb") as f:
            def rd(off: int, n: int) -> bytes:
                f.seek(off)
                b = f.read(n)
                if len(b) != n:
                    raise ValueError("truncated font")
                return b

            head = rd(0, 12)
            offsets = [0]
            if head[:4] == b"ttcf":
                n = min(struct.unpack_from(">I", head, 8)[0], 64)
                offsets = list(struct.unpack(f">{n}I", rd(12, 4 * n)))
            elif head[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                return []
            out = []
            for i, off in enumerate(offsets):
                face = _face(rd, off)
                if face:
                    face.update(path=path, index=i)
                    out.append(face)
            return out
    except (OSError, struct.error, ValueError):
        return []


# ------------------------------------------------------------------- index
class FontIndex:
    def __init__(self, dirs: list[str] | None = None, cache_path: str | None = None):
        self.dirs = font_dirs() if dirs is None else dirs
        self.cache_path = cache_path
        self.faces: list[dict] = []
        self.by_family: dict[str, list[dict]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> "FontIndex":
        with self._lock:
            if not self._loaded:
                self._load()
                self._loaded = True
        return self

    def _load(self) -> None:
        stamps, files = _walk(self.dirs)
        key = {d: round(m, 3) for d, m in stamps.items()}
        cached = None
        if self.cache_path:
            try:
                with open(self.cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
            except (OSError, ValueError):
                cached = None
        if isinstance(cached, dict) and cached.get("version") == CACHE_VERSION and cached.get("dirs") == key:
            self.faces = cached.get("faces", [])
        else:
            self.faces = [face for p in files for face in scan_file(p)]
            if self.cache_path:
                try:
                    os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                    tmp = self.cache_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump({"version": CACHE_VERSION, "dirs": key, "faces": self.faces}, f)
                    os.replace(tmp, self.cache_path)
                except OSError:
                    pass
        self.by_family = {}
        for face in self.faces:
            for fam in face["families"]:
                self.by_family.setdefault(fam.casefold(), []).append(face)

    def faces_of(self, family: str) -> list[dict]:
        return self.load().by_family.get(family.casefold(), [])

    def find(self, family: str, bold: bool, italic: bool) -> dict | None:
        """The best face of `family` for bold/italic (exact style preferred), or None if the family isn't installed."""
        faces = self.faces_of(family)
        if not faces:
            return None
        want, fam = 700 if bold else 400, family.casefold()
        return min(faces, key=lambda f: (f["italic"] != italic, abs(f["weight"] - want), abs(f.get("stretch", 5) - 5),
                                         f.get("legacy", "").casefold() != fam, f["index"], f["path"].lower()))


def _cache_path() -> str | None:
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    except Exception:  # noqa: BLE001 - no Qt: no cache
        return None
    return os.path.join(base, "font-index.json") if base else None


_index: FontIndex | None = None


def index() -> FontIndex:
    global _index
    if _index is None:
        _index = FontIndex(cache_path=_cache_path())
    return _index


def set_index(ix: FontIndex | None) -> None:
    """Replace the shared index (tests)."""
    global _index
    _index = ix


def _usable(face: dict) -> bool:
    # MuPDF's @font-face loads the first face of a collection only
    return face["index"] == 0 or not face["path"].lower().endswith((".ttc", ".otc"))


def embeddable(key: str) -> bool:
    return is_base(key) or any(_usable(f) for f in index().faces_of(key))


_warned: set[str] = set()


def warn_unembeddable(win, delta: dict) -> None:
    """Tell the user once per family when a picked font can't go into the PDF (a base font stands in)."""
    key = delta.get("font") if isinstance(delta, dict) else None
    if not key or key in _warned or embeddable(key):
        return
    _warned.add(key)
    toast = getattr(win, "toast", None)
    if toast is not None:
        toast(f"No font file found for “{key}”: the saved PDF will use {label(fallback(key))} instead.")


def fallback(key: str) -> str:
    """The closest base font for a family that can't be embedded."""
    if key in BASE:
        return key
    faces = index().faces_of(key)
    if faces:
        f = faces[0]
        return "Courier" if f["mono"] else "Times" if f["serif"] else "Helvetica"
    try:
        from PySide6.QtGui import QFontDatabase

        if QFontDatabase.isFixedPitch(key):
            return "Courier"
    except Exception:  # noqa: BLE001
        pass
    low = key.lower()
    if "mono" in low or "courier" in low or "code" in low:
        return "Courier"
    if "serif" in low and "sans" not in low or any(w in low for w in ("times", "roman", "georgia", "garamond", "book")):
        return "Times"
    return "Helvetica"


class Embedder:
    """Collects the fonts one text box needs: CSS family names for runs, @font-face rules and a pymupdf.Archive."""

    def __init__(self):
        self.css: list[str] = []
        self.files: dict[tuple[str, int], str] = {}  # (path, index) -> archive name
        self.families: dict[str, str] = {}  # family key -> css family
        self.declared: set[tuple[str, bool, bool]] = set()
        self.archive = None
        self.missing: set[str] = set()

    def family(self, key: str, bold: bool, italic: bool) -> str:
        """CSS font-family for a run; registers the embedded face if needed."""
        if key in BASE:
            return BASE[key][2]
        face = index().find(key, bold, italic)
        if face is None or face["index"] != 0 and face["path"].lower().endswith((".ttc", ".otc")):
            # not installed, or a later face of a collection (MuPDF's @font-face loads the first face only)
            face = None if face is None else next((f for f in index().faces_of(key) if f["index"] == 0), None)
        if face is None:
            self.missing.add(key)
            return BASE[fallback(key)][2]
        css_fam = self.families.setdefault(key, f"leaf{len(self.families)}")
        sig = (key, bold, italic)
        if sig not in self.declared:
            self.declared.add(sig)
            fid = (face["path"], face["index"])
            name = self.files.get(fid)
            if name is None:
                try:
                    with open(face["path"], "rb") as f:
                        data = f.read()
                except OSError:
                    self.missing.add(key)
                    return BASE[fallback(key)][2]
                import pymupdf

                if self.archive is None:
                    self.archive = pymupdf.Archive()
                name = self.files[fid] = f"f{len(self.files)}{os.path.splitext(face['path'])[1].lower()}"
                self.archive.add(data, name)
            self.css.append(f"@font-face {{font-family: {css_fam}; src: url({name}); "
                            f"font-weight: {'bold' if bold else 'normal'}; font-style: {'italic' if italic else 'normal'};}}")
        return css_fam
