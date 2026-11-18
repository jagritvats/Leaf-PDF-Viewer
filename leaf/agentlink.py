"""Live agent link: AI agents (e.g. through `leaf mcp`) talk to the running Leaf over the local server (leaf.ipc).

Off by default (Settings › Agents). When on, a random token is written to a file only this user can read
(`token_path()`); every agent request must carry it. Agents can list the open documents, read one (text with
boxes, form fields, annotations, outline, pending edits) and *propose* an edit set (leaf.editset format). A
proposal only shows up in the window's "Agent suggestions" panel: nothing changes until the user accepts it
(or ticked auto-accept there), and the accepted changes become one undo step "Agent: <title>". Agents can't
save, sign, close or open dialogs: there is simply no request for it.

Requests (newline-delimited JSON, see leaf.ipc), all with "token":
    hello {client}                         -> {session, pid}
    list                                   -> {documents: [{doc, path, name, pages, modified, active}]}
    read {doc|path, pages?, boxes?, include?} -> {document: {...}}
    propose {doc|path, title, edits, open?} -> {proposal, status}
    status {proposal}                      -> {status, accepted, total, warnings}
    withdraw {proposal}                    -> {status}
"""
from __future__ import annotations

import hmac
import itertools
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field

TOKEN_FILE = "agent-token"
LOG_KEY = "agent/log"
ENABLED_KEY = "agent/enabled"
LOG_MAX = 60
REFUSED = {
    "save": "Agents can't save: the user saves in Leaf.",
    "sign": "Signing isn't available to agents.",
    "close": "Agents can't close documents.",
}


# ------------------------------------------------------------------ token file (no Qt: `leaf mcp` reads it too)
def agent_dir() -> str:
    """Leaf's AppDataLocation (what QStandardPaths gives for organization "Leaf", application "Leaf"),
    computed without Qt so the MCP process finds the same file. LEAF_AGENT_DIR overrides it."""
    env = os.environ.get("LEAF_AGENT_DIR")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "Leaf", "Leaf")


def token_path() -> str:
    return os.path.join(agent_dir(), TOKEN_FILE)


def read_token_file(path: str | None = None) -> dict | None:
    """{"token": ..., "server": ...} or None when the link is off (no file) or the file is unreadable."""
    try:
        with open(path or token_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("token"), str) else None


def write_token_file(server: str | None) -> str:
    """A new random token, written (owner-only) with the server name. Returns the token."""
    token = secrets.token_urlsafe(32)
    path = token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # an existing .tmp keeps its old mode otherwise
        os.write(fd, json.dumps({"token": token, "server": server, "pid": os.getpid()}).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)
    return token


def remove_token_file() -> None:
    try:
        os.remove(token_path())
    except OSError:
        pass


# ------------------------------------------------------------------ proposals
@dataclass
class Change:
    """One reviewable change of an edit set: `path` selects it in editset.subset()."""
    path: tuple
    summary: str
    value: object = None
    page: int | None = None  # 1-based page of the result, when the change names one


@dataclass
class Proposal:
    id: int
    title: str
    data: dict
    session: str = ""
    doc: object = None  # document.PdfDocument it targets (None while its file is being opened)
    path: str = ""
    changes: list = field(default_factory=list)
    status: str = "pending"  # pending | accepted | partially accepted | rejected | withdrawn | failed
    accepted: int = 0
    warnings: list = field(default_factory=list)
    created: float = field(default_factory=time.time)


_ids = itertools.count(1)


def _short(v, n: int = 60) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def split_changes(data: dict) -> list[Change]:
    """The reviewable changes of an edit set (editset.entries), each with its JSON for the tooltip."""
    from . import editset

    out = []
    for path, label, text in editset.entries(data):
        sec = data.get(path[0])
        value = sec if len(path) == 1 else sec[path[1]] if isinstance(sec, (list, dict)) else None
        page = value.get("page") if isinstance(value, dict) else None
        out.append(Change(tuple(path), f"{label}: {text}", value,
                          page if isinstance(page, int) and not isinstance(page, bool) else None))
    return out


def subset(data: dict, changes: list[Change]) -> dict:
    from . import editset

    return editset.subset(data, [c.path for c in changes])


def file_refs(data) -> list[str]:
    """Every local file an edit set would read ("file" entries: pages inserted from a file, replaced images)."""
    out = []
    if isinstance(data, dict):
        f = data.get("file")
        if isinstance(f, str):
            out.append(f)
        for v in data.values():
            out += file_refs(v)
    elif isinstance(data, list):
        for v in data:
            out += file_refs(v)
    return out


def check_file_refs(data, allow=None) -> list[str]:
    """Problems with the files an agent's edit set reads: they must be absolute, local and exist; `allow(path)`
    may veto more (the MCP server's root folders)."""
    from .safeopen import is_network_path

    problems = []
    for f in file_refs(data):
        if not os.path.isabs(f) or is_network_path(f) or "://" in f:
            problems.append(f"file {f!r}: agents must use absolute local paths")
        elif not os.path.isfile(f):
            problems.append(f"file {f!r} doesn't exist")
        elif allow is not None:
            why = allow(f)
            if why:
                problems.append(why)
    return problems


def _local_file(path) -> bool:
    """An absolute path of an existing local file (checked without touching the network)."""
    from .safeopen import is_network_path

    return (isinstance(path, str) and os.path.isabs(path) and not is_network_path(path) and "://" not in path
            and os.path.isfile(path))


# ------------------------------------------------------------------ the link (Qt)
def settings():
    from PySide6.QtCore import QSettings

    return QSettings()


def is_enabled() -> bool:
    return settings().value(ENABLED_KEY, False) in (True, "true", "1", 1)


def load_log() -> list[dict]:
    try:
        v = json.loads(settings().value(LOG_KEY, "[]") or "[]")
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def log_event(client: str, text: str) -> None:
    entries = load_log()
    entries.insert(0, {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "client": client, "event": text})
    settings().setValue(LOG_KEY, json.dumps(entries[:LOG_MAX]))
    lk = _link
    if lk is not None:
        lk.logChanged.emit()


def clear_log() -> None:
    settings().setValue(LOG_KEY, "[]")
    if _link is not None:
        _link.logChanged.emit()


_link = None


def link():
    """The process-wide AgentLink (created on first use)."""
    global _link
    if _link is None:
        _link = _link_class()()
    return _link


def install_handlers(server) -> None:
    link().attach(server)


def _link_class():
    from PySide6.QtCore import QObject, QTimer, Signal

    class AgentLink(QObject):
        proposalAdded = Signal(object)  # Proposal
        proposalChanged = Signal(object)
        sessionsChanged = Signal()
        logChanged = Signal()

        def __init__(self):
            super().__init__()
            self.server = None
            self.proposals: dict[int, Proposal] = {}
            self.sessions: dict = {}  # QLocalSocket -> {"id", "client", "since"}
            self._token: str | None = None
            self.auto_accept: set = set()  # documents (PdfDocument ids) whose window ticked auto-accept

        # ---------------------------------------------------------- setup
        def attach(self, server) -> None:
            self.server = server
            for op in ("hello", "list", "read", "propose", "status", "withdraw", *REFUSED):
                server.handlers[op] = self._dispatch
            server.on_disconnect = self._disconnected
            if is_enabled():
                self.ensure_token()

        def server_name(self) -> str | None:
            return self.server.name if self.server is not None else None

        def ensure_token(self) -> str:
            data = read_token_file()
            if data and data.get("server") == self.server_name():
                self._token = data["token"]
            else:
                self._token = write_token_file(self.server_name())
            return self._token

        def set_enabled(self, on: bool) -> None:
            settings().setValue(ENABLED_KEY, bool(on))
            if on:
                self.ensure_token()
                log_event("Leaf", "Agent link turned on")
            else:
                self._token = None
                remove_token_file()
                self.drop_sessions()
                log_event("Leaf", "Agent link turned off")

        def regenerate(self) -> str:
            """New token; connected agents are disconnected and must read the new one."""
            self.drop_sessions()
            self._token = write_token_file(self.server_name())
            log_event("Leaf", "Token regenerated (old token revoked)")
            return self._token

        def drop_sessions(self) -> None:
            for c in list(self.sessions):
                try:
                    c.abort()
                except RuntimeError:
                    pass
            self.sessions.clear()
            self.sessionsChanged.emit()

        def _disconnected(self, conn) -> None:
            s = self.sessions.pop(conn, None)
            if s is not None:
                log_event(s["client"], "Disconnected")
                self.sessionsChanged.emit()

        # ---------------------------------------------------------- requests
        def _dispatch(self, server, conn, req: dict) -> dict:
            if not is_enabled() or self._token is None and not self._reload_token():
                return {"ok": False, "error": "The agent link is off. Turn it on in Leaf: Settings › Agents."}
            tok = req.get("token")
            if not isinstance(tok, str) or not hmac.compare_digest(tok.encode(), (self._token or "").encode()):
                return {"ok": False, "error": "invalid token (Leaf may have regenerated it: read the token file again)",
                        "auth": False}
            op = req.get("op")
            if op in REFUSED:
                return {"ok": False, "error": REFUSED[op]}
            sess = self.sessions.get(conn)
            if sess is None or op == "hello":
                client = _short(str(req.get("client") or (sess or {}).get("client") or "agent"), 40)
                if sess is None:
                    sess = self.sessions[conn] = {"id": secrets.token_hex(4), "client": client, "since": time.time()}
                    log_event(client, "Connected")
                    self.sessionsChanged.emit()
                else:
                    sess["client"] = client
            if op == "hello":
                return {"ok": True, "session": sess["id"], "pid": os.getpid(), "app": "leaf"}
            return getattr(self, f"_op_{op}")(req, sess)

        def _reload_token(self) -> bool:
            data = read_token_file()
            self._token = data["token"] if data else None
            return self._token is not None

        def _op_list(self, req, sess) -> dict:
            return {"ok": True, "documents": [doc_entry(w) for w in open_windows()]}

        def _op_read(self, req, sess) -> dict:
            win = find_window(req)
            if win is None:
                return {"ok": False, "error": "no such open document (see list)"}
            include = req.get("include") or ["text", "fields", "annotations", "outline"]
            try:
                d = read_document(win, req.get("pages"), bool(req.get("boxes")), set(include))
            except ValueError as ex:
                return {"ok": False, "error": str(ex)}
            return {"ok": True, "document": d}

        def _op_propose(self, req, sess) -> dict:
            data = req.get("edits")
            if not isinstance(data, dict):
                return {"ok": False, "error": "propose: 'edits' must be an edit set object"}
            try:
                from . import editset
            except ImportError:
                return {"ok": False, "error": "this Leaf can't apply edit sets"}
            problems = list(editset.validate(data)) or check_file_refs(data)
            if problems:
                return {"ok": False, "error": "invalid edit set", "problems": problems}
            title = _short(str(req.get("title") or data.get("comment") or "Suggested edits"), 80)
            changes = split_changes(data)
            if not changes:
                return {"ok": False, "error": "the edit set has no changes"}
            p = Proposal(next(_ids), title, data, sess["client"], changes=changes)
            win = find_window(req)
            if win is None:
                path = req.get("path")
                if not (req.get("open") and _local_file(path)):
                    return {"ok": False, "error": "the document isn't open in Leaf (pass an absolute 'path' and "
                                                  "open: true to open it)"}
                p.path = os.path.abspath(path)
                self.proposals[p.id] = p
                QTimer.singleShot(0, lambda: self._open_then_add(p))
            else:
                p.doc, p.path = win.canvas.doc, win.canvas.doc.path or ""
                self.proposals[p.id] = p
                self._add(p)
            log_event(sess["client"], f"Proposed “{title}” ({len(changes)} change{'s' * (len(changes) != 1)}) "
                                      f"for {os.path.basename(p.path) or 'a document'}")
            return {"ok": True, "proposal": p.id, "status": p.status, "changes": len(changes)}

        def _open_then_add(self, p: Proposal) -> None:
            from .ipc import open_in_running

            open_in_running([p.path])
            win = next((w for w in open_windows() if w.canvas.doc.path == p.path), None)
            if win is None:
                p.status = "failed"
                p.warnings.append("Leaf couldn't open the document")
                self.proposalChanged.emit(p)
                return
            p.doc = win.canvas.doc
            self._add(p)

        def _add(self, p: Proposal) -> None:
            self.proposalAdded.emit(p)
            if doc_id(p.doc) in self.auto_accept and p.status == "pending":
                self.accept(p)

        def _op_status(self, req, sess) -> dict:
            p = self.proposals.get(req.get("proposal"))
            if p is None:
                return {"ok": False, "error": "no such proposal"}
            if p.status == "pending" and p.doc is not None and not any(w.canvas.doc is p.doc for w in open_windows()):
                p.status, p.warnings = "failed", ["the document was closed before the user decided"]
            return {"ok": True, "proposal": p.id, "status": p.status, "accepted": p.accepted,
                    "total": len(p.changes), "warnings": p.warnings}

        def _op_withdraw(self, req, sess) -> dict:
            p = self.proposals.get(req.get("proposal"))
            if p is None:
                return {"ok": False, "error": "no such proposal"}
            if p.status == "pending":
                p.status = "withdrawn"
                self.proposalChanged.emit(p)
            return {"ok": True, "proposal": p.id, "status": p.status}

        # ---------------------------------------------------------- user decisions
        def pending_for(self, doc) -> list[Proposal]:
            return [p for p in self.proposals.values() if p.doc is doc and doc is not None and p.status == "pending"]

        def accept(self, p: Proposal, changes: list[Change] | None = None) -> list[str]:
            """Apply `changes` (default: all) of a pending proposal as one undo step "Agent: <title>"."""
            if p.status != "pending":
                return [f"already {p.status}"]
            win = next((w for w in open_windows() if w.canvas.doc is p.doc), None)
            if win is None:
                p.status = "failed"
                p.warnings = ["the document was closed"]
                self.proposalChanged.emit(p)
                return p.warnings
            chosen = p.changes if changes is None else [c for c in p.changes if any(c is x for x in changes)]
            if not chosen:
                return self.reject(p)
            from . import editset

            warnings = list(editset.apply_edits(win.canvas, subset(p.data, chosen), label=f"Agent: {p.title}") or [])
            p.accepted = len(chosen)
            p.status = "accepted" if len(chosen) == len(p.changes) else "partially accepted"
            p.warnings = warnings
            log_event(p.session, f"“{p.title}”: {p.status} ({p.accepted}/{len(p.changes)})")
            self.proposalChanged.emit(p)
            return warnings

        def reject(self, p: Proposal) -> list[str]:
            if p.status == "pending":
                p.status = "rejected"
                log_event(p.session, f"“{p.title}”: rejected")
                self.proposalChanged.emit(p)
            return []

    return AgentLink


# ------------------------------------------------------------------ reading open documents
_doc_ids = itertools.count(1)


def doc_id(doc) -> int:
    if doc is None:
        return 0
    v = doc.__dict__.get("_agent_id")
    if v is None:
        v = doc.__dict__["_agent_id"] = next(_doc_ids)
    return v


def open_windows() -> list:
    from .window import _windows

    return [w for w in _windows if w.canvas.doc is not None]


def doc_entry(win) -> dict:
    doc = win.canvas.doc
    return {"doc": doc_id(doc), "path": doc.path, "name": os.path.basename(doc.path or "Untitled"),
            "pages": len(doc.pages), "modified": not win.canvas.undo.isClean(), "active": win.isActiveWindow()}


def find_window(req: dict):
    wins = open_windows()
    d = req.get("doc")
    if isinstance(d, int) and not isinstance(d, bool):
        return next((w for w in wins if doc_id(w.canvas.doc) == d), None)
    p = req.get("path")
    if isinstance(p, str) and p:
        p = os.path.normcase(os.path.abspath(p))
        return next((w for w in wins if w.canvas.doc.path and os.path.normcase(w.canvas.doc.path) == p), None)
    return None


def _r(rect) -> list[float]:
    return [round(rect[0], 2), round(rect[1], 2), round(rect[2], 2), round(rect[3], 2)]


def read_document(win, pages=None, boxes: bool = False, include: set | None = None) -> dict:
    """What an agent sees of an open document, as shown in Leaf (unsaved edits included where Leaf tracks them).
    Coordinates are points in each source page's display space (page.rect: origin top-left, its own /Rotate
    applied, Leaf's extra rotation not)."""
    import pymupdf

    from .document import rect_key
    from .pageops import parse_ranges

    include = include or {"text"}
    canvas = win.canvas
    doc = canvas.doc
    rows = parse_ranges(pages if isinstance(pages, str) else "", len(doc.pages)) if not isinstance(pages, list) \
        else [int(p) - 1 for p in pages if isinstance(p, int) and 1 <= p <= len(doc.pages)]
    out = {"doc": doc_id(doc), "path": doc.path, "page_count": len(doc.pages),
           "modified": not canvas.undo.isClean(), "pages": []}
    for row in rows:
        ref = doc.pages[row]
        page = doc.page(ref)
        rect = page.rect
        entry = {"page": row + 1, "width": round(rect.width, 2), "height": round(rect.height, 2),
                 "rotation": (page.rotation + ref.rotation) % 360}
        if "text" in include:
            words = ref.src.words(ref.index, ref.uid)
            tp = ref.src.text_page(ref.index, ref.uid)
            text = tp.get_text("text", sort=True, flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES).rstrip()
            if not text.strip() and words:  # OCR-only page: its recognized words
                text = " ".join(w[4] for w in words)
            entry["text"] = text
            if boxes:
                entry["words"] = [[*_r(w[:4]), w[4]] for w in words]
        if "fields" in include:
            fields = []
            m = page.rotation_matrix
            for w in page.widgets():
                key = (ref.uid, w.field_name, rect_key(w.rect))
                val = doc.form_values.get(key, w.field_value)
                f = {"name": w.field_name, "type": w.field_type_string, "value": val,
                     "rect": _r(w.rect * m), "read_only": bool(w.field_flags & 1)}
                if w.choice_values:
                    f["choices"] = [c if isinstance(c, str) else c[-1] for c in w.choice_values]
                fields.append(f)
            entry["fields"] = fields
        if "annotations" in include:
            m = page.rotation_matrix
            ann = [{"type": a.type[1], "rect": _r(a.rect * m), "content": a.info.get("content", ""),
                    "author": a.info.get("title", ""), "in_file": True} for a in page.annots()]
            for it in canvas.overlays_for(ref):
                try:
                    r = it.mapRectToParent(it.boundingRect())
                    rr = [r.left(), r.top(), r.right(), r.bottom()]
                except Exception:  # noqa: BLE001
                    rr = None
                a = {"type": getattr(it, "kind", type(it).__name__), "rect": _r(rr) if rr else None, "in_file": False}
                text = getattr(it, "toPlainText", None)
                if callable(text):
                    a["content"] = text()
                ann.append(a)
            entry["annotations"] = ann
        out["pages"].append(entry)
    if "outline" in include:
        if doc.outline is None:
            out["outline"] = [{"level": lvl, "title": t, "page": p} for lvl, t, p, *_ in doc.primary.doc.get_toc(simple=True)]
        else:
            rows_by_uid = {r.uid: i + 1 for i, r in enumerate(doc.pages)}
            out["outline"] = [{"level": b.level, "title": b.title, "page": rows_by_uid.get(b.uid)} for b in doc.outline]
    if "edits" in include:
        try:
            from . import editset

            out["edits"] = editset.export_edits(canvas)
        except Exception as ex:  # noqa: BLE001
            out["edits_error"] = str(ex)
    return out
