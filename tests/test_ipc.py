"""Single instance (local server), live agent link (token, propose -> review panel -> one undo step) and the MCP
server (stdio JSON-RPC) — all in-process: clients run in threads while the test pumps Qt events."""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QCoreApplication, QSettings

from leaf import agentlink as AL
from leaf import ipc, mcp_server
from tests.test_editor import app, make_pdf, pump  # noqa: F401


def unique_name(tmp_path) -> str:
    if sys.platform == "win32":
        return f"leaf-test-{uuid.uuid4().hex[:12]}"
    return os.path.join(str(tmp_path), "s.sock")


def in_thread(app, fn, timeout=10.0):
    """Run a blocking client call in a thread while the GUI thread serves it."""
    box = []
    t = threading.Thread(target=lambda: box.append(_catch(fn)), daemon=True)
    t.start()
    end = time.time() + timeout
    while t.is_alive() and time.time() < end:
        app.processEvents()
        time.sleep(0.002)
    assert box, "client call timed out"
    ok, v = box[0]
    if not ok:
        raise v
    return v


def _catch(fn):
    try:
        return True, fn()
    except BaseException as ex:  # noqa: BLE001
        return False, ex


@pytest.fixture
def sandbox(app, tmp_path, monkeypatch):  # noqa: F811
    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafIpcTest")
    QSettings().clear()
    monkeypatch.setenv("LEAF_AGENT_DIR", str(tmp_path / "agent"))
    AL._link = None
    yield
    AL._link = None
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


@pytest.fixture
def server(sandbox, tmp_path):
    srv = ipc.IpcServer(unique_name(tmp_path))
    assert srv.listen()
    AL.install_handlers(srv)
    yield srv
    srv.close()
    srv.deleteLater()


# ============================================================== single instance
def test_forward_open_reaches_running_instance(app, server, tmp_path, monkeypatch):  # noqa: F811
    got = []
    server.on_open = got.extend
    monkeypatch.chdir(tmp_path)
    rel = "a.pdf"
    t = time.perf_counter()
    assert in_thread(app, lambda: ipc.forward_open([rel, str(tmp_path / "b.pdf")], server.name)) is True
    assert time.perf_counter() - t < 2.0
    pump(app, 0.05)
    assert got == [os.path.abspath(rel), str(tmp_path / "b.pdf")]


def test_no_server_means_normal_start_quickly(tmp_path):
    t = time.perf_counter()
    assert ipc.forward_open(["x.pdf"], unique_name(tmp_path)) is False
    assert time.perf_counter() - t < 0.15


def test_second_server_defers_and_stale_socket_is_cleaned(app, sandbox, tmp_path):  # noqa: F811
    name = unique_name(tmp_path)
    first = ipc.IpcServer(name)
    assert first.listen()
    second = ipc.IpcServer(name)
    assert not second.listen()  # a live Leaf owns the name
    first.close()
    if sys.platform != "win32":  # a crashed Leaf leaves its socket file behind
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(name):
            os.remove(name)
        s.bind(name)
        s.close()
        assert os.path.exists(name)
    third = ipc.IpcServer(name)
    assert third.listen()
    assert in_thread(app, lambda: ipc.request({"op": "ping"}, name))["ok"]
    third.close()
    # let the closed pipes' pending notifications run while the servers still exist (dropping them here left
    # freed objects for a later event-loop turn: an access violation in whichever test activated a window next)
    pump(app, 0.1)
    for srv in (first, second, third):
        srv.deleteLater()
    pump(app, 0.05)


def test_open_in_running_reuses_and_creates_windows(app, sandbox, tmp_path):  # noqa: F811
    from leaf.window import MainWindow, _windows

    a, b = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    make_pdf(a)
    make_pdf(b)
    w = MainWindow()
    w.show()
    try:
        shown = ipc.open_in_running([a])
        assert shown is w and w.canvas.doc.path == a
        assert ipc.open_in_running([a]) is w  # already open: same window, no new one
        n = len(_windows)
        other = ipc.open_in_running([b])
        assert other is not w and other.canvas.doc.path == b and len(_windows) == n + 1
        empty = ipc.open_in_running([], new_window=True)  # `leaf --new-window`
        assert empty not in (w, other) and empty.canvas.doc is None and len(_windows) == n + 2
    finally:
        for x in list(_windows):
            x.close()
        pump(app, 0.05)


# ============================================================== agent link
def test_token_required_and_rejected(app, server, tmp_path):  # noqa: F811
    ask = lambda msg: in_thread(app, lambda: ipc.request(msg, server.name))  # noqa: E731
    r = ask({"op": "list", "token": "x"})
    assert not r["ok"] and "off" in r["error"]
    AL.link().set_enabled(True)
    info = AL.read_token_file()
    assert info and info["server"] == server.name and len(info["token"]) >= 32
    if os.name == "posix":
        assert os.stat(AL.token_path()).st_mode & 0o777 == 0o600
    assert not ask({"op": "list"})["ok"]
    bad = ask({"op": "list", "token": "nope"})
    assert not bad["ok"] and bad["auth"] is False
    good = ask({"op": "list", "token": info["token"]})
    assert good["ok"] and good["documents"] == []
    for op in ("save", "sign"):
        r = ask({"op": op, "token": info["token"]})
        assert not r["ok"]
    AL.link().regenerate()
    assert not ask({"op": "list", "token": info["token"]})["ok"]
    assert ask({"op": "list", "token": AL.read_token_file()["token"]})["ok"]
    AL.link().set_enabled(False)
    assert not os.path.exists(AL.token_path())


@pytest.fixture
def doc_win(app, server, tmp_path):  # noqa: F811
    from leaf.window import MainWindow, _windows

    path = str(tmp_path / "in.pdf")
    make_pdf(path)
    AL.link().set_enabled(True)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    w.open_path(path)
    yield w
    for x in list(_windows):
        if x.canvas.doc is not None:
            x.canvas.undo.setClean()
        x.close()
    pump(app, 0.05)


EDITS = {"leaf_edits": 1, "forms": {"name": "Jane Agent"},
         "annotations": [{"type": "rect", "page": 1, "rect": [100, 300, 200, 360]}]}


def _propose(app, server, w, edits=EDITS, title="Fill the form"):  # noqa: F811
    tok = AL.read_token_file()["token"]
    msg = {"op": "propose", "token": tok, "path": w.canvas.doc.path, "title": title, "edits": edits}
    return in_thread(app, lambda: ipc.request(msg, server.name))


def test_read_open_document(app, server, doc_win):  # noqa: F811
    tok = AL.read_token_file()["token"]
    docs = in_thread(app, lambda: ipc.request({"op": "list", "token": tok}, server.name))["documents"]
    assert len(docs) == 1 and docs[0]["pages"] == 3
    r = in_thread(app, lambda: ipc.request({"op": "read", "token": tok, "doc": docs[0]["doc"], "pages": "1",
                                            "boxes": True, "include": ["text", "fields", "annotations", "outline"]},
                                           server.name))
    assert r["ok"], r
    p = r["document"]["pages"][0]
    assert "PAGE1" in p["text"] and any(wd[4] == "quick" for wd in p["words"])
    assert {f["name"] for f in p["fields"]} >= {"name", "agree", "color"}
    assert r["document"]["outline"] == []


def test_propose_accept_is_one_undo_macro(app, server, doc_win):  # noqa: F811
    w = doc_win
    before = w.canvas.undo.count()
    r = _propose(app, server, w)
    assert r["ok"] and r["status"] == "pending" and r["changes"] == 2
    pump(app, 0.05)
    panel = w.agent_panel
    assert panel.tree.topLevelItemCount() == 1
    assert panel.tree.topLevelItem(0).childCount() == 2
    assert w.agent_dock.isVisible()
    assert w.canvas.undo.count() == before  # nothing applied yet
    panel.accept_current()
    assert w.canvas.undo.count() == before + 1
    assert w.canvas.undo.undoText() == "Agent: Fill the form"
    assert any(v == "Jane Agent" for v in w.canvas.doc.form_values.values())
    assert panel.tree.topLevelItemCount() == 0
    tok = AL.read_token_file()["token"]
    st = in_thread(app, lambda: ipc.request({"op": "status", "token": tok, "proposal": r["proposal"]}, server.name))
    assert st["status"] == "accepted" and st["accepted"] == 2
    w.canvas.undo.undo()
    assert not any(v == "Jane Agent" for v in w.canvas.doc.form_values.values())


def test_partial_accept_and_reject(app, server, doc_win):  # noqa: F811
    from PySide6.QtCore import Qt

    w = doc_win
    before = w.canvas.undo.count()
    _propose(app, server, w)
    pump(app, 0.05)
    top = w.agent_panel.tree.topLevelItem(0)
    top.child(1).setCheckState(0, Qt.CheckState.Unchecked)  # keep the form value, drop the rectangle
    w.agent_panel.accept_current()
    assert w.canvas.undo.count() == before + 1
    assert not any(w.canvas.overlays_for(ref) for ref in w.canvas.doc.pages)
    p = next(iter(AL.link().proposals.values()))
    assert p.status == "partially accepted"
    # reject leaves nothing
    r = _propose(app, server, w, title="Second")
    pump(app, 0.05)
    w.agent_panel.reject_current()
    assert w.canvas.undo.count() == before + 1
    assert AL.link().proposals[r["proposal"]].status == "rejected"
    assert w.agent_panel.tree.topLevelItemCount() == 0


def test_auto_accept_and_invalid_edits(app, server, doc_win):  # noqa: F811
    w = doc_win
    before = w.canvas.undo.count()
    bad = _propose(app, server, w, edits={"leaf_edits": 1, "bogus": []})
    assert not bad["ok"] and bad["problems"]
    w.agent_panel.auto.setChecked(True)
    r = _propose(app, server, w)
    pump(app, 0.05)
    assert AL.link().proposals[r["proposal"]].status == "accepted"
    assert w.canvas.undo.count() == before + 1
    outside = _propose(app, server, w, edits={"leaf_edits": 1, "images": [
        {"page": 1, "index": 0, "file": "relative.png"}]})
    assert not outside["ok"]


# ============================================================== MCP
class Pipe:
    """A blocking in-memory line pipe (stdin of the server)."""

    def __init__(self):
        self.lines: list[bytes] = []
        self.cv = threading.Condition()
        self.closed = False

    def send(self, msg) -> None:
        with self.cv:
            self.lines.append((json.dumps(msg) if not isinstance(msg, (bytes, str)) else msg).encode() + b"\n"
                              if not isinstance(msg, bytes) else msg)
            self.cv.notify()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify()

    def readline(self) -> bytes:
        with self.cv:
            while not self.lines and not self.closed:
                self.cv.wait()
            return self.lines.pop(0) if self.lines else b""


class Out(io.BytesIO):
    def messages(self) -> list[dict]:
        return [json.loads(x) for x in self.getvalue().splitlines() if x.strip()]


def run_mcp(app, msgs, roots=None, live=None):  # noqa: F811
    inp, out = Pipe(), Out()
    for m in msgs:
        inp.send(m)
    inp.close()
    srv = mcp_server.Server(roots, live)
    in_thread(app, lambda: mcp_server.serve(inp, out, srv), timeout=60)
    return out.messages()


def init_msgs(version="2025-06-18"):
    return [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]


def call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def result_of(msgs, i):
    m = next(m for m in msgs if m.get("id") == i)
    return m


def test_mcp_handshake_list_and_call(app, sandbox, tmp_path):  # noqa: F811
    pdf = str(tmp_path / "doc.pdf")
    make_pdf(pdf)
    msgs = run_mcp(app, [*init_msgs(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                         call(4, "pdf_info", {"path": pdf}),
                         call(5, "pdf_text", {"path": pdf, "pages": "2", "boxes": True}),
                         call(6, "pdf_fields", {"path": pdf}),
                         call(7, "pdf_find", {"path": pdf, "query": "lazy dog"}),
                         {"jsonrpc": "2.0", "id": 8, "method": "nope"},
                         b"{not json\n",
                         call(9, "no_such_tool", {})])
    init = result_of(msgs, 1)["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "leaf"
    assert "tools" in init["capabilities"]
    assert len([m for m in msgs if m.get("id") is None and "error" not in m]) == 0  # the notification got no reply
    names = {t["name"] for t in result_of(msgs, 2)["result"]["tools"]}
    assert {"pdf_info", "pdf_text", "pdf_fields", "pdf_find", "pdf_propose_edits", "pdf_apply_edits_to_copy",
            "pdf_ocr_to_copy", "pdf_redact_to_copy", "pdf_export_text"} <= names
    assert result_of(msgs, 3)["result"] == {}
    info = result_of(msgs, 4)["result"]["structuredContent"]
    assert info["pages"] == 3 and info["form_fields"] == 3
    text = result_of(msgs, 5)["result"]["structuredContent"]["pages"]
    assert len(text) == 1 and "PAGE2" in text[0]["text"] and text[0]["words"]
    assert {f["name"] for f in result_of(msgs, 6)["result"]["structuredContent"]["fields"]} == {"name", "agree", "color"}
    assert result_of(msgs, 7)["result"]["structuredContent"]["count"] == 3
    assert result_of(msgs, 8)["error"]["code"] == -32601
    assert any(m.get("error", {}).get("code") == -32700 for m in msgs)
    assert result_of(msgs, 9)["error"]["code"] == -32602


def test_mcp_version_negotiation(app, sandbox):  # noqa: F811
    msgs = run_mcp(app, init_msgs("1999-01-01")[:1])
    assert msgs[0]["result"]["protocolVersion"] == mcp_server.SUPPORTED_VERSIONS[0]


def test_mcp_path_restrictions(app, sandbox, tmp_path):  # noqa: F811
    inside = tmp_path / "in"
    inside.mkdir()
    pdf = str(inside / "doc.pdf")
    make_pdf(pdf)
    outside = str(tmp_path / "other.pdf")
    make_pdf(outside)
    unc = r"\\server\share\x.pdf" if sys.platform == "win32" else "//server/share/x.pdf"
    msgs = run_mcp(app, [*init_msgs(),
                         call(2, "pdf_info", {"path": "doc.pdf"}),
                         call(3, "pdf_info", {"path": unc}),
                         call(4, "pdf_info", {"path": "https://example.com/x.pdf"}),
                         call(5, "pdf_info", {"path": outside}),
                         call(6, "pdf_info", {"path": pdf}),
                         call(7, "pdf_export_text", {"path": pdf, "output": pdf}),
                         call(8, "pdf_export_text", {"path": pdf})],
                   roots=[str(inside)])
    for i in (2, 3, 4, 5, 7):
        assert result_of(msgs, i)["result"]["isError"], i
    assert "absolute" in result_of(msgs, 2)["result"]["content"][0]["text"]
    assert not result_of(msgs, 6)["result"].get("isError")
    out = result_of(msgs, 8)["result"]["structuredContent"]["output"]
    assert out.endswith("doc-text.txt") and os.path.isfile(out) and "PAGE1" in open(out, encoding="utf-8").read()


def test_mcp_redact_requires_confirm(app, sandbox, tmp_path):  # noqa: F811
    pdf = str(tmp_path / "doc.pdf")
    make_pdf(pdf, form=False)
    before = sorted(os.listdir(tmp_path))
    msgs = run_mcp(app, [*init_msgs(), call(2, "pdf_redact_to_copy", {"path": pdf, "query": "quick brown"}),
                         call(3, "pdf_redact_to_copy", {"path": pdf, "query": "quick brown", "confirm": True})])
    dry = result_of(msgs, 2)["result"]["structuredContent"]
    assert dry["confirmed"] is False and dry["count"] == 3 and "output" not in dry
    done = result_of(msgs, 3)["result"]["structuredContent"]
    out = done["output"]
    assert sorted(os.listdir(tmp_path)) == sorted(before + [os.path.basename(out)])
    d = pymupdf.open(out)
    assert all("quick" not in p.get_text() and "lazy dog" in p.get_text() for p in d)
    d.close()
    src = pymupdf.open(pdf)
    assert "quick" in src[0].get_text()  # the source is untouched
    src.close()


def test_mcp_apply_to_copy_and_propose_fallback(app, sandbox, tmp_path):  # noqa: F811
    pdf = str(tmp_path / "doc.pdf")
    make_pdf(pdf)
    msgs = run_mcp(app, [*init_msgs(), call(2, "pdf_apply_edits_to_copy", {"path": pdf, "edits": EDITS}),
                         call(3, "pdf_propose_edits", {"path": pdf, "edits": EDITS, "title": "T"}),
                         call(4, "pdf_apply_edits_to_copy", {"path": pdf, "edits": {"leaf_edits": 1, "x": 1}})])
    out = result_of(msgs, 2)["result"]["structuredContent"]["output"]
    d = pymupdf.open(out)
    assert {w.field_name: w.field_value for w in d[0].widgets()}["name"] == "Jane Agent"
    assert any(a.type[1] == "Square" for a in d[0].annots())
    d.close()
    prop = result_of(msgs, 3)["result"]["structuredContent"]
    assert prop["delivered_to"] == "file" and os.path.isfile(prop["proposal_file"])
    with open(prop["proposal_file"], encoding="utf-8") as f:
        from leaf import editset

        assert editset.validate(json.load(f)) == []
    assert result_of(msgs, 4)["result"]["isError"]


def test_mcp_copies_never_overwrite_a_file_that_appears_meanwhile(app, sandbox, tmp_path, monkeypatch):  # noqa: F811
    pdf = str(tmp_path / "doc.pdf")
    make_pdf(pdf, form=False)
    real_new_out = mcp_server.Paths.new_out

    def racing_new_out(self, src, output, suffix, ext=".pdf"):
        out = real_new_out(self, src, output, suffix, ext)
        with open(out, "wb") as f:  # another process (or a planted link) takes the name after the check
            f.write(b"precious")
        return out

    monkeypatch.setattr(mcp_server.Paths, "new_out", racing_new_out)
    msgs = run_mcp(app, [*init_msgs(), call(2, "pdf_export_text", {"path": pdf}),
                         call(3, "pdf_redact_to_copy", {"path": pdf, "query": "quick", "confirm": True}),
                         call(4, "pdf_apply_edits_to_copy", {"path": pdf, "edits": EDITS})])
    for i, name in ((2, "doc-text.txt"), (3, "doc-redacted.pdf"), (4, "doc-edited.pdf")):
        r = result_of(msgs, i)["result"]
        assert r.get("isError") and "already exists" in r["content"][0]["text"], (i, r)
        assert (tmp_path / name).read_bytes() == b"precious"
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".leaf-")]  # no temp files left behind


def test_mcp_propose_reaches_running_leaf(app, server, doc_win):  # noqa: F811
    w = doc_win
    msgs = run_mcp(app, [*init_msgs(), call(2, "pdf_propose_edits", {"path": w.canvas.doc.path, "edits": EDITS,
                                                                      "title": "From MCP"})],
                   )
    r = result_of(msgs, 2)["result"]["structuredContent"]
    assert r["delivered_to"] == "leaf" and r["status"] == "pending"
    pump(app, 0.05)
    assert w.agent_panel.tree.topLevelItemCount() == 1
    assert "pytest" in AL.link().proposals[r["proposal"]].session



def test_entry_point_helpers():
    from leaf import __main__ as M

    assert M.APP_ID.count(".") >= 2
    assert M._in_msix_package() is False  # tests never run packaged
