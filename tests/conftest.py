"""Session teardown: free Qt/MuPDF-backed objects while the QApplication still exists.

Detached QGraphicsItems (e.g. in the page clipboard) or MuPDF documents freed during interpreter shutdown, after
Qt is gone, can crash the process at exit even though every test passed.

Session start: every QSettings() and every per-user Qt data folder (AppDataLocation & co: recovery copies, signature
library, identities, tessdata downloads) points into a private temp folder for this pytest process. Otherwise all test
runs share the native store (registry `LeafTest/...`) and the user's AppData, so concurrent runs wipe each other's
settings (trust list, recent files, ...) mid-test with `QSettings().clear()` and tests read the user's real data."""
import gc
import os
import shutil
import tempfile

def _session_dir() -> tuple[str, bool]:
    """(folder, new): one per pytest process, also when this module is imported a second time as `tests.conftest`."""
    pid, _, d = os.environ.get("LEAF_TEST_SESSION", "").partition("|")
    if pid == str(os.getpid()) and d:
        return d, False
    d = tempfile.mkdtemp(prefix="leaf-tests-")
    os.environ["LEAF_TEST_SESSION"] = f"{os.getpid()}|{d}"
    return d, True


_SESSION_DIR, _NEW_SESSION = _session_dir()


def _isolate_qt_storage() -> None:
    from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, os.path.join(_SESSION_DIR, "settings", scope.name))
    QStandardPaths.setTestModeEnabled(True)
    orig = QStandardPaths.writableLocation
    SL = QStandardPaths.StandardLocation
    private = {SL.AppDataLocation: "appdata", SL.AppLocalDataLocation: "applocaldata", SL.CacheLocation: "cache",
               SL.AppConfigLocation: "appconfig", SL.GenericDataLocation: "genericdata",
               SL.GenericCacheLocation: "genericcache", SL.GenericConfigLocation: "genericconfig"}

    def writable_location(loc):
        sub = private.get(loc)
        if sub is None:
            return orig(loc)
        parts = [_SESSION_DIR, sub]
        if sub.startswith("app") or sub == "cache":
            parts += [QCoreApplication.organizationName() or "_", QCoreApplication.applicationName() or "_"]
        return os.path.join(*parts)

    QStandardPaths.writableLocation = staticmethod(writable_location)


if _NEW_SESSION:
    _isolate_qt_storage()
os.environ.setdefault("LEAF_AGENT_DIR", os.path.join(_SESSION_DIR, "agent"))  # agentlink's token file (Qt-free path)


def pytest_sessionfinish(session, exitstatus):
    try:
        from leaf import pageops

        pageops._clip.clear()
    except Exception:  # noqa: BLE001
        pass
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is not None:
            QGuiApplication.clipboard().clear()  # mime data owned by the clipboard outliving the app crashes at exit
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)
