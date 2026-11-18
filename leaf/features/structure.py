"""structure feature: links (Link tool `K`, Insert › Link… Ctrl+L), bookmark editing (Add bookmark Ctrl+Shift+B, the
Outline panel's own editing), and the Attachments panel (Shift+F8). Models and save steps: leaf/links.py,
leaf/outline.py, leaf/attachments.py."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget

from .. import attachments as AT
from .. import links as LK
from .. import outline as OL


def _truthy(v, default: bool) -> bool:
    if v is None:
        return default
    return v in (True, 1, "1", "true", "True")


def _action(win, name: str, text: str, slot, keys=(), tip: str | None = None, checkable: bool = False) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    a.setCheckable(checkable)
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    base = tip or text.replace("&", "").rstrip("…")
    a.setProperty("base_tip", base)
    a.setToolTip(base + (f"  ({sc})" if sc else ""))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def _toast(win, text: str, undo: bool = False) -> None:
    t = getattr(win, "toast", None)
    if callable(t):
        if undo:
            t(text, "Undo", win.canvas.undo.undo)
        else:
            t(text)
    else:
        win.statusBar().showMessage(text, 4000)


class AttachmentsDock(QDockWidget):
    def __init__(self, win):
        super().__init__("Attachments", win)
        self.win = win
        self.setObjectName("attachments")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.panel = AT.AttachmentsPanel(win.canvas)
        self.setWidget(self.panel)
        self.panel.changed.connect(self._title)

    def _title(self) -> None:
        n = self.panel.count()
        self.setWindowTitle(f"Attachments ({n})" if n else "Attachments")

    def closeEvent(self, e) -> None:  # the user closed it: don't pop it up for the next document
        self.win.settings.setValue("view/attachments", False)
        super().closeEvent(e)


class Structure:
    def __init__(self, win):
        self.win = win
        cv = self.cv = win.canvas
        self._orphans = 0

        # ---------------------------------------------------------------- links
        self.tool = LK.LinkTool(win)
        self.a_link_tool = _action(win, "link_tool", "Lin&k tool", lambda: cv.set_tool("link"),
                                   tip="Drag a rectangle to add a link; click a link to edit it  (K)", checkable=True)
        self.a_insert_link = _action(win, "insert_link", "&Link…", self.insert_link, ["Ctrl+L"],
                                     tip="Make the selected text a link (or drag a rectangle with the Link tool)")
        tool_keys = getattr(cv, "tool_keys", None)
        if isinstance(tool_keys, dict) and Qt.Key.Key_K not in tool_keys:
            tool_keys[Qt.Key.Key_K] = "link"
        group = getattr(win, "tool_group", None)
        if group is not None:
            group.addAction(self.a_link_tool)
        tools = getattr(win, "tool_actions", None)
        if isinstance(tools, dict):
            tools["link"] = self.a_link_tool
        try:
            from .. import window as W
            W.HINTS.setdefault("link", "Drag a rectangle to add a link. Click a link to select it: double-click or Enter "
                                       "edits, Delete removes, right-click for more. Esc to finish.")
        except Exception:  # noqa: BLE001
            pass

        # ------------------------------------------------------------ bookmarks
        self.a_add_bookmark = _action(win, "add_bookmark", "Add &bookmark", self.add_bookmark, ["Ctrl+Shift+B"],
                                      tip="Bookmark the current view (title from the selected text or the heading)")
        panel = self.outline_panel()
        if panel is not None:
            panel.changed.connect(self._outline_changed)

        # ---------------------------------------------------------- attachments
        self.dock = AttachmentsDock(win)
        side = getattr(win, "side_dock", None)
        win.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)
        if side is not None:
            win.tabifyDockWidget(side, self.dock)
            side.raise_()
        self.dock.hide()
        QTimer.singleShot(0, lambda: self.dock.setVisible(False) if not self._has_files() else None)
        self.a_attachments = self.dock.toggleViewAction()
        self.a_attachments.setText("&Attachments panel")
        self.a_attachments.setObjectName("a_attachments")
        self.a_attachments.setShortcut(QKeySequence("Shift+F8"))
        self.a_attachments.setToolTip("Attachments panel  (Shift+F8)")
        self.a_attachments.triggered.connect(lambda on: win.settings.setValue("view/attachments", on))
        win.a_attachments = self.a_attachments
        win.addAction(self.a_attachments)
        self.a_attach = _action(win, "attach_file", "Attach &file…", self.attach_file,
                                tip="Embed a file in the document (it travels with the PDF)")

        # ---------------------------------------------------------------- menus
        menus = getattr(win, "menus", {})
        ins = menus.get("insert")
        if ins is not None:
            first_sep = next((a for a in ins.actions() if a.isSeparator()), None)
            for a in (self.a_insert_link, self.a_add_bookmark, self.a_attach):
                ins.insertAction(first_sep, a) if first_sep is not None else ins.addAction(a)
            ins.addAction(self.a_link_tool)
        view = menus.get("view")
        if view is not None:
            acts = view.actions()
            outline = getattr(win, "a_outline", None)
            if outline in acts and acts.index(outline) + 1 < len(acts):
                view.insertAction(acts[acts.index(outline) + 1], self.a_attachments)
            else:
                view.addAction(self.a_attachments)

        cv.documentChanged.connect(self._document_changed)
        cv.pagesChanged.connect(self._pages_changed)
        self._doc_actions = [self.a_link_tool, self.a_insert_link, self.a_add_bookmark, self.a_attachments, self.a_attach]
        self._update_enabled()

    # ------------------------------------------------------------------ state
    def outline_panel(self):
        p = getattr(self.win, "outline", None)
        return p if isinstance(p, OL.OutlinePanel) else None

    def _has_files(self) -> bool:
        doc = self.cv.doc
        return doc is not None and bool(AT.files_of(doc))

    def _update_enabled(self) -> None:
        has = self.cv.doc is not None
        for a in self._doc_actions:
            a.setEnabled(has)

    def _document_changed(self) -> None:
        self._orphans = 0
        self._update_enabled()
        self.dock.panel.schedule()
        show = self._has_files() and _truthy(self.win.settings.value("view/attachments"), True)
        self.dock.setVisible(show)

    def _pages_changed(self) -> None:
        doc = self.cv.doc
        if doc is None:
            return
        n = LK.dropped_targets(doc) + OL.orphaned(doc)
        if n > self._orphans:
            _toast(self.win, f"{n} link(s)/bookmark(s) point at deleted pages; they'll be left out when you save.")
        self._orphans = n
        if self.cv.tool == "link":
            self.tool.refresh()

    def _outline_changed(self) -> None:
        viewing = getattr(self.win, "viewing", None)
        panel = self.outline_panel()
        if viewing is None or panel is None:
            return
        if hasattr(viewing, "_update_enabled"):
            viewing._update_enabled()
        if panel.has_outline() and not viewing.dock.isVisible():
            viewing.dock.show()
            viewing.dock.raise_()

    # ---------------------------------------------------------------- actions
    def insert_link(self) -> None:
        if self.cv.doc is None:
            return
        if self.tool.link_selection():
            _toast(self.win, "Link added", undo=True)

    def add_bookmark(self) -> None:
        panel = self.outline_panel()
        if self.cv.doc is None or panel is None:
            return
        viewing = getattr(self.win, "viewing", None)
        if viewing is not None:
            viewing.dock.show()
            viewing.dock.raise_()
        n = panel.add_here(edit=True)
        if n is not None:
            self.win.settings.setValue("view/outline", True)

    def attach_file(self) -> None:
        if self.cv.doc is None:
            return
        self.dock.show()
        self.dock.raise_()
        self.win.settings.setValue("view/attachments", True)
        self.dock.panel.add_dialog()


def install(win) -> None:
    win.structure = Structure(win)
    win.attachments = win.structure.dock.panel
