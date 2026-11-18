"""`leaf mcp`: a Model Context Protocol server on stdio (JSON-RPC 2.0, one message per line), no extra deps.

Tools read PDFs, find text, propose edits to the running Leaf (the user reviews them in the Agent suggestions
panel; see leaf.agentlink) and write NEW files (edit set applied, OCR text layer, redactions, text export).
Nothing is ever overwritten and nothing is signed. Paths must be absolute, local (no UNC/network drives, no
URLs) and, when the server was started with `--root DIR` (repeatable), inside one of those folders.

Register it with an MCP client as the command `leaf mcp` (or `uv run leaf mcp` in a source checkout); see
docs/agents.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback

SERVER_NAME = "leaf"
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_TEXT = 200_000  # characters of text returned by one call unless max_chars asks for more
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "sshfs", "9p", "afs", "ncpfs", "davfs", "fuse.rclone"}


class ToolError(Exception):
    """A tool failed in a way the model should see (returned as isError content, not a protocol error)."""


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("leaf")
    except Exception:  # noqa: BLE001
        return "0.1.0"


# ===================================================================== paths
def _is_network(path: str) -> bool:
    if path.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            drive = os.path.splitdrive(path)[0]
            if drive:
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
        except Exception:  # noqa: BLE001
            return False
        return False
    try:
        best, fstype = "", ""
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    mnt = parts[1].replace("\\040", " ")
                    if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                        best, fstype = mnt, parts[2]
        return fstype in NETWORK_FS
    except OSError:
        return False


class Paths:
    """Path policy: absolute local paths only, optionally restricted to root folders."""

    def __init__(self, roots: list[str] | None = None):
        self.roots = [os.path.normcase(os.path.realpath(r)) for r in (roots or [])]

    def _check(self, p, what: str) -> str:
        if not isinstance(p, str) or not p.strip():
            raise ToolError(f"{what}: a path is required")
        p = p.strip()
        if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", p):
            raise ToolError(f"{what}: URLs aren't accepted, only local file paths")
        if p.startswith(("\\\\", "//")):
            raise ToolError(f"{what}: network paths aren't accepted")
        if not os.path.isabs(p) or (sys.platform == "win32" and not os.path.splitdrive(p)[0]):
            raise ToolError(f"{what}: use an absolute path (got {p!r})")
        real = os.path.realpath(p)
        if _is_network(real) or _is_network(os.path.abspath(p)):
            raise ToolError(f"{what}: files on network drives aren't accepted")
        if self.roots:
            n = os.path.normcase(real)
            if not any(n == r or n.startswith(r.rstrip("\\/") + os.sep) for r in self.roots):
                raise ToolError(f"{what}: {p} is outside the folders this server may use")
        return real

    def pdf_in(self, p) -> str:
        real = self._check(p, "path")
        if not os.path.isfile(real):
            raise ToolError(f"path: no such file: {p}")
        if os.path.splitext(real)[1].lower() != ".pdf":
            raise ToolError("path: only .pdf files are accepted")
        return real

    def new_out(self, src: str, output, suffix: str, ext: str = ".pdf") -> str:
        """The file a *_to_copy tool writes: `output` (must not exist) or "<name>-<suffix><ext>" next to the
        source, numbered until free. Never the source itself."""
        if output:
            out = self._check(output, "output")
            if os.path.splitext(out)[1].lower() != ext:
                raise ToolError(f"output: must end in {ext}")
            if os.path.exists(out):
                raise ToolError(f"output: {output} already exists (Leaf never overwrites files)")
            if not os.path.isdir(os.path.dirname(out)):
                raise ToolError("output: the folder doesn't exist")
            return out
        stem = os.path.splitext(src)[0]
        out = f"{stem}-{suffix}{ext}"
        n = 2
        while os.path.exists(out):
            out = f"{stem}-{suffix} ({n}){ext}"
            n += 1
        return out


# ===================================================================== pdf helpers
def _open(path: str):
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as ex:  # noqa: BLE001
        raise ToolError(f"couldn't open {os.path.basename(path)}: {ex}") from ex
    if doc.needs_pass:
        doc.close()
        raise ToolError("the PDF is password-protected; open it in Leaf instead")
    return doc


def rows_for(spec, n: int) -> list[int]:
    """'1-3, 7, 10-' / 'all' / [1, 2] -> 0-based page rows (1-based input). Raises ToolError."""
    if spec is None or spec == "" or spec == "all":
        return list(range(n))
    if isinstance(spec, int) and not isinstance(spec, bool):
        spec = [spec]
    if isinstance(spec, list):
        bad = [p for p in spec if not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= n]
        if bad:
            raise ToolError(f"pages: {bad} out of range 1–{n}")
        return [p - 1 for p in spec]
    if not isinstance(spec, str):
        raise ToolError("pages: use a range like '1-3, 7' or a list of page numbers")
    out: list[int] = []
    for tok in [t for t in re.split(r"[,;\s]+", re.sub(r"\s*[-–]\s*", "-", spec.strip().lower())) if t]:
        m = re.fullmatch(r"(\d+|last)?(-)?(\d+|last)?", tok)
        if not m or not (m.group(1) or m.group(3)):
            raise ToolError(f"pages: can't read {tok!r}")
        a = n if m.group(1) == "last" else int(m.group(1)) if m.group(1) else 1
        b = (n if m.group(3) == "last" else int(m.group(3)) if m.group(3) else n) if m.group(2) else a
        if not (1 <= a <= n and 1 <= b <= n):
            raise ToolError(f"pages: {tok} is out of range 1–{n}")
        step = 1 if b >= a else -1
        out += [i - 1 for i in range(a, b + step, step) if i - 1 not in out]
    return out


def _r(rect) -> list[float]:
    return [round(rect[0], 2), round(rect[1], 2), round(rect[2], 2), round(rect[3], 2)]


def _words(page) -> list[tuple]:
    from .document import WORD_FLAGS

    return page.get_text("words", sort=True, flags=WORD_FLAGS)


def _joined(words) -> tuple[str, list[tuple[int, int]]]:
    parts, spans, pos, prev = [], [], 0, None
    for w in words:
        key = (w[5], w[6])
        if prev is not None:
            parts.append(" " if key == prev else "\n")
            pos += 1
        prev = key
        spans.append((pos, pos + len(w[4])))
        parts.append(w[4])
        pos += len(w[4])
    return "".join(parts), spans


def find_hits(page, rx: re.Pattern, check=None) -> list[dict]:
    """Matches of `rx` in a page's words. Rects are per line, whole words, in unrotated PDF page space
    ("pdf_rects") and in the page's display space ("rects", origin top-left as shown)."""
    import pymupdf

    words = _words(page)
    text, spans = _joined(words)
    hits = []
    for m in rx.finditer(text):
        s, e = m.span()
        if e <= s or (check is not None and not check(m.group(0))):
            continue
        lines: dict = {}
        for k, (ws, we) in enumerate(spans):
            if we <= s or ws >= e:
                continue
            w = words[k]
            r = pymupdf.Rect(w[:4])
            key = (w[5], w[6])
            lines[key] = lines[key] | r if key in lines else r
        if lines:
            rects = list(lines.values())
            hits.append({"page": page.number + 1, "text": m.group(0).replace("\n", " "),
                         "context": text[max(0, s - 30):e + 30].replace("\n", " "),
                         "rects": [_r(r * page.rotation_matrix) for r in rects], "pdf_rects": rects})
    return hits


def _regex(args: dict) -> tuple[re.Pattern, object]:
    """The search of pdf_find / pdf_redact_to_copy: `query` (plain text), `regex`, or a `preset`."""
    presets = {"email": "Email addresses", "phone": "Phone numbers", "card": "Credit card numbers",
               "iban": "IBANs", "date": "Dates"}
    case = bool(args.get("case_sensitive"))
    whole = bool(args.get("whole_word"))
    preset = args.get("preset")
    if preset:
        if preset not in presets:
            raise ToolError(f"preset: one of {', '.join(presets)}")
        from .redact import PRESETS, build_regex

        p = next(p for p in PRESETS if p.name == presets[preset])
        return build_regex("", p, case, whole), p.check
    pat = args.get("regex")
    if pat:
        try:
            rx = re.compile(f"(?<!\\w)(?:{pat})(?!\\w)" if whole else pat, 0 if case else re.IGNORECASE)
        except re.error as ex:
            raise ToolError(f"regex: {ex}") from ex
        return rx, None
    q = args.get("query")
    if not isinstance(q, str) or not q.split():
        raise ToolError("give 'query' (text), 'regex' or 'preset'")
    pat = r"\s+".join(re.escape(t) for t in q.split())
    if whole:
        pat = rf"(?<!\w)(?:{pat})(?!\w)"
    return re.compile(pat, 0 if case else re.IGNORECASE), None


# ===================================================================== live link to the running Leaf
class LiveLink:
    """A persistent agent session with the running Leaf (one session per MCP server run, so the user's
    "auto-accept for this session" covers this agent)."""

    def __init__(self):
        self.client = None
        self.client_name = "MCP agent"
        self.lock = threading.Lock()

    def _connect(self):
        from . import agentlink, ipc

        info = agentlink.read_token_file()
        if not info:
            raise ToolError("Leaf's agent link is off (turn it on in Leaf: Settings › Agents), or Leaf isn't running")
        try:
            c = ipc.Client(info.get("server") or ipc.server_name(), timeout=5.0)
        except ipc.IpcError as ex:
            raise ToolError("Leaf isn't running") from ex
        r = c.request({"op": "hello", "token": info["token"], "client": self.client_name})
        if not r.get("ok"):
            c.close()
            raise ToolError(r.get("error") or "Leaf refused the connection")
        self.token = info["token"]
        return c

    def call(self, msg: dict) -> dict:
        from . import ipc

        with self.lock:
            for attempt in (0, 1):
                if self.client is None:
                    self.client = self._connect()
                try:
                    r = self.client.request({**msg, "token": self.token})
                except ipc.IpcError:
                    self.close()
                    if attempt:
                        raise ToolError("lost the connection to Leaf") from None
                    continue
                if r.get("auth") is False and not attempt:  # token regenerated: read it again
                    self.close()
                    continue
                return r
        raise ToolError("Leaf didn't accept the request")

    def available(self) -> bool:
        try:
            self.call({"op": "list"})
            return True
        except ToolError:
            return False

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


# ===================================================================== tools
def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


PATH = {"type": "string", "description": "Absolute path of a local PDF file"}
PAGES = {"type": ["string", "array"], "items": {"type": "integer"},
         "description": "Pages, 1-based: '1-3, 7, 10-' or a list; default all"}
OUTPUT = {"type": "string", "description": "Absolute path of the new file (must not exist). Default: next to the "
                                           "source with a suffix"}
SEARCH = {
    "query": {"type": "string", "description": "Plain text to find (whitespace-insensitive)"},
    "regex": {"type": "string", "description": "Python regular expression instead of query"},
    "preset": {"type": "string", "enum": ["email", "phone", "card", "iban", "date"],
               "description": "A built-in pattern instead of query"},
    "case_sensitive": {"type": "boolean"},
    "whole_word": {"type": "boolean"},
    "pages": PAGES,
}
EDITS = {"type": "object", "description": "A Leaf edit set: {\"leaf_edits\": 1, ...} (see docs/agents.md)"}
RO = {"readOnlyHint": True, "openWorldHint": False}
WRITE_COPY = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

TOOLS = [
    ("pdf_info", "PDF info", "Page count, page sizes, metadata, encryption, form/annotation/outline/signature "
     "counts of a PDF.", _schema({"path": PATH}, ["path"]), RO),
    ("pdf_text", "Read PDF text", "Text of pages in reading order; with boxes=true also every word with its box "
     "[x0, y0, x1, y1, word] in points (origin top-left of the page as displayed).",
     _schema({"path": PATH, "pages": PAGES, "boxes": {"type": "boolean"},
              "max_chars": {"type": "integer", "minimum": 1000}}, ["path"]), RO),
    ("pdf_fields", "List form fields", "Form fields: page, name, type, value, choices, rect, read-only.",
     _schema({"path": PATH}, ["path"]), RO),
    ("pdf_find", "Find in PDF", "Find text (plain, regex or a preset: email/phone/card/iban/date). Returns each "
     "hit with page, text, context and rects (display space).",
     _schema({"path": PATH, **SEARCH, "limit": {"type": "integer", "minimum": 1}}, ["path"]), RO),
    ("pdf_propose_edits", "Propose edits", "Propose an edit set. When Leaf is running with the agent link on, it "
     "opens the PDF and shows the changes in its Agent suggestions panel for the user to accept or reject "
     "(nothing changes and nothing is saved without the user); returns a proposal id for pdf_proposal_status. "
     "Otherwise writes a proposal file next to the PDF and returns its path.",
     _schema({"path": PATH, "edits": EDITS, "title": {"type": "string", "description": "Short summary shown to "
              "the user"}}, ["path", "edits"]),
     {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}),
    ("pdf_proposal_status", "Proposal status", "Whether the user accepted, partially accepted or rejected a "
     "proposal sent to Leaf (pending until they decide).",
     _schema({"proposal": {"type": "integer"}}, ["proposal"]), RO),
    ("pdf_apply_edits_to_copy", "Apply edits to a copy", "Apply an edit set without Leaf's UI and write the result "
     "to a NEW PDF (the source is never changed). Returns the new path and warnings.",
     _schema({"path": PATH, "edits": EDITS, "output": OUTPUT}, ["path", "edits"]), WRITE_COPY),
    ("pdf_ocr_to_copy", "OCR to a copy", "Recognize text on scanned pages and write a NEW searchable PDF with an "
     "invisible text layer.",
     _schema({"path": PATH, "pages": PAGES, "language": {"type": "string", "description": "Tesseract code(s), e.g. "
              "eng or eng+deu; default eng"}, "all_pages": {"type": "boolean", "description": "Also pages that "
              "already have text (default: only pages that need it)"}, "output": OUTPUT}, ["path"]), WRITE_COPY),
    ("pdf_redact_to_copy", "Redact to a copy", "Find text (query/regex/preset) and, with confirm=true, write a NEW "
     "PDF where every hit is truly removed (text, image pixels, vector graphics, annotations and form fields "
     "under it). Without confirm=true only returns the hit list so it can be reviewed first.",
     _schema({"path": PATH, **SEARCH, "confirm": {"type": "boolean", "description": "Must be true to write the "
              "redacted copy"}, "fill": {"type": "string", "description": "Box color, #rrggbb (default black)"},
              "output": OUTPUT}, ["path"]), WRITE_COPY),
    ("pdf_export_text", "Export text", "Write the text of pages (reading order) or Markdown (headings from font "
     "sizes) to a NEW .txt/.md file.",
     _schema({"path": PATH, "pages": PAGES, "markdown": {"type": "boolean"}, "output": {"type": "string",
              "description": "Absolute path of the new .txt/.md file (must not exist)"}}, ["path"]), WRITE_COPY),
    ("leaf_documents", "Documents open in Leaf", "The documents open in the running Leaf (needs the agent link): "
     "doc id, path, page count, unsaved changes.", _schema({}), RO),
    ("leaf_read", "Read an open document", "Read a document open in Leaf as the user sees it (unsaved edits "
     "included): text (optionally with word boxes), form fields, annotations, outline, pending edits.",
     _schema({"doc": {"type": "integer"}, "path": PATH, "pages": PAGES, "boxes": {"type": "boolean"},
              "include": {"type": "array", "items": {"type": "string", "enum": ["text", "fields", "annotations",
                                                                                  "outline", "edits"]}}}), RO),
]


class Server:
    def __init__(self, roots: list[str] | None = None, live: LiveLink | None = None):
        self.paths = Paths(roots)
        self.live = live or LiveLink()
        self.initialized = False
        self.protocol = SUPPORTED_VERSIONS[0]

    # ------------------------------------------------------------- JSON-RPC
    def handle(self, msg) -> dict | list | None:
        if isinstance(msg, list):  # batches (2025-03-26): answer each request
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to something we never ask: ignore
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        mid = msg.get("id")
        method = msg["method"]
        params = msg.get("params") or {}
        if "id" not in msg:  # notification: never answered
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if not isinstance(params, dict):
            return _error(mid, -32602, "params must be an object")
        try:
            if method == "initialize":
                return _result(mid, self.initialize(params))
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": [self._tool_def(t) for t in TOOLS]})
            if method == "tools/call":
                name = params.get("name")
                if name not in {t[0] for t in TOOLS}:
                    return _error(mid, -32602, f"Unknown tool: {name}")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    return _error(mid, -32602, "arguments must be an object")
                return _result(mid, self.call(name, args))
            if method in ("resources/list", "prompts/list"):
                return _result(mid, {method.split("/")[0]: []})
            return _error(mid, -32601, f"Method not found: {method}")
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _error(mid, -32603, f"Internal error: {ex}")

    def initialize(self, params: dict) -> dict:
        want = params.get("protocolVersion")
        self.protocol = want if want in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        info = params.get("clientInfo") or {}
        if isinstance(info, dict) and info.get("name"):
            self.live.client_name = f"{str(info['name'])[:40]} (MCP)"
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Leaf PDF editor", "version": _version()},
            "instructions": "Read PDFs with pdf_info/pdf_text/pdf_fields/pdf_find. To change a document the user "
                            "has open in Leaf, use pdf_propose_edits: the user reviews and accepts in Leaf (you "
                            "can't save or sign). The *_to_copy tools write new files and never overwrite. Paths "
                            "must be absolute local paths.",
        }

    def _tool_def(self, t) -> dict:
        name, title, desc, schema, ann = t
        d = {"name": name, "title": title, "description": desc, "inputSchema": schema}
        if self.protocol >= "2025-03-26":
            d["annotations"] = {"title": title, **ann}
        return d

    def call(self, name: str, args: dict) -> dict:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # nothing a library prints may corrupt the protocol stream
        try:
            result = getattr(self, f"t_{name}")(args)
        except ToolError as ex:
            return {"content": [{"type": "text", "text": str(ex)}], "isError": True}
        except Exception as ex:  # noqa: BLE001 - reported to the model, the server keeps going
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"{type(ex).__name__}: {ex}"}], "isError": True}
        finally:
            sys.stdout = real_stdout
        text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
        out = {"content": [{"type": "text", "text": text}]}
        if self.protocol >= "2025-06-18":
            out["structuredContent"] = json.loads(text)
        return out

    # ------------------------------------------------------------- read tools
    def t_pdf_info(self, a) -> dict:
        path = self.paths.pdf_in(a.get("path"))
        doc = _open(path)
        try:
            sizes = {}
            for p in doc:
                k = f"{round(p.rect.width)}x{round(p.rect.height)}"
                sizes[k] = sizes.get(k, 0) + 1
            sigs = 0
            fields = 0
            annots = 0
            for p in doc:
                for w in p.widgets():
                    fields += 1
                    if w.field_type == 6 and w.field_value:  # PDF_WIDGET_TYPE_SIGNATURE
                        sigs += 1
                annots += sum(1 for _ in p.annots())
            return {"path": path, "pages": doc.page_count, "page_sizes_pt": sizes,
                    "metadata": {k: v for k, v in (doc.metadata or {}).items() if v},
                    "encrypted": bool(doc.is_encrypted), "form_fields": fields, "annotations": annots,
                    "outline_entries": len(doc.get_toc()), "signed_signature_fields": sigs,
                    "file_size": os.path.getsize(path), "pdf_version": (doc.metadata or {}).get("format", "")}
        finally:
            doc.close()

    def t_pdf_text(self, a) -> dict:
        import pymupdf

        path = self.paths.pdf_in(a.get("path"))
        limit = max(1000, int(a.get("max_chars") or MAX_TEXT))
        doc = _open(path)
        try:
            pages, used, truncated = [], 0, False
            flags = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
            for i in rows_for(a.get("pages"), doc.page_count):
                p = doc[i]
                text = p.get_text("text", sort=True, flags=flags).rstrip()
                e = {"page": i + 1, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2), "text": text}
                if a.get("boxes"):
                    m = p.rotation_matrix
                    e["words"] = [[*_r(pymupdf.Rect(w[:4]) * m), w[4]] for w in _words(p)]
                used += len(text)
                pages.append(e)
                if used > limit:
                    truncated = True
                    break
            return {"path": path, "page_count": doc.page_count, "pages": pages, "truncated": truncated}
        finally:
            doc.close()

    def t_pdf_fields(self, a) -> dict:
        path = self.paths.pdf_in(a.get("path"))
        doc = _open(path)
        try:
            out = []
            for p in doc:
                m = p.rotation_matrix
                for w in p.widgets():
                    f = {"page": p.number + 1, "name": w.field_name, "type": w.field_type_string,
                         "value": w.field_value, "rect": _r(w.rect * m), "read_only": bool(w.field_flags & 1)}
                    if w.choice_values:
                        f["choices"] = [c if isinstance(c, str) else c[-1] for c in w.choice_values]
                    if w.field_type in (2, 5):  # checkbox / radio: the "on" value to set
                        try:
                            f["on_state"] = w.on_state()
                        except Exception:  # noqa: BLE001
                            pass
                    out.append(f)
            return {"path": path, "fields": out}
        finally:
            doc.close()

    def t_pdf_find(self, a) -> dict:
        path = self.paths.pdf_in(a.get("path"))
        rx, check = _regex(a)
        limit = int(a.get("limit") or 500)
        doc = _open(path)
        try:
            hits = []
            for i in rows_for(a.get("pages"), doc.page_count):
                for h in find_hits(doc[i], rx, check):
                    h.pop("pdf_rects")
                    hits.append(h)
                if len(hits) >= limit:
                    break
            return {"path": path, "count": len(hits[:limit]), "hits": hits[:limit], "truncated": len(hits) > limit}
        finally:
            doc.close()

    # ------------------------------------------------------------- edits
    def _check_edits(self, data) -> list[str]:
        if not isinstance(data, dict):
            raise ToolError("edits: must be an edit set object {\"leaf_edits\": 1, ...}")
        try:
            from . import editset
        except ImportError as ex:
            raise ToolError("this Leaf has no edit-set support") from ex
        from .agentlink import check_file_refs

        def allow(f):
            try:
                self.paths._check(f, "edit set file")
            except ToolError as ex:
                return str(ex)
            return None

        problems = list(editset.validate(data)) or check_file_refs(data, allow)
        if problems:
            raise ToolError("invalid edit set:\n- " + "\n- ".join(map(str, problems)))
        return problems

    def t_pdf_propose_edits(self, a) -> dict:
        path = self.paths.pdf_in(a.get("path"))
        data = a.get("edits")
        self._check_edits(data)
        title = str(a.get("title") or data.get("comment") or "Suggested edits")[:80]
        try:
            r = self.live.call({"op": "propose", "path": path, "open": True, "title": title, "edits": data})
        except ToolError as ex:
            r = None
            why = str(ex)
        if r is not None:
            if not r.get("ok"):
                msg = r.get("error") or "Leaf refused the proposal"
                if r.get("problems"):
                    msg += ":\n- " + "\n- ".join(map(str, r["problems"]))
                raise ToolError(msg)
            return {"delivered_to": "leaf", "proposal": r.get("proposal"), "status": r.get("status"),
                    "changes": r.get("changes"),
                    "next": "The user reviews it in Leaf's Agent suggestions panel. Check pdf_proposal_status; "
                            "Leaf never saves for you."}
        out = self.paths.new_out(path, None, "proposal", ".leaf-edits.json")
        with open(out, "x", encoding="utf-8") as f:
            json.dump({**data, "comment": data.get("comment") or title}, f, ensure_ascii=False, indent=1)
        return {"delivered_to": "file", "proposal_file": out, "reason": why,
                "next": "Leaf isn't reachable, so the proposal was saved next to the PDF. The user can apply it "
                        "from Leaf (edit sets) or ask you to use pdf_apply_edits_to_copy."}

    def t_pdf_proposal_status(self, a) -> dict:
        r = self.live.call({"op": "status", "proposal": a.get("proposal")})
        if not r.get("ok"):
            raise ToolError(r.get("error") or "unknown proposal")
        return {k: v for k, v in r.items() if k not in ("ok", "id")}

    def t_pdf_apply_edits_to_copy(self, a) -> dict:
        path = self.paths.pdf_in(a.get("path"))
        data = a.get("edits")
        self._check_edits(data)
        out = self.paths.new_out(path, a.get("output"), "edited")
        from . import editset

        warnings = apply_edit_set_to_copy(editset, path, data, out)
        return {"output": out, "warnings": warnings}

    # ------------------------------------------------------------- copies
    def t_pdf_ocr_to_copy(self, a) -> dict:
        from . import ocr

        path = self.paths.pdf_in(a.get("path"))
        language = str(a.get("language") or "eng")
        engines = ocr.available_engines()
        info = ocr.choose_engine(engines, language)
        if info is None:
            raise ToolError(ocr.NOT_AVAILABLE if not engines else
                            f"no OCR engine reads {language!r} here (install its language data in Leaf)")
        out = self.paths.new_out(path, a.get("output"), "ocr")
        with open(path, "rb") as f:
            data = f.read()
        doc = _open(path)
        try:
            rows = rows_for(a.get("pages"), doc.page_count)
            engine = ocr.create_engine(info.key, info.tessdata_for(language))
            done, skipped, t0 = [], [], time.perf_counter()
            try:
                for i, words, _secs in ocr.recognize_document(data, rows, engine, language,
                                                              only_if_needed=not a.get("all_pages")):
                    if words is None:
                        skipped.append(i + 1)
                        continue
                    page = doc[i]
                    ocr.write_text_layer(page, words, page.derotation_matrix)
                    done.append({"page": i + 1, "words": len(words)})
            finally:
                engine.close()
            if not done:
                return {"output": None, "skipped_pages": skipped,
                        "message": "every page already has text; pass all_pages=true to recognize anyway"}
            doc.save(out, garbage=3, deflate=True)
        finally:
            doc.close()
        return {"output": out, "engine": info.key, "language": language, "pages": done, "skipped_pages": skipped,
                "seconds": round(time.perf_counter() - t0, 1)}

    def t_pdf_redact_to_copy(self, a) -> dict:
        import pymupdf

        path = self.paths.pdf_in(a.get("path"))
        rx, check = _regex(a)
        doc = _open(path)
        try:
            rows = rows_for(a.get("pages"), doc.page_count)
            per_page, listed = {}, []
            for i in rows:
                hits = find_hits(doc[i], rx, check)
                if hits:
                    per_page[i] = hits
                    listed += [{k: v for k, v in h.items() if k != "pdf_rects"} for h in hits]
            if not a.get("confirm"):
                return {"confirmed": False, "count": len(listed), "hits": listed,
                        "message": "Nothing was written. Review the hits, then call again with confirm=true to "
                                   "write a redacted copy."}
            if not listed:
                return {"confirmed": True, "count": 0, "hits": [], "output": None, "message": "no hits, no file"}
            out = self.paths.new_out(path, a.get("output"), "redacted")
            from .redact import remove_under, signed_fields

            fill = _color(a.get("fill"))
            signed = signed_fields(doc)
            for i, hits in per_page.items():
                page = doc[i]
                rects = [r for h in hits for r in h["pdf_rects"]]
                remove_under(page, rects)
                for r in rects:
                    page.add_redact_annot(r, fill=fill, cross_out=False)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            doc.save(out, garbage=4, deflate=True)
        finally:
            doc.close()
        res = {"confirmed": True, "count": len(listed), "hits": listed, "output": out}
        if signed:
            res["warning"] = f"The source has {signed} digital signature(s); they aren't valid in the redacted copy."
        return res

    def t_pdf_export_text(self, a) -> dict:
        from .export import export_text

        path = self.paths.pdf_in(a.get("path"))
        md = bool(a.get("markdown"))
        ext = ".md" if md else ".txt"
        out = self.paths.new_out(path, a.get("output"), "text", ext)
        doc = _open(path)
        try:
            rows = rows_for(a.get("pages"), doc.page_count)
            for _ in export_text(doc, rows, out, markdown=md):
                pass
        finally:
            doc.close()
        return {"output": out, "pages": len(rows), "bytes": os.path.getsize(out)}

    # ------------------------------------------------------------- live
    def t_leaf_documents(self, a) -> dict:
        r = self.live.call({"op": "list"})
        if not r.get("ok"):
            raise ToolError(r.get("error") or "Leaf refused")
        return {"documents": r.get("documents", [])}

    def t_leaf_read(self, a) -> dict:
        msg = {"op": "read", "boxes": bool(a.get("boxes"))}
        if a.get("doc") is not None:
            msg["doc"] = a["doc"]
        elif a.get("path"):
            msg["path"] = self.paths._check(a["path"], "path")
        else:
            raise ToolError("give 'doc' (from leaf_documents) or 'path'")
        if a.get("pages") is not None:
            p = a["pages"]
            msg["pages"] = p if isinstance(p, (str, list)) else str(p)
        if a.get("include"):
            msg["include"] = a["include"]
        r = self.live.call(msg)
        if not r.get("ok"):
            raise ToolError(r.get("error") or "Leaf refused")
        return r.get("document", {})


def apply_edit_set_to_copy(editset, path: str, data: dict, out: str) -> list[str]:
    """Headless: open `path`, apply the edit set (editset.apply_to_document), write `out` (a new file)."""
    from .document import PdfDocument

    doc = PdfDocument.open(path)
    try:
        warnings = list(editset.apply_to_document(doc, data, base_dir=os.path.dirname(path)) or [])
    except editset.EditSetError as ex:
        raise ToolError(f"invalid edit set: {ex}") from ex
    editset.save(doc, out)
    return warnings


def _color(v) -> tuple:
    if isinstance(v, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", v.strip()):
        h = v.strip().lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0, 0, 0)


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


# ===================================================================== stdio loop
def serve(instream, outstream, server: Server | None = None) -> int:
    """Read JSON-RPC messages (one per line, UTF-8) from `instream` until EOF; write responses to `outstream`."""
    server = server or Server()
    while True:
        line = instream.readline()
        if not line:
            break
        if isinstance(line, str):
            line = line.encode("utf-8")
        if not line.strip():
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp = _error(None, -32700, "Parse error")
        else:
            resp = server.handle(msg)
        if resp is not None:
            data = json.dumps(resp, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
            outstream.write(data)
            outstream.flush()
    server.live.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="leaf mcp", description="Leaf's MCP server (stdio).")
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="only allow files inside DIR (repeatable; default: any local file)")
    args = ap.parse_args(argv)
    roots = list(args.root) + [r for r in os.environ.get("LEAF_MCP_ROOTS", "").split(os.pathsep) if r]
    for r in roots:
        if not os.path.isabs(r) or not os.path.isdir(r):
            print(f"leaf mcp: --root must be an existing absolute folder: {r}", file=sys.stderr)
            return 2
    try:
        import pymupdf

        pymupdf.TOOLS.mupdf_display_errors(False)  # MuPDF warnings go to stdout otherwise
    except Exception:  # noqa: BLE001
        pass
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints must never reach the protocol stream
    return serve(sys.stdin.buffer, out, Server(roots))
