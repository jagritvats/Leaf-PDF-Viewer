"""Out-of-process page rendering.

PyMuPDF holds the GIL while it renders, so a thread can't keep the GUI responsive on heavy
pages. Sources are immutable bytes, so a separate process can open its own copies and render
them independently. This module is deliberately Qt-free: the spawned child imports only this
file and PyMuPDF.

Protocol (GUI -> worker, pickled tuples over a multiprocessing Pipe):
    ("src", sid, path, password)   source bytes are handed over in a temp file (never blocks the GUI)
    ("flags", sid, {xref: F})      set annotation /F values in the worker's copy (hides changed annotations)
    ("upage", sid, uid, payload)   text edits: show page `uid` from its own slot page (Source.upage_payload: the
                                   slot's page keys + the objects Leaf created for it, renumbered here), or with
                                   payload None from the source page again. The worker never runs the text engine.
    ("render", rid, sid, index, scale, rotation, clip, uid)
    ("cancel", [rid, ...])
    ("reset",) / ("quit",)
Worker -> GUI: ("ready", pid), ("img", rid, x, y, w, h, stride) followed by the raw samples
via send_bytes, or ("err", rid, message).
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import os
import tempfile
import time
from collections import OrderedDict, deque


def _worker_main(conn) -> None:
    import pymupdf

    docs: dict = {}
    pages: OrderedDict = OrderedDict()
    queue: deque = deque()
    slots: dict = {}  # (sid, uid) -> [slot page number, in use]

    def page(sid: int, index: int, uid=None):
        slot = slots.get((sid, uid))
        if slot is not None and slot[1]:
            index = slot[0]
        key = (sid, index)
        p = pages.get(key)
        if p is None:
            p = docs[sid][index]
            pages[key] = p
            if len(pages) > 64:
                pages.popitem(last=False)
        else:
            pages.move_to_end(key)
        return p

    def handle(msg) -> bool:
        kind = msg[0]
        if kind == "render":
            queue.append(msg)
        elif kind == "cancel":
            drop = set(msg[1])
            for m in [m for m in queue if m[1] in drop]:
                queue.remove(m)
        elif kind == "src":
            _, sid, path, password = msg
            with open(path, "rb") as f:
                data = f.read()
            try:
                os.remove(path)
            except OSError:
                pass
            d = pymupdf.open(stream=data, filetype="pdf")
            if d.needs_pass:
                d.authenticate(password or "")
            docs[sid] = d
        elif kind == "flags":
            _, sid, flags = msg
            d = docs.get(sid)
            if d is not None:
                for xref, f in flags.items():
                    try:
                        d.xref_set_key(xref, "F", str(f))
                    except Exception:  # noqa: BLE001
                        pass
        elif kind == "upage":
            _, sid, uid, payload = msg
            d = docs.get(sid)
            if d is not None:
                try:
                    _install_slot(pymupdf, d, slots, pages, sid, uid, payload)
                except Exception:  # noqa: BLE001  (the page then shows without its text edits)
                    slots.pop((sid, uid), None)
        elif kind == "reset":
            queue.clear()
            pages.clear()
            slots.clear()
            for d in docs.values():
                d.close()
            docs.clear()
        elif kind == "quit":
            return False
        return True

    conn.send(("ready", os.getpid()))
    try:
        while True:
            if not queue and not handle(conn.recv()):
                return
            while conn.poll():
                if not handle(conn.recv()):
                    return
            if not queue:
                continue
            _, rid, sid, index, scale, rotation, clip, *rest = queue.popleft()
            try:
                p = page(sid, index, rest[0] if rest else None)
                m = pymupdf.Matrix(scale, scale).prerotate(rotation)
                pix = p.get_pixmap(matrix=m, clip=pymupdf.Rect(clip) if clip else None, alpha=False, annots=True)
            except Exception as ex:  # noqa: BLE001
                conn.send(("err", rid, str(ex)))
                continue
            conn.send(("img", rid, pix.x, pix.y, pix.width, pix.height, pix.stride))
            conn.send_bytes(pix.samples_mv)
            del pix
    except (EOFError, OSError, KeyboardInterrupt):
        return  # parent went away


def _install_slot(pymupdf, d, slots: dict, pages, sid: int, uid: int, payload) -> None:
    """Mirror a text-edit slot of the GUI's display copy (see Source.upage_payload) in the worker's copy `d`."""
    import re

    slot = slots.get((sid, uid))
    if payload is None:
        if slot is not None:
            slot[1] = False
        return
    _index, keys, objs = payload
    xmap = {x: d.get_new_xref() for x, _, _ in objs}  # the GUI's numbers -> fresh numbers here

    def ren(s: str) -> str:
        return re.sub(r"\b(\d+) 0 R\b", lambda mm: f"{xmap.get(int(mm.group(1)), int(mm.group(1)))} 0 R", s)

    pdf = pymupdf._as_pdf_document(d)
    m = pymupdf.mupdf
    for x, src, raw in objs:
        nx = xmap[x]
        d.update_object(nx, ren(src))
        if raw is not None:  # raw = still encoded as the dictionary's /Filter says
            m.pdf_update_stream(pdf, m.pdf_new_indirect(pdf, nx, 0), m.fz_new_buffer_from_copied_data(raw), 1)
    if slot is None:
        r = d[0].mediabox
        d.new_page(-1, width=max(r.width, 1), height=max(r.height, 1))
        pages.clear()  # adding a page invalidates every Page object of the document
        slot = slots[(sid, uid)] = [d.page_count - 1, True]
    px = d.page_xref(slot[0])
    for k in ("MediaBox", "CropBox", "BleedBox", "TrimBox", "ArtBox", "Rotate", "Resources", "Contents", "Annots",
              "Group", "UserUnit"):
        d.xref_set_key(px, k, ren(keys[k]) if k in keys else "null")
    pages.pop((sid, slot[0]), None)  # (MuPDF reads the shared annotations when it first loads the slot page)
    slot[1] = True


class RenderWorker:
    """GUI-side handle. All calls are non-blocking (except tiny pipe writes); any failure marks
    the worker `failed` and callers fall back to in-process rendering."""

    START_TIMEOUT = 15.0
    WAIT_FOR_START = 2.5

    def __init__(self) -> None:
        self.ready = False
        self.failed = False
        self.error = ""  # why it failed, for diagnostics (self-test)
        self.sent: set[int] = set()
        self.flag_revs: dict[int, int] = {}  # source id -> display_rev last sent
        self.slot_revs: dict[tuple, int] = {}  # (source id, page uid) -> text-edit slot revision last sent
        self.pending_files: set[str] = set()
        self._ids = itertools.count(1)
        self._t0 = time.monotonic()
        try:
            ctx = mp.get_context("spawn")
            self.conn, child = ctx.Pipe(duplex=True)
            self.proc = ctx.Process(target=_worker_main, args=(child,), daemon=True, name="leaf-render")
            self.proc.start()
            child.close()
        except Exception as e:  # noqa: BLE001
            self.failed = True
            self.error = f"start: {e!r}"
            self.proc = None

    @property
    def starting(self) -> bool:
        """Still booting (and not for long): worth waiting for rather than rendering in-process."""
        return not self.ready and not self.failed and time.monotonic() - self._t0 < self.WAIT_FOR_START

    @property
    def usable(self) -> bool:
        if self.failed:
            return False
        if not self.ready:
            self._check_ready()
        return self.ready and not self.failed

    def _check_ready(self) -> None:
        try:
            if self.conn.poll():
                msg = self.conn.recv()
                self.ready = msg[0] == "ready"
            elif not self.proc.is_alive():
                self._fail(f"worker exited during startup (code {self.proc.exitcode})")
            elif time.monotonic() - self._t0 > self.START_TIMEOUT:
                self._fail("worker start timed out")
        except (EOFError, OSError) as e:
            self._fail(f"startup pipe: {e!r}")

    def _fail(self, why: str = "") -> None:
        self.error = self.error or why or "pipe error"
        self.failed = True
        self.ready = False
        self.close()

    def _send(self, msg) -> bool:
        if self.failed:
            return False
        try:
            self.conn.send(msg)
            return True
        except (OSError, EOFError, ValueError):
            self._fail()
            return False

    def reset(self) -> None:
        self.sent.clear()
        self.flag_revs.clear()
        self.slot_revs.clear()
        self._send(("reset",))

    def ensure_source(self, src) -> None:
        if src.id in self.sent:
            return
        fd, path = tempfile.mkstemp(prefix="leaf-src-", suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(src.data)
        self.pending_files.add(path)
        if self._send(("src", src.id, path, src.password)):
            self.sent.add(src.id)

    def sync_flags(self, src) -> None:
        """Bring the worker's copy of `src` in line with its display copy (hidden annotations)."""
        rev = getattr(src, "display_rev", 0)
        if src.id in self.sent and self.flag_revs.get(src.id, 0) != rev:
            if not self._send(("flags", src.id, src.annot_flags())):
                return
            # text edits: slots whose content changed since last sent (Source.upage_payload)
            for uid, r in getattr(src, "upage_revs", dict)().items():
                if self.slot_revs.get((src.id, uid)) != r:
                    if not self._send(("upage", src.id, uid, src.upage_payload(uid))):
                        return
                    self.slot_revs[(src.id, uid)] = r
            self.flag_revs[src.id] = rev

    def submit(self, src, index: int, scale: float, rotation: int, clip=None, uid: int | None = None) -> int:
        """`clip` is in the source page.rect space (as get_pixmap expects); `uid` picks the page's text-edit
        slot if it has one. Returns a request id, 0 on failure."""
        self.ensure_source(src)
        self.sync_flags(src)
        rid = next(self._ids)
        clip = tuple(clip) if clip is not None else None
        return rid if self._send(("render", rid, src.id, index, float(scale), int(rotation), clip, uid)) else 0

    def cancel(self, rids) -> None:
        if rids:
            self._send(("cancel", list(rids)))

    def poll(self, max_items: int = 4) -> list[tuple]:
        """Returns finished results: ("img", rid, x, y, w, h, stride, data) or ("err", rid, msg)."""
        out = []
        if self.failed:
            return out
        try:
            while len(out) < max_items and self.conn.poll():
                msg = self.conn.recv()
                if msg[0] == "img":
                    out.append((*msg, self.conn.recv_bytes()))
                elif msg[0] == "err":
                    out.append(msg)
                elif msg[0] == "ready":
                    self.ready = True
        except (EOFError, OSError):
            self._fail()
        return out

    def close(self) -> None:
        conn, proc = getattr(self, "conn", None), getattr(self, "proc", None)
        self.failed = True
        if conn is not None:
            try:
                conn.send(("quit",))
            except (OSError, EOFError, ValueError):
                pass
            try:
                conn.close()
            except OSError:
                pass
        if proc is not None:
            proc.join(0.3)
            if proc.is_alive():
                proc.terminate()
        self.proc = None
        for p in self.pending_files:
            try:
                os.remove(p)
            except OSError:
                pass
        self.pending_files.clear()
