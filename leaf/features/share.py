"""File › Share (the Windows share sheet, Copy file, Email…, Show in folder) and "Make Leaf the default PDF app"
(Settings › System and the command palette). The platform work is in leaf/shellint.py.

Sharing sends the saved file. With unsaved changes (or a document that was never saved) the user picks: save
first, or share a copy written from the current state to a temp folder (named like the document)."""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from .. import shellint as S

APP = "Leaf"


def _toast(win, text: str, ms: int = 3500) -> None:
    try:
        win.toast(text, ms=ms)
    except Exception:  # noqa: BLE001
        win.statusBar().showMessage(text, ms)


def _on_disk(doc) -> bool:
    return bool(doc is not None and doc.path and not getattr(doc, "converted", False) and os.path.exists(doc.path))


def shareable_path(win, verb: str = "Share") -> str | None:
    """The file to share: the saved file, or (after asking) the saved-now file or a temp copy of the current state."""
    doc = win.canvas.doc
    if doc is None:
        return None
    win._commit_edits()
    saved = _on_disk(doc)
    if saved and win.canvas.undo.isClean():
        return doc.path
    name = os.path.basename(doc.path or "Untitled.pdf")
    box = QMessageBox(win)
    box.setWindowTitle(verb)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(f"{name} has unsaved changes." if saved else f"{name} hasn't been saved yet.")
    box.setInformativeText("Save it first, or share a copy with the changes as they are now?")
    b_save = box.addButton("Save and share" if saved else "Save as…", QMessageBox.ButtonRole.AcceptRole)
    b_copy = box.addButton("Share a copy", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(b_save)
    box.exec()
    chosen = box.clickedButton()
    if chosen is b_save:
        return doc.path if win.save() and _on_disk(doc) else None
    if chosen is b_copy:
        return write_copy(win, name)
    return None


def write_copy(win, name: str) -> str | None:
    """Write the document as it is now (edits included) to a fresh temp file named `name`."""
    path = S.temp_share_path(name)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        win.canvas.doc.save(path, win.canvas.overlays_for)
    except Exception as ex:  # noqa: BLE001
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(win, APP, f"Couldn't write a copy to share:\n{ex}")
        return None
    QApplication.restoreOverrideCursor()
    return path


class _Relay(QObject):
    """Brings the mail client's result (a worker thread) back to the GUI thread."""

    done = Signal(int)


class Share:
    def __init__(self, win):
        self.win = win
        cv = win.canvas
        self.relay = _Relay(win)
        self.relay.done.connect(self._mail_done)

        def act(name: str, text: str, slot, tip: str) -> QAction:
            a = QAction(text, win)
            a.setObjectName(f"a_{name}")
            a.setToolTip(tip)
            a.setProperty("base_tip", tip)
            a.triggered.connect(lambda _=False: slot())
            win.addAction(a)
            setattr(win, f"a_{name}", a)
            return a

        self.a_share = act("share", "&Share…", self.share,
                           "Send the PDF with the Windows share sheet (Mail, Teams, nearby devices, …)")
        self.a_share.setVisible(S.share_sheet_available())
        self.a_copy_file = act("copy_file", "&Copy file", self.copy_file,
                               "Put the PDF on the clipboard as a file: paste it into a folder, an email or a chat")
        self.a_email = act("email", "&Email…", self.email, "Attach the PDF to a new message in your mail app")
        self.a_email.setVisible(S.email_available())
        self.a_show = act("show_in_folder", "Show in &folder", self.show_in_folder,
                          "Open the folder that contains this file, with the file selected")
        self.a_default = act("make_default", "Make Leaf the default PDF app…", lambda: make_default(win),
                             "Open PDF files in Leaf when you double-click them")
        self.actions = [self.a_share, self.a_copy_file, self.a_email, self.a_show]

        menus = getattr(win, "menus", {})
        file_menu = menus.get("file")
        self.menu = QMenu("S&hare", win)
        self.menu.addActions([self.a_share, self.a_copy_file, self.a_email])
        self.menu.addSeparator()
        self.menu.addAction(self.a_show)
        self.menu.aboutToShow.connect(self.update)
        if file_menu is not None:
            acts = file_menu.actions()
            anchor = next((a for a in acts if a.menu() is not None and "xport" in a.text()), None)  # after Export
            if anchor is None:
                anchor = getattr(win, "a_print", None)
            nxt = acts[acts.index(anchor) + 1] if anchor in acts and acts.index(anchor) + 1 < len(acts) else None
            if nxt is not None:
                file_menu.insertMenu(nxt, self.menu)
            else:
                file_menu.addMenu(self.menu)
        cv.documentChanged.connect(self.update)
        # after the save that cleans the stack, Save As still has to set the new path: look once that's done
        cv.undo.cleanChanged.connect(lambda _c: QTimer.singleShot(0, self.update))
        self.update()

    def update(self) -> None:
        doc = self.win.canvas.doc
        for a in (self.a_share, self.a_copy_file, self.a_email):
            a.setEnabled(doc is not None)
        self.a_show.setEnabled(_on_disk(doc))
        self.menu.setEnabled(doc is not None)

    # ----------------------------------------------------------------- actions
    def share(self) -> None:
        path = shareable_path(self.win)
        if not path:
            return
        hwnd = None
        try:
            hwnd = int(self.win.winId()) if S.IS_WIN else None
        except Exception:  # noqa: BLE001
            pass
        if S.show_share_sheet(path, hwnd):
            return
        S.copy_file_to_clipboard([path])  # no share sheet here: the next best thing
        _toast(self.win, "Sharing isn't available here. The file is on the clipboard instead: paste it into an "
                         "email or a chat.", 5000)

    def copy_file(self) -> None:
        path = shareable_path(self.win, "Copy file")
        if not path:
            return
        S.copy_file_to_clipboard([path])
        _toast(self.win, f"Copied {os.path.basename(path)} as a file. Paste it into a folder, an email or a chat.")

    def email(self) -> None:
        path = shareable_path(self.win, "Email")
        if not path:
            return
        subject = os.path.splitext(os.path.basename(path))[0]
        if not S.send_email(path, subject, self.relay.done.emit):
            QMessageBox.warning(self.win, APP, "Couldn't start your mail app.")

    def _mail_done(self, code: int) -> None:
        if code not in (0, 1):  # 0 sent, 1 cancelled by the user
            QMessageBox.warning(self.win, APP, "Your mail app couldn't create the message (Simple MAPI error "
                                               f"{code}). Use Copy file and paste it into a message instead.")

    def show_in_folder(self) -> None:
        doc = self.win.canvas.doc
        if _on_disk(doc) and not S.reveal(doc.path):
            _toast(self.win, "Couldn't open the folder.")


# ====================================================================== default app
def make_default(win) -> None:
    plan = S.default_app_plan()
    kind = plan["kind"]
    if kind in ("msix", "registered"):
        if S.open_default_apps_settings():
            _toast(win, "In Settings, choose Leaf for .pdf.", 5000)
        else:
            QMessageBox.information(win, APP, plan["text"])
        return
    if kind == "unregistered":
        if QMessageBox.question(win, APP, plan["text"]) != QMessageBox.StandardButton.Yes:
            return
        try:
            S.register(S.frozen_exe())
            S.notify_assoc_changed()
        except OSError as ex:
            QMessageBox.warning(win, APP, f"Couldn't register Leaf:\n{ex}")
            return
        S.open_default_apps_settings()
        _toast(win, "Leaf is registered. In Settings, choose Leaf for .pdf.", 5000)
        return
    if kind == "xdg":
        if QMessageBox.question(win, APP, plan["text"]) != QMessageBox.StandardButton.Yes:
            return
        ok, msg = S.set_linux_default()
        if ok:
            _toast(win, msg)
        else:
            QMessageBox.warning(win, APP, msg)
        return
    QMessageBox.information(win, APP, plan["text"])


def unregister_portable(win) -> bool:
    """Undo `register()` for a portable copy (Settings › System)."""
    if QMessageBox.question(win, APP, "Remove Leaf from Windows' Default apps and Open with lists? (Leaf itself "
                                      "and its settings stay.)") != QMessageBox.StandardButton.Yes:
        return False
    S.unregister()
    S.notify_assoc_changed()
    _toast(win, "Leaf was removed from Windows' app lists.")
    return True


def install(win) -> None:
    win.share = Share(win)
