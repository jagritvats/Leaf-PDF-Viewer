"""Edit existing text: the Edit text tool (toolbar next to Text, single key, Edit menu, palette), the line /
paragraph mode toggle (Tab while hovering; remembered), the status-bar hint, and keeping find/selection in step
with edited text. The tool itself lives in leaf/textedit_ui.py, the engine in leaf/textedit.py."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QToolButton

from .. import shortcuts
from .. import textedit_ui as U
from ..icons import icon

KEYS = (Qt.Key.Key_I, Qt.Key.Key_M, Qt.Key.Key_Y)  # first free one wins


def _insert_before(menu, before: QAction | None, actions: list) -> None:
    if before is not None and before in menu.actions():
        menu.insertActions(before, actions)
    else:
        menu.addActions(actions)


class _PaletteWatch(QObject):
    """Re-tints the outline/bar when the system switches between light and dark."""

    def __init__(self, win, tool):
        super().__init__(win)
        self.tool = tool
        win.installEventFilter(self)

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.PaletteChange:
            self.tool.restyle()
        return False


def _tool_button(win, a: QAction, a_para: QAction) -> None:
    strip = getattr(win, "tool_strip", None)
    if strip is None or strip.layout() is None:
        return
    b = QToolButton()
    b.setAutoRaise(True)
    b.setIconSize(QSize(20, 20))
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setDefaultAction(a)
    menu = QMenu(b)
    menu.addAction(a_para)
    b.setMenu(menu)
    b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    lay = strip.layout()
    text = getattr(win, "tool_actions", {}).get("text")
    at = next((i for i in range(lay.count()) if isinstance(lay.itemAt(i).widget(), QToolButton)
               and lay.itemAt(i).widget().defaultAction() is text), -1)
    lay.insertWidget(at + 1 if at >= 0 else lay.count(), b)
    buttons = getattr(win, "tool_buttons", None)
    if buttons is not None:
        b.setToolButtonStyle(buttons[0].toolButtonStyle() if buttons else win.toolbar.toolButtonStyle())
        buttons.append(b)
    win.textedit_button = b


def install(win) -> None:
    cv = win.canvas
    tool = win.textedit_tool = U.EditTextTool(cv, getattr(win, "settings", None), win.toast)
    _PaletteWatch(win, tool)
    try:
        from .. import props, window

        window.HINTS[U.TOOL] = U.hint(tool.para)
        props.TITLES.setdefault(U.TOOL, "Edit text")
    except Exception:  # noqa: BLE001
        pass

    used = set(cv.tool_keys)
    key = next((k for k in KEYS if k not in used), None)
    letter = key.name[-1] if key is not None else ""
    a = QAction(icon(U.TOOL), "&Edit text", win)
    a.setObjectName("a_tool_edittext")
    a.setCheckable(True)
    a.setProperty("icon_name", U.TOOL)
    a.setIconText("Edit")
    a.setToolTip(f"Edit existing text  ({letter})" if letter else "Edit existing text")
    a.setStatusTip("Click a line of the page's own text to change it")
    a.triggered.connect(lambda _=False: cv.set_tool(U.TOOL))
    win.a_tool_edittext = a
    if hasattr(win, "tool_group"):
        win.tool_group.addAction(a)
    if hasattr(win, "tool_actions"):
        win.tool_actions[U.TOOL] = a
    win.addAction(a)
    if key is not None:
        cv.tool_keys = {**cv.tool_keys, key: U.TOOL}

    a_para = QAction(icon("pilcrow"), "Edit whole &paragraphs", win)
    a_para.setObjectName("a_textedit_paragraphs")
    a_para.setCheckable(True)
    a_para.setChecked(tool.para)
    a_para.setProperty("icon_name", "pilcrow")
    a_para.setToolTip("Edit text: whole paragraphs (reflowed) instead of single lines. Tab switches while the "
                      "Edit text tool is active")

    def para_toggled(on: bool) -> None:
        tool.set_para(on)
        if on and cv.doc is not None and cv.tool != U.TOOL and not tool.editing():
            cv.set_tool(U.TOOL)

    a_para.toggled.connect(para_toggled)
    win.a_textedit_paragraphs = a_para
    win.addAction(a_para)

    def mode_changed(on: bool) -> None:
        if a_para.isChecked() != on:
            a_para.setChecked(on)
        try:
            from .. import window

            window.HINTS[U.TOOL] = U.hint(on)
        except Exception:  # noqa: BLE001
            pass
        if cv.tool == U.TOOL and hasattr(win, "hint_label"):
            win.hint_label.setText(U.hint(on))
        if cv.tool == U.TOOL and cv.underMouse():
            win.toast("Editing whole paragraphs" if on else "Editing single lines", ms=1200)

    tool.modeChanged.connect(mode_changed)

    menus = getattr(win, "menus", {})
    if "edit" in menus:
        m = menus["edit"]
        find = getattr(win, "a_find", None)
        _insert_before(m, find, [a, a_para])
        if find in m.actions():
            m.insertSeparator(find)
    _tool_button(win, a, a_para)

    # find bar and text selection follow edited text (the engine re-reads the words of the page)
    def undo_changed(idx: int) -> None:
        st = cv.undo
        if not (U.is_edit_command(st.command(idx - 1)) or U.is_edit_command(st.command(idx))):
            return
        cv._end_text_selection()
        find = getattr(win, "find", None)
        if find is not None and not find.isHidden() and find.edit.text().strip():
            find._debounce.start()

    cv.undo.indexChanged.connect(undo_changed)

    def enable() -> None:
        has = cv.doc is not None
        a.setEnabled(has)
        a_para.setEnabled(has)

    cv.documentChanged.connect(enable)
    enable()
    shortcuts.apply_saved(win, only_new=True)  # picks up the tool key
