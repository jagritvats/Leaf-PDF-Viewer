"""Crash recovery. While a document has unsaved changes, a recovery copy is written every minute and shortly after
edits stop (skipped when nothing changed since the last one). It is deleted on a clean save or close. After a crash
the next start offers to reopen it as an untitled document."""
from __future__ import annotations

import glob
import hashlib
import inspect
import json
import os
import sys
import time

from PySide6.QtCore import QCoreApplication, QDateTime, QEvent, QLockFile, QObject, QStandardPaths, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

OVERRIDE_DIR: str | None = None  # tests point this at a temp dir
INTERVAL_MS = 60_000
IDLE_MS = 4_000
BUSY_SHARE = 0.01  # a slow save (full rewrite of a big file) may use at most ~1% of the time: spaced out accordingly
_offered = False


def recovery_dir() -> str | None:
    """None disables recovery (e.g. when embedded/tested without the app's organization name set)."""
    if OVERRIDE_DIR:
        d = OVERRIDE_DIR
    elif QCoreApplication.organizationName():
        d = os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), "recovery")
    else:
        return None
    os.makedirs(d, exist_ok=True)
    return d


def _lock(path: str) -> QLockFile:
    lk = QLockFile(path)
    lk.setStaleLockTime(0)  # stale only when the owning process is gone, never by age
    return lk


class Recovery(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.canvas = win.canvas
        self.gen = 0  # bumped on every undo-stack change
        self.saved_gen = 0
        self.key: str | None = None
        self.lock: QLockFile | None = None
        self.last_cost = 0.0
        self.last_time = 0.0
        self._closing = False
        self._untitled = f"untitled-{os.getpid()}-{id(win)}"
        self.periodic = QTimer(self, interval=INTERVAL_MS, timeout=self._tick)
        self.idle = QTimer(self, singleShot=True, interval=IDLE_MS, timeout=self._tick)
        st = self.canvas.undo
        st.indexChanged.connect(self._changed)
        st.cleanChanged.connect(lambda clean: clean and self.discard())
        win.installEventFilter(self)
        self.enabled = True
        settings = getattr(win, "settings", None)
        minutes = settings.value("recovery/interval", None) if settings is not None else None  # Settings › Saving
        self.set_interval(1 if minutes is None else minutes)

    def set_interval(self, minutes) -> None:
        """Minutes between recovery copies; 0 turns recovery off (and drops this window's copy)."""
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            minutes = 1.0
        was = getattr(self, "enabled", True)
        self.enabled = minutes > 0
        if self.enabled and not was:
            self._changed()  # unsaved changes made while it was off get a copy soon
        if self.enabled:
            self.periodic.setInterval(int(minutes * INTERVAL_MS))
            self.periodic.start()
        else:
            self.periodic.stop()
            self.idle.stop()
            self.discard()

    def _changed(self, _=None) -> None:
        self.gen += 1
        if self.enabled:
            self.idle.start()

    def _tick(self) -> None:
        if time.monotonic() - self.last_time >= self.last_cost / BUSY_SHARE:
            self.autosave()

    def key_for(self, doc) -> str:
        src = os.path.normcase(os.path.abspath(doc.path)) if doc.path else self._untitled
        return hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]

    def paths(self, key: str) -> tuple[str, str, str]:
        d = recovery_dir() or ""
        return tuple(os.path.join(d, key + ext) for ext in (".pdf", ".json", ".lock"))  # type: ignore[return-value]

    def needed(self) -> bool:
        doc = self.canvas.doc
        return self.enabled and doc is not None and not self.canvas.undo.isClean() and self.gen != self.saved_gen and recovery_dir() is not None

    def autosave(self) -> bool:
        """Write the recovery copy if anything changed since the last one. Returns True if written."""
        if not self.needed():
            return False
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton or self.canvas._editing_text():
            self.idle.start()  # don't stall a drag or typing; try again once idle
            return False
        doc = self.canvas.doc
        key = self.key_for(doc)
        if self.key and key != self.key:
            self.discard()
        pdf, meta, lock = self.paths(key)
        if self.lock is None:
            self.lock = _lock(lock)
            self.lock.tryLock(0)
        gen = self.gen
        t0 = time.perf_counter()
        try:
            fast = {"fast": True} if "fast" in inspect.signature(doc.save).parameters else {}
            doc.save(pdf, self.canvas.overlays_for, **fast)
            with open(meta + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"path": doc.path, "name": os.path.basename(doc.path or "Untitled"), "time": time.time(),
                           "pages": len(doc.pages), "pid": os.getpid()}, f)
            os.replace(meta + ".tmp", meta)
        except Exception as ex:  # noqa: BLE001 - recovery must never disturb editing
            print(f"Leaf: recovery save failed: {ex}", file=sys.stderr)
            return False
        self.last_cost = time.perf_counter() - t0
        self.last_time = time.monotonic()
        self.key, self.saved_gen = key, gen
        return True

    def discard(self) -> None:
        self.saved_gen = self.gen
        self.idle.stop()
        if self.key is None:
            return
        pdf, meta, _ = self.paths(self.key)
        for p in (pdf, meta):
            try:
                os.remove(p)
            except OSError:
                pass
        if self.lock is not None:
            self.lock.unlock()
            self.lock = None
        self.key = None

    def eventFilter(self, obj, e) -> bool:
        if obj is self.win:
            t = e.type()
            if t == QEvent.Type.Close:
                self._closing = True
                QTimer.singleShot(0, self._close_done)
            elif t == QEvent.Type.Hide and self._closing:  # close accepted (saved or discarded)
                self.discard()
                self.periodic.stop()
        return False

    def _close_done(self) -> None:
        self._closing = False


# ------------------------------------------------------------------- restore
def orphans() -> list[dict]:
    """Recovery copies whose owning process is gone. Their locks are taken so no other instance offers them too."""
    d = recovery_dir()
    if d is None:
        return []
    out = []
    for meta in sorted(glob.glob(os.path.join(d, "*.json"))):
        key = os.path.splitext(os.path.basename(meta))[0]
        pdf, lock = os.path.join(d, key + ".pdf"), os.path.join(d, key + ".lock")
        if not os.path.exists(pdf):
            continue
        lk = _lock(lock)
        if not lk.tryLock(0):
            continue  # another running Leaf owns it
        try:
            with open(meta, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        info.update(key=key, pdf=pdf, meta=meta, lock=lk)
        out.append(info)
    return out


def drop(info: dict) -> None:
    for p in (info["pdf"], info["meta"]):
        try:
            os.remove(p)
        except OSError:
            pass
    info["lock"].unlock()


def restore(win, info: dict):
    """Open a recovery copy as an unsaved document (Save goes to Save As). Returns the window showing it."""
    from .window import _windows

    pdf = info["pdf"]
    win.open_path(pdf)
    target = next((w for w in _windows if w.canvas.doc and w.canvas.doc.path == os.path.abspath(pdf)), None)
    if target is None:
        return None
    doc = target.canvas.doc
    orig = info.get("path")
    base = os.path.splitext(orig)[0] if orig else os.path.join(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation), "Recovered")
    doc.path = base + (" (recovered).pdf" if orig else ".pdf")
    doc.converted = True  # Save asks for a file name
    rec = [p for p in (target.settings.value("recent") or []) if p != os.path.abspath(pdf)]
    target.settings.setValue("recent", rec)
    target.canvas.undo.resetClean()  # unsaved until Saved As
    rec = getattr(target, "recovery", None)
    if rec is not None:  # re-protect it under its new name before dropping the old copy
        rec._changed()
        rec.autosave()
    drop(info)
    target._update_title()
    return target


def offer(win) -> None:
    """Once per process: ask whether to restore what a crashed session left behind (non-blocking)."""
    global _offered
    if _offered:
        return
    _offered = True
    found = orphans()
    if not found:
        return
    lines = []
    for info in found:
        when = QDateTime.fromSecsSinceEpoch(int(info.get("time", 0))).toString("d MMM, HH:mm")
        pages = info.get("pages")
        lines.append(f"• {info.get('name', 'Untitled')}, {when}" + (f" ({pages} pages)" if pages else ""))
    box = QMessageBox(QMessageBox.Icon.Question, "Restore unsaved changes",
                      "Leaf didn't close properly last time. Restore unsaved changes from:\n\n" + "\n".join(lines), parent=win)
    restore_btn = box.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(restore_btn)

    def done(_=None) -> None:
        clicked = box.clickedButton()
        for info in found:
            if clicked is restore_btn:
                restore(win, info)
            elif clicked is discard_btn:
                drop(info)
            else:
                info["lock"].unlock()  # keep the files; offer again next start
        box.deleteLater()

    box.finished.connect(done)
    box.open()
