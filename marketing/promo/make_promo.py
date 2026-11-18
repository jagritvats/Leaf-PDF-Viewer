"""Render Leaf's promo images (store hero, tiles, social cards, posters, banner, feature cards).

Pure Qt: every image is painted with QPainter onto a QImage; posters also get a PDF (the PNG placed on an
A3/A4 page with PyMuPDF). The logo and the small feature icons come from leaf/icons.py, so they always
match the app.

    python marketing/promo/make_promo.py SCREENS [--out DIR] [--only PATTERN] [--themes dark,light]

SCREENS is a folder with dark/ and light/ subfolders of real app screenshots named NN-scene-WxH.png
(e.g. 01-hero-2400x1600.png, 02-edit-text-2400x1600.png, 02-edit-text-detail-1200x500.png). A folder
without those subfolders is used for both themes. Scenes are matched by keyword (see ROLES); a missing
light scene falls back to the dark one. --map roles.json pins roles to files: {"hero": "path.png", ...}.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush, QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter,
    QPainterPath, QPen, QRadialGradient,
)

from leaf.icons import STROKE, _draw as draw_icon, _leaf_path  # noqa: E402

# --------------------------------------------------------------------------------------------- copy
# Only verified claims (see marketing/out/promo/COPY.md for the rules).
HEADLINE = "Edit PDFs.\nKeep them yours."
SUBLINE = "A native PDF editor for Windows and Linux.\nWorks offline, so nothing leaves your computer."
PLATFORMS = "Windows  \u00b7  Linux"
OFFLINE = "Works offline. Nothing leaves your computer."

CALLOUTS = [  # (icon, label) - at most four per piece
    ("edittext", "Edit existing text"),
    ("redact", "True redaction"),
    ("form_field", "Fill & sign forms"),
    ("find", "OCR built in"),
]

GRID = [  # poster feature grid: (icon, title, one line)
    ("edittext", "Edit existing text", "Change words in place, in the document's own font."),
    ("redact", "True redaction", "Removes what's under the mark, not just covers it."),
    ("form_field", "Fill & sign forms", "Type into fields, tick boxes, add your signature."),
    ("signature", "Digital signatures", "PAdES signatures with your certificate, checked offline."),
    ("find", "OCR built in", "Scans become searchable. English included, more on request."),
    ("organizer", "Reorder, merge, crop", "Arrange, combine, split and crop pages."),
]

FEATURES = [  # feature cards: (slug, role, icon, eyebrow, headline, subline)
    ("edit-text", "edit-text", "edittext", "Edit text", "Edit the text that's already there.",
     "Click a line and type. Leaf rewrites it in the document's own font, size and color."),
    ("redaction", "redact", "redact", "Redaction", "Redaction that really removes.",
     "Text, images and drawings under each mark are deleted from the saved file, not just hidden "
     "behind a box."),
    ("fill-sign", "forms", "form_field", "Forms", "Fill and sign forms.",
     "Type into fields, tick boxes and pick options, then place a signature you drew, typed or imported."),
    ("digital-signatures", "signatures", "signature", "Digital signatures", "Sign with your own certificate.",
     "PAdES signatures from your .p12/.pfx file or a certificate made in Leaf, verified offline."),
    ("ocr", "ocr", "find", "OCR", "Make scanned pages searchable.",
     "Text recognition is built in and runs on your computer. English is included; more languages "
     "can be downloaded."),
    ("pages", "pages", "organizer", "Pages", "Reorder, merge, split, crop.",
     "Drag pages into order, bring in pages from other files, split documents apart and crop margins."),
]

# ------------------------------------------------------------------------------------------- theme
GREEN_TOP, GREEN_BOTTOM, GREEN_VEIN = QColor("#43c463"), QColor("#1f8a3b"), QColor("#2a9a48")


@dataclass
class Theme:
    name: str
    bg: QColor
    bg2: QColor
    text: QColor
    muted: QColor
    accent: QColor  # accent used for text/icons (passes AA on bg)
    glow: QColor
    surface: QColor
    hairline: QColor
    frame: QColor  # window border
    shadow: QColor
    tile: QColor  # icon tile fill


THEMES = {
    "dark": Theme("dark", QColor("#0d110f"), QColor("#121915"), QColor("#eef3ef"), QColor("#a5b1a9"),
                  QColor("#5fd27f"), QColor(67, 196, 99, 46), QColor("#141b17"), QColor(255, 255, 255, 22),
                  QColor(255, 255, 255, 34), QColor(0, 0, 0, 200), QColor(95, 210, 127, 30)),
    "light": Theme("light", QColor("#f7f6f1"), QColor("#eeede5"), QColor("#131b16"), QColor("#4a5650"),
                   QColor("#1b7334"), QColor(67, 196, 99, 40), QColor("#ffffff"), QColor(20, 30, 24, 26),
                   QColor(20, 30, 24, 40), QColor(16, 32, 22, 110), QColor(31, 138, 59, 26)),
}


def _lum(c: QColor) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check_contrast() -> list[str]:
    out = []
    for t in THEMES.values():
        for fg in ("text", "muted", "accent"):
            for bg in ("bg", "bg2", "surface"):
                r = contrast(getattr(t, fg), getattr(t, bg))
                out.append(f"{t.name}: {fg} on {bg} = {r:.1f}:1")
                assert r >= 4.5, out[-1]
    return out


# ------------------------------------------------------------------------------------------- fonts
FONT_FILES = ["SegUIVar.ttf", "segoeui.ttf", "seguisb.ttf", "segoeuib.ttf", "segoeuisl.ttf", "bahnschrift.ttf"]
FONT_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/mnt/c/Windows/Fonts")]
_VAR = False
_NOISE: QImage | None = None


def load_fonts() -> None:
    global _VAR
    for d in FONT_DIRS:
        for f in FONT_FILES:
            if (d / f).exists():
                QFontDatabase.addApplicationFont(str(d / f))
    _VAR = "Segoe UI Variable" in QFontDatabase.families()
    if not _VAR and "Segoe UI" not in QFontDatabase.families():
        print("warning: Segoe UI not found; using the default sans-serif", file=sys.stderr)


def font(px: float, weight: str = "regular", optical: str = "display", track: float = 0.0) -> QFont:
    """weight: light/semilight/regular/semibold/bold; optical: display (big) or text (small sizes)."""
    if _VAR:
        style = f"{weight.capitalize()} {optical.capitalize()}"
        if style == "Regular Text":
            style = "Regular"
        f = QFontDatabase.font("Segoe UI Variable", style, 12)
    else:
        f = QFont("Segoe UI")
        f.setWeight({"light": QFont.Weight.Light, "semilight": QFont.Weight.Light, "regular": QFont.Weight.Normal,
                     "semibold": QFont.Weight.DemiBold, "bold": QFont.Weight.Bold}[weight])
    f.setPixelSize(max(1, round(px)))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if track:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, track * px)
    return f


# ----------------------------------------------------------------------------------------- screens
ROLES = {
    "hero": ["hero", "overview", "main", "document", "doc", "annot", "edit"],
    "edit-text": ["edit-text", "edittext", "edit_text", "text-edit", "editing", "edit"],
    "redact": ["redact"],
    "forms": ["fill", "form"],
    "signatures": ["digital", "signature", "signed", "sign"],
    "ocr": ["ocr", "recogn", "scan"],
    "pages": ["organiz", "reorder", "pages", "merge", "page", "crop"],
    "welcome": ["welcome", "start"],
}
DETAIL = re.compile(r"(detail|zoom|closeup|close-up|inset)", re.I)
NAME = re.compile(r"^(?:\d+[-_])?(?P<scene>.+?)(?:[-_]\d+x\d+)?$")


class Screens:
    def __init__(self, root: Path, mapping: dict | None = None):
        self.dirs = {}
        for theme in THEMES:
            d = root / theme
            self.dirs[theme] = d if d.is_dir() else root
        self.mapping = mapping or {}
        self._cache: dict[str, QImage] = {}
        self.used: dict[tuple, str] = {}

    def _files(self, theme: str) -> list[Path]:
        d = self.dirs[theme]
        return sorted(p for p in d.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))

    def _find(self, theme: str, role: str, detail: bool) -> Path | None:
        key = role + ("-detail" if detail else "")
        if key in self.mapping:
            m = self.mapping[key]
            m = m.get(theme) if isinstance(m, dict) else m
            if m:
                return Path(m)
        files = [(NAME.match(p.stem).group("scene").lower(), p) for p in self._files(theme)]
        files = [(s, p) for s, p in files if bool(DETAIL.search(s)) == detail]
        for kw in ROLES[role]:
            hits = [p for s, p in files if kw in s]
            if hits:
                return max(hits, key=lambda p: self._load(p).width() * self._load(p).height())
        return None

    def _load(self, path: Path) -> QImage:
        k = str(path)
        if k not in self._cache:
            img = QImage(k)
            if img.isNull():
                raise SystemExit(f"cannot read {path}")
            self._cache[k] = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        return self._cache[k]

    def exact(self, theme: str, role: str) -> QImage | None:
        """Only this theme's own screenshot for the role (no fallbacks)."""
        p = self._find(theme, role, False)
        if p:
            self.used[(theme, role)] = p.name
        return self._load(p) if p else None

    def focus(self, role: str) -> tuple[float, float, float] | None:
        """Optional zoomed crop for a feature card, from --map: {"redact": {"focus": [0.4, 0.3], "zoom": 1.6}}."""
        m = self.mapping.get(role)
        if isinstance(m, dict) and "focus" in m:
            fx, fy = m["focus"]
            return float(fx), float(fy), float(m.get("zoom", 1.5))
        return None

    def get(self, theme: str, role: str, detail: bool = False) -> QImage | None:
        order = [theme] + [t for t in THEMES if t != theme]
        for t in order:
            p = self._find(t, role, detail)
            if p:
                self.used[(theme, role + ("-detail" if detail else ""))] = p.name + ("" if t == theme else f" ({t})")
                return self._load(p)
        if detail:
            return None
        if role != "hero":
            return self.get(theme, "hero")
        files = self._files(theme) or self._files("dark")
        if not files:
            raise SystemExit(f"no screenshots in {self.dirs[theme]}")
        return self._load(files[0])


# ------------------------------------------------------------------------------------------ canvas
class Canvas:
    def __init__(self, w: float, h: float, theme: Theme, scale: float = 1.0, safe: float = 0.0):
        self.W, self.H, self.t, self.k = w, h, theme, scale
        self.img = QImage(round(w * scale), round(h * scale), QImage.Format.Format_ARGB32_Premultiplied)
        self.img.fill(theme.bg)
        self.p = QPainter(self.img)
        for hint in (QPainter.RenderHint.Antialiasing, QPainter.RenderHint.TextAntialiasing,
                     QPainter.RenderHint.SmoothPixmapTransform):
            self.p.setRenderHint(hint)
        self.p.scale(scale, scale)
        sx, sy = (w * safe, h * safe) if safe < 1 else (safe, safe)
        self.safe = QRectF(sx, sy, w - 2 * sx, h - 2 * sy)
        self.texts: list[tuple[QRectF, str]] = []

    def finish(self) -> QImage:
        self.p.end()
        return self.img

    def problems(self) -> list[str]:
        bad = []
        for r, s in self.texts:
            if not self.safe.adjusted(-0.5, -0.5, 0.5, 0.5).contains(r):
                bad.append(f"text outside safe area: {s[:40]!r} {r.getRect()}")
        return bad

    # ---- background
    def background(self, glows=(), motif: tuple | None = None) -> None:
        p, t = self.p, self.t
        g = QLinearGradient(0, 0, self.W, self.H)
        g.setColorAt(0, t.bg2 if t.name == "dark" else t.bg)
        g.setColorAt(1, t.bg if t.name == "dark" else t.bg2)
        p.fillRect(QRectF(0, 0, self.W, self.H), g)
        for cx, cy, r, strength in glows:
            rg = QRadialGradient(QPointF(cx, cy), r)
            c = QColor(t.glow)
            c.setAlphaF(min(1.0, c.alphaF() * strength))
            rg.setColorAt(0, c)
            c2 = QColor(c)
            c2.setAlphaF(c.alphaF() * 0.35)
            rg.setColorAt(0.45, c2)
            c3 = QColor(c)
            c3.setAlpha(0)
            rg.setColorAt(1, c3)
            p.fillRect(QRectF(0, 0, self.W, self.H), rg)
        self.dither()
        if motif:  # a large, very faint leaf outline
            x, y, s = motif
            p.save()
            p.translate(x, y)
            p.scale(s / 24, s / 24)
            c = QColor(t.accent)
            c.setAlphaF(0.07 if t.name == "dark" else 0.09)
            p.setPen(QPen(c, 24 / s * max(1.5, s / 260), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(_leaf_path())
            p.drawLine(QPointF(4.5, 19.5), QPointF(14, 10))
            p.restore()

    def dither(self) -> None:
        """+-1 level of fixed noise over the gradients so 8-bit output shows no banding rings."""
        global _NOISE
        if _NOISE is None:
            import random
            rnd = random.Random(7)
            _NOISE = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
            for yy in range(128):
                for xx in range(128):
                    a = rnd.choice((0, 0, 3, 5))
                    v = 255 if rnd.random() < 0.5 else 0
                    _NOISE.setPixel(xx, yy, QColor(v * a // 255, v * a // 255, v * a // 255, a).rgba())
        self.p.save()
        self.p.resetTransform()
        self.p.fillRect(self.img.rect(), QBrush(_NOISE))
        self.p.restore()

    # ---- text
    def wrap(self, text: str, f: QFont, maxw: float) -> list[str]:
        fm = QFontMetricsF(f)
        lines = []
        for para in text.split("\n"):
            cur = ""
            for w in para.split(" "):
                t = f"{cur} {w}" if cur else w
                if not cur or fm.horizontalAdvance(t) <= maxw:
                    cur = t
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        return lines

    def balanced(self, text: str, f: QFont, maxw: float) -> list[str]:
        """Greedy wrap, then the narrowest width that keeps the same line count (no lonely last word)."""
        if "\n" in text:
            return [line for para in text.split("\n") for line in self.balanced(para, f, maxw)]
        lines = self.wrap(text, f, maxw)
        if len(lines) < 2:
            return lines
        sentences = re.split(r"(?<=[.:;])\s+", text)
        if 1 < len(sentences) <= len(lines):  # one sentence per line reads best when it fits
            fm = QFontMetricsF(f)
            if all(fm.horizontalAdvance(x) <= maxw for x in sentences):
                return sentences
        lo, hi = maxw * 0.4, maxw
        for _ in range(18):
            mid = (lo + hi) / 2
            if len(self.wrap(text, f, mid)) == len(lines):
                hi = mid
            else:
                lo = mid
        return self.wrap(text, f, hi)

    def text(self, x: float, y: float, w: float, s: str, f: QFont, color: QColor, lh: float = 1.25,
             align: str = "left", balance: bool = True) -> float:
        """Draw wrapped text with its first line box at y; returns the bottom of the last line box."""
        p = self.p
        fm = QFontMetricsF(f)
        lines = self.balanced(s, f, w) if balance else self.wrap(s, f, w)
        line_h = f.pixelSize() * lh
        p.setFont(f)
        p.setPen(color)
        top = y
        for line in lines:
            base = y + (line_h - (fm.ascent() + fm.descent())) / 2 + fm.ascent()
            lw = fm.horizontalAdvance(line)
            lx = x if align == "left" else x + (w - lw) / 2 if align == "center" else x + w - lw
            p.drawText(QPointF(lx, base), line)
            self.texts.append((QRectF(lx, y, lw, line_h), line))
            y += line_h
        if not lines:
            return top
        return y

    def text_width(self, s: str, f: QFont) -> float:
        return max(QFontMetricsF(f).horizontalAdvance(line) for line in s.split("\n"))

    # ---- logo / icons
    def logo(self, x: float, y: float, size: float, shadow: bool = False) -> None:
        """The app icon from leaf/icons.py app_icon(), drawn as vectors at any size."""
        p = self.p
        if shadow:
            self.shadow(QRectF(x + size / 24, y + size / 24, size * 22 / 24, size * 22 / 24), size * 5.5 / 24,
                        blur=size * 0.18, dy=size * 0.06, alpha=0.55 if self.t.name == "dark" else 0.35)
        p.save()
        p.translate(x, y)
        p.scale(size / 24, size / 24)
        g = QLinearGradient(0, 0, 0, 24)
        g.setColorAt(0, GREEN_TOP)
        g.setColorAt(1, GREEN_BOTTOM)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(QRectF(1, 1, 22, 22), 5.5, 5.5)
        p.translate(12, 12)
        p.scale(0.82, 0.82)
        p.translate(-12, -12.5)
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawPath(_leaf_path())
        p.setPen(QPen(GREEN_VEIN, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(5, 19), QPointF(14.5, 9.5))
        p.restore()

    def lockup(self, x: float, y: float, size: float, align: str = "left") -> QRectF:
        """Logo + 'Leaf' wordmark; size = logo height. Returns the bounding rect."""
        f = font(size * 0.62, "semibold", "display", track=-0.01)
        gap = size * 0.32
        w = size + gap + self.text_width("Leaf", f)
        if align == "center":
            x -= w / 2
        self.logo(x, y, size)
        fm = QFontMetricsF(f)
        self.p.setFont(f)
        self.p.setPen(self.t.text)
        cap = fm.capHeight()
        base = y + size / 2 + cap / 2
        self.p.drawText(QPointF(x + size + gap, base), "Leaf")
        self.texts.append((QRectF(x + size + gap, y, w - size - gap, size), "Leaf"))
        return QRectF(x, y, w, size)

    def icon(self, name: str, x: float, y: float, size: float, color: QColor, stroke: float = STROKE) -> None:
        p = self.p
        p.save()
        p.translate(x, y)
        p.scale(size / 24, size / 24)
        p.setPen(QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        draw_icon(name, p)
        p.restore()

    def icon_tile(self, name: str, x: float, y: float, s: float) -> None:
        p = self.p
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.t.tile)
        p.drawRoundedRect(QRectF(x, y, s, s), s * 0.26, s * 0.26)
        i = s * 0.56
        self.icon(name, x + (s - i) / 2, y + (s - i) / 2, i, self.t.accent, stroke=1.7)

    # ---- windows (real screenshots)
    def shadow(self, rect: QRectF, radius: float, blur: float, dy: float, alpha: float) -> None:
        pad = blur * 2.2
        ds = max(1.0, blur / 4)
        sw, sh = max(4, math.ceil((rect.width() + 2 * pad) / ds)), max(4, math.ceil((rect.height() + 2 * pad) / ds))
        small = QImage(sw, sh, QImage.Format.Format_ARGB32_Premultiplied)
        small.fill(Qt.GlobalColor.transparent)
        sp = QPainter(small)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing)
        sp.scale(1 / ds, 1 / ds)
        c = QColor(self.t.shadow)
        c.setAlphaF(c.alphaF() * alpha)
        sp.setPen(Qt.PenStyle.NoPen)
        sp.setBrush(c)
        sp.drawRoundedRect(QRectF(pad, pad, rect.width(), rect.height()), radius, radius)
        sp.end()
        for _ in range(3):  # cheap gaussian-ish blur: shrink and grow back
            small = small.scaled(max(1, sw // 3), max(1, sh // 3), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation).scaled(
                sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.p.drawImage(QRectF(rect.x() - pad, rect.y() - pad + dy, sw * ds, sh * ds), small)

    def image(self, img: QImage, rect: QRectF) -> None:
        """Draw img into rect, pre-scaled to the device size (sharper than letting the painter scale)."""
        dev = self.p.transform().mapRect(rect)
        scaled = img.scaled(max(1, round(dev.width())), max(1, round(dev.height())),
                            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.p.save()
        self.p.resetTransform()
        self.p.drawImage(QPointF(round(dev.x()), round(dev.y())), scaled)
        self.p.restore()

    def window(self, img: QImage, x: float, y: float, w: float, h: float | None = None,
               radius: float | None = None, lift: float = 1.0, ring: bool = False) -> QRectF:
        """A screenshot as a real window: rounded corners, hairline border, soft layered shadow."""
        if h is None:
            h = w * img.height() / img.width()
        else:  # fit by height
            w = h * img.width() / img.height()
        rect = QRectF(x, y, w, h)
        r = radius if radius is not None else max(6.0, w * 0.0085)
        self.shadow(rect, r, blur=w * 0.035 * lift, dy=w * 0.022 * lift, alpha=0.9)
        self.shadow(rect, r, blur=w * 0.006 * lift, dy=w * 0.003 * lift, alpha=0.6)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)
        self.p.save()
        self.p.setClipPath(path)
        self.image(img, rect)
        self.p.restore()
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.setPen(QPen(self.t.frame, max(1.0, w / 1400)))
        self.p.drawPath(path)
        if ring:  # marks a close-up
            ring_c = QColor(self.t.accent)
            ring_c.setAlphaF(0.85)
            self.p.setPen(QPen(ring_c, 3))
            o = 4.5
            self.p.drawRoundedRect(rect.adjusted(-o, -o, o, o), r + o, r + o)
        return rect

    def panel(self, img: QImage, rect: QRectF, img_rect: QRectF) -> QRectF:
        """A rounded panel showing part of a screenshot (img drawn at img_rect, clipped to rect)."""
        r = 12.0
        self.shadow(rect, r, blur=40, dy=26, alpha=0.9)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)
        self.p.save()
        self.p.setClipPath(path)
        self.image(img, img_rect)
        self.p.restore()
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.setPen(QPen(self.t.frame, 1.0))
        self.p.drawPath(path)
        return rect

    def callouts(self, x: float, y: float, items, size: float, cols: int, colw: float, rowgap: float) -> float:
        f = font(size, "semibold", "text")
        row_h = size * 1.5
        for i, (ico, label) in enumerate(items):
            cx = x + (i % cols) * colw
            cy = y + (i // cols) * (row_h + rowgap)
            isz = size * 1.25
            self.icon(ico, cx, cy + (row_h - isz) / 2, isz, self.t.accent, stroke=1.8)
            self.text(cx + isz + size * 0.6, cy, colw - isz - size, label, f, self.t.text, lh=1.5, balance=False)
        rows = math.ceil(len(items) / cols)
        return y + rows * row_h + (rows - 1) * rowgap

    def rule(self, x1: float, y: float, x2: float) -> None:
        self.p.setPen(QPen(self.t.hairline, max(1.0, self.W / 2400)))
        self.p.drawLine(QPointF(x1, y), QPointF(x2, y))


# ----------------------------------------------------------------------------------------- layouts
def hero(c: Canvas, s: Screens) -> None:
    """Store hero 1920x1080: text left, window right; everything inside the central 80%."""
    W, H = c.W, c.H
    c.background(glows=[(W * 0.70, H * 0.52, W * 0.42, 1.0), (W * 0.12, H * 0.1, W * 0.3, 0.5)])
    sx, sy, sw, sh = c.safe.x(), c.safe.y(), c.safe.width(), c.safe.height()
    img = s.get(c.t.name, "hero")
    win_w = sw * 0.6
    win_h = win_w * img.height() / img.width()
    if win_h > sh * 0.86:
        win_h = sh * 0.86
        win_w = win_h * img.width() / img.height()
    c.window(img, sx + sw - win_w, H / 2 - win_h / 2, win_w)
    colw = sw - win_w - 72
    hf, sf = font(70, "semibold", "display", -0.015), font(23, "regular", "text")
    heights = [52, 40, len(c.wrap(HEADLINE, hf, colw)) * 70 * 1.08, 26, 3 * 25 * 1.4, 44, 2 * 25 * 1.5 + 18]
    y = H / 2 - sum(heights) / 2
    c.lockup(sx, y, 52)
    y += 52 + 40
    y = c.text(sx, y, colw, HEADLINE, hf, c.t.text, lh=1.08)
    y = c.text(sx, y + 26, colw, SUBLINE, sf, c.t.muted, lh=1.45)
    c.callouts(sx, y + 44, CALLOUTS, 21, 2, colw / 2, 18)


def hero_clean(c: Canvas, s: Screens) -> None:
    """Store hero without any text: this theme's window in front of the other theme's, both inside the safe area."""
    W, H = c.W, c.H
    c.background(glows=[(W * 0.5, H * 0.5, W * 0.5, 1.1)])
    front = s.get(c.t.name, "hero")
    other = "light" if c.t.name == "dark" else "dark"
    back = s.exact(other, "edit-text") or s.exact(other, "hero") or s.get(other, "edit-text")
    sx, sy, sw, sh = c.safe.x(), c.safe.y(), c.safe.width(), c.safe.height()
    fw = sw * 0.66
    fh = fw * front.height() / front.width()
    bw = fw * 0.92
    bh = bw * back.height() / back.width()
    dx, dy = sw - fw, max(0.0, sh - fh)
    if fh > sh * 0.86:
        fh = sh * 0.86
        fw = fh * front.width() / front.height()
        dx, dy = sw - fw, sh - fh
    bx, by = sx, sy + min(sh * 0.03, sh - bh)
    fx, fy = sx + dx, sy + dy
    # centre the pair horizontally inside the safe area
    left, right = min(bx, fx), max(bx + bw, fx + fw)
    off = sx + (sw - (right - left)) / 2 - left
    c.window(back, bx + off, by, bw, lift=0.7)
    c.window(front, fx + off, fy, fw)
    del bh


def tile(c: Canvas, s: Screens) -> None:
    """Square box art: logo + name, nothing else."""
    W, H = c.W, c.H
    c.background(glows=[(W / 2, H * 0.43, W * 0.55, 1.3)])
    size = W * 0.34
    f = font(W * 0.105, "semibold", "display", -0.01)
    gap = W * 0.07
    fm = QFontMetricsF(f)
    total = size + gap + fm.capHeight()
    top = H / 2 - total / 2 - W * 0.01
    c.logo(W / 2 - size / 2, top, size, shadow=True)
    c.p.setFont(f)
    c.p.setPen(c.t.text)
    tw = fm.horizontalAdvance("Leaf")
    base = top + size + gap + fm.capHeight()
    c.p.drawText(QPointF(W / 2 - tw / 2, base), "Leaf")
    c.texts.append((QRectF(W / 2 - tw / 2, base - fm.capHeight(), tw, fm.capHeight()), "Leaf"))


def social_landscape(c: Canvas, s: Screens) -> None:
    """Link previews / GitHub / X: lockup + headline + subline left, a window bleeding off the right edge."""
    W, H = c.W, c.H
    u = H / 630
    m = 64 * u
    c.background(glows=[(W * 0.78, H * 0.62, W * 0.45, 1.0)])
    img = s.get(c.t.name, "hero")
    wx, wy = W * 0.505, H * 0.155
    c.window(img, wx, wy, max(W * 0.62, (H - wy + 40 * u) * img.width() / img.height()))
    colw = wx - m - 44 * u
    c.lockup(m, m, 40 * u)
    hf = font(min(58 * u, W * 0.044), "semibold", "display", -0.015)
    sf = font(21 * u, "regular", "text")
    lines = len(c.balanced(SUBLINE, sf, colw))
    block = len(c.wrap(HEADLINE, hf, colw)) * hf.pixelSize() * 1.08 + 22 * u + lines * sf.pixelSize() * 1.42
    y = m + 40 * u + (H - 2 * m - 40 * u - 36 * u - block) / 2
    y = c.text(m, y, colw, HEADLINE, hf, c.t.text, lh=1.08)
    c.text(m, y + 22 * u, colw, SUBLINE, sf, c.t.muted, lh=1.42)
    pf = font(17 * u, "semibold", "text", 0.01)
    c.text(m, H - m - 17 * u * 1.4, colw, PLATFORMS, pf, c.t.accent, lh=1.4, balance=False)


def social_portrait(c: Canvas, s: Screens) -> None:
    """Instagram portrait 1080x1350."""
    W, H = c.W, c.H
    m = 88
    c.background(glows=[(W * 0.7, H * 0.78, W * 0.75, 1.1)])
    c.lockup(m, m, 54)
    y = c.text(m, m + 54 + 70, W - 2 * m, HEADLINE, font(96, "semibold", "display", -0.015), c.t.text, lh=1.06)
    y = c.text(m, y + 32, W - 2 * m - 40, SUBLINE, font(31, "regular", "text"), c.t.muted, lh=1.4)
    img = s.get(c.t.name, "hero")
    c.window(img, m, y + 76, max(W - m + 110, (H - y - 76 + 60) * img.width() / img.height()))


def poster(c: Canvas, s: Screens) -> None:
    """A4/A3 portrait (designed on A4 2480x3508 px, scaled for A3)."""
    W, H = c.W, c.H
    m = 190
    c.background(glows=[(W * 0.5, H * 0.45, W * 0.72, 1.1)], motif=(W * 0.66, -W * 0.13, W * 0.6))
    c.lockup(m, m, 110)
    y = c.text(m, m + 110 + 130, W - 2 * m, HEADLINE, font(196, "semibold", "display", -0.02), c.t.text, lh=1.04)
    y = c.text(m, y + 56, W * 0.66, SUBLINE, font(58, "regular", "text"), c.t.muted, lh=1.38)
    # footer
    fy = H - m - 60
    c.rule(m, fy - 70, W - m)
    ff = font(44, "semibold", "text", 0.01)
    c.icon("lock", m, fy + 4, 52, c.t.accent, stroke=1.7)
    c.text(m + 80, fy, W, OFFLINE, ff, c.t.text, lh=1.35, balance=False)
    c.text(W - m - 900, fy, 900, PLATFORMS, ff, c.t.accent, lh=1.35, align="right", balance=False)
    # feature grid (3 x 2), anchored above the footer
    cols, gap, tile_s = 3, 72, 92
    cw = (W - 2 * m - (cols - 1) * gap) / cols
    tf, df = font(50, "semibold", "text"), font(36, "regular", "text")
    cell_h = tile_s + 34 + 50 * 1.3 + 12 + 2 * 36 * 1.4
    row_gap = 84
    gy = fy - 70 - 124 - (2 * cell_h + row_gap)
    for i, (ico, title, desc) in enumerate(GRID):
        x = m + (i % cols) * (cw + gap)
        yy = gy + (i // cols) * (cell_h + row_gap)
        c.icon_tile(ico, x, yy, tile_s)
        ty = c.text(x, yy + tile_s + 34, cw, title, tf, c.t.text, lh=1.3, balance=False)
        c.text(x, ty + 12, cw - 10, desc, df, c.t.muted, lh=1.4)
    # hero screenshot: as large as the space between the subline and the grid allows
    img = s.get(c.t.name, "hero")
    top, bottom = y + 124, gy - 136
    ww = W - 2 * m
    wh = ww * img.height() / img.width()
    if wh > bottom - top:
        wh = bottom - top
        ww = wh * img.width() / img.height()
    c.window(img, (W - ww) / 2, top + (bottom - top - wh) / 2, ww)


def banner(c: Canvas, s: Screens) -> None:
    """Landscape banner 3000x1000."""
    W, H = c.W, c.H
    m = 190
    c.background(glows=[(W * 0.74, H * 0.6, W * 0.32, 1.1), (W * 0.05, 0, W * 0.25, 0.5)])
    img = s.get(c.t.name, "hero")
    wx = W * 0.5
    c.window(img, wx, H * 0.15, max(W * 0.56, (H * 0.85 + 60) * img.width() / img.height()))
    colw = wx - m - 120
    hf, sf = font(112, "semibold", "display", -0.015), font(40, "regular", "text")
    block = 78 + 70 + len(c.wrap(HEADLINE, hf, colw)) * 112 * 1.06 + 40 + \
        len(c.balanced(SUBLINE, sf, colw)) * 40 * 1.4 + 70 + 2 * 34 * 1.5 + 26
    y = (H - block) / 2
    c.lockup(m, y, 78)
    y = c.text(m, y + 78 + 70, colw, HEADLINE, hf, c.t.text, lh=1.06)
    y = c.text(m, y + 40, colw, SUBLINE, sf, c.t.muted, lh=1.4)
    c.callouts(m, y + 70, CALLOUTS, 34, 2, colw / 2, 26)


def feature_card(c: Canvas, s: Screens, feat) -> None:
    """1600x900: icon, eyebrow, one-line headline, one-sentence subline; the matching screenshot on the right."""
    slug, role, ico, eyebrow, head, sub = feat
    W, H = c.W, c.H
    m = 96
    c.background(glows=[(W * 0.76, H * 0.55, W * 0.42, 1.0)])
    img = s.get(c.t.name, role)
    detail = s.get(c.t.name, role, detail=True)
    wx, wy = 690, 116
    base_w = max(1180, (H - wy + 40) * img.width() / img.height())
    fz = s.focus(role)
    if fz:  # a zoomed crop around the interesting part, in a rounded panel that bleeds off the edges
        fx, fy, zoom = fz
        panel = QRectF(wx, wy, W - wx + 60, H - wy + 60)
        iw = base_w * zoom
        ih = iw * img.height() / img.width()
        cx, cy = (wx + W) / 2, (wy + H) / 2
        ix = min(wx, max(panel.right() - iw, cx - fx * iw))
        iy = min(wy, max(panel.bottom() - ih, cy - fy * ih))
        win = c.panel(img, panel, QRectF(ix, iy, iw, ih))
    else:
        win = c.window(img, wx, wy, base_w)
    if detail is not None:  # a close-up of the interesting part, floating over the window
        dw = 560
        dh = dw * detail.height() / detail.width()
        if dh > 420:
            dh = 420
            dw = dh * detail.width() / detail.height()
        c.window(detail, wx - 90, H - m - dh, dw, lift=1.6, ring=True)
    del win
    colw = wx - m - 70
    hf, sf = font(54, "semibold", "display", -0.012), font(25, "regular", "text")
    ef = font(19, "semibold", "text", 0.09)
    hl = c.balanced(head, hf, colw)
    sl = c.balanced(sub, sf, colw)
    block = 72 + 36 + 19 * 1.4 + 18 + len(hl) * 54 * 1.1 + 26 + len(sl) * 25 * 1.45
    y = (H - block) / 2 + 10
    c.lockup(m, m - 24, 34)
    c.icon_tile(ico, m, y, 72)
    y = c.text(m, y + 72 + 36, colw, eyebrow.upper(), ef, c.t.accent, lh=1.4, balance=False)
    y = c.text(m, y + 18, colw, head, hf, c.t.text, lh=1.1)
    c.text(m, y + 26, colw, sub, sf, c.t.muted, lh=1.45)


# ------------------------------------------------------------------------------------------ assets
@dataclass
class Asset:
    name: str
    w: int
    h: int
    draw: callable
    design: tuple[int, int] | None = None  # design size if rendered scaled (e.g. A3 from the A4 layout)
    safe: float = 0.0
    pdf: tuple[float, float] | None = None  # page size in points
    args: tuple = field(default_factory=tuple)


A4_PT, A3_PT = (595.28, 841.89), (841.89, 1190.55)
ASSETS = [
    Asset("hero", 1920, 1080, hero, safe=0.10),
    Asset("hero-clean", 1920, 1080, hero_clean, safe=0.10),
    Asset("tile", 1080, 1080, tile),
    Asset("tile", 2160, 2160, tile, design=(1080, 1080)),
    Asset("social-link", 1200, 630, social_landscape),
    Asset("social-github", 1280, 640, social_landscape, safe=40),
    Asset("social-x", 1600, 900, social_landscape),
    Asset("social-instagram", 1080, 1350, social_portrait),
    Asset("poster-a4", 2480, 3508, poster, pdf=A4_PT),
    Asset("poster-a3", 3508, 4961, poster, design=(2480, 3508), pdf=A3_PT),
    Asset("banner", 3000, 1000, banner),
] + [Asset(f"feature-{i + 1:02d}-{f[0]}", 1600, 900, feature_card, args=(f,)) for i, f in enumerate(FEATURES)]


def render(asset: Asset, theme: str, screens: Screens) -> tuple[QImage, list[str]]:
    dw, dh = asset.design or (asset.w, asset.h)
    k = asset.w / dw
    c = Canvas(dw, dh, THEMES[theme], scale=k, safe=asset.safe)
    asset.draw(c, screens, *asset.args)
    img = c.finish()
    if img.height() != asset.h:  # A3 from A4: 1 px rounding
        img = img.copy(0, 0, asset.w, asset.h)
    return img, c.problems()


def write_pdf(png: Path, pdf: Path, size_pt: tuple[float, float]) -> None:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=size_pt[0], height=size_pt[1])
    page.insert_image(page.rect, filename=str(png))
    doc.set_metadata({"title": "Leaf", "subject": "Leaf PDF editor poster", "creator": "", "producer": ""})
    doc.save(str(pdf), garbage=3, deflate=True)
    doc.close()


def preview(img: QImage, path: Path, longest: int = 1400) -> None:
    sm = img.scaled(longest, longest, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) \
        if max(img.width(), img.height()) > longest else img
    path.parent.mkdir(parents=True, exist_ok=True)
    sm.convertToFormat(QImage.Format.Format_RGB32).save(str(path), "JPG", 88)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("screens", type=Path, help="folder with dark/ and light/ screenshot subfolders")
    ap.add_argument("--out", type=Path, default=ROOT / "marketing" / "out" / "promo")
    ap.add_argument("--only", default="*", help="glob on the output name, e.g. 'feature-*' or 'poster-a4-dark*'")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--map", type=Path, help="JSON {role: path} or {role: {theme: path}} overrides")
    ap.add_argument("--no-preview", action="store_true", help="skip the downscaled JPEGs in OUT/_preview")
    a = ap.parse_args()

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    del app
    load_fonts()
    check_contrast()  # asserts WCAG AA (4.5:1) for every text color on every background
    mapping = json.loads(a.map.read_text("utf-8")) if a.map else None
    screens = Screens(a.screens.resolve(), mapping)
    a.out.mkdir(parents=True, exist_ok=True)
    problems = 0
    for theme in a.themes.split(","):
        for asset in ASSETS:
            name = f"{asset.name}-{theme}-{asset.w}x{asset.h}"
            if not fnmatch.fnmatch(name, a.only):
                continue
            img, bad = render(asset, theme, screens)
            png = a.out / f"{name}.png"
            img.convertToFormat(QImage.Format.Format_RGB32).save(str(png), "PNG")
            if asset.pdf:
                write_pdf(png, png.with_suffix(".pdf"), asset.pdf)
            if not a.no_preview:
                preview(img, a.out / "_preview" / f"{name}.jpg")
            print(f"{name}.png" + (" + .pdf" if asset.pdf else ""))
            for b in bad:
                problems += 1
                print("   !", b)
    print("\nscreenshots used:")
    for (theme, role), f in sorted(screens.used.items()):
        print(f"  {theme:5} {role:18} {f}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
