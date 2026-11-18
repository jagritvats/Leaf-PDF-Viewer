"""Rich text for text boxes.

Box-wide style lives in the item's props (the document's default font/color/alignment); per-run formatting is
stored as explicit QTextCharFormat properties that differ from it. This module holds the run helpers, a compact
serialization (undo, copies) and the PDF exporter.

Export: Qt has already laid the text out, so instead of letting MuPDF re-wrap, every visual line becomes its own
paragraph and its line-height/margin is solved so MuPDF's baseline lands on Qt's (MuPDF puts a line's baseline at
0.8em + (line-height - 1em) / 2 below the line top). Justified paragraphs are the exception: they are emitted whole
at exactly the layout width so MuPDF can stretch them. Runs become spans with explicit CSS, never Qt's toHtml().
"""
from __future__ import annotations

import html
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat

from . import fonts

CHAR_KEYS = ("font", "size", "color", "bold", "italic", "underline", "strike")
ALIGNS = ("left", "center", "right", "justify")
SPACINGS = (1.0, 1.15, 1.5, 2.0)
P = QTextFormat.Property
FONT_KEY = int(P.UserProperty) + 1
_CSS_ALIGN = {"left": "left", "center": "center", "right": "right", "justify": "justify"}


def qt_alignment(align: str) -> Qt.AlignmentFlag:
    A = Qt.AlignmentFlag
    return {"center": A.AlignHCenter, "right": A.AlignRight | A.AlignAbsolute,
            "justify": A.AlignJustify}.get(align, A.AlignLeft | A.AlignAbsolute)


def base_font(props: dict) -> QFont:
    f = QFont()
    key = props.get("font") or fonts.DEFAULT
    f.setFamilies(fonts.qt_families(key))
    base = fonts.fallback(key) if not fonts.is_base(key) else key
    f.setStyleHint({"Times": QFont.StyleHint.Serif, "Courier": QFont.StyleHint.TypeWriter}.get(base, QFont.StyleHint.SansSerif))
    f.setPixelSize(max(1, round(float(props.get("size", 12)))))
    f.setBold(bool(props.get("bold")))
    f.setItalic(bool(props.get("italic")))
    f.setUnderline(bool(props.get("underline")))
    f.setStrikeOut(bool(props.get("strike")))
    return f


# ------------------------------------------------------------------- runs
def run_get(fmt: QTextCharFormat, key: str):
    """The run's explicit value for `key`, or None when it follows the box style."""
    if key == "font":
        return fmt.stringProperty(FONT_KEY) if fmt.hasProperty(FONT_KEY) else None
    if key == "size":
        return fmt.intProperty(P.FontPixelSize) if fmt.hasProperty(P.FontPixelSize) else None
    if key == "color":
        return fmt.foreground().color().name() if fmt.hasProperty(P.ForegroundBrush) else None
    if key == "bold":
        return fmt.fontWeight() >= 600 if fmt.hasProperty(P.FontWeight) else None
    if key == "italic":
        return fmt.fontItalic() if fmt.hasProperty(P.FontItalic) else None
    if key == "underline":
        return fmt.fontUnderline() if fmt.hasProperty(P.TextUnderlineStyle) or fmt.hasProperty(P.FontUnderline) else None
    if key == "strike":
        return fmt.fontStrikeOut() if fmt.hasProperty(P.FontStrikeOut) else None
    return None


def run_set(fmt: QTextCharFormat, key: str, v) -> None:
    if key == "font":
        fmt.setProperty(FONT_KEY, str(v))
        fmt.setFontFamilies(fonts.qt_families(str(v)))
    elif key == "size":
        fmt.setProperty(P.FontPixelSize, max(1, int(round(float(v)))))
    elif key == "color":
        fmt.setForeground(QColor(v))
    elif key == "bold":
        fmt.setFontWeight(700 if v else 400)
    elif key == "italic":
        fmt.setFontItalic(bool(v))
    elif key == "underline":
        fmt.setFontUnderline(bool(v))
    elif key == "strike":
        fmt.setFontStrikeOut(bool(v))


def run_clear(fmt: QTextCharFormat, key: str) -> None:
    props = {"font": (FONT_KEY, P.FontFamilies, P.FontFamily), "size": (P.FontPixelSize, P.FontPointSize),
             "color": (P.ForegroundBrush,), "bold": (P.FontWeight,), "italic": (P.FontItalic,),
             "underline": (P.TextUnderlineStyle, P.FontUnderline), "strike": (P.FontStrikeOut,)}[key]
    for p in props:
        fmt.clearProperty(p)


def run_props(fmt: QTextCharFormat) -> dict:
    return {k: v for k in CHAR_KEYS if (v := run_get(fmt, k)) is not None}


def _same(key: str, a, b) -> bool:
    if key == "color":
        return QColor(a).name() == QColor(b).name()
    if key == "size":
        return round(float(a)) == round(float(b))
    if key in ("bold", "italic", "underline", "strike"):
        return bool(a) == bool(b)
    return a == b


def effective(fmt: QTextCharFormat, base: dict) -> dict:
    out = {k: base.get(k) for k in CHAR_KEYS}
    out.update(run_props(fmt))
    return out


def _restyle(fmt: QTextCharFormat, delta: dict, base: dict) -> QTextCharFormat:
    fmt = QTextCharFormat(fmt)
    for k, v in delta.items():
        if k not in CHAR_KEYS:
            continue
        if _same(k, v, base.get(k)):
            run_clear(fmt, k)  # back to the box style: drop the override
        else:
            run_set(fmt, k, v)
    return fmt


def _apply(doc: QTextDocument, changes: list[tuple[int, int, QTextCharFormat]]) -> bool:
    if not changes:
        return False
    c = QTextCursor(doc)
    c.beginEditBlock()
    for a, b, fmt in changes:
        c.setPosition(a)
        c.setPosition(b, QTextCursor.MoveMode.KeepAnchor)
        c.setCharFormat(fmt)
    c.endEditBlock()
    return True


def _fragments(doc: QTextDocument, start: int = 0, end: int | None = None):
    end = doc.characterCount() if end is None else end
    block = doc.findBlock(start)
    while block.isValid() and block.position() < end:
        for it in block:
            fr = it.fragment()
            a, b = max(fr.position(), start), min(fr.position() + fr.length(), end)
            if a < b:
                yield a, b, fr
        block = block.next()


def format_cursor(cursor: QTextCursor, delta: dict, base: dict) -> QTextCursor:
    """Apply character props to the cursor's selection (one undo step) or, without a selection, to what gets typed
    next. Values equal to the box style clear the override instead of storing it."""
    if not cursor.hasSelection():
        cursor.setCharFormat(_restyle(cursor.charFormat(), delta, base))
        return cursor
    doc = cursor.document()
    s, e = cursor.selectionStart(), cursor.selectionEnd()
    _apply(doc, [(a, b, _restyle(fr.charFormat(), delta, base)) for a, b, fr in list(_fragments(doc, s, e))])
    return cursor


def has_overrides(doc: QTextDocument, keys) -> bool:
    return any(run_get(fr.charFormat(), k) is not None for _, _, fr in _fragments(doc) for k in keys)


def clear_overrides(doc: QTextDocument, keys) -> bool:
    changes = []
    for a, b, fr in list(_fragments(doc)):
        fmt = QTextCharFormat(fr.charFormat())
        if any(run_get(fmt, k) is not None for k in keys):
            for k in keys:
                run_clear(fmt, k)
            changes.append((a, b, fmt))
    return _apply(doc, changes)


def cursor_props(cursor: QTextCursor, base: dict) -> dict:
    """Effective character props at the cursor; for a selection a toggle is on only if it is on for all of it."""
    if not cursor.hasSelection():
        return effective(cursor.charFormat(), base)
    runs = [effective(fr.charFormat(), base) for a, b, fr in _fragments(cursor.document(), cursor.selectionStart(),
                                                                         cursor.selectionEnd())]
    if not runs:
        return effective(cursor.charFormat(), base)
    out = dict(runs[0])
    for k in ("bold", "italic", "underline", "strike"):
        out[k] = all(r[k] for r in runs)
    return out


# ---------------------------------------------------------- serialization
def serialize(doc: QTextDocument) -> str:
    blocks = []
    block = doc.begin()
    while block.isValid():
        blocks.append([[it.fragment().text(), run_props(it.fragment().charFormat())] for it in block])
        block = block.next()
    if all(not rp for b in blocks for _, rp in b):  # plain text: keep it readable (raw: keeps nbsp, U+2028)
        return "\n".join("".join(t for t, _ in b) for b in blocks)
    return json.dumps({"leaf": 1, "blocks": blocks}, ensure_ascii=False, separators=(",", ":"))


def deserialize(doc: QTextDocument, data: str) -> None:
    blocks = None
    if data.startswith('{"leaf":'):
        try:
            blocks = json.loads(data)["blocks"]
        except (ValueError, KeyError, TypeError):
            blocks = None
    if blocks is None:
        doc.setPlainText(data)
        return
    doc.setPlainText("")
    c = QTextCursor(doc)
    c.beginEditBlock()
    for i, runs in enumerate(blocks):
        if i:
            c.insertBlock(QTextBlockFormat(), QTextCharFormat())
        for text, rp in runs:
            fmt = QTextCharFormat()
            for k, v in rp.items():
                if k in CHAR_KEYS:
                    run_set(fmt, k, v)
            c.insertText(text, fmt)
    c.endEditBlock()


def set_spacing(doc: QTextDocument, spacing: float) -> None:
    """Line spacing for every paragraph (proportional to the font's natural line height)."""
    spacing = float(spacing or 1.0)
    T = QTextBlockFormat.LineHeightTypes
    kind, value = (T.SingleHeight, 0.0) if abs(spacing - 1.0) < 1e-3 else (T.ProportionalHeight, spacing * 100)
    block = doc.begin()
    same = True
    while block.isValid():
        bf = block.blockFormat()
        if bf.lineHeightType() != kind.value or abs(bf.lineHeight() - value) > 1e-3:
            same = False
            break
        block = block.next()
    if same:
        return
    c = QTextCursor(doc)
    c.select(QTextCursor.SelectionType.Document)
    bf = QTextBlockFormat()
    bf.setLineHeight(value, kind.value)
    c.mergeBlockFormat(bf)


# ------------------------------------------------------------------ export
_calib: tuple[float, float] | None = None


def calibration() -> tuple[float, float]:
    """MuPDF's offset of the content box inside an htmlbox rect (x, y), measured once."""
    global _calib
    if _calib is None:
        import pymupdf

        try:
            d = pymupdf.open()
            p = d.new_page(width=200, height=200)
            p.insert_htmlbox((10, 10, 190, 190), '<p style="line-height:20px;font-size:20px">H</p>',
                             css="p {margin:0; padding:0; font-family: Helvetica;}")
            span = next(s for b in p.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"])
            ox, oy = span["origin"]
            _calib = (ox - 10, oy - 10 - (0.3 * 20 + 0.5 * 20))
            d.close()
        except Exception:  # noqa: BLE001 - fall back to the value MuPDF 1.2x uses
            _calib = (1.0, 1.0)
    return _calib


def _line_runs(block, start: int, end: int) -> list[tuple[str, QTextCharFormat]]:
    bpos = block.position()
    out = []
    for it in block:
        fr = it.fragment()
        fs = fr.position() - bpos
        a, b = max(fs, start), min(fs + fr.length(), end)
        if a < b:
            out.append((fr.text()[a - fs:b - fs], fr.charFormat()))
    return out


def _clean(runs, strip_end: bool) -> list:
    runs = [(t.replace(" ", "").replace(" ", ""), f) for t, f in runs]
    if strip_end:
        while runs and not runs[-1][0].rstrip():
            runs.pop()
        if runs:
            runs[-1] = (runs[-1][0].rstrip(), runs[-1][1])
    return [(t, f) for t, f in runs if t]


def _span(text: str, eff: dict, emb: fonts.Embedder) -> str:
    fam = emb.family(eff["font"] or fonts.DEFAULT, bool(eff["bold"]), bool(eff["italic"]))
    deco = " ".join(n for k, n in (("underline", "underline"), ("strike", "line-through")) if eff[k])
    style = (f"font-family:{fam};font-size:{float(eff['size']):g}px;color:{QColor(eff['color']).name()};"
             f"font-weight:{'bold' if eff['bold'] else 'normal'};font-style:{'italic' if eff['italic'] else 'normal'};"
             f"text-decoration:{deco or 'none'}")
    body = html.escape(text).replace(" ", "<br>")
    return f'<span style="{style}">{body}</span>'


def export(doc: QTextDocument, props: dict, emb: fonts.Embedder) -> dict | None:
    """HTML for insert_htmlbox reproducing Qt's layout, plus where to put it (document coordinates):
    {html, css, top, bottom, left, right, max_size, align}. None if there is no visible text."""
    base = {k: props.get(k) for k in CHAR_KEYS}
    align = props.get("align", "left") if props.get("align") in ALIGNS else "left"
    pad = doc.documentMargin()
    groups = []
    block = doc.begin()
    while block.isValid():
        tl = block.layout()
        n = tl.lineCount()
        oy = tl.position().y()
        bf = block.blockFormat()
        factor = bf.lineHeight() / 100 if bf.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value else 1.0
        lines = []
        for i in range(n):
            ln = tl.lineAt(i)
            pitch = (tl.lineAt(i + 1).y() - ln.y()) if i + 1 < n else ln.height() * factor
            s, e = ln.textStart(), ln.textStart() + ln.textLength()
            lines.append({"top": oy + ln.y(), "base": oy + ln.y() + ln.ascent(), "pitch": pitch, "s": s, "e": e})
        text = block.text()
        soft = [ln["e"] for ln in lines[:-1]]
        if align == "justify" and n > 1 and all(0 < b <= len(text) and text[b - 1] in " \t " for b in soft):
            runs = _clean(_line_runs(block, 0, len(text)), True)
            if runs:
                groups.append({"runs": runs, "lines": lines, "align": "justify", "ws": "pre-wrap"})
        else:
            for ln in lines:
                runs = _clean(_line_runs(block, ln["s"], ln["e"]), True)
                if any(t.strip() for t, _ in runs):
                    groups.append({"runs": runs, "lines": [ln], "align": "left" if align == "justify" else align,
                                   "ws": "pre"})
        block = block.next()
    if not groups:
        return None

    parts, cursor, top, max_size = [], 0.0, 0.0, 1.0
    for gi, g in enumerate(groups):
        effs = [(t, effective(f, base)) for t, f in g["runs"]]
        M = max(float(e["size"]) for _, e in effs)
        max_size = max(max_size, M)
        first = g["lines"][0]
        h = max(1.0, first["pitch"] if len(g["lines"]) == 1 else
                (g["lines"][1]["top"] - first["top"]))
        m = 0.0
        if gi == 0:
            top = first["base"] - (0.3 * M + 0.5 * h)
            cursor = top
        else:
            need = first["base"] - cursor
            m = need - (0.3 * M + 0.5 * h)
            if m < 0:
                if len(g["lines"]) == 1:
                    h = max(1.0, 2 * (need - 0.3 * M))
                m = 0.0
        cursor += m + h * len(g["lines"])
        spans = "".join(_span(t, e, emb) for t, e in effs)
        parts.append(f'<p style="margin-top:{m:.3f}px;line-height:{h:.3f}px;font-size:{M:g}px;'
                     f'text-align:{_CSS_ALIGN[g["align"]]};white-space:{g["ws"]}">{spans}</p>')
    width = doc.size().width() - 2 * pad
    css = "\n".join(emb.css + ["p {margin:0; padding:0;}"])
    return {"html": "".join(parts), "css": css, "top": top, "bottom": cursor, "left": pad, "right": pad + width,
            "max_size": max_size, "align": align}
