"""OS integration, end to end: Combine (open_merged, `leaf --merge` through the running instance), File › Share
(saved file / unsaved changes / never saved / fallbacks) and Make Leaf the default PDF app. System calls (share
sheet, file manager, mail, registry, xdg-mime) are replaced by recorders."""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

import pymupdf
import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMessageBox, QPushButton

from leaf import ipc
from leaf import shellint as S
from tests.e2e_kit import accept_with, driver, open_in, pump, sandbox, wait_until, windows  # noqa: F401
from tests.test_editor import app, make_pdf as _make_pdf  # noqa: F401


def make_pdf(path: str, **kw) -> str:
    _make_pdf(path, **kw)
    return path


def small_pdf(path: str, n: int, tag: str) -> str:
    d = pymupdf.open()
    for i in range(n):
        d.new_page(width=400, height=500).insert_text((50, 80), f"{tag}{i + 1}", fontsize=14)
    d.save(path)
    d.close()
    return path


def texts(doc) -> list[str]:
    return [doc.page(r).get_text().split()[0] for r in doc.pages]


# ====================================================================== combine
def test_open_merged_makes_an_unsaved_document(app, windows, driver, tmp_path):  # noqa: F811
    a = small_pdf(str(tmp_path / "a.pdf"), 2, "A")
    b = small_pdf(str(tmp_path / "b.pdf"), 3, "B")
    (tmp_path / "Combined.pdf").write_bytes(b"taken")
    win = windows()
    assert win.open_merged([a, b]) is win
    doc = win.canvas.doc
    assert texts(doc) == ["A1", "A2", "B1", "B2", "B3"]
    assert doc.path == str(tmp_path / "Combined (2).pdf") and doc.converted
    assert not win.canvas.undo.isClean() and "Combined (2).pdf" in win.windowTitle()
    assert win.a_save.isEnabled()
    out = str(tmp_path / "out.pdf")
    driver.on("file", lambda call: (out, "PDF files (*.pdf)"))
    assert win.save()
    with pymupdf.open(out) as d:
        assert [p.get_text().split()[0] for p in d] == ["A1", "A2", "B1", "B2", "B3"]
    # a window with a document opens the combination in a new one
    other = win.open_merged([b, a])
    assert other is not win and texts(other.canvas.doc) == ["B1", "B2", "B3", "A1", "A2"]
    driver.check()


def _name(tmp_path) -> str:
    return f"leaf-test-{uuid.uuid4().hex[:12]}" if sys.platform == "win32" else str(tmp_path / "s.sock")


def test_merge_launches_are_gathered_by_the_running_instance(app, windows, driver, tmp_path):  # noqa: F811
    a = small_pdf(str(tmp_path / "scan 10.pdf"), 1, "TEN")
    b = small_pdf(str(tmp_path / "scan 2.pdf"), 1, "TWO")
    c = small_pdf(str(tmp_path / "cover.pdf"), 1, "COVER")
    srv = ipc.IpcServer(_name(tmp_path))
    assert srv.listen()
    try:
        empty = windows()
        # Explorer: one launch per selected file, in no particular order
        for p in (a, c, b):
            box = []
            t = threading.Thread(target=lambda p=p: box.append(ipc.forward_open([p], srv.name, merge=True)))
            t.start()
            end = time.time() + 5
            while t.is_alive() and time.time() < end:
                app.processEvents()
                time.sleep(0.002)
            assert box == [True]
        wait_until(lambda: empty.canvas.doc is not None, secs=5, msg="combined document")
        assert texts(empty.canvas.doc) == ["COVER1", "TWO1", "TEN1"]  # by name, naturally
        assert empty.canvas.doc.converted
    finally:
        srv.close()
        pump(0.1)
        srv.deleteLater()
    driver.check()


def test_startup_lock_serializes_launches(tmp_path):
    name = _name(tmp_path)
    first, second = ipc.StartupLock(name), ipc.StartupLock(name)

    def other(lock, timeout):  # another launch (a Windows mutex is re-entrant within one thread)
        box = []
        t = threading.Thread(target=lambda: box.append((lock.acquire(timeout), lock.release())))
        t.start()
        t.join(10)
        return box[0][0]

    assert first.acquire(1.0)
    t = time.perf_counter()
    assert not other(second, 0.2)  # waits for the first one (here: gives up)
    assert time.perf_counter() - t >= 0.15
    first.release()
    assert other(second, 1.0)


# ====================================================================== share
@pytest.fixture
def calls(monkeypatch, tmp_path):
    rec: dict[str, list] = {}

    def recorder(name, result=True):
        def f(*a, **k):
            rec.setdefault(name, []).append(a)
            return result
        return f

    monkeypatch.setattr(S, "SHARE_DIR", str(tmp_path / "share"))
    for n in ("show_share_sheet", "reveal", "send_email", "open_default_apps_settings", "register",
              "notify_assoc_changed", "set_linux_default"):
        monkeypatch.setattr(S, n, recorder(n, (True, "Leaf is now the default app for PDF files.")
                                           if n == "set_linux_default" else True))
    rec["_recorder"] = recorder
    return rec


def test_share_menu_saved_file(app, windows, driver, tmp_path, calls):  # noqa: F811
    src = make_pdf(str(tmp_path / "doc.pdf"))
    win = windows()
    sh = win.share
    assert not sh.menu.isEnabled()  # no document
    open_in(win, src)
    file_menu = [a.menu() for a in win.menus["file"].actions() if a.menu() is not None]
    assert sh.menu in file_menu
    sh.menu.aboutToShow.emit()
    assert all(a.isEnabled() for a in (win.a_copy_file, win.a_show_in_folder))
    win.a_share.trigger()  # saved, no changes: shares the file itself, no question
    assert calls["show_share_sheet"][0][0] == src
    win.a_show_in_folder.trigger()
    assert calls["reveal"] == [(src,)]
    win.a_email.trigger()
    assert calls["send_email"][0][:2] == (src, "doc")
    win.a_copy_file.trigger()
    md = QGuiApplication.clipboard().mimeData()
    assert [os.path.normcase(os.path.abspath(u.toLocalFile())) for u in md.urls()] == [os.path.normcase(src)]
    # no share sheet: the file goes to the clipboard instead
    QGuiApplication.clipboard().clear()
    import leaf.shellint

    leaf.shellint.show_share_sheet = calls["_recorder"]("show_share_sheet", False)
    win.a_share.trigger()
    assert [u.toLocalFile() for u in QGuiApplication.clipboard().mimeData().urls()]
    driver.check()


def test_share_with_unsaved_changes(app, windows, driver, tmp_path, calls):  # noqa: F811
    src = make_pdf(str(tmp_path / "doc.pdf"))
    win = windows()
    open_in(win, src)
    win.rotate_pages(90)
    assert not win.canvas.undo.isClean()
    # 1. a copy of the current state, named like the document, in a temp folder
    driver.on("dialog", accept_with(button="Share a copy"))
    win.a_share.trigger()
    shared = calls["show_share_sheet"][-1][0]
    assert os.path.basename(shared) == "doc.pdf" and shared != src and shared.startswith(S.SHARE_DIR)
    with pymupdf.open(shared) as d, pymupdf.open(src) as o:
        assert d.page_count == o.page_count and d[0].rotation == (o[0].rotation + 90) % 360
    assert not win.canvas.undo.isClean()  # the document itself is still unsaved
    # 2. cancel: nothing shared
    n = len(calls["show_share_sheet"])
    driver.on("dialog", lambda box: box.reject() or 0)
    win.a_share.trigger()
    assert len(calls["show_share_sheet"]) == n
    # 3. save first, then share the saved file
    driver.on("dialog", accept_with(button="Save and share"))
    win.a_email.trigger()
    assert calls["send_email"][-1][0] == src and win.canvas.undo.isClean()
    driver.check()


def test_share_never_saved_document_offers_save_as(app, windows, driver, tmp_path, calls):  # noqa: F811
    a = small_pdf(str(tmp_path / "a.pdf"), 1, "A")
    b = small_pdf(str(tmp_path / "b.pdf"), 1, "B")
    win = windows()
    win.open_merged([a, b])
    assert not win.a_show_in_folder.isEnabled()  # nothing on disk yet
    out = str(tmp_path / "both.pdf")
    driver.on("dialog", accept_with(button="Save as"))
    driver.on("file", lambda call: (out, "PDF files (*.pdf)"))
    win.a_copy_file.trigger()
    md = QGuiApplication.clipboard().mimeData()
    assert [os.path.normcase(u.toLocalFile()) for u in md.urls()] == [os.path.normcase(out.replace("\\", "/"))]
    pump(0.05)
    assert win.a_show_in_folder.isEnabled()
    driver.check()


# ====================================================================== default app
@pytest.mark.parametrize("kind", ["registered", "msix", "unregistered", "xdg", "flatpak"])
def test_make_default(app, windows, driver, tmp_path, calls, monkeypatch, kind):  # noqa: F811
    monkeypatch.setattr(S, "default_app_plan", lambda: {"kind": kind, "text": f"plan {kind}"})
    monkeypatch.setattr(S, "frozen_exe", lambda: r"C:\Apps\Leaf\Leaf.exe")
    win = windows()
    if kind in ("unregistered", "xdg"):
        driver.on("msg", lambda call: QMessageBox.StandardButton.Yes)
    elif kind == "flatpak":
        driver.on("msg", lambda call: QMessageBox.StandardButton.Ok)
    win.a_make_default.trigger()
    if kind in ("registered", "msix"):
        assert calls["open_default_apps_settings"]
    if kind == "unregistered":
        assert calls["register"] == [(r"C:\Apps\Leaf\Leaf.exe",)] and calls["notify_assoc_changed"]
        assert calls["open_default_apps_settings"]
    if kind == "xdg":
        assert calls["set_linux_default"]
    if kind == "flatpak":
        assert not any(k in calls for k in ("register", "set_linux_default", "open_default_apps_settings"))
    driver.check()


def test_make_default_in_settings_and_palette(app, windows, driver):  # noqa: F811
    win = windows()
    from leaf.features.osint import settings_page
    from leaf.palette import action_entries

    page = settings_page(win)
    b = page.findChild(QPushButton, "system_make_default")
    assert b is not None and "default PDF app" in b.text()
    titles = [e.title for e in action_entries(win)]
    assert "Make Leaf the default PDF app" in titles  # (the palette drops the ellipsis)
    assert "Copy file" in titles and "Show in folder" in titles
    driver.check()
