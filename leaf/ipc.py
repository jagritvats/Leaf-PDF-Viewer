"""Single instance + the local endpoint agents talk to.

One per-user local server (QLocalServer: a named pipe on Windows, a Unix socket in the runtime dir elsewhere,
restricted to this user). Messages are newline-delimited JSON objects; each request gets one response line.

- `{"op": "ping"}` -> `{"ok": true, "pid": ..., "app": "leaf"}`
- `{"open": ["/abs/a.pdf", ...]}` -> opens each file in the running Leaf (or raises it when the list is empty);
  with `"merge": true` (`leaf --merge`, Explorer's "Combine with Leaf") the files are gathered for a moment and
  opened together as one new untitled document (Explorer starts one process per selected file)
- everything else is handled by leaf.agentlink (token required).

The client half (`request`, `forward_open`) is plain Python (no Qt), so `leaf file.pdf` can hand its files to a
running Leaf and exit in a few milliseconds, and `leaf mcp` can talk to it without starting Qt.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time

MAX_LINE = 64 * 1024 * 1024  # a request/response larger than this is refused (and the connection dropped)
CONNECT_TIMEOUT = 2.0  # seconds; a server that doesn't answer in time counts as gone (normal start)


# --------------------------------------------------------------------------- naming
def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (e.g. a container): fall back to the uid / profile
        return str(getattr(os, "getuid", lambda: os.path.expanduser("~"))())


def server_name() -> str:
    """Per-user server name: a pipe name on Windows, a socket path in the runtime dir elsewhere.

    LEAF_IPC_NAME overrides it (tests, a second isolated Leaf)."""
    env = os.environ.get("LEAF_IPC_NAME")
    if env:
        return env
    tag = hashlib.sha256(f"leaf\0{_user()}".encode("utf-8", "replace")).hexdigest()[:16]
    if sys.platform == "win32":
        return f"leaf-{tag}"
    run = os.environ.get("XDG_RUNTIME_DIR")
    if not run or not os.path.isdir(run):
        run = tempfile.gettempdir()
    return os.path.join(run, f"leaf-{tag}.sock")


def _pipe_path(name: str) -> str:
    return name if name.startswith("\\\\") else "\\\\.\\pipe\\" + name


# --------------------------------------------------------------------------- client (no Qt)
class IpcError(OSError):
    pass


class Client:
    """A blocking connection to a running Leaf. Raises IpcError when there's none (or it doesn't answer)."""

    def __init__(self, name: str | None = None, timeout: float = CONNECT_TIMEOUT):
        self.name = name or server_name()
        self.timeout = timeout
        self._sock = None
        self._pipe = None
        self._buf = b""
        self._connect()

    def _connect(self) -> None:
        if sys.platform == "win32":
            path = _pipe_path(self.name)
            end = time.monotonic() + self.timeout
            while True:
                try:
                    self._pipe = open(path, "r+b", buffering=0)  # noqa: SIM115 - kept for the session
                    return
                except FileNotFoundError as ex:
                    raise IpcError("Leaf isn't running") from ex
                except OSError as ex:  # ERROR_PIPE_BUSY: every instance is taken right now; Qt adds one shortly
                    if time.monotonic() > end:
                        raise IpcError(f"Leaf didn't answer: {ex}") from ex
                    time.sleep(0.01)
        if not hasattr(socket, "AF_UNIX"):
            raise IpcError("no local sockets on this platform")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.name)
        except OSError as ex:
            s.close()
            raise IpcError("Leaf isn't running") from ex
        self._sock = s

    def _io(self, fn):
        """Run one blocking pipe exchange with a timeout (Windows pipes opened with open() have none)."""
        if self._sock is not None:
            try:
                return fn()
            except OSError as ex:
                raise IpcError(f"Leaf didn't answer: {ex}") from ex
        box: list = []

        def run():
            try:
                box.append((True, fn()))
            except BaseException as ex:  # noqa: BLE001 - re-raised in the caller
                box.append((False, ex))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(self.timeout)
        if not box:
            self._pipe = None  # a stuck read can't be cancelled portably; drop the handle with the thread
            raise IpcError("Leaf didn't answer in time")
        ok, v = box[0]
        if not ok:
            raise IpcError(f"Leaf connection failed: {v}") from v
        return v

    def _send(self, data: bytes) -> None:
        if self._sock is not None:
            self._sock.sendall(data)
        elif self._pipe is not None:
            self._pipe.write(data)
        else:
            raise IpcError("not connected")

    def _readline(self) -> bytes:
        while b"\n" not in self._buf:
            if self._sock is not None:
                chunk = self._sock.recv(65536)
            elif self._pipe is not None:
                chunk = self._pipe.read(65536)
            else:
                raise IpcError("not connected")
            if not chunk:
                raise IpcError("Leaf closed the connection")
            self._buf += chunk
            if len(self._buf) > MAX_LINE:
                raise IpcError("response too large")
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def request(self, msg: dict) -> dict:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        def go():
            self._send(data)
            return self._readline()

        line = self._io(go)
        try:
            out = json.loads(line.decode("utf-8"))
        except ValueError as ex:
            raise IpcError("bad response from Leaf") from ex
        if not isinstance(out, dict):
            raise IpcError("bad response from Leaf")
        return out

    def close(self) -> None:
        for h in (self._sock, self._pipe):
            if h is not None:
                try:
                    h.close()
                except OSError:
                    pass
        self._sock = self._pipe = None

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def request(msg: dict, name: str | None = None, timeout: float = CONNECT_TIMEOUT) -> dict:
    """One request on a fresh connection. Raises IpcError if Leaf isn't running or doesn't answer."""
    with Client(name, timeout) as c:
        return c.request(msg)


def is_listening(name: str | None = None) -> bool:
    """Whether a server accepts connections on `name` (connect only: works even when that server is busy, or
    is this very thread). A leftover socket file of a crashed Leaf refuses the connection."""
    try:
        Client(name, timeout=0.5).close()
        return True
    except IpcError:
        return False


def _allow_foreground(pid) -> None:
    """Windows: let the running Leaf take the foreground (we got it from the user's launch; it didn't)."""
    if sys.platform != "win32" or not isinstance(pid, int):
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(pid)
    except Exception:  # noqa: BLE001 - activation falls back to flashing the taskbar button
        pass


def forward_open(paths: list[str], name: str | None = None, timeout: float = CONNECT_TIMEOUT,
                 new_window: bool = False, merge: bool = False) -> bool:
    """Hand `paths` (made absolute here: the running Leaf has another working directory) to a running Leaf;
    `new_window` (`leaf --new-window`) also opens an empty window there; `merge` (`leaf --merge`) adds them to
    the document being combined.

    True when it took them (this process should exit), False when there's no Leaf to talk to (start normally)."""
    absolute = [os.path.abspath(p) for p in paths]
    try:
        with Client(name, timeout) as c:
            hello = c.request({"op": "ping"})
            if not hello.get("ok") or hello.get("app") != "leaf":
                return False
            _allow_foreground(hello.get("pid"))
            msg = {"open": absolute}
            if new_window:
                msg["new_window"] = True
            if merge:
                msg["merge"] = True
            r = c.request(msg)
            return bool(r.get("ok"))
    except IpcError:
        return False


# --------------------------------------------------------------------------- server (Qt)
_server = None


def current_server():
    """The running IpcServer of this process, or None."""
    return _server


def _server_class():
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    class IpcServer(QObject):
        """The per-user local server. `handlers[op](server, conn, request) -> dict` answers a request;
        `conn` is the QLocalSocket (agentlink keeps per-connection sessions on it)."""

        requestHandled = Signal(str)  # op, after its response was written (tests, logging)

        def __init__(self, name: str | None = None, parent=None):
            super().__init__(parent)
            self.name = name or server_name()
            self.server = QLocalServer(self)
            self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            self.server.newConnection.connect(self._accept)
            self.handlers: dict = {"ping": _ping, "open": _open}
            self.on_open = None  # callable(paths) override (tests); default opens windows
            self.on_merge = None  # callable(paths) override (tests); default: queue_merge
            self.conns: list = []

        def listen(self) -> bool:
            """Start listening. A live Leaf on the name is left alone (returns False); a leftover socket from a
            crashed one is removed. (Asked first: Windows would let a second server share the pipe name.)"""
            if is_listening(self.name):
                return False
            if self.server.listen(self.name):
                return True
            QLocalServer.removeServer(self.name)  # stale socket file (POSIX); a no-op for pipes
            return self.server.listen(self.name)

        def close(self) -> None:
            for c in list(self.conns):
                c.abort()
            self.conns.clear()
            self.server.close()

        def _accept(self) -> None:
            while self.server.hasPendingConnections():
                c = self.server.nextPendingConnection()
                c.setProperty("leaf_buf", b"")
                self.conns.append(c)
                c.readyRead.connect(lambda c=c: self._read(c))
                c.disconnected.connect(lambda c=c: self._gone(c))

        def _gone(self, c) -> None:
            if c in self.conns:
                self.conns.remove(c)
                closed = getattr(self, "on_disconnect", None)
                if closed:
                    closed(c)
            c.deleteLater()

        def _read(self, c) -> None:
            buf = bytes(c.property("leaf_buf") or b"") + bytes(c.readAll().data())
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if line.strip():
                    self._handle(c, line)
                if c.state() != QLocalSocket.LocalSocketState.ConnectedState:
                    return
            if len(buf) > MAX_LINE:
                c.abort()
                return
            c.setProperty("leaf_buf", buf)

        def _handle(self, c, line: bytes) -> None:
            op, req = "", None
            try:
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("not an object")
                op = "open" if "open" in req and "op" not in req else str(req.get("op") or "")
                fn = self.handlers.get(op)
                if fn is None:
                    resp = {"ok": False, "error": f"unknown request {op!r}"}
                else:
                    resp = fn(self, c, req)
            except ValueError as ex:
                resp = {"ok": False, "error": f"bad request: {ex}"}
            except Exception as ex:  # noqa: BLE001 - a handler bug must not kill the server
                resp = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            if isinstance(req, dict) and "id" in req:
                resp.setdefault("id", req["id"])
            c.write(json.dumps(resp, separators=(",", ":"), default=str).encode("utf-8") + b"\n")
            c.flush()
            self.requestHandled.emit(op)

    def _ping(server, conn, req) -> dict:
        return {"ok": True, "app": "leaf", "pid": os.getpid()}

    def _open(server, conn, req) -> dict:
        paths = req.get("open")
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return {"ok": False, "error": "open: expected a list of paths"}
        paths = [os.path.abspath(p) for p in paths]
        # after the response: opening may show a dialog (password, error) with its own event loop
        if req.get("merge"):
            QTimer.singleShot(0, lambda: (server.on_merge or queue_merge)(paths))
        elif req.get("new_window") and server.on_open is None:
            QTimer.singleShot(0, lambda: open_in_running(paths, new_window=True))
        else:
            QTimer.singleShot(0, lambda: (server.on_open or open_in_running)(paths))
        return {"ok": True}

    return IpcServer


_IpcServer = None


def IpcServer(name: str | None = None, parent=None):  # noqa: N802 - class built lazily (Qt only when serving)
    global _IpcServer
    if _IpcServer is None:
        _IpcServer = _server_class()
    return _IpcServer(name, parent)


def start_server(name: str | None = None):
    """Listen for other Leaf launches and agents (this process becomes the running instance). None if another
    Leaf already listens or the name can't be used."""
    global _server
    try:
        srv = IpcServer(name)
    except ImportError as ex:  # a build without QtNetwork: no single instance, no agent link
        print(f"leaf: local server unavailable: {ex}", file=sys.stderr)
        return None
    if not srv.listen():
        srv.deleteLater()
        return None
    _server = srv
    try:
        from . import agentlink

        agentlink.install_handlers(srv)
    except Exception as ex:  # noqa: BLE001 - single instance still works without the agent link
        print(f"leaf: agent link unavailable: {ex}", file=sys.stderr)
    return srv


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.close()
        _server = None


# --------------------------------------------------------------------------- opening + activation
def activate(win) -> None:
    """Bring `win` to the front. Windows only lets a background process do that when the launching process
    allowed it (forward_open does); otherwise the taskbar button flashes."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    if win.windowState() & Qt.WindowState.WindowMinimized:
        win.setWindowState((win.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
    win.show()
    win.raise_()
    win.activateWindow()
    handle = win.windowHandle()
    if handle is not None:
        handle.requestActivate()
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.SetForegroundWindow(int(win.winId()))
        except Exception:  # noqa: BLE001
            pass
    if not win.isActiveWindow():
        QApplication.alert(win, 0)


def open_in_running(paths: list[str], new_window: bool = False):
    """Open files sent by another launch: each in its existing window if already open, else in a new window
    (the window.open_path rules). No files: raise the most recent window (`new_window`: open an empty one).
    Returns the last window shown."""
    from .window import MainWindow, _windows

    if new_window:
        fresh = MainWindow()
        fresh.show()
        for p in paths:
            fresh.open_path(os.path.abspath(p))
        activate(fresh)
        return fresh
    live = [w for w in _windows if w.isVisible()]
    target = live[-1] if live else None
    active = next((w for w in live if w.isActiveWindow()), None)
    if active is not None:
        target = active
    if target is None:
        target = MainWindow()
        target.show()
    shown = target
    for p in paths:
        p = os.path.abspath(p)
        target.open_path(p)
        shown = next((w for w in _windows if w.canvas.doc is not None and w.canvas.doc.path == p), shown)
    activate(shown)
    return shown


# --------------------------------------------------------------------------- combine (leaf --merge)
MERGE_SETTLE_MS = 800  # Explorer's per-file launches arrive within this of each other
_merge_batches: list[list[str]] = []
_merge_timer = None


def queue_merge(paths: list[str]) -> None:
    """Add one launch's files to the document being combined; it opens MERGE_SETTLE_MS after the last one."""
    global _merge_timer
    from PySide6.QtCore import QTimer

    _merge_batches.append([os.path.abspath(p) for p in paths])
    if _merge_timer is None:
        _merge_timer = QTimer()
        _merge_timer.setSingleShot(True)
        _merge_timer.timeout.connect(flush_merge)
    _merge_timer.start(MERGE_SETTLE_MS)


def flush_merge():
    """Open what queue_merge gathered (now). Returns the window showing it, or None."""
    from .shellint import merge_order

    if _merge_timer is not None:
        _merge_timer.stop()
    batches = list(_merge_batches)
    _merge_batches.clear()
    files = merge_order(batches)
    if not files:
        return None
    return open_merged_in_running(files)


def open_merged_in_running(paths: list[str]):
    """Open `paths` combined: in an empty window if there is one, else in a new window."""
    from .window import MainWindow, _windows

    live = [w for w in _windows if w.isVisible()]
    target = next((w for w in reversed(live) if w.canvas.doc is None), None)
    if target is None:
        target = MainWindow()
        target.show()
    shown = target.open_merged(paths) or target
    activate(shown)
    return shown


# --------------------------------------------------------------------------- startup lock
class StartupLock:
    """Serializes Leaf launches until the first one listens (Explorer starts one process per selected file, all
    at once: without this each would find no running Leaf and open its own window).

    Held from before `forward_open` until `start_server` returns; others wait (at most `timeout`) and then
    forward to it. A crashed holder releases it (Windows: abandoned mutex; POSIX: the lock dies with the fd)."""

    def __init__(self, name: str | None = None):
        tag = hashlib.sha256((name or server_name()).encode("utf-8", "replace")).hexdigest()[:16]
        self._name = f"leaf-start-{tag}"
        self._handle = None
        self._fd = None

    def acquire(self, timeout: float = 10.0) -> bool:
        try:
            if sys.platform == "win32":
                return self._acquire_win(timeout)
            return self._acquire_posix(timeout)
        except Exception:  # noqa: BLE001 - never block a launch over the lock
            return False

    def _acquire_win(self, timeout: float) -> bool:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        h = k32.CreateMutexW(None, 0, "Local\\" + self._name)
        if not h:
            return False
        r = k32.WaitForSingleObject(h, int(timeout * 1000))
        if r in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED (a holder crashed: ours now)
            self._handle = h
            return True
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle(h)
        return False

    def _acquire_posix(self, timeout: float) -> bool:
        import fcntl

        run = os.environ.get("XDG_RUNTIME_DIR")
        if not run or not os.path.isdir(run):
            run = tempfile.gettempdir()
        fd = os.open(os.path.join(run, self._name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                if time.monotonic() > end:
                    os.close(fd)
                    return False
                time.sleep(0.02)

    def release(self) -> None:
        if self._handle is not None:
            try:
                import ctypes

                k32 = ctypes.windll.kernel32
                k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
                k32.ReleaseMutex(self._handle)
                k32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None
        if self._fd is not None:
            try:
                os.close(self._fd)  # drops the flock
            except OSError:
                pass
            self._fd = None
