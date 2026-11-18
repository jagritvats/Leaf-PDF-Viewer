"""Edit sets: a versioned JSON description of changes to a PDF (docs/edit-set.md).

    export_edits(canvas) -> dict                  the canvas' pending changes as an edit set
    validate(data) -> list[str]                    structural problems (empty list: well-formed)
    apply_edits(canvas, data, label=...) -> list   apply on a canvas as ONE undo step; warnings and refusals
    apply_to_document(doc, data) -> list           apply on a PdfDocument without a canvas (CLI, MCP)
    save(doc, target)                              save a document changed by apply_to_document (its overlays too)

Page numbers: inside `pages`, `page` is a page of the document as the edit set finds it (1-based); everywhere
else it is a page of the result, after `pages` (the same thing when there is no `pages` section). `uid` may stand
in for `page` (a live PageRef.uid, for callers that hold an open document). Geometry is in PDF points in the
page's own display space (page.rect: origin top left, the page's own /Rotate applied, Leaf's extra rotation and
crop not applied), the space overlays live in.

The edit model is the same one the GUI uses (document.PdfDocument): applying an edit set only fills page list,
form values, text edits, image edits, overlays, crop, metadata, decorations, sanitize and OCR layer; nothing is
written until the document is saved. Qt is only needed for overlays (annotations, redactions) and decorations;
headless callers get an offscreen QApplication created on demand (ensure_qt).
"""
from __future__ import annotations

import base64
import copy
import difflib
import os
import re
from dataclasses import asdict, fields as dc_fields

VERSION = 1
# applied in this order (page list first: every other page number refers to its result)
SECTIONS = ("pages", "forms", "text_edits", "replace", "images", "annotations", "redactions", "crop", "metadata",
            "decorations", "sanitize", "ocr")
ANNOT_TYPES = ("rect", "ellipse", "line", "arrow", "ink", "highlight", "text", "note", "whiteout", "image")
META_KEYS = ("title", "author", "subject", "keywords")
REDACT_PRESETS = {"text": "Text", "regex": "Custom regular expression", "email": "Email addresses",
                  "phone": "Phone numbers", "card": "Credit card numbers", "iban": "IBANs", "date": "Dates"}
SECTION_LABELS = {"pages": "Page list", "forms": "Form fields", "text_edits": "Text edits", "replace": "Find & replace",
                  "images": "Images", "annotations": "Annotations", "redactions": "Redactions", "crop": "Crop",
                  "metadata": "Properties", "decorations": "Watermarks & headers", "sanitize": "Sanitize",
                  "ocr": "Recognize text (OCR)"}
_TYPE_NAMES = {"rect": "rectangle", "ellipse": "ellipse", "line": "line", "arrow": "arrow", "ink": "drawing",
               "highlight": "highlight", "text": "text box", "note": "note", "whiteout": "whiteout", "image": "image"}

_qt_app = None  # offscreen QApplication made by ensure_qt (kept alive)


class EditSetError(ValueError):
    """The edit set is not well-formed; str(e) lists the problems."""


# ============================================================================================ helpers
def ensure_qt():
    """A QApplication for overlay items: the running one, else an offscreen one made here (headless use)."""
    global _qt_app
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        if not os.environ.get("QT_QPA_PLATFORM"):
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = _qt_app = QApplication(["leaf-cli"])
    return app


def tuplify(v):
    """JSON lists back to the tuples Leaf uses as keys (text units, image draws)."""
    if isinstance(v, list):
        return tuple(tuplify(x) for x in v)
    return v


def listify(v):
    if isinstance(v, (list, tuple)):
        return [listify(x) for x in v]
    return v


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    if s.startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s, validate=False)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rect(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 4 and all(_num(x) for x in v)


def _point(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v)


def _color(v) -> bool:
    return isinstance(v, str) and (v == "" or bool(re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})|[a-zA-Z]+", v)))


def _norm(s: str) -> str:
    return " ".join(str(s).split())


def _short(s: str, n: int = 48) -> str:
    s = _norm(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def empty(document: dict | None = None) -> dict:
    """An edit set with every section present and empty (a template)."""
    out: dict = {"leaf_edits": VERSION}
    if document:
        out["document"] = document
    for s in SECTIONS:
        out[s] = {} if s in ("forms", "metadata") else None if s in ("sanitize", "ocr") else []
    return out


def compact(data: dict) -> dict:
    """Without empty sections."""
    return {k: v for k, v in data.items() if k not in SECTIONS or v not in (None, [], {})}


# ============================================================================================ validate
def validate(data) -> list[str]:
    """Problems that make `data` unusable as an edit set (structure and types; the document isn't needed)."""
    p: list[str] = []
    if not isinstance(data, dict):
        return ["An edit set is a JSON object."]
    v = data.get("leaf_edits")
    if v is None:
        p.append("Missing \"leaf_edits\" (the format version, 1).")
    elif v != VERSION:
        p.append(f"Unsupported edit set version {v!r} (this Leaf reads version {VERSION}).")
    known = set(SECTIONS) | {"leaf_edits", "document", "sources", "generator", "comment"}
    for k in data:
        if k not in known:
            p.append(f"Unknown section \"{k}\".")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict):
        p.append("\"sources\" must be an object of id -> {\"name\", \"data\"}.")
        sources = {}
    for sid, s in sources.items():
        if not isinstance(s, dict) or not isinstance(s.get("data"), str):
            p.append(f"sources.{sid}: needs \"data\" (base64 PDF).")

    def lst(name):
        v = data.get(name)
        if v is None:
            return []
        if not isinstance(v, list):
            p.append(f"\"{name}\" must be a list.")
            return []
        out = []
        for i, e in enumerate(v):
            if not isinstance(e, dict):
                p.append(f"{name}[{i}]: must be an object.")
            else:
                out.append((f"{name}[{i}]", e))
        return out

    def page_of(where, e, required=True):
        has_p, has_u = "page" in e, "uid" in e
        if has_p and not (isinstance(e["page"], int) and not isinstance(e["page"], bool) and e["page"] >= 1):
            p.append(f"{where}: \"page\" must be a page number (1 or more).")
        if has_u and not isinstance(e["uid"], int):
            p.append(f"{where}: \"uid\" must be an integer.")
        if required and not (has_p or has_u):
            p.append(f"{where}: needs \"page\".")

    def pages_spec(where, e):
        v = e.get("pages")
        if v is None:
            return
        if not (isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, int) and x >= 1 for x in v))):
            p.append(f"{where}: \"pages\" must be a range like \"1-3, 7\" or a list of page numbers.")

    def flags(where, e, names):
        for n in names:
            if n in e and not isinstance(e[n], bool):
                p.append(f"{where}: \"{n}\" must be true or false.")

    def data_or_file(where, e, required=True):
        if "data" in e and not isinstance(e["data"], str):
            p.append(f"{where}: \"data\" must be a base64 string.")
        if "file" in e and not isinstance(e["file"], str):
            p.append(f"{where}: \"file\" must be a path.")
        if required and "data" not in e and "file" not in e:
            p.append(f"{where}: needs \"data\" (base64) or \"file\".")

    # pages
    for w, e in lst("pages"):
        kinds = [k for k in ("blank", "source", "file") if k in e]
        if len(kinds) > 1:
            p.append(f"{w}: use one of \"page\", \"blank\", \"source\" or \"file\".")
        if "blank" in e:
            if not _point(e["blank"]) or min(e["blank"]) <= 0:
                p.append(f"{w}: \"blank\" must be [width, height] in points.")
        elif "source" in e:
            if e["source"] not in sources:
                p.append(f"{w}: unknown source {e['source']!r}.")
            page_of(w, e)
        elif "file" in e:
            if not isinstance(e["file"], str):
                p.append(f"{w}: \"file\" must be a path.")
            if "page" in e:
                page_of(w, e)
        else:
            page_of(w, e)
        if "rotate" in e and not (isinstance(e["rotate"], int) and e["rotate"] % 90 == 0):
            p.append(f"{w}: \"rotate\" must be a multiple of 90.")

    # forms
    forms = data.get("forms")
    if forms is not None:
        if not isinstance(forms, dict):
            p.append("\"forms\" must be an object of field name -> value.")
        else:
            def plain(v):
                return isinstance(v, (str, bool, int, float)) or v is None or \
                    (isinstance(v, list) and all(isinstance(x, str) for x in v))

            for k, v in forms.items():
                if _scoped(v):
                    for x in ([v] if isinstance(v, dict) else v):
                        if set(x) - {"value", "pages"} or "value" not in x or not plain(x["value"]):
                            p.append(f"forms.{k}: a page-scoped value is {{\"value\": ..., \"pages\": [...]}}.")
                        else:
                            pages_spec(f"forms.{k}", x)
                elif not plain(v):
                    p.append(f"forms.{k}: a value is text, true/false, a number or a list of texts.")

    # text edits
    for w, e in lst("text_edits"):
        page_of(w, e)
        if not isinstance(e.get("text"), str):
            p.append(f"{w}: needs \"text\" (the new text).")
        if "key" in e:
            k = e["key"]
            if not (isinstance(k, list) and len(k) == 2 and k[0] in ("line", "para") and isinstance(k[1], list)):
                p.append(f"{w}: \"key\" isn't a text unit key.")
        elif not isinstance(e.get("find"), str) or not e.get("find", "").strip():
            p.append(f"{w}: needs \"find\" (the line's current text) or \"key\".")
        flags(w, e, ("para",))

    # replace
    for w, e in lst("replace"):
        if not isinstance(e.get("find"), str) or not e.get("find"):
            p.append(f"{w}: needs \"find\".")
        if not isinstance(e.get("replace"), str):
            p.append(f"{w}: needs \"replace\" (may be empty).")
        flags(w, e, ("regex", "case", "word"))
        pages_spec(w, e)

    # images
    for w, e in lst("images"):
        page_of(w, e)
        if "key" not in e and not (isinstance(e.get("index"), int) and e["index"] >= 0):
            p.append(f"{w}: needs \"index\" (0-based image on the page) or \"key\".")
        flags(w, e, ("delete",))
        if "rect" in e and not _rect(e["rect"]):
            p.append(f"{w}: \"rect\" must be [x0, y0, x1, y1].")
        if "turns" in e and not isinstance(e["turns"], int):
            p.append(f"{w}: \"turns\" must be an integer (quarter turns clockwise).")
        data_or_file(w, e, required=False)
        if not any(k in e for k in ("delete", "rect", "turns", "data", "file")):
            p.append(f"{w}: says nothing to do (delete, rect, turns, data or file).")

    # annotations
    from .items import DEFAULTS
    for w, e in lst("annotations"):
        t = e.get("type")
        page_of(w, e, required=not (t == "highlight" and "search" in e))
        if t not in ANNOT_TYPES:
            p.append(f"{w}: \"type\" must be one of {', '.join(ANNOT_TYPES)}.")
            continue
        if t in ("rect", "ellipse", "whiteout", "image") and not _rect(e.get("rect")):
            p.append(f"{w}: needs \"rect\": [x0, y0, x1, y1].")
        if t in ("line", "arrow") and not (_point(e.get("from")) and _point(e.get("to"))):
            p.append(f"{w}: needs \"from\" and \"to\": [x, y].")
        if t == "ink":
            strokes = e.get("strokes") if "strokes" in e else [e.get("points")]
            if not (isinstance(strokes, list) and strokes and all(
                    isinstance(s, list) and s and all(_point(q) for q in s) for s in strokes)):
                p.append(f"{w}: needs \"points\": [[x, y], ...] or \"strokes\": [[[x, y], ...], ...].")
            elif "pressure" in e:
                pr = e["pressure"] if "strokes" in e else [e["pressure"]]
                if not (isinstance(pr, list) and len(pr) == len(strokes) and all(
                        isinstance(q, list) and len(q) == len(s) and all(_num(x) and 0 <= x <= 1 for x in q)
                        for q, s in zip(pr, strokes))):
                    p.append(f"{w}: \"pressure\" must give one value 0-1 per point (a list per stroke with "
                             "\"strokes\").")
        if t == "highlight":
            if "search" in e:
                if not isinstance(e["search"], str) or not e["search"].strip():
                    p.append(f"{w}: \"search\" must be text.")
            elif not (isinstance(e.get("rects"), list) and e["rects"] and all(_rect(r) for r in e["rects"])):
                p.append(f"{w}: needs \"rects\": [[x0, y0, x1, y1], ...] or \"search\".")
        if t == "text":
            if not isinstance(e.get("text"), str):
                p.append(f"{w}: needs \"text\".")
            if not (_point(e.get("at")) or _rect(e.get("rect"))):
                p.append(f"{w}: needs \"at\": [x, y] (top left) or \"rect\".")
        if t == "note" and not _point(e.get("at")):
            p.append(f"{w}: needs \"at\": [x, y].")
        if t == "image":
            data_or_file(w, e)
        style = e.get("style", {})
        if not isinstance(style, dict):
            p.append(f"{w}: \"style\" must be an object.")
        else:
            allowed = set(DEFAULTS.get(t, {})) | ({"box_w"} if t == "text" else set())
            for k, v in style.items():
                if k not in allowed:
                    p.append(f"{w}: unknown style \"{k}\" for {t} (known: {', '.join(sorted(allowed)) or 'none'}).")
                elif k in ("color", "bg", "border") and not _color(v):
                    p.append(f"{w}: style \"{k}\" must be a color like \"#ff0000\".")
        for k in ("contents", "author"):
            if k in e and not isinstance(e[k], str):
                p.append(f"{w}: \"{k}\" must be text.")
        flags(w, e, ("signature", "regex", "case", "whole"))
        pages_spec(w, e)

    # redactions
    for w, e in lst("redactions"):
        if "search" in e or "preset" in e:
            pr = e.get("preset", "text")
            if pr not in REDACT_PRESETS and pr not in REDACT_PRESETS.values():
                p.append(f"{w}: unknown preset {pr!r} (one of {', '.join(REDACT_PRESETS)}).")
            if REDACT_PRESETS.get(pr, pr) in ("Text", "Custom regular expression") and \
                    not (isinstance(e.get("search"), str) and e["search"].strip()):
                p.append(f"{w}: needs \"search\".")
            pages_spec(w, e)
            flags(w, e, ("case", "whole"))
        else:
            page_of(w, e)
            if not _rect(e.get("rect")):
                p.append(f"{w}: needs \"rect\": [x0, y0, x1, y1] or \"search\".")
        if "color" in e and not _color(e["color"]):
            p.append(f"{w}: \"color\" must be a color like \"#000000\".")
        if "label" in e and not isinstance(e["label"], str):
            p.append(f"{w}: \"label\" must be text.")

    # crop
    for w, e in lst("crop"):
        if "pages" in e:
            pages_spec(w, e)
        else:
            page_of(w, e)
        how = [k for k in ("rect", "margins", "auto", "remove") if k in e]
        if len(how) != 1:
            p.append(f"{w}: use exactly one of \"rect\", \"margins\", \"auto\" or \"remove\".")
        if "rect" in e and not _rect(e["rect"]):
            p.append(f"{w}: \"rect\" must be [x0, y0, x1, y1].")
        if "margins" in e and not (_rect(e["margins"]) and min(e["margins"]) >= 0):
            p.append(f"{w}: \"margins\" must be [left, top, right, bottom] (0 or more).")
        if e.get("unit", "pt") not in ("pt", "mm", "in"):
            p.append(f"{w}: \"unit\" must be pt, mm or in.")
        flags(w, e, ("auto", "remove"))

    # metadata
    meta = data.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            p.append("\"metadata\" must be an object.")
        else:
            for k, v in meta.items():
                if k not in META_KEYS:
                    p.append(f"metadata.{k}: unknown (known: {', '.join(META_KEYS)}).")
                elif not isinstance(v, str):
                    p.append(f"metadata.{k}: must be text.")

    # decorations
    from .decorations import KINDS, Decoration
    dfields = {f.name: f for f in dc_fields(Decoration)}
    for w, e in lst("decorations"):
        if e.get("kind") not in KINDS:
            p.append(f"{w}: \"kind\" must be one of {', '.join(KINDS)}.")
        for k, v in e.items():
            if k == "kind" or k == "uid":
                continue
            if k not in dfields:
                p.append(f"{w}: unknown setting \"{k}\".")
            elif k == "slots":
                if not (isinstance(v, list) and len(v) == 6 and all(isinstance(x, str) for x in v)):
                    p.append(f"{w}: \"slots\" must be 6 texts (top left/center/right, bottom left/center/right).")
            elif k == "image":
                if not isinstance(v, str):
                    p.append(f"{w}: \"image\" must be base64 image data.")
            else:
                d = dfields[k].default
                ok = isinstance(v, bool) if isinstance(d, bool) else _num(v) if isinstance(d, (int, float)) \
                    else isinstance(v, str)
                if not ok:
                    p.append(f"{w}: \"{k}\" has the wrong type.")

    # sanitize
    san = data.get("sanitize")
    if san is not None:
        from .redact import Sanitize
        names = {f.name for f in dc_fields(Sanitize)}
        if not isinstance(san, dict):
            p.append("\"sanitize\" must be an object of option -> true/false.")
        else:
            for k, v in san.items():
                if k not in names:
                    p.append(f"sanitize.{k}: unknown (known: {', '.join(sorted(names))}).")
                elif not isinstance(v, bool):
                    p.append(f"sanitize.{k}: must be true or false.")

    # ocr
    ocr = data.get("ocr")
    if ocr is not None:
        if not isinstance(ocr, dict):
            p.append("\"ocr\" must be an object.")
        else:
            pages_spec("ocr", ocr)
            for k in ("language", "engine"):
                if k in ocr and not isinstance(ocr[k], str):
                    p.append(f"ocr.{k}: must be text.")
            if "dpi" in ocr and not (isinstance(ocr["dpi"], int) and 72 <= ocr["dpi"] <= 1200):
                p.append("ocr.dpi: must be 72-1200.")
            flags("ocr", ocr, ("only_if_needed",))
    return p


# ============================================================================================ describe
def entries(data: dict) -> list[tuple[tuple, str, str]]:
    """Every change of an edit set as (path, section label, description), for previews. `path` selects it in
    subset(): (section,) for a whole section, (section, index or key) for one item."""
    out = []
    pages = data.get("pages")
    if pages:
        bits = []
        for e in pages:
            if "blank" in e:
                bits.append("blank")
            elif "source" in e or "file" in e:
                bits.append(f"{os.path.basename(str(e.get('file') or e.get('source')))}:{e.get('page', 1)}")
            else:
                bits.append(str(e.get("page", f"uid {e.get('uid')}")))
            if e.get("rotate"):
                bits[-1] += f" ↻{e['rotate'] % 360}°"
        out.append((("pages",), SECTION_LABELS["pages"], f"{len(pages)} pages: " + _short(", ".join(bits), 80)))
    for k, v in (data.get("forms") or {}).items():
        if _scoped(v):
            txt = "; ".join(f"{_short(str(x.get('value')), 30)} (pages {_pgs(x) if x.get('pages') else 'all'})"
                            for x in ([v] if isinstance(v, dict) else v))
        else:
            txt = _short(v if isinstance(v, str) else repr(v))
        out.append((("forms", k), SECTION_LABELS["forms"], f"“{k}” = {txt}"))
    for i, e in enumerate(data.get("text_edits") or []):
        old = e.get("find") or e.get("original") or "text"
        out.append((("text_edits", i), SECTION_LABELS["text_edits"],
                     f"Page {_pg(e)}: “{_short(old, 36)}” → “{_short(e.get('text', ''), 36)}”"))
    for i, e in enumerate(data.get("replace") or []):
        where = f" (pages {_pgs(e)})" if e.get("pages") else ""
        out.append((("replace", i), SECTION_LABELS["replace"],
                     f"Replace “{_short(e.get('find', ''), 30)}” with “{_short(e.get('replace', ''), 30)}”{where}"))
    for i, e in enumerate(data.get("images") or []):
        what = "delete" if e.get("delete") else ", ".join(
            x for x in ("replace" if ("data" in e or "file" in e) else "", "move/resize" if "rect" in e else "",
                        f"rotate {90 * e.get('turns', 0)}°" if e.get("turns") else "") if x)
        n = e.get("index")
        out.append((("images", i), SECTION_LABELS["images"],
                     f"Page {_pg(e)}: image{f' #{n + 1}' if isinstance(n, int) else ''} — {what}"))
    for i, e in enumerate(data.get("annotations") or []):
        t = _TYPE_NAMES.get(e.get("type"), str(e.get("type")))
        extra = e.get("text") or e.get("contents") or e.get("search") or ""
        out.append((("annotations", i), SECTION_LABELS["annotations"],
                     f"Page {_pg(e)}: {t}" + (f" “{_short(extra, 40)}”" if extra else "")
                     if "search" not in e else f"Highlight every “{_short(extra, 40)}”"))
    for i, e in enumerate(data.get("redactions") or []):
        if "search" in e or "preset" in e:
            pr = REDACT_PRESETS.get(e.get("preset", "text"), e.get("preset"))
            what = f"“{_short(e.get('search', ''), 40)}”" if pr in ("Text", "Custom regular expression") else pr.lower()
            txt = f"Redact every {what}" + (f" (pages {_pgs(e)})" if e.get("pages") else "")
        else:
            txt = f"Page {_pg(e)}: redact area {_fmt_rect(e.get('rect'))}"
        out.append((("redactions", i), SECTION_LABELS["redactions"], txt))
    for i, e in enumerate(data.get("crop") or []):
        where = f"pages {_pgs(e)}" if "pages" in e else f"page {_pg(e)}"
        how = "remove crop" if e.get("remove") else "auto (whitespace)" if e.get("auto") else \
            f"margins {e['margins']} {e.get('unit', 'pt')}" if "margins" in e else f"to {_fmt_rect(e.get('rect'))}"
        out.append((("crop", i), SECTION_LABELS["crop"], f"Crop {where}: {how}"))
    for k, v in (data.get("metadata") or {}).items():
        out.append((("metadata", k), SECTION_LABELS["metadata"], f"{k.capitalize()}: “{_short(v)}”"))
    for i, e in enumerate(data.get("decorations") or []):
        try:
            from .decorations import describe
            txt = describe(_decoration(e))
        except Exception:  # noqa: BLE001
            txt = str(e.get("kind"))
        out.append((("decorations", i), SECTION_LABELS["decorations"], txt))
    if data.get("sanitize"):
        on = [k for k, v in data["sanitize"].items() if v]
        out.append((("sanitize",), SECTION_LABELS["sanitize"], "Remove " + ", ".join(on).replace("_", " ")))
    if data.get("ocr") is not None:
        o = data["ocr"]
        out.append((("ocr",), SECTION_LABELS["ocr"],
                     f"{o.get('language', 'eng')}, pages {_pgs(o) if o.get('pages') else 'all'}"))
    return out


def subset(data: dict, paths) -> dict:
    """A copy of `data` with only the changes at `paths` (see entries())."""
    paths = {tuple(p) for p in paths}
    out = {k: copy.deepcopy(v) for k, v in data.items() if k not in SECTIONS}
    for s in SECTIONS:
        v = data.get(s)
        if v is None:
            continue
        if (s,) in paths:
            out[s] = copy.deepcopy(v)
        elif isinstance(v, list):
            keep = [copy.deepcopy(e) for i, e in enumerate(v) if (s, i) in paths]
            if keep:
                out[s] = keep
        elif isinstance(v, dict) and s in ("forms", "metadata"):
            keep = {k: copy.deepcopy(x) for k, x in v.items() if (s, k) in paths}
            if keep:
                out[s] = keep
    return out


def _pg(e) -> str:
    return str(e["page"]) if "page" in e else f"uid {e.get('uid')}"


def _pgs(e) -> str:
    v = e.get("pages")
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)


def _fmt_rect(r) -> str:
    return "[" + ", ".join(f"{x:g}" for x in r) + "]" if _rect(r) else "?"


def _decoration(e: dict):
    from .decorations import Decoration
    kw = {k: v for k, v in e.items() if k not in ("uid",)}
    if "slots" in kw:
        kw["slots"] = tuple(kw["slots"])
    if isinstance(kw.get("image"), str):
        kw["image"] = unb64(kw["image"]) if kw["image"] else b""
    defaults = {"watermark": {"size": 60.0, "color": "#808080", "opacity": 0.3, "bold": True},
                "bates": {"margin_y": 18.0}}.get(kw.get("kind"), {})
    return Decoration(**{**defaults, **kw})


# ============================================================================================ export
def export_edits(canvas) -> dict:
    """The canvas' pending changes as an edit set, relative to the file as it was opened (its primary source).
    Changes an edit set can't carry are listed under document.not_exported."""
    from . import annots as A, textedit as T
    from .redact import is_mark

    doc = canvas.doc
    out = empty()
    skipped: list[str] = []
    prim = doc.primary
    out["document"] = {"name": os.path.basename(doc.path or prim.name), "pages": prim.doc.page_count}
    pos = {r.uid: i + 1 for i, r in enumerate(doc.pages)}
    if not doc.is_identity(doc.pages):
        sources: dict = {}
        for r in doc.pages:
            e: dict
            if r.src is prim:
                e = {"page": r.index + 1}
            elif r.src.name == "blank" and r.src.doc.page_count == 1:
                pr = r.src.doc[0].rect
                e = {"blank": [round(pr.width, 3), round(pr.height, 3)]}
            else:
                sid = next((k for k, s in sources.items() if s is r.src), None)
                if sid is None:
                    sid = f"s{len(sources) + 1}"
                    sources[sid] = r.src
                e = {"source": sid, "page": r.index + 1}
            if r.rotation:
                e["rotate"] = r.rotation
            out["pages"].append(e)
        if sources:
            out["sources"] = {k: {"name": s.name, "data": b64(s.data)} for k, s in sources.items()}

    # forms: by field name (every widget of a name takes the value), scoped to pages where copies differ
    if doc.form_values:
        out["forms"] = _export_forms(doc, pos)

    for r in doc.pages:
        for key, text in (doc.text_edits.get(r.uid) or {}).items():
            e = {"page": pos[r.uid], "key": listify(key), "text": text}
            try:
                e["original"] = T.original_text(doc, r, key)
            except Exception:  # noqa: BLE001
                pass
            out["text_edits"].append(e)
        edits = doc.image_edits.get(r.uid) or {}
        if edits:
            from . import images as I
            draws = I.draws(r.src, r.index)
            for key, ie in edits.items():
                e = {"page": pos[r.uid], "key": listify(key)}
                d = next((d for d in draws if d.key == key), None)
                if d is not None:
                    e["index"] = d.index
                if ie.delete:
                    e["delete"] = True
                if ie.rect is not None:
                    e["rect"] = [round(v, 3) for v in ie.rect]
                if ie.turns % 4:
                    e["turns"] = ie.turns % 4
                if ie.data is not None:
                    e["data"] = b64(ie.data)
                out["images"].append(e)

    # overlays
    adopted_changed = 0
    others = 0
    for r in doc.pages:
        pg = canvas.items_by_uid.get(r.uid)
        if pg is None:
            continue
        for it in pg.overlays():
            if isinstance(it, A.Adopted):
                adopted_changed += it.is_dirty()
                continue
            e = _export_item(it, pos[r.uid])
            if e is None:
                others += 1
            elif is_mark(it):
                out["redactions"].append(e)
            else:
                out["annotations"].append(e)
        adopted_changed += sum(1 for it in (pg.adopted or []) if it.parentItem() is None)
    if adopted_changed:
        skipped.append(f"{adopted_changed} change(s) to annotations already in the file")
    if others:
        skipped.append(f"{others} item(s) of other kinds (new form fields, replies)")

    crops = [(pos[r.uid], r.crop) for r in doc.pages if r.crop is not None]
    for p, c in crops:
        out["crop"].append({"page": p, "rect": [round(v, 3) for v in c]})
    if doc.metadata_edits:
        out["metadata"] = {k: v for k, v in doc.metadata_edits.items() if k in META_KEYS}
    for spec in doc.decorations or []:
        d = {k: v for k, v in asdict(spec).items() if k != "uid"}
        d["slots"] = list(spec.slots)
        d["image"] = b64(spec.image) if spec.image else ""
        out["decorations"].append(d)
    if doc.sanitize:
        out["sanitize"] = {k: v for k, v in asdict(doc.sanitize).items() if v}
    if doc.ocr:
        rows = [pos[r.uid] for r in doc.pages if r.uid in doc.ocr]
        if rows:
            first = doc.ocr[next(r.uid for r in doc.pages if r.uid in doc.ocr)]
            out["ocr"] = {"pages": rows, "language": first.language or "eng", "engine": first.engine or "auto"}
    if doc.links:
        skipped.append("link edits")
    if doc.outline is not None:
        skipped.append("bookmark edits")
    if doc.attachments is not None:
        skipped.append("attachment edits")
    if doc.security is not None:
        skipped.append("password protection")
    if skipped:
        out["document"]["not_exported"] = skipped
    return compact(out)


def _export_forms(doc, pos: dict) -> dict:
    import pymupdf

    from .document import rect_key
    radio, check = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    per: dict[str, dict[int, object]] = {}  # name -> page -> value (as forms.collect_values gives it)
    where: dict[str, set] = {}  # name -> pages with a widget of that name
    for ref in doc.pages:
        page = doc.page(ref)
        if not page.first_widget:
            continue
        p = pos[ref.uid]
        for w in page.widgets():
            name = w.field_name
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                continue
            where.setdefault(name, set()).add(p)
            key = (ref.uid, name, rect_key(w.rect))
            if key not in doc.form_values:
                continue
            v = doc.form_values[key]
            d = per.setdefault(name, {})
            if w.field_type == radio:
                if v:
                    d[p] = str(w.on_state())
                else:
                    d.setdefault(p, "Off")
            elif w.field_type == check:
                d[p] = bool(v) or bool(d.get(p))
            else:
                d[p] = list(v) if isinstance(v, (list, tuple)) else ("" if v is None else v)
    out: dict = {}
    for name in sorted(per):
        groups: list[tuple] = []
        for p, v in sorted(per[name].items()):
            g = next((g for g in groups if g[0] == v), None)
            if g is None:
                groups.append((v, [p]))
            else:
                g[1].append(p)
        if len(groups) == 1 and set(groups[0][1]) == where.get(name, set()):
            out[name] = groups[0][0]
        else:
            scoped = [{"value": v, "pages": ps} for v, ps in groups]
            out[name] = scoped[0] if len(scoped) == 1 else scoped
    return out


def _r(r) -> list:
    return [round(r.left(), 3), round(r.top(), 3), round(r.right(), 3), round(r.bottom(), 3)]


def _p(p) -> list:
    return [round(p.x(), 3), round(p.y(), 3)]


def _style(it) -> dict:
    from .items import DEFAULTS
    base = DEFAULTS.get(it.kind, {})
    return {k: v for k, v in it.props.items() if k in base or (it.kind == "text" and k == "box_w")}


def _export_item(it, page: int) -> dict | None:
    from .items import HighlightItem, ImageItem, InkItem, LineItem, NoteItem, ShapeItem, TextItem
    from .redact import RedactItem

    e: dict = {"page": page}
    if isinstance(it, RedactItem):
        e["rect"] = _r(it.scene_rect_in_parent())
        if it.props.get("color") and it.props["color"] != "#000000":
            e["color"] = it.props["color"]
        if it.props.get("text"):
            e["label"] = it.props["text"]
        return e
    if isinstance(it, NoteItem):
        e.update(type="note", at=_p(it.scene_rect_in_parent().topLeft()))
    elif isinstance(it, ImageItem):
        e.update(type="image", rect=_r(it.scene_rect_in_parent()), data=b64(it.png()))
        if it.kind == "signature":
            e["signature"] = True
    elif isinstance(it, ShapeItem) and it.kind in ("rect", "ellipse", "whiteout"):
        e.update(type=it.kind, rect=_r(it.scene_rect_in_parent()))
    elif isinstance(it, LineItem):
        e.update({"type": it.kind, "from": _p(it.mapToParent(it.p1)), "to": _p(it.mapToParent(it.p2))})
    elif isinstance(it, InkItem):
        e.update(type="ink", points=[_p(it.mapToParent(q)) for q in it.pts])
        if getattr(it, "has_pressure", lambda: False)():
            e["pressure"] = [round(float(w), 4) for w in it.widths]
    elif isinstance(it, HighlightItem):
        e.update(type="highlight", rects=[_r(it.mapRectToParent(q)) for q in it.rects])
        if it.marked_text:
            e["text"] = it.marked_text
    elif isinstance(it, TextItem):
        if not it.toPlainText().strip():
            return None
        e.update(type="text", at=_p(it.pos()), text=it.content())
    else:
        return None
    style = _style(it)
    if style:
        e["style"] = style
    meta = getattr(it, "meta", {}) or {}
    if meta.get("contents"):
        e["contents"] = meta["contents"]
    if meta.get("author"):
        e["author"] = meta["author"]
    return e


# ============================================================================================ apply
class _Target:
    """Where an edit set's changes go: the canvas (undoable) or the bare document (headless)."""

    # words in a warning that mean a change was not made (CLI exit codes, report["refused"])
    _REFUSAL = ("skipped", "can’t edit", "couldn’t be applied", "no matches", "failed", "no OCR engine",
                "couldn’t read", "would be empty")

    def __init__(self, doc):
        self.doc = doc
        self.warnings: list[str] = []
        self.refused: list[str] = []

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)
            if any(k in text for k in self._REFUSAL):
                self.refused.append(text)


class _DocTarget(_Target):
    def set_pages(self, state) -> None:
        for ref, rot in state:
            ref.rotation = rot
        self.doc.pages = [ref for ref, _ in state]

    def set_form_values(self, vals: dict) -> None:
        self.doc.form_values.update(vals)

    def set_text_edits(self, changes) -> tuple[dict, dict]:
        """changes: [(ref, key, new)]. Returns (failed {uid: reason}, warnings {uid: [..]})."""
        doc = self.doc
        by: dict[int, tuple] = {}
        for ref, key, new in changes:
            by.setdefault(ref.uid, (ref, []))[1].append((key, new))
        failed, warns = {}, {}
        for uid, (ref, items) in by.items():
            before = dict(doc.text_edits.get(uid) or {})
            edits = dict(before)
            for key, new in items:
                if new is None:
                    edits.pop(key, None)
                else:
                    edits[key] = new
            if edits:
                doc.text_edits[uid] = edits
            else:
                doc.text_edits.pop(uid, None)
            try:
                warns[uid] = _sync(doc, ref)
            except Exception as ex:  # noqa: BLE001  (textedit.Refuse or a MuPDF error)
                if before:
                    doc.text_edits[uid] = before
                else:
                    doc.text_edits.pop(uid, None)
                _sync(doc, ref)
                failed[uid] = str(ex) or "The change couldn’t be applied."
        return failed, warns

    def set_image_edits(self, per: dict) -> None:
        for uid, (ref, edits) in per.items():
            if edits:
                self.doc.image_edits[uid] = edits
            else:
                self.doc.image_edits.pop(uid, None)
            _sync(self.doc, ref)

    def add_items(self, items) -> None:
        reg = self.doc.__dict__.setdefault("_editset_overlays", {})
        for ref, it in items:
            reg.setdefault(ref.uid, []).append(it)

    def set_crops(self, crops: dict) -> None:
        for ref, c in crops.items():
            ref.crop = c

    def set_metadata(self, new: dict) -> None:
        self.doc.metadata_edits = new

    def set_decorations(self, specs) -> None:
        self.doc.decorations = list(specs)

    def set_sanitize(self, spec) -> None:
        self.doc.sanitize = spec

    def set_ocr(self, changes: dict) -> None:
        for uid, e in changes.items():
            self.doc.set_ocr(uid, e)


def _sync(doc, ref) -> list[str]:
    """Headless: show page `ref` with its text and image edits (keeps search/plans of later steps right)."""
    ref.src.set_image_edits(ref.uid, ref.index, doc.image_edits.get(ref.uid))
    return ref.src.set_text_edits(ref.uid, ref.index, dict(doc.text_edits.get(ref.uid, {})))


class _CanvasTarget(_Target):
    def __init__(self, canvas, label: str):
        super().__init__(canvas.doc)
        self.cv = canvas
        self.label = label
        self.pushed = 0

    def push(self, cmd) -> None:
        if not self.pushed:
            self.cv.undo.beginMacro(self.label)
        self.pushed += 1
        self.cv.undo.push(cmd)

    def finish(self) -> None:
        if self.pushed:
            self.cv.undo.endMacro()

    def set_pages(self, state) -> None:
        from . import commands as C, pageops
        self.push(C.SetPages(self.cv, pageops.state(self.doc), state, "Change pages"))

    def set_form_values(self, vals: dict) -> None:
        from . import commands as C
        changes = {k: (self.doc.form_values.get(k, C.UNSET), v) for k, v in vals.items()}
        self.push(C.SetFormValues(self.cv, changes))

    def set_text_edits(self, changes) -> tuple[dict, dict]:
        from . import commands as C
        cmd = C.EditPageTexts(self.cv, changes, "Edit page text")
        if cmd.isObsolete():
            return {}, {}
        self.push(cmd)
        return dict(cmd.failed), dict(cmd.warnings)

    def set_image_edits(self, per: dict) -> None:
        self.push(_SetImageEdits(self.cv, {uid: (ref, self.doc.image_edits.get(uid), edits or None)
                                           for uid, (ref, edits) in per.items()}))

    def add_items(self, items) -> None:
        from . import commands as C
        its = []
        for ref, it in items:
            it.setParentItem(self.cv.page_item(ref).content)
            its.append(it)
        self.push(C.AddItems(its, "Add"))

    def set_crops(self, crops: dict) -> None:
        from .crop import SetCrop
        changes = {ref: (ref.crop, c) for ref, c in crops.items() if ref.crop != c}
        if changes:
            self.push(SetCrop(self.cv, changes, "Crop pages"))

    def set_metadata(self, new: dict) -> None:
        from .docprops import SetMetadata
        self.push(SetMetadata(self.doc, new))

    def set_decorations(self, specs) -> None:
        from .decorations import SetDecorations
        self.push(SetDecorations(self.cv, specs, "Watermarks & headers"))

    def set_sanitize(self, spec) -> None:
        from .redact import SetSanitize
        self.push(SetSanitize(self.doc, spec))

    def set_ocr(self, changes: dict) -> None:
        self.push(_SetOcr(self.cv, changes))


def _qundo():
    from PySide6.QtGui import QUndoCommand
    return QUndoCommand


class _SetImageEdits(_qundo()):
    def __init__(self, canvas, per: dict):
        """per: uid -> (ref, old edits or None, new edits or None)"""
        super().__init__("Edit images")
        self.cv, self.per = canvas, per

    def _set(self, idx: int) -> None:
        doc = self.cv.doc
        for uid, pair in self.per.items():
            ref, v = pair[0], pair[idx]
            if v:
                doc.image_edits[uid] = dict(v)
            else:
                doc.image_edits.pop(uid, None)
            ref.src.set_image_edits(ref.uid, ref.index, doc.image_edits.get(uid))
            self.cv.sync_text(ref)

    def redo(self) -> None:
        self._set(2)

    def undo(self) -> None:
        self._set(1)


class _SetOcr(_qundo()):
    def __init__(self, canvas, changes: dict):
        super().__init__("Recognize text")
        self.cv = canvas
        self.new = dict(changes)
        self.old = {uid: canvas.doc.ocr.get(uid) for uid in changes}

    def _set(self, values: dict) -> None:
        for uid, e in values.items():
            self.cv.doc.set_ocr(uid, e)
        win = self.cv.window()
        try:
            from .features.ocr import _changed
            _changed(win)
        except Exception:  # noqa: BLE001  (no window: tests, headless canvases)
            pass
        for it in self.cv.items_by_uid.values():
            it.invalidate()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def apply_edits(canvas, data: dict, *, label: str = "Apply edit set", base_dir: str | None = None) -> list[str]:
    """Apply an edit set to the canvas' document as ONE undo step. Returns warnings and refusals (each a sentence);
    a malformed edit set changes nothing and returns its problems."""
    problems = validate(data)
    if problems:
        return [f"Invalid edit set: {p}" for p in problems]
    t = _CanvasTarget(canvas, label)
    try:
        _apply(t, data, base_dir)
    except BaseException:
        t.finish()
        if t.pushed:
            canvas.undo.undo()
        raise
    t.finish()
    return t.warnings


def apply_to_document(doc, data: dict, *, base_dir: str | None = None, report: dict | None = None) -> list[str]:
    """Apply an edit set to a PdfDocument without a canvas (overlays are kept on the document for save()).
    Returns warnings and refusals; raises EditSetError when the edit set is malformed. `report` (optional dict)
    receives "refused" (the warnings that are refusals/skips), "replaced", "redaction_marks", "ocr_pages"."""
    problems = validate(data)
    if problems:
        raise EditSetError("\n".join(problems))
    t = _DocTarget(doc)
    _apply(t, data, base_dir)
    if report is not None:
        report["refused"] = list(t.refused)
        for k in ("replaced", "redaction_marks", "ocr_pages"):
            report[k] = t.__dict__.get(k, 0)
    return t.warnings


def overlays_for(doc):
    """The overlays_for callable for doc.save() after apply_to_document."""
    reg = doc.__dict__.get("_editset_overlays", {})
    return lambda ref: sorted(reg.get(ref.uid, []), key=lambda it: it.zValue())


def overlay_count(doc) -> int:
    return sum(len(v) for v in doc.__dict__.get("_editset_overlays", {}).values())


def save(doc, target: str, fast: bool = False) -> bool:
    """Save a document changed by apply_to_document (overlays included). Returns True if saved incrementally."""
    return doc.save(target, overlays_for(doc), fast=fast)


# ---------------------------------------------------------------------------------------------- engine
def _rows(spec, n: int) -> list[int]:
    """0-based rows of a "pages" spec (None = all)."""
    from .pageops import parse_ranges
    if spec is None:
        return list(range(n))
    if isinstance(spec, list):
        bad = [x for x in spec if not 1 <= x <= n]
        if bad:
            raise ValueError(f"Page {bad[0]} doesn't exist (1–{n})")
        return [x - 1 for x in dict.fromkeys(spec)]
    return parse_ranges(str(spec), n)


class _Skip(Exception):
    pass


def _ref(t: _Target, e: dict, where: str):
    pages = t.doc.pages
    if "uid" in e:
        r = next((r for r in pages if r.uid == e["uid"]), None)
        if r is None:
            t.warn(f"{where}: no page with uid {e['uid']}; skipped.")
            raise _Skip
        return r
    n = e["page"]
    if not 1 <= n <= len(pages):
        t.warn(f"{where}: page {n} doesn't exist (the document has {len(pages)}); skipped.")
        raise _Skip
    return pages[n - 1]


def _read(e: dict, base_dir: str | None) -> bytes:
    if "data" in e:
        return unb64(e["data"])
    path = e["file"]
    if base_dir and not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    with open(path, "rb") as f:
        return f.read()


def _apply(t: _Target, data: dict, base_dir: str | None) -> None:
    for s in SECTIONS:
        if data.get(s) in (None, [], {}):
            continue
        globals()[f"_do_{s}"](t, data, base_dir)


def _do_pages(t: _Target, data: dict, base_dir) -> None:
    from .document import PageRef
    doc = t.doc
    base = list(doc.pages)
    used: set[int] = set()
    srcs: dict = {}
    state = []
    for i, e in enumerate(data["pages"]):
        where = f"pages[{i}]"
        rot = int(e.get("rotate", 0))
        if "blank" in e:
            w, h = e["blank"]
            ref = PageRef(doc.add_blank_source(float(w), float(h)), 0)
            state.append((ref, rot % 360))
            continue
        if "source" in e or "file" in e:
            key = ("s", e["source"]) if "source" in e else ("f", e["file"])
            src = srcs.get(key)
            if src is None:
                try:
                    if "source" in e:
                        s = data["sources"][e["source"]]
                        src = doc.add_pdf_bytes(s.get("name") or e["source"], unb64(s["data"]))
                    else:
                        path = e["file"]
                        if base_dir and not os.path.isabs(path):
                            path = os.path.join(base_dir, path)
                        src = doc.add_source(path)
                except Exception as ex:  # noqa: BLE001
                    t.warn(f"{where}: couldn’t read {e.get('file') or e.get('source')}: {ex}; page skipped.")
                    continue
                srcs[key] = src
            k = e.get("page", 1)
            if not 1 <= k <= src.doc.page_count:
                t.warn(f"{where}: page {k} doesn't exist in {src.name} ({src.doc.page_count} pages); skipped.")
                continue
            state.append((PageRef(src, k - 1), rot % 360))
            continue
        if "uid" in e:
            ref = next((r for r in base if r.uid == e["uid"]), None)
        else:
            ref = base[e["page"] - 1] if 1 <= e["page"] <= len(base) else None
        if ref is None:
            t.warn(f"{where}: page {e.get('page', e.get('uid'))} doesn't exist (the document has {len(base)}); "
                   "skipped.")
            continue
        if ref.uid in used:  # a second copy of the page: a new page of the list
            ref = PageRef(ref.src, ref.index, crop=ref.crop)
        else:
            used.add(ref.uid)
        state.append((ref, (ref.rotation + rot) % 360))
    if not state:
        t.warn("The page list would be empty; pages left as they are.")
        return
    t.set_pages(state)


def _do_forms(t: _Target, data: dict, base_dir) -> None:
    from .forms import collect_values, value_changes
    want = data["forms"]
    have = collect_values(t.doc)
    unknown = [k for k in want if k not in have]
    if unknown:
        t.warn(f"No form field named {', '.join(f'“{k}”' for k in unknown)}; skipped.")
    plain = {k: v for k, v in want.items() if k in have and not _scoped(v)}
    vals = value_changes(t.doc, plain) if plain else {}
    for k, v in want.items():
        if k not in have or not _scoped(v):
            continue
        for x in ([v] if isinstance(v, dict) else v):
            try:
                rows = _rows(x.get("pages"), len(t.doc.pages))
            except ValueError as ex:
                t.warn(f"forms.{k}: {ex}; skipped.")
                continue
            vals.update(value_changes(_PagesView(t.doc, [t.doc.pages[r] for r in rows]), {k: x["value"]}))
    if vals:
        t.set_form_values(vals)


def _scoped(v) -> bool:
    """A page-scoped form value: {"value": v, "pages": [...]} or a list of them."""
    return isinstance(v, dict) or (isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v))


class _PagesView:
    """A document restricted to some pages (forms.value_changes walks doc.pages)."""

    def __init__(self, doc, pages):
        self.pages, self.form_values, self.page = pages, doc.form_values, doc.page


def find_units(doc, ref, text: str, para: bool = False) -> list:
    """Text units (lines, or paragraphs with `para`) of page `ref` as shown whose text is `text` (whitespace
    normalized). Each is (key, current text)."""
    from . import textedit as T
    want = _norm(text)
    if not para:
        return [(s.key, s.text) for s in T.shown_segments(doc, ref) if _norm(s.text) == want]
    pt = T.page_text(ref.src, ref.index)
    edits = (doc.text_edits or {}).get(ref.uid, {})
    out = [(k, v) for k, v in edits.items() if _norm(v) == want]
    for p in pt.paragraphs():
        k = T.unit_key(p)
        if k not in edits and _norm(T._unit_text(p)) == want:
            out.append((k, T._unit_text(p)))
    return out


def _candidates(doc, ref, text: str) -> list[str]:
    from . import textedit as T
    try:
        lines = [s.text for s in T.shown_segments(doc, ref)]
    except Exception:  # noqa: BLE001
        return []
    return difflib.get_close_matches(_norm(text), [_norm(x) for x in lines], n=3, cutoff=0.5)


def _do_text_edits(t: _Target, data: dict, base_dir) -> None:
    from . import textedit as T
    doc = t.doc
    changes = []
    where_of: dict = {}
    for i, e in enumerate(data["text_edits"]):
        where = f"text_edits[{i}]"
        try:
            ref = _ref(t, e, where)
        except _Skip:
            continue
        pno = doc.pages.index(ref) + 1
        if "key" in e:
            key = tuplify(e["key"])
            try:
                T.page_text(ref.src, ref.index).find(key)
            except T.Refuse:
                t.warn(f"Page {pno}: the text to edit isn’t on this page (it belongs to another page or file); "
                       "skipped.")
                continue
        else:
            found = find_units(doc, ref, e["find"], bool(e.get("para")))
            if not found:
                near = _candidates(doc, ref, e["find"])
                hint = f" Did you mean {' or '.join(f'“{_short(c, 50)}”' for c in near)}?" if near else ""
                t.warn(f"Page {pno}: no {'paragraph' if e.get('para') else 'line'} reads “{_short(e['find'])}”; "
                       f"skipped.{hint}")
                continue
            if len(found) > 1:
                t.warn(f"Page {pno}: {len(found)} lines read “{_short(e['find'])}”; all of them are changed.")
            keys = [k for k, _ in found]
            key = None
        for k in ([key] if key is not None else keys):
            try:
                warns = T.check(doc, ref, T.KeyUnit(k), e["text"])
            except T.Refuse as ex:
                t.warn(f"Page {pno}: can’t edit “{_short(T.original_text(doc, ref, k))}”: {ex}")
                continue
            for w in warns:
                t.warn(f"Page {pno}: {w}")
            new = e["text"]
            if new.rstrip() == T.original_text(doc, ref, k).rstrip():
                new = None  # back to the original: removes the edit
            changes.append((ref, k, new))
            where_of[ref.uid] = pno
    if changes:
        failed, _ = t.set_text_edits(changes)
        for uid, why in failed.items():
            t.warn(f"Page {where_of.get(uid, '?')}: text edits couldn’t be applied: {why}")


def _do_replace(t: _Target, data: dict, base_dir) -> None:
    from . import replace as R
    doc = t.doc
    for i, e in enumerate(data["replace"]):
        try:
            rx = R.compile_query(e["find"], bool(e.get("case")), bool(e.get("word")), bool(e.get("regex")))
            R.check_template(rx, e["replace"], bool(e.get("regex")))
            rows = _rows(e.get("pages"), len(doc.pages))
        except (R.PatternError, ValueError) as ex:
            t.warn(f"replace[{i}]: {ex}")
            continue
        tot = R.Totals()
        for row in rows:
            ref = doc.pages[row]
            tot.add(row + 1, R.plan_page(doc, ref, row + 1, rx, e["replace"], bool(e.get("regex"))))
        for s in tot.skipped:
            t.warn(f"Page {s.pno}: skipped “{_short(s.excerpt or e['find'])}” ({s.label}): {s.reason}")
        for w in tot.warnings():
            t.warn(w)
        if tot.changes:
            failed, _ = t.set_text_edits(tot.changes)
            for uid, why in failed.items():
                pno = next((k + 1 for k, r in enumerate(doc.pages) if r.uid == uid), "?")
                t.warn(f"Page {pno}: replacements couldn’t be applied: {why}")
        elif not tot.skipped:
            t.warn(f"Replace “{_short(e['find'])}”: no matches.")
        t.__dict__["replaced"] = t.__dict__.get("replaced", 0) + tot.replaced


def _do_images(t: _Target, data: dict, base_dir) -> None:
    from . import images as I
    doc = t.doc
    per: dict = {}
    for i, e in enumerate(data["images"]):
        where = f"images[{i}]"
        try:
            ref = _ref(t, e, where)
        except _Skip:
            continue
        pno = doc.pages.index(ref) + 1
        draws = I.draws(ref.src, ref.index)
        if "key" in e:
            d = I.find(ref.src, ref.index, tuplify(e["key"]))
        else:
            d = draws[e["index"]] if e["index"] < len(draws) else None
        if d is None:
            what = f"#{e['index'] + 1}" if "key" not in e else "(by key)"
            t.warn(f"Page {pno}: image {what} not found (the page has {len(draws)} image(s)); skipped.")
            continue
        try:
            I.check(ref.src, ref.index, d)
            payload = _read(e, base_dir) if ("data" in e or "file" in e) else None
            if payload is not None:
                I.image_info(payload)
        except I.Refuse as ex:
            t.warn(f"Page {pno}: can’t edit image #{d.index + 1}: {ex}")
            continue
        except Exception as ex:  # noqa: BLE001
            t.warn(f"Page {pno}: image #{d.index + 1}: the replacement isn’t a usable image ({ex}); skipped.")
            continue
        edit = I.ImageEdit(delete=bool(e.get("delete")), rect=tuple(e["rect"]) if "rect" in e else None,
                           turns=int(e.get("turns", 0)) % 4, data=payload)
        cur = per.setdefault(ref.uid, (ref, dict(doc.image_edits.get(ref.uid) or {})))[1]
        if edit.is_noop:
            cur.pop(d.key, None)
        else:
            cur[d.key] = edit
    if per:
        t.set_image_edits(per)


def _props(kind: str, e: dict) -> dict:
    from .items import DEFAULTS
    p = dict(DEFAULTS.get(kind, {}))
    p.update(e.get("style") or {})
    return p


def _make_item(t: _Target, e: dict, ref, pno: int, base_dir):
    """The overlay item(s) for an annotation entry (Qt needed)."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QImage

    from .items import HighlightItem, ImageItem, InkItem, LineItem, NoteItem, ShapeItem, TextItem
    kind = e["type"]
    props = _props(kind, e)

    def qr(r):
        return QRectF(QPointF(r[0], r[1]), QPointF(r[2], r[3])).normalized()

    items = []
    if kind in ("rect", "ellipse", "whiteout"):
        items.append(ShapeItem(kind, qr(e["rect"]), props))
    elif kind in ("line", "arrow"):
        items.append(LineItem(kind, QPointF(*e["from"]), QPointF(*e["to"]), props))
    elif kind == "ink":
        strokes = e["strokes"] if "strokes" in e else [e["points"]]
        pressure = (e["pressure"] if "strokes" in e else [e["pressure"]]) if "pressure" in e else [None] * len(strokes)
        for stroke, pr in zip(strokes, pressure):
            p0 = QPointF(*stroke[0])
            it = InkItem(QPointF(0, 0), props)
            it.setPos(p0)
            it.prepareGeometryChange()
            it.pts = [QPointF(x, y) - p0 for x, y in stroke]
            if pr is not None and hasattr(it, "widths"):
                it.widths = [float(x) for x in pr]  # pen pressure per point
            for attr in ("_cache", "_ocache"):
                if hasattr(it, attr):
                    setattr(it, attr, None)
            items.append(it)
    elif kind == "highlight":
        if "search" in e:
            from .redact import PRESETS, build_regex, search_page
            preset = PRESETS[-1] if e.get("regex") else PRESETS[0]
            try:
                rx = build_regex(e["search"], preset, bool(e.get("case")), bool(e.get("whole")))
                rows = _rows(e.get("pages"), len(t.doc.pages)) if "page" not in e else [pno - 1]
            except (re.error, ValueError) as ex:
                t.warn(f"Highlight “{_short(e['search'])}”: {ex}")
                return []
            hits = [h for row in rows for h in search_page(t.doc, row, rx)]
            if not hits:
                t.warn(f"Highlight “{_short(e['search'])}”: no matches.")
            out = []
            for h in hits:
                it = HighlightItem(h.rects, props)
                it.marked_text = h.text
                _meta(it, e)
                out.append((t.doc.pages[h.page], it))
            return out
        it = HighlightItem([qr(r) for r in e["rects"]], props)
        it.marked_text = e.get("text", "")
        items.append(it)
    elif kind == "text":
        if "rect" in e and "at" not in e:
            r = qr(e["rect"])
            at = r.topLeft()
            if not (e.get("style") or {}).get("box_w"):
                props["box_w"] = r.width()
        else:
            at = QPointF(*e["at"])
        items.append(TextItem(at, props, e["text"]))
    elif kind == "note":
        items.append(NoteItem(QPointF(*e["at"]), props, {"contents": e.get("contents", "")}))
    elif kind == "image":
        try:
            raw = _read(e, base_dir)
        except OSError as ex:
            t.warn(f"Page {pno}: image {e.get('file')}: {ex.strerror or ex}; skipped.")
            return []
        img = QImage.fromData(raw)
        if img.isNull():
            t.warn(f"Page {pno}: the image isn’t a readable picture; skipped.")
            return []
        k = "signature" if e.get("signature") else "image"
        items.append(ImageItem(k, qr(e["rect"]), img, _props(k, e)))
    for it in items:
        _meta(it, e)
    return [(ref, it) for it in items]


def _meta(it, e: dict) -> None:
    from .items import ANNOT_KINDS
    if it.kind not in ANNOT_KINDS:
        return
    if e.get("contents") is not None:
        it.meta["contents"] = e["contents"]
    if e.get("author"):
        it.meta["author"] = e["author"]


def _do_annotations(t: _Target, data: dict, base_dir) -> None:
    ensure_qt()
    items = []
    for i, e in enumerate(data["annotations"]):
        where = f"annotations[{i}]"
        if e.get("type") == "highlight" and "search" in e and "page" not in e and "uid" not in e:
            items += _make_item(t, e, None, 0, base_dir)
            continue
        try:
            ref = _ref(t, e, where)
        except _Skip:
            continue
        items += _make_item(t, e, ref, t.doc.pages.index(ref) + 1, base_dir)
    if items:
        t.add_items(items)


def _do_redactions(t: _Target, data: dict, base_dir) -> None:
    ensure_qt()
    from PySide6.QtCore import QPointF, QRectF

    from .redact import KIND, PRESETS, RedactItem, build_regex, search_page
    doc = t.doc
    items = []
    by_name = {p.name: p for p in PRESETS}
    for i, e in enumerate(data["redactions"]):
        props = {"color": e.get("color") or "#000000", "text": e.get("label", "")}
        if "search" in e or "preset" in e:
            preset = by_name[REDACT_PRESETS.get(e.get("preset", "text"), e.get("preset", "Text"))]
            try:
                rx = build_regex(e.get("search", ""), preset, bool(e.get("case")), bool(e.get("whole")))
                rows = _rows(e.get("pages"), len(doc.pages))
            except (re.error, ValueError) as ex:
                t.warn(f"redactions[{i}]: {ex}")
                continue
            hits = [h for row in rows for h in search_page(doc, row, rx, preset.check)]
            if not hits:
                t.warn(f"Redact {_short(e.get('search') or preset.name)}: no matches.")
            for h in hits:
                lines = [h.text] * len(h.rects)
                for k, r in enumerate(h.rects):
                    items.append((doc.pages[h.page], RedactItem(KIND, QRectF(r), props, lines[k])))
            continue
        try:
            ref = _ref(t, e, f"redactions[{i}]")
        except _Skip:
            continue
        r = e["rect"]
        items.append((ref, RedactItem(KIND, QRectF(QPointF(r[0], r[1]), QPointF(r[2], r[3])).normalized(), props,
                                      e.get("text", ""))))
    if items:
        t.add_items(items)
    t.__dict__["redaction_marks"] = t.__dict__.get("redaction_marks", 0) + len(items)


def _do_crop(t: _Target, data: dict, base_dir) -> None:
    from . import crop as K
    doc = t.doc
    crops: dict = {}
    unit = {"pt": 1.0, "mm": 72 / 25.4, "in": 72.0}
    for i, e in enumerate(data["crop"]):
        try:
            if "pages" in e:
                refs = [doc.pages[r] for r in _rows(e["pages"], len(doc.pages))]
            else:
                refs = [_ref(t, e, f"crop[{i}]")]
        except _Skip:
            continue
        except ValueError as ex:
            t.warn(f"crop[{i}]: {ex}")
            continue
        for ref in refs:
            pno = doc.pages.index(ref) + 1
            if e.get("remove"):
                c = None
            elif "rect" in e:
                c = K.clamp(doc.page_rect(ref), e["rect"])
            elif "margins" in e:
                f = unit[e.get("unit", "pt")]
                c = K.crop_from_margins(doc, ref, [v * f for v in e["margins"]])
            else:
                c = K.auto_crop(doc, ref, float(e.get("pad", 6.0)))
            if c is None and not e.get("remove"):
                t.warn(f"Page {pno}: nothing to crop (the crop would be empty, tiny or the whole page); skipped.")
                continue
            crops[ref] = c
    if crops:
        t.set_crops(crops)


def _do_metadata(t: _Target, data: dict, base_dir) -> None:
    new = dict(t.doc.metadata_edits)
    new.update({k: v for k, v in data["metadata"].items() if k in META_KEYS})
    if new != t.doc.metadata_edits:
        t.set_metadata(new)


def _do_decorations(t: _Target, data: dict, base_dir) -> None:
    ensure_qt()
    specs = []
    for i, e in enumerate(data["decorations"]):
        try:
            specs.append(_decoration(e))
        except Exception as ex:  # noqa: BLE001
            t.warn(f"decorations[{i}]: {ex}")
    if specs:
        t.set_decorations(list(t.doc.decorations or []) + specs)


def _do_sanitize(t: _Target, data: dict, base_dir) -> None:
    from .redact import Sanitize
    cur = asdict(t.doc.sanitize) if t.doc.sanitize else {}
    cur.update(data["sanitize"])
    spec = Sanitize(**cur)
    t.set_sanitize(spec if spec else None)


def _do_ocr(t: _Target, data: dict, base_dir) -> None:
    from . import ocr as O
    doc = t.doc
    o = data["ocr"]
    lang = o.get("language") or "eng"
    try:
        rows = _rows(o.get("pages"), len(doc.pages))
    except ValueError as ex:
        t.warn(f"ocr: {ex}")
        return
    engines = O.available_engines()
    info = O.choose_engine(engines, lang, o.get("engine") or "auto")
    if info is None:
        have = ", ".join(sorted({c for e in engines for c, _ in e.languages})) or "none"
        t.warn(f"Recognize text: no OCR engine reads “{lang}” here (languages: {have}).")
        return
    engine = O.create_engine(info.key, info.tessdata_for(lang))
    changes = {}
    try:
        groups: dict = {}
        for row in rows:
            ref = doc.pages[row]
            groups.setdefault(ref.src, []).append(ref)
        for src, refs in groups.items():
            got = {}
            for idx, words, _secs in O.recognize_document(src.data, sorted({r.index for r in refs}), engine, lang,
                                                          int(o.get("dpi", O.DEFAULT_DPI)), src.password,
                                                          bool(o.get("only_if_needed"))):
                got[idx] = words
            for r in refs:
                words = got.get(r.index)
                if words is None:
                    continue
                changes[r.uid] = O.OcrPage(r.src, r.index, list(words), info.key, lang)
    except Exception as ex:  # noqa: BLE001  (engine failures: missing data, ONNX errors, ...)
        t.warn(f"Recognize text failed: {ex}")
        return
    finally:
        engine.close()
    if changes:
        t.set_ocr(changes)
    t.__dict__["ocr_pages"] = len(changes)
