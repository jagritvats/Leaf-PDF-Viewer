"""Command line: `leaf <command> ...` / `leaf-cli <command> ...` (no window). See `leaf-cli --help`.

Every command that changes a PDF writes a NEW file: `-o/--output`, else `<name>-<command>.pdf` next to the input
(numbered, never overwriting). `--in-place` replaces the input and must be asked for. `--json` prints one JSON
object on stdout. Passwords never come from the command line: an encrypted PDF's password is read from the
environment variable named by `--password-env` (default LEAF_PASSWORD), else asked for on a terminal; a signing
identity's password likewise from `--identity-password-env` (default LEAF_SIGN_PASSWORD).

Exit codes: 0 done · 1 error (unreadable file, failed save) · 2 usage (bad arguments, malformed edit set, bad page
range) · 3 password needed or wrong · 4 nothing done (every change refused, no matches, signature check failed)
· 5 written, but some changes were refused or skipped (listed).

Uses the same engine as the window (document.PdfDocument + leaf/editset.py), so results match the GUI's.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

COMMANDS = ("info", "text", "fields", "fill", "annots", "redact", "ocr", "merge", "split", "extract", "rotate",
            "delete", "reorder", "crop", "optimize", "export", "edit-text", "replace", "sign", "verify", "apply",
            "edits")

OK, ERROR, USAGE, PASSWORD, REFUSED, PARTIAL = 0, 1, 2, 3, 4, 5


class CliError(Exception):
    def __init__(self, message: str, code: int = ERROR, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ============================================================================================ I/O helpers
def _console() -> None:
    """Make printing safe: a windowed Windows build has no stdout (attach the parent console); pipes get UTF-8."""
    if sys.platform == "win32" and sys.stdout is None:
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
                sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    for s in (sys.stdout, sys.stderr):
        rc = getattr(s, "reconfigure", None)
        if rc is None:
            continue
        try:
            rc(errors="replace") if s.isatty() else rc(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _out(text: str = "") -> None:
    if sys.stdout is not None:
        print(text)


def _err(text: str) -> None:
    if sys.stderr is not None:
        print(text, file=sys.stderr)


def _password(args, name: str, env_default: str, what: str) -> str | None:
    var = getattr(args, name, None) or env_default
    pw = os.environ.get(var)
    if pw is not None:
        return pw
    if sys.stdin is not None and sys.stdin.isatty() and not getattr(args, "json", False):
        import getpass
        return getpass.getpass(f"{what}: ")
    return None


def _open(args, path: str | None = None):
    """PdfDocument of the input (asks for / reads the password if it is encrypted)."""
    from .document import PasswordRequired, PdfDocument

    path = path or args.file
    if not os.path.isfile(path):
        raise CliError(f"No such file: {path}", ERROR)
    try:
        return PdfDocument.open(path)
    except PasswordRequired:
        pw = _password(args, "password_env", "LEAF_PASSWORD", f"Password for {os.path.basename(path)}")
        if pw is None:
            raise CliError(f"{os.path.basename(path)} is encrypted: set LEAF_PASSWORD (or the variable named by "
                           "--password-env).", PASSWORD) from None
        try:
            return PdfDocument.open(path, pw)
        except PasswordRequired:
            raise CliError(f"Wrong password for {os.path.basename(path)}.", PASSWORD) from None
    except CliError:
        raise
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Couldn't open {path}: {ex}", ERROR) from None


def _rows(spec, n: int) -> list[int]:
    from .pageops import parse_ranges
    try:
        return parse_ranges(spec or "", n)
    except ValueError as ex:
        raise CliError(f"Pages: {ex}", USAGE) from None


def _target(args, suffix: str, ext: str = ".pdf") -> str:
    """Where to write: --output, the input itself with --in-place, else a new numbered name next to it."""
    if getattr(args, "in_place", False):
        if getattr(args, "output", None):
            raise CliError("Use either --output or --in-place, not both.", USAGE)
        return os.path.abspath(args.file)
    if getattr(args, "output", None):
        out = os.path.abspath(args.output)
        if os.path.exists(out) and not getattr(args, "force", False) and \
                os.path.normcase(out) != os.path.normcase(os.path.abspath(getattr(args, "file", "") or "")):
            raise CliError(f"{out} exists; add --force to replace it.", USAGE)
        if os.path.normcase(out) == os.path.normcase(os.path.abspath(getattr(args, "file", "") or "")) and \
                not getattr(args, "in_place", False):
            raise CliError("The output is the input file; use --in-place to replace it.", USAGE)
        return out
    base = os.path.splitext(os.path.abspath(args.file))[0]
    cand = f"{base}-{suffix}{ext}"
    k = 2
    while os.path.exists(cand):
        cand = f"{base}-{suffix}-{k}{ext}"
        k += 1
    return cand


def _save(doc, target: str) -> dict:
    from . import editset as E
    try:
        incremental = E.save(doc, target)
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Couldn't save {target}: {ex}", ERROR) from None
    return {"output": target, "pages": len(doc.pages), "incremental": bool(incremental)}


def _round(r, nd: int = 2) -> list:
    return [round(float(v), nd) for v in r]


# ============================================================================================ commands
def cmd_info(args) -> dict:
    import pymupdf

    from . import signing
    from .docprops import security_info

    doc = _open(args)
    pdf = doc.primary.doc
    meta = {k: v for k, v in (pdf.metadata or {}).items() if k not in ("format", "encryption") and v}
    pages = []
    for i in range(pdf.page_count):
        p = pdf[i]
        e = {"page": i + 1, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2), "rotation": p.rotation}
        try:
            lab = p.get_label()
            if lab and lab != str(i + 1):
                e["label"] = lab
        except Exception:  # noqa: BLE001
            pass
        pages.append(e)
    sec = security_info(doc)
    fields = []
    if pdf.is_form_pdf:
        for p in pdf:
            fields += [w.field_name for w in p.widgets()]
    sigs = [{"field": s.name, "page": s.page + 1, "signed": s.signed} for s in signing.signature_fields(doc)]
    try:
        toc = [{"level": lvl, "title": t, "page": pg} for lvl, t, pg in pdf.get_toc(simple=True)]
    except Exception:  # noqa: BLE001
        toc = []
    try:
        attachments = pdf.embfile_names()
    except Exception:  # noqa: BLE001
        attachments = []
    res = {
        "file": os.path.abspath(args.file), "size": os.path.getsize(args.file),
        "format": (pdf.metadata or {}).get("format", ""), "page_count": pdf.page_count, "pages": pages,
        "metadata": meta,
        "encryption": {"encrypted": sec.encrypted, "method": sec.method, "open_password": sec.open_password},
        "signatures": sigs, "forms": {"present": bool(fields), "fields": len(set(fields))},
        "outline": toc, "attachments": attachments, "mupdf": pymupdf.mupdf_version,
    }
    if not args.json:
        _out(f"File:        {res['file']} ({res['size']:,} bytes, {res['format']})")
        _out(f"Pages:       {pdf.page_count}")
        sizes: dict = {}
        for e in pages:
            sizes.setdefault((e["width"], e["height"], e["rotation"]), []).append(e["page"])
        from .pageops import format_ranges, size_text
        for (w, h, rot), ps in sizes.items():
            _out(f"  {size_text(w, h)}{f', rotated {rot}°' if rot else ''}: pages {format_ranges([p - 1 for p in ps])}")
        for k, v in meta.items():
            _out(f"{k.capitalize() + ':':<12} {v}")
        _out(f"Encryption:  {sec.method if sec.encrypted else 'none'}")
        _out(f"Forms:       {len(set(fields))} field(s)" if fields else "Forms:       none")
        _out(f"Signatures:  {sum(s['signed'] for s in sigs)} signed, {sum(not s['signed'] for s in sigs)} empty"
             if sigs else "Signatures:  none")
        _out(f"Outline:     {len(toc)} bookmark(s)" if toc else "Outline:     none")
        if attachments:
            _out(f"Attachments: {', '.join(attachments)}")
    return res


def cmd_text(args) -> dict:
    from .export import page_text

    doc = _open(args)
    rows = _rows(args.pages, len(doc.pages))
    out = []
    for i in rows:
        ref = doc.pages[i]
        e: dict = {"page": i + 1, "text": page_text(doc.page(ref))}
        if args.words:
            e["words"] = [_round(w[:4]) + [w[4]] for w in ref.src.words(ref.index, ref.uid)]
        out.append(e)
    if not args.json:
        for k, e in enumerate(out):
            if args.per_page:
                _out(f"===== Page {e['page']} =====")
            if args.words:
                for w in e["words"]:
                    _out(f"{w[0]:8.2f} {w[1]:8.2f} {w[2]:8.2f} {w[3]:8.2f}  {w[4]}")
            else:
                _out(e["text"])
            if k + 1 < len(out) and not args.per_page:
                _out("\f")
    return {"pages": out}


def _fields(doc) -> list[dict]:
    import pymupdf

    from .forms import widget_value
    out = []
    for i, ref in enumerate(doc.pages):
        page = doc.page(ref)
        if not page.first_widget:
            continue
        m = page.rotation_matrix
        for w in page.widgets():
            t = w.field_type
            e = {"name": w.field_name, "type": w.field_type_string.lower(), "page": i + 1,
                 "rect": _round((w.rect * m).normalize())}
            if t == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                e["value"] = None
            else:
                v = widget_value(page.parent, w)
                if t in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                    e["on_state"] = str(w.on_state())
                e["value"] = v
            if w.choice_values:
                e["options"] = [c if isinstance(c, str) else list(c) for c in w.choice_values]
            fl = w.field_flags or 0
            if fl & pymupdf.PDF_FIELD_IS_READ_ONLY:
                e["read_only"] = True
            if fl & pymupdf.PDF_FIELD_IS_REQUIRED:
                e["required"] = True
            out.append(e)
    return out


def cmd_fields(args) -> dict:
    doc = _open(args)
    fs = _fields(doc)
    if not args.json:
        if not fs:
            _out("No form fields.")
        for f in fs:
            v = "" if f["value"] is None else f["value"]
            extra = f"  options: {', '.join(map(str, f['options']))}" if f.get("options") else ""
            _out(f"{f['name']}\t{f['type']}\t{json.dumps(v, ensure_ascii=False)}\tpage {f['page']}{extra}")
    return {"fields": fs}


def _pairs(items) -> dict:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise CliError(f"Expected name=value, got “{it}”.", USAGE)
        k, v = it.split("=", 1)
        out[k] = v
    return out


def _run_edits(args, data: dict, suffix: str, base_dir: str | None = None, dry: bool = False) -> dict:
    """Apply an edit set to the input and save (unless dry). Exit code from what was refused."""
    from . import editset as E

    doc = _open(args)
    report: dict = {}
    try:
        warnings = E.apply_to_document(doc, data, base_dir=base_dir, report=report)
    except E.EditSetError as ex:
        raise CliError(f"Invalid edit set:\n{ex}", USAGE, problems=str(ex).splitlines()) from None
    res: dict = {"warnings": warnings, "refused": report["refused"]}
    for k in ("replaced", "redaction_marks", "ocr_pages"):
        if report.get(k):
            res[k] = report[k]
    res["_doc"] = doc
    changed = _changed(doc)
    res["changed"] = changed
    if not changed:
        res["_code"] = REFUSED
        return res
    if not dry:
        res.update(_save(doc, _target(args, suffix)))
    if report["refused"]:
        res["_code"] = PARTIAL
    return res


def _changed(doc) -> bool:
    from . import editset as E
    return bool(not doc.is_identity(doc.pages) or doc.form_values or doc.text_edits or doc.image_edits
                or E.overlay_count(doc) or any(r.crop is not None for r in doc.pages) or doc.metadata_edits
                or doc.decorations or doc.sanitize or doc.ocr)


def _report(res: dict, verb: str) -> None:
    for w in res.get("warnings", []):
        _err(f"warning: {w}")
    if res.get("output"):
        _out(f"{verb}: {res['output']}")
    elif res.get("changed") is False:
        _err("Nothing changed; no file written.")


def cmd_fill(args) -> dict:
    from . import forms

    values: dict = {}
    if args.data:
        try:
            values.update(forms.read_xfdf(args.data) if args.data.lower().endswith(".xfdf") else forms.read_json(args.data))
        except (OSError, ValueError) as ex:
            raise CliError(f"Couldn't read {args.data}: {ex}", USAGE) from None
    values.update(_pairs(args.values))
    if not values:
        raise CliError("Give field values: name=value pairs and/or --data file.json|.xfdf", USAGE)
    res = _run_edits(args, {"leaf_edits": 1, "forms": values}, "filled")
    res["fields_set"] = len({k[1] for k in res["_doc"].form_values})
    if not args.json:
        _report(res, "Filled")
    return res


def cmd_annots(args) -> dict:
    from .annots import hex_color

    doc = _open(args)
    out = []
    for i, ref in enumerate(doc.pages):
        page = doc.page(ref)
        m = page.rotation_matrix
        for a in page.annots():
            info = a.info
            e = {"page": i + 1, "type": a.type[1], "rect": _round((a.rect * m).normalize()), "xref": a.xref}
            for k, src in (("contents", "content"), ("author", "title"), ("subject", "subject"),
                           ("created", "creationDate"), ("modified", "modDate")):
                if info.get(src):
                    e[k] = info[src]
            col = (a.colors or {}).get("stroke")
            if col:
                e["color"] = hex_color(col)
            out.append(e)
    if not args.json:
        if not out:
            _out("No annotations.")
        for e in out:
            _out(f"page {e['page']}\t{e['type']}\t{e['rect']}\t{e.get('contents', '')}")
    return {"annotations": out}


def cmd_redact(args) -> dict:
    from . import editset as E
    from .redact import DEFAULT_SANITIZE

    red = []
    common = {k: v for k, v in (("case", args.case), ("whole", args.whole)) if v}
    if args.pages:
        common["pages"] = args.pages
    style = {}
    if args.color:
        style["color"] = args.color
    if args.label:
        style["label"] = args.label
    for t in args.text or []:
        red.append({"search": t, "preset": "text", **common, **style})
    for t in args.regex or []:
        red.append({"search": t, "preset": "regex", **common, **style})
    for p in args.preset or []:
        if p not in E.REDACT_PRESETS or p in ("text", "regex"):
            raise CliError(f"Unknown preset “{p}” (email, phone, card, iban, date).", USAGE)
        red.append({"preset": p, **common, **style})
    for spec in args.rect or []:
        pg, _, coords = spec.partition(":")
        try:
            r = [float(v) for v in coords.split(",")]
            assert len(r) == 4 and int(pg) >= 1
        except (ValueError, AssertionError):
            raise CliError(f"--rect wants PAGE:x0,y0,x1,y1, got “{spec}”.", USAGE) from None
        red.append({"page": int(pg), "rect": r, **style})
    if not red:
        raise CliError("Nothing to redact: give --text, --regex, --preset or --rect.", USAGE)
    data = {"leaf_edits": 1, "redactions": red}
    if args.sanitize:
        from dataclasses import asdict
        data["sanitize"] = {k: v for k, v in asdict(DEFAULT_SANITIZE).items() if v}
    res = _run_edits(args, data, "redacted", dry=args.dry_run)
    doc = res["_doc"]
    marks = []
    for i, ref in enumerate(doc.pages):
        for it in E.overlays_for(doc)(ref):
            if getattr(it, "redact", False):
                marks.append({"page": i + 1, "rect": _round(E._r(it.scene_rect_in_parent())), "text": it.marked_text})
    res["marks"] = marks
    if not marks and res.get("_code") is None and not args.sanitize:
        res["_code"] = REFUSED
    if not args.json:
        for mk in marks:
            _out(f"page {mk['page']}\t{mk['rect']}\t{mk['text']}")
        _out(f"{len(marks)} area(s) {'found' if args.dry_run else 'redacted'}.")
        _report(res, "Redacted")
    return res


def cmd_ocr(args) -> dict:
    o = {"language": args.lang, "engine": args.engine, "dpi": args.dpi, "only_if_needed": args.skip_text}
    if args.pages:
        o["pages"] = args.pages
    res = _run_edits(args, {"leaf_edits": 1, "ocr": o}, "ocr")
    if not args.json:
        _out(f"Recognized {res.get('ocr_pages', 0)} page(s).")
        _report(res, "Saved")
    return res


def _page_list(args, doc, refs_state, suffix: str) -> dict:
    """Save `doc` with a new page list [(ref, rotation)]."""
    if not refs_state:
        raise CliError("A PDF needs at least one page.", USAGE)
    for ref, rot in refs_state:
        ref.rotation = rot % 360
    doc.pages = [r for r, _ in refs_state]
    return _save(doc, _target(args, suffix))


def cmd_merge(args) -> dict:
    from .document import PageRef, PasswordRequired

    args.file = args.files[0]
    doc = _open(args)
    for path in args.files[1:]:
        if not os.path.isfile(path):
            raise CliError(f"No such file: {path}", ERROR)
        try:
            src = doc.add_source(path)
        except PasswordRequired:
            pw = _password(args, "password_env", "LEAF_PASSWORD", f"Password for {os.path.basename(path)}")
            try:
                src = doc.add_source(path, pw)
            except PasswordRequired:
                raise CliError(f"{os.path.basename(path)} needs a password.", PASSWORD) from None
        except Exception as ex:  # noqa: BLE001
            raise CliError(f"Couldn't open {path}: {ex}", ERROR) from None
        doc.pages += [PageRef(src, i) for i in range(src.doc.page_count)]
    res = _page_list(args, doc, [(r, r.rotation) for r in doc.pages], "merged")
    res["files"] = len(args.files)
    if not args.json:
        _out(f"Merged {len(args.files)} file(s), {res['pages']} pages: {res['output']}")
    return res


def _write_parts(args, doc, parts: list[list[int]], suffix: str) -> list[dict]:
    from .pageops import numbered_paths
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        base = os.path.join(args.output, os.path.splitext(os.path.basename(args.file))[0] + ".pdf")
    else:
        base = os.path.splitext(os.path.abspath(args.file))[0] + f"-{suffix}.pdf"
    paths = numbered_paths(base, len(parts))
    clash = [p for p in paths if os.path.exists(p)]
    if clash and not args.force:
        raise CliError(f"{clash[0]} exists; add --force to replace it.", USAGE)
    out = []
    for rows, path in zip(parts, paths):
        try:
            doc.save(path, lambda r: [], [doc.pages[i] for i in rows])
        except Exception as ex:  # noqa: BLE001
            raise CliError(f"Couldn't save {path}: {ex}", ERROR) from None
        out.append({"output": path, "pages": [i + 1 for i in rows]})
    return out


def cmd_split(args) -> dict:
    from .pageops import split_at, split_every

    doc = _open(args)
    n = len(doc.pages)
    if args.every:
        parts = split_every(n, args.every)
    elif args.at:
        parts = split_at(n, [r for r in _rows(args.at, n)])
    elif args.range:
        parts = [_rows(r, n) for r in args.range]
    else:
        raise CliError("How to split: --every N, --at PAGES (first page of each part) or --range R (repeat).", USAGE)
    files = _write_parts(args, doc, parts, "part")
    if not args.json:
        for f in files:
            _out(f"{f['output']}  (pages {f['pages'][0]}–{f['pages'][-1]})" if len(f["pages"]) > 1 else
                 f"{f['output']}  (page {f['pages'][0]})")
    return {"files": files}


def cmd_extract(args) -> dict:
    doc = _open(args)
    rows = _rows(args.pages, len(doc.pages))
    if args.separate:
        files = _write_parts(args, doc, [[r] for r in rows], "page")
        if not args.json:
            for f in files:
                _out(f["output"])
        return {"files": files}
    target = _target(args, "extract")
    try:
        doc.save(target, lambda r: [], [doc.pages[i] for i in rows])
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Couldn't save {target}: {ex}", ERROR) from None
    if not args.json:
        _out(f"Extracted {len(rows)} page(s): {target}")
    return {"output": target, "pages": len(rows)}


def cmd_rotate(args) -> dict:
    if args.angle % 90:
        raise CliError("--angle must be a multiple of 90.", USAGE)
    doc = _open(args)
    rows = set(_rows(args.pages, len(doc.pages)))
    res = _page_list(args, doc, [(r, r.rotation + (args.angle if i in rows else 0)) for i, r in enumerate(doc.pages)],
                     "rotated")
    if not args.json:
        _out(f"Rotated {len(rows)} page(s) by {args.angle}°: {res['output']}")
    return res


def cmd_delete(args) -> dict:
    doc = _open(args)
    rows = set(_rows(args.pages, len(doc.pages)))
    if len(rows) >= len(doc.pages):
        raise CliError("Can't delete every page.", USAGE)
    res = _page_list(args, doc, [(r, r.rotation) for i, r in enumerate(doc.pages) if i not in rows], "deleted")
    if not args.json:
        _out(f"Deleted {len(rows)} page(s), {res['pages']} left: {res['output']}")
    return res


def cmd_reorder(args) -> dict:
    doc = _open(args)
    n = len(doc.pages)
    if args.reverse:
        order = list(range(n - 1, -1, -1))
    elif args.order:
        first = _rows(args.order, n)
        order = first + [i for i in range(n) if i not in set(first)]
    else:
        raise CliError("Give --order \"3,1,2\" (the pages listed come first) or --reverse.", USAGE)
    res = _page_list(args, doc, [(doc.pages[i], doc.pages[i].rotation) for i in order], "reordered")
    res["order"] = [i + 1 for i in order]
    if not args.json:
        _out(f"New order {', '.join(map(str, res['order']))}: {res['output']}")
    return res


def _floats(s: str, n: int, what: str) -> list[float]:
    try:
        v = [float(x) for x in s.replace(" ", "").split(",")]
    except ValueError:
        v = []
    if len(v) != n:
        raise CliError(f"{what} wants {n} comma-separated numbers, got “{s}”.", USAGE)
    return v


def cmd_crop(args) -> dict:
    e: dict = {"pages": args.pages or "all"}
    if args.remove:
        e["remove"] = True
    elif args.rect:
        e["rect"] = _floats(args.rect, 4, "--rect")
    elif args.margins:
        e["margins"] = _floats(args.margins, 4, "--margins")
        e["unit"] = args.unit
    elif args.auto:
        e["auto"] = True
        e["pad"] = args.pad
    else:
        raise CliError("How to crop: --rect, --margins, --auto or --remove.", USAGE)
    res = _run_edits(args, {"leaf_edits": 1, "crop": [e]}, "cropped")
    if not args.json:
        _report(res, "Cropped")
    return res


def cmd_optimize(args) -> dict:
    import shutil
    import tempfile

    from . import optimize as O

    doc = _open(args)  # (checks the password)
    if args.preset not in O.PRESETS:
        raise CliError(f"Unknown preset {args.preset} ({', '.join(O.PRESETS)}).", USAGE)
    opts = O.preset(args.preset)
    if args.gray:
        opts.gray = True
    if args.dpi:
        opts.images, opts.dpi = True, args.dpi
    if args.quality:
        opts.quality = args.quality
    if args.strip_metadata:
        opts.strip_metadata = True
    target = _target(args, "optimized")
    before = os.path.getsize(args.file)
    d = tempfile.mkdtemp(prefix="leaf-cli-", dir=os.path.dirname(target))
    try:
        tmp = os.path.join(d, "out.pdf")
        try:
            size = O.optimize_file(os.path.abspath(args.file), tmp, opts, doc.primary.password)
        except Exception as ex:  # noqa: BLE001
            raise CliError(f"Optimizing failed: {ex}", ERROR) from None
        os.replace(tmp, target)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    res = {"output": target, "before": before, "after": size, "preset": args.preset}
    if not args.json:
        _out(f"{O.human(before)} → {O.human(size)} ({100 * (1 - size / max(1, before)):.0f}% smaller): {target}")
    return res


def cmd_export(args) -> dict:
    from . import export as X

    doc = _open(args)
    pdf = doc.primary.doc
    rows = _rows(args.pages, pdf.page_count)
    stem = os.path.splitext(os.path.basename(args.file))[0]
    fmt = args.format.lower()
    files: list[str] = []
    try:
        if fmt in ("text", "markdown", "md", "txt"):
            md = fmt in ("markdown", "md")
            path = os.path.abspath(args.output) if args.output else os.path.splitext(os.path.abspath(args.file))[0] + (
                ".md" if md else ".txt")
            if os.path.exists(path) and not args.force:
                raise CliError(f"{path} exists; add --force to replace it.", USAGE)
            for _done, _total, p in X.export_text(pdf, rows, path, markdown=md):
                if p:
                    files.append(p)
        elif fmt == "images":
            folder = os.path.abspath(args.output or os.path.splitext(args.file)[0] + "-images")
            for _done, _total, p in X.export_images(pdf, folder, stem, rows):
                if p:
                    files.append(p)
        else:
            folder = os.path.abspath(args.output or os.path.splitext(args.file)[0] + "-pages")
            for _done, _total, p in X.export_pages(pdf, rows, folder, stem, fmt, args.dpi, args.quality):
                files.append(p)
    except CliError:
        raise
    except ValueError as ex:
        raise CliError(str(ex), USAGE) from None
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Export failed: {ex}", ERROR) from None
    if not args.json:
        for f in files:
            _out(f)
        _out(f"{len(files)} file(s) written.")
    res = {"files": files}
    if not files:
        res["_code"] = REFUSED
    return res


def cmd_edit_text(args) -> dict:
    from . import textedit as T

    if args.list:
        doc = _open(args)
        rows = _rows(str(args.page) if args.page else "", len(doc.pages))
        lines = []
        for i in rows:
            ref = doc.pages[i]
            for s in T.shown_segments(doc, ref):
                lines.append({"page": i + 1, "text": s.text, "rect": _round(s.bbox)})
        if not args.json:
            for ln in lines:
                _out(f"page {ln['page']}\t{ln['text']}")
        return {"lines": lines}
    if not (args.page and args.find is not None and args.text is not None):
        raise CliError("edit-text needs --page N --find \"current line\" --text \"new text\" (or --list).", USAGE)
    e = {"page": args.page, "find": args.find, "text": args.text}
    if args.para:
        e["para"] = True
    res = _run_edits(args, {"leaf_edits": 1, "text_edits": [e]}, "edited", dry=args.dry_run)
    if not res.get("changed") or not res["_doc"].text_edits:
        res["_code"] = REFUSED
    if not args.json:
        _report(res, "Edited")
    return res


def cmd_replace(args) -> dict:
    e = {"find": args.find, "replace": args.replace, "regex": args.regex, "case": args.case, "word": args.word}
    if args.pages:
        e["pages"] = args.pages
    res = _run_edits(args, {"leaf_edits": 1, "replace": [e]}, "replaced", dry=args.dry_run)
    if not args.json:
        _out(f"Replaced {res.get('replaced', 0)} occurrence(s).")
        _report(res, "Saved")
    return res


def cmd_sign(args) -> dict:
    from . import signing as S

    doc = _open(args)
    pw = _password(args, "identity_password_env", "LEAF_SIGN_PASSWORD", f"Password for {os.path.basename(args.identity)}")
    if pw is None:
        raise CliError("The identity's password: set LEAF_SIGN_PASSWORD (or the variable named by "
                       "--identity-password-env); it is never read from the command line.", PASSWORD)
    try:
        ident = S.load_identity(args.identity, pw)
    except S.IdentityError as ex:
        raise CliError(str(ex), PASSWORD if "password" in str(ex).lower() else ERROR) from None
    req = S.SignRequest(reason=args.reason or "", location=args.location or "", contact=args.contact or "",
                        certify=args.certify, tsa_url=args.tsa or "", password=doc.primary.password)
    if args.field:
        req.field = args.field
    elif args.rect:
        req.page = (args.page or 1) - 1
        req.rect = tuple(_floats(args.rect, 4, "--rect"))
        if not 0 <= req.page < doc.primary.doc.page_count:
            raise CliError(f"Page {args.page} doesn't exist.", USAGE)
    if args.image:
        try:
            with open(args.image, "rb") as f:
                req.image = f.read()
        except OSError as ex:
            raise CliError(f"Couldn't read {args.image}: {ex.strerror or ex}", ERROR) from None
    target = _target(args, "signed")
    try:
        name = S.sign_pdf(os.path.abspath(args.file), target, ident, req)
    except S.SigningError as ex:
        raise CliError(str(ex), ERROR) from None
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Signing failed: {ex}", ERROR) from None
    res = {"output": target, "field": name, "signer": ident.name}
    if not args.json:
        _out(f"Signed as {ident.name} (field {name}): {target}")
    return res


def cmd_verify(args) -> dict:
    from . import signing as S

    doc = _open(args)
    trusted = []
    for path in args.trust or []:
        try:
            with open(path, "rb") as f:
                trusted += S.parse_certificates(f.read())
        except (OSError, ValueError) as ex:
            raise CliError(f"Couldn't read certificate {path}: {ex}", USAGE) from None
    try:
        sts = S.verify_pdf(doc.primary.data, trusted, args.system_trust, doc.primary.password)
    except Exception as ex:  # noqa: BLE001
        raise CliError(f"Couldn't check the signatures: {ex}", ERROR) from None
    sigs = []
    for s in sts:
        sigs.append({"field": s.field, "page": None if s.page is None else s.page + 1, "signer": s.signer,
                     "state": s.state, "summary": s.headline(), "intact": s.intact, "valid": s.valid,
                     "trusted": s.trusted, "trust": s.trust, "coverage": s.coverage, "changes": s.changes,
                     "signing_time": s.signing_time.isoformat() if s.signing_time else None,
                     "timestamp": s.timestamp.isoformat() if s.timestamp else None, "reason": s.reason,
                     "certify": s.certify, "error": s.error})
    bad = [s for s in sigs if s["state"] in ("invalid", "modified") or (args.require_trust and s["state"] == "untrusted")]
    res: dict = {"signatures": sigs, "valid": bool(sigs) and not bad}
    if not sigs or bad:
        res["_code"] = REFUSED
    if not args.json:
        if not sigs:
            _out("No signatures.")
        for s in sigs:
            _out(f"{s['field']}: {s['summary']}")
    return res


def _load_edits(path: str) -> tuple[dict, str | None]:
    try:
        if path == "-":
            return json.load(sys.stdin), None
        with open(path, encoding="utf-8") as f:
            return json.load(f), os.path.dirname(os.path.abspath(path))
    except OSError as ex:
        raise CliError(f"Couldn't read {path}: {ex.strerror or ex}", ERROR) from None
    except ValueError as ex:
        raise CliError(f"{path} isn't valid JSON: {ex}", USAGE) from None


def cmd_apply(args) -> dict:
    from . import editset as E

    data, base = _load_edits(args.edits)
    problems = E.validate(data)
    if problems:
        raise CliError("Invalid edit set:\n" + "\n".join(f"  {p}" for p in problems), USAGE, problems=problems)
    res = _run_edits(args, data, "edited", base_dir=base, dry=args.dry_run)
    res["changes"] = [d for _, _, d in E.entries(data)]
    if not args.json:
        _report(res, "Applied")
        if args.dry_run:
            _out(f"Dry run: {len(res['changes'])} change(s) checked, nothing written.")
    return res


EXAMPLE = {
    "leaf_edits": 1,
    "pages": [{"page": 2}, {"page": 1, "rotate": 90}, {"blank": [595, 842]}],
    "forms": {"name": "Ada Lovelace", "agree": True},
    "text_edits": [{"page": 2, "find": "Total: 100 EUR", "text": "Total: 120 EUR"}],
    "replace": [{"find": "ACME Ltd", "replace": "ACME GmbH", "pages": "all"}],
    "annotations": [
        {"page": 1, "type": "rect", "rect": [72, 300, 272, 360], "style": {"color": "#d43a2a", "width": 2}},
        {"page": 1, "type": "text", "at": [72, 400], "text": "Approved", "style": {"size": 14, "bold": True}},
        {"page": 1, "type": "note", "at": [500, 72], "contents": "Please check the totals."},
        {"type": "highlight", "search": "deadline"},
    ],
    "redactions": [{"preset": "email"}, {"page": 1, "rect": [72, 700, 300, 720]}],
    "crop": [{"pages": "all", "margins": [10, 10, 10, 10], "unit": "mm"}],
    "metadata": {"title": "Contract (signed copy)", "author": "Legal"},
    "decorations": [{"kind": "watermark", "text": "DRAFT"},
                    {"kind": "header_footer", "slots": ["", "", "", "", "Page {page} of {pages}", ""]}],
}


def cmd_edits(args) -> dict:
    from . import editset as E

    if args.example:
        data = EXAMPLE
    else:
        if not args.file:
            raise CliError("edits needs a PDF (or --example).", USAGE)
        doc = _open(args)
        from .forms import collect_values
        fields = collect_values(doc)
        data = E.empty({"name": os.path.basename(args.file), "pages": len(doc.pages),
                        **({"fields": fields} if fields else {})})
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        if os.path.exists(args.output) and not args.force:
            raise CliError(f"{args.output} exists; add --force to replace it.", USAGE)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        if not args.json:
            _out(f"Wrote {args.output}")
        return {"output": os.path.abspath(args.output)}
    if not args.json:
        _out(text)
    return {"edits": data}


# ============================================================================================ parser
def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON object on stdout")
    common.add_argument("--password-env", metavar="VAR",
                        help="environment variable holding the PDF's password (default LEAF_PASSWORD)")
    writes = argparse.ArgumentParser(add_help=False)
    writes.add_argument("-o", "--output", help="output file (default: <name>-<command>.pdf next to the input)")
    writes.add_argument("--in-place", action="store_true", help="replace the input file")
    writes.add_argument("--force", action="store_true", help="replace an existing output file")

    p = argparse.ArgumentParser(prog="leaf-cli", description="Leaf PDF editor, command line. Every command that "
                                "changes a PDF writes a new file unless --in-place is given.",
                                epilog="Exit codes: 0 done, 1 error, 2 usage, 3 password, 4 nothing done, "
                                       "5 partly done. Edit sets: docs/edit-set.md.")
    sub = p.add_subparsers(dest="command", metavar="command")

    def cmd(name, help, fn, write=False):
        sp = sub.add_parser(name, help=help, description=help, parents=[common] + ([writes] if write else []))
        sp.set_defaults(fn=fn)
        return sp

    def pdf(sp):
        sp.add_argument("file", help="PDF file")

    pdf(cmd("info", "pages, sizes, metadata, encryption, signatures, forms, outline", cmd_info))
    sp = cmd("text", "extract text (plain, or words with boxes)", cmd_text)
    pdf(sp)
    sp.add_argument("--pages", help="page range, e.g. \"1-3, 7\" (default all)")
    sp.add_argument("--words", action="store_true", help="words with their boxes (x0 y0 x1 y1 in points)")
    sp.add_argument("--per-page", action="store_true", help="a header line before each page")
    pdf(cmd("fields", "list form fields and their values", cmd_fields))
    sp = cmd("fill", "fill form fields", cmd_fill, write=True)
    pdf(sp)
    sp.add_argument("values", nargs="*", metavar="name=value")
    sp.add_argument("--data", help="values from a JSON ({name: value}) or XFDF file")
    pdf(cmd("annots", "list annotations", cmd_annots))
    sp = cmd("redact", "remove text/areas for good (search, patterns, rectangles)", cmd_redact, write=True)
    pdf(sp)
    sp.add_argument("--text", action="append", help="text to redact (repeatable)")
    sp.add_argument("--regex", action="append", help="regular expression to redact (repeatable)")
    sp.add_argument("--preset", action="append", help="email, phone, card, iban or date (repeatable)")
    sp.add_argument("--rect", action="append", help="PAGE:x0,y0,x1,y1 area in points (repeatable)")
    sp.add_argument("--pages", help="search only these pages")
    sp.add_argument("--case", action="store_true", help="match case")
    sp.add_argument("--whole", action="store_true", help="whole words only")
    sp.add_argument("--color", help="fill color, e.g. #000000")
    sp.add_argument("--label", help="text drawn on each redaction")
    sp.add_argument("--sanitize", action="store_true",
                    help="also remove metadata, JavaScript, attachments, hidden text and hidden layers")
    sp.add_argument("--dry-run", action="store_true", help="list what would be redacted; write nothing")
    sp = cmd("ocr", "recognize text in scanned pages (adds an invisible text layer)", cmd_ocr, write=True)
    pdf(sp)
    sp.add_argument("--lang", default="eng", help="language, e.g. eng, deu, eng+hin (default eng)")
    sp.add_argument("--engine", default="auto", choices=("auto", "tesseract", "rapidocr"))
    sp.add_argument("--pages", help="page range (default all)")
    sp.add_argument("--dpi", type=int, default=300)
    sp.add_argument("--skip-text", action="store_true", help="skip pages that already have text")
    sp = cmd("merge", "join PDFs (and images) into one", cmd_merge, write=True)
    sp.add_argument("files", nargs="+", help="files in order")
    sp = cmd("split", "split into several files", cmd_split)
    pdf(sp)
    sp.add_argument("--every", type=int, help="N pages per file")
    sp.add_argument("--at", help="first pages of the parts, e.g. \"4, 9\"")
    sp.add_argument("--range", action="append", help="one part per range (repeatable)")
    sp.add_argument("-o", "--output", help="output folder (default: next to the input)")
    sp.add_argument("--force", action="store_true", help="replace existing files")
    sp = cmd("extract", "copy pages into a new file", cmd_extract, write=True)
    pdf(sp)
    sp.add_argument("--pages", required=True, help="page range")
    sp.add_argument("--separate", action="store_true", help="one file per page (-o is then a folder)")
    sp = cmd("rotate", "rotate pages", cmd_rotate, write=True)
    pdf(sp)
    sp.add_argument("--pages", help="page range (default all)")
    sp.add_argument("--angle", type=int, default=90, help="degrees clockwise: 90, 180, 270, -90")
    sp = cmd("delete", "delete pages", cmd_delete, write=True)
    pdf(sp)
    sp.add_argument("--pages", required=True, help="page range")
    sp = cmd("reorder", "change the page order", cmd_reorder, write=True)
    pdf(sp)
    sp.add_argument("--order", help="pages in their new order, e.g. \"3,1,2\" (unlisted pages follow)")
    sp.add_argument("--reverse", action="store_true")
    sp = cmd("crop", "crop pages (sets the visible area)", cmd_crop, write=True)
    pdf(sp)
    sp.add_argument("--pages", help="page range (default all)")
    sp.add_argument("--rect", help="x0,y0,x1,y1 in points")
    sp.add_argument("--margins", help="left,top,right,bottom")
    sp.add_argument("--unit", default="pt", choices=("pt", "mm", "in"))
    sp.add_argument("--auto", action="store_true", help="trim white margins")
    sp.add_argument("--pad", type=float, default=6.0, help="--auto: space kept around the content (pt)")
    sp.add_argument("--remove", action="store_true", help="remove the crop")
    sp = cmd("optimize", "reduce file size", cmd_optimize, write=True)
    pdf(sp)
    sp.add_argument("--preset", default="lossless", help="lossless, print, ebook or screen (default lossless)")
    sp.add_argument("--dpi", type=int, help="downsample images to this resolution")
    sp.add_argument("--quality", type=int, help="JPEG quality 1-100")
    sp.add_argument("--gray", action="store_true", help="convert to grayscale")
    sp.add_argument("--strip-metadata", action="store_true")
    sp = cmd("export", "pages as images, embedded images, text or Markdown", cmd_export)
    pdf(sp)
    sp.add_argument("--format", default="png", help="png, jpg, webp, tif, images, text or markdown")
    sp.add_argument("--pages", help="page range (default all)")
    sp.add_argument("--dpi", type=int, default=150)
    sp.add_argument("--quality", type=int, default=90)
    sp.add_argument("-o", "--output", help="folder (images) or file (text, markdown)")
    sp.add_argument("--force", action="store_true")
    sp = cmd("edit-text", "change a line (or paragraph) of the page's own text", cmd_edit_text, write=True)
    pdf(sp)
    sp.add_argument("--page", type=int, help="page number")
    sp.add_argument("--find", help="the line's current text (exactly; see --list)")
    sp.add_argument("--text", help="the new text")
    sp.add_argument("--para", action="store_true", help="--find is a whole paragraph")
    sp.add_argument("--list", action="store_true", help="list the editable lines (of --page, or all pages)")
    sp.add_argument("--dry-run", action="store_true", help="check only; write nothing")
    sp = cmd("replace", "find & replace in the page's own text", cmd_replace, write=True)
    pdf(sp)
    sp.add_argument("--find", required=True)
    sp.add_argument("--replace", required=True)
    sp.add_argument("--regex", action="store_true", help="--find is a regular expression ($1 in --replace)")
    sp.add_argument("--case", action="store_true", help="match case")
    sp.add_argument("--word", action="store_true", help="whole words only")
    sp.add_argument("--pages", help="page range (default all)")
    sp.add_argument("--dry-run", action="store_true", help="count and check only; write nothing")
    sp = cmd("sign", "digitally sign (PAdES) with a .p12/.pfx identity", cmd_sign, write=True)
    pdf(sp)
    sp.add_argument("--identity", required=True, help=".p12/.pfx file")
    sp.add_argument("--identity-password-env", metavar="VAR",
                    help="environment variable with its password (default LEAF_SIGN_PASSWORD); else asked for")
    sp.add_argument("--field", help="sign this existing empty signature field")
    sp.add_argument("--page", type=int, help="page of a new visible signature (with --rect)")
    sp.add_argument("--rect", help="x0,y0,x1,y1 of a new visible signature (default: invisible)")
    sp.add_argument("--image", help="PNG of a handwritten signature for the appearance")
    sp.add_argument("--reason")
    sp.add_argument("--location")
    sp.add_argument("--contact")
    sp.add_argument("--certify", type=int, choices=(1, 2, 3), help="certify: 1 no changes, 2 forms, 3 comments")
    sp.add_argument("--tsa", help="timestamp server URL (PAdES B-T)")
    sp = cmd("verify", "check digital signatures (offline)", cmd_verify)
    pdf(sp)
    sp.add_argument("--trust", action="append", help="trusted certificate file (PEM/DER; repeatable)")
    sp.add_argument("--system-trust", action="store_true", help="also trust the system's root certificates")
    sp.add_argument("--require-trust", action="store_true", help="fail (exit 4) on untrusted signers too")
    sp = cmd("apply", "apply an edit set (JSON; docs/edit-set.md)", cmd_apply, write=True)
    pdf(sp)
    sp.add_argument("edits", help="edit set file, or - for stdin")
    sp.add_argument("--dry-run", action="store_true", help="check and report; write nothing")
    sp = cmd("edits", "print an empty edit set for a PDF (or --example)", cmd_edits)
    sp.add_argument("file", nargs="?", help="PDF file")
    sp.add_argument("--example", action="store_true", help="a filled-in example")
    sp.add_argument("-o", "--output", help="write to this file instead of stdout")
    sp.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    _console()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not argv:
        parser.print_help()
        return USAGE
    try:
        args = parser.parse_args(argv)
    except SystemExit as ex:  # argparse: --help (0) or a usage error (2)
        return int(ex.code or 0)
    if not getattr(args, "fn", None):
        parser.print_help()
        return USAGE
    code = OK
    try:
        res = args.fn(args)
        code = res.pop("_code", OK)
        res.pop("_doc", None)
        if args.json:
            res = {"ok": code in (OK, PARTIAL), "command": args.command, "code": code, **res}
            _out(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif code == REFUSED and res.get("refused"):
            for r in res["refused"]:
                if r not in res.get("warnings", []):
                    _err(f"refused: {r}")
    except CliError as ex:
        code = ex.code
        if getattr(args, "json", False):
            _out(json.dumps({"ok": False, "command": args.command, "code": code, "error": str(ex), **ex.extra},
                            ensure_ascii=False, indent=2))
        else:
            _err(f"leaf {args.command}: {ex}")
    except KeyboardInterrupt:
        code = ERROR
    finally:
        _cleanup()
    return code


def _cleanup() -> None:
    """Free overlay items and documents while Qt still exists (avoids crashes at interpreter exit)."""
    import gc
    gc.collect()


if __name__ == "__main__":
    sys.exit(main())
