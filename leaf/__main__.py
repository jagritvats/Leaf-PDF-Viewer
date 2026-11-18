"""Entry point: `leaf [file.pdf ...]`, `leaf --merge a.pdf b.pdf ...` (one new document made of the files)."""
from __future__ import annotations

import sys

# AppStream / desktop-file id (Linux: matches the .desktop file and the Flatpak id); kept in sync by
# packaging/set_app_id.py.
APP_ID = "io.github.jagritvats.Leaf"


def _in_msix_package() -> bool:
    """Windows: running from an MSIX package (Windows assigns the app identity then)."""
    try:
        import ctypes

        n = ctypes.c_uint32(0)
        # ERROR_INSUFFICIENT_BUFFER (122) with a zero-length buffer = we have a package; APPMODEL_ERROR_NO_PACKAGE = not
        return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None) in (0, 122)
    except Exception:  # noqa: BLE001 - Windows 7 or no kernel32 export: not packaged
        return False


def _set_app_id() -> None:
    """Windows: own taskbar identity (groups windows, shows our icon instead of python's)."""
    if sys.platform != "win32" or _in_msix_package():
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Leaf.PdfEditor")
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def _cli_commands() -> tuple:
    from .cli import COMMANDS  # (leaf/cli.py imports only the standard library at module level)

    return COMMANDS


def main() -> int:
    import multiprocessing

    # in a packaged build the render worker is this same executable: let it become the worker and stop here
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] in ("cli",) + _cli_commands():  # `leaf <command> ...`: no window
        from .cli import main as cli_main

        return cli_main(sys.argv[2:] if sys.argv[1] == "cli" else sys.argv[1:])
    if "--self-test" in sys.argv:
        from .selftest import run

        return run()
    if sys.argv[1:2] == ["mcp"]:  # MCP server on stdio for AI agents (leaf/mcp_server.py): no GUI, never forwarded
        from .mcp_server import main as mcp_main

        return mcp_main(sys.argv[2:])
    import os

    files = [a for a in sys.argv[1:] if not a.startswith("-")]
    merge = "--merge" in sys.argv
    lock = None
    if "--new-instance" not in sys.argv and not os.environ.get("LEAF_NO_SINGLE_INSTANCE"):
        from .ipc import StartupLock, forward_open

        # launches that start together (Explorer: one per selected file) queue here until the first one listens
        lock = StartupLock()
        if not lock.acquire():
            lock = None
        # single instance: a running Leaf takes the files (and comes to the front); this process just exits
        if forward_open(files, new_window="--new-window" in sys.argv, merge=merge):
            if lock is not None:
                lock.release()
            return 0

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setOrganizationName("Leaf")
    QCoreApplication.setApplicationName("Leaf")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _set_app_id()
    app = QApplication(sys.argv)
    app.setApplicationDisplayName("Leaf")
    app.setDesktopFileName(APP_ID)

    from .icons import app_icon
    from .window import MainWindow

    app.setWindowIcon(app_icon())

    win = MainWindow()
    win.show()
    from . import ipc

    ipc.start_server()  # later launches hand their files to this process; agents connect here (leaf/agentlink.py)
    if lock is not None:
        lock.release()
    if merge:
        ipc.queue_merge(files)  # files of launches that were waiting on the lock join the same document
    else:
        for f in files:
            win.open_path(f)
    opener = _FileOpenFilter(win)  # macOS: Finder "Open With" / dropping on the Dock icon
    app.installEventFilter(opener)
    code = app.exec()
    ipc.stop_server()
    # free clipboard pages/items and other Qt-backed garbage while Qt still exists (avoids crashes at exit)
    import gc

    from . import pageops

    pageops._clip.clear()
    gc.collect()
    return code


def _file_open_filter_class():
    from PySide6.QtCore import QEvent, QObject

    class FileOpenFilter(QObject):
        def __init__(self, win):
            super().__init__()
            self.win = win

        def eventFilter(self, obj, e) -> bool:
            if e.type() == QEvent.Type.FileOpen and e.file():
                from .window import _windows

                target = next((w for w in reversed(_windows) if w.isVisible()), self.win)
                target.open_path(e.file())
                return True
            return False

    return FileOpenFilter


def _FileOpenFilter(win):  # noqa: N802 - builds the QObject subclass lazily (keeps startup imports minimal)
    return _file_open_filter_class()(win)


if __name__ == "__main__":
    sys.exit(main())
