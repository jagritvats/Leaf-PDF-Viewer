"""Annotations feature: Note tool (N), Comments panel (F9), author name, thumbnails that follow hidden annotations.

The model lives in leaf/annots.py (adoption of the PDF's own annotations, display copy, notes, replies) and the
panel in leaf/comments.py; this module only wires them into the window."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget, QToolButton

from .. import shortcuts
from ..comments import CommentsPanel
from ..icons import icon

NOTE_HINT = "Click to add a sticky note. Double-click a note or any annotation (or press Enter) to edit its comment."


def _insert_after(menu, after: QAction | None, actions: list[QAction]) -> None:
    acts = menu.actions()
    if after in acts and acts.index(after) + 1 < len(acts):
        menu.insertActions(acts[acts.index(after) + 1], actions)
    else:
        menu.addActions(actions)


def _note_tool(win) -> QAction:
    cv = win.canvas
    a = QAction(icon("note"), "Sticky note", win)
    a.setObjectName("a_tool_note")
    a.setCheckable(True)
    a.setProperty("icon_name", "note")
    a.setIconText("Note")
    a.setToolTip("Sticky note  (N)")
    a.triggered.connect(lambda _=False: cv.set_tool("note"))
    win.a_tool_note = a
    if hasattr(win, "tool_group"):
        win.tool_group.addAction(a)
    if hasattr(win, "tool_actions"):
        win.tool_actions["note"] = a
    win.addAction(a)
    cv.tool_keys = {**cv.tool_keys, Qt.Key.Key_N: "note"}
    try:
        from .. import window

        window.HINTS.setdefault("note", NOTE_HINT)
    except Exception:  # noqa: BLE001
        pass
    menus = getattr(win, "menus", {})
    if "insert" in menus:
        tools = getattr(win, "tool_actions", {})
        _insert_after(menus["insert"], tools.get("highlight"), [a])
    strip = getattr(win, "tool_strip", None)
    if strip is not None and strip.layout() is not None:
        b = QToolButton()
        b.setAutoRaise(True)
        b.setIconSize(QSize(20, 20))
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.setDefaultAction(a)
        lay = strip.layout()
        hl = next((i for i in range(lay.count()) if isinstance(lay.itemAt(i).widget(), QToolButton)
                   and lay.itemAt(i).widget().defaultAction() is getattr(win, "tool_actions", {}).get("highlight")), -1)
        lay.insertWidget(hl + 1 if hl >= 0 else lay.count(), b)
        buttons = getattr(win, "tool_buttons", None)
        if buttons is not None:
            b.setToolButtonStyle(buttons[0].toolButtonStyle() if buttons else win.toolbar.toolButtonStyle())
            buttons.append(b)
    return a


def _comments_dock(win) -> QDockWidget:
    panel = CommentsPanel(win.canvas)
    dock = QDockWidget("Comments", win)
    dock.setObjectName("comments")
    dock.setWidget(panel)
    dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    if getattr(win, "props_dock", None) is not None:
        win.tabifyDockWidget(win.props_dock, dock)
        win.props_dock.raise_()
    dock.hide()
    dock.panel = panel
    return dock


def _drop_thumbs(win, uids: list) -> None:
    for view in (getattr(win, "sidebar", None), getattr(win, "organizer", None)):
        thumbs = getattr(view, "_thumbs", None)
        if thumbs is None:
            continue
        for uid in uids:
            thumbs.pop(uid, None)
        view.viewport().update()


def install(win) -> None:
    cv = win.canvas
    mgr = cv.annots
    s = win.settings
    name = s.value("annotations/author", "")
    if name:
        mgr.author = str(name)
    mgr.authorChanged.connect(lambda n: s.setValue("annotations/author", n))
    mgr.thumbsStale.connect(lambda uids: _drop_thumbs(win, uids))

    _note_tool(win)

    win.comments_dock = _comments_dock(win)
    act = win.comments_dock.toggleViewAction()
    act.setText("&Comments panel")
    act.setObjectName("a_comments_panel")
    act.setShortcut(QKeySequence("F9"))
    act.setIcon(icon("comment"))
    act.setProperty("icon_name", "comment")
    win.a_comments_panel = act
    act.triggered.connect(lambda on: on and win.comments_dock.raise_())  # in front of its tab siblings
    menus = getattr(win, "menus", {})
    if "view" in menus:
        _insert_after(menus["view"], getattr(win, "a_history_panel", None) or getattr(win, "a_props", None), [act])

    def enable() -> None:
        has = cv.doc is not None
        win.a_tool_note.setEnabled(has)

    cv.documentChanged.connect(enable)
    enable()
    shortcuts.apply_saved(win, only_new=True)  # picks up the new tool key and the panel shortcut
