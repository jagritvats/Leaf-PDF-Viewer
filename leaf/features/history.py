"""History feature: command palette, customizable shortcuts + cheat sheet, History panel, Duplicate / Select all on
page, crash recovery."""
from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget, QStyle, QUndoView

from .. import commands as C
from .. import recovery, shortcuts
from ..palette import show_palette

DUP_OFFSET = 12.0


def _action(win, name: str, text: str, slot, keys=(), tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    a.setProperty("base_tip", tip or shortcuts.clean(text))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)  # shortcuts work even when the action isn't in a visible menu
    return a


def _insert_after(menu, after: QAction | None, actions: list[QAction]) -> None:
    acts = menu.actions()
    if after in acts and acts.index(after) + 1 < len(acts):
        menu.insertActions(acts[acts.index(after) + 1], actions)
    else:
        menu.addActions(actions)


# ------------------------------------------------------------ duplicate etc
def clone(it):
    """A detached copy of an overlay item (same parent page not yet set), or None if unsupported."""
    if hasattr(it, "clone"):
        return it.clone()
    from ..items import ImageItem, InkItem, LineItem, ShapeItem, TextItem

    props = dict(it.props)
    if isinstance(it, ImageItem):
        c = ImageItem(it.kind, QRectF(it._r), it.image, props)
        c._png = it._png
        c.set_geometry(it.geometry())
    elif isinstance(it, ShapeItem):
        c = ShapeItem(it.kind, QRectF(it._r), props)
        c.set_geometry(it.geometry())
    elif isinstance(it, LineItem):
        c = LineItem(it.kind, QPointF(0, 0), QPointF(1, 1), props)
        c.set_geometry(it.geometry())
    elif isinstance(it, InkItem):
        c = InkItem(QPointF(it.pts[0]), props)
        c.pts = [QPointF(p) for p in it.pts]
        c.widths = list(getattr(it, "widths", []))  # pen pressure
        c._cache = None
        c.setPos(it.pos())
    elif isinstance(it, TextItem):
        c = TextItem(QPointF(it.pos()), props, it.toPlainText())
    else:
        return None
    c.setZValue(it.zValue())
    return c


def duplicate(canvas) -> list:
    sel = [it for it in canvas.selected_overlays() if it.flags() & it.GraphicsItemFlag.ItemIsMovable]
    copies = []
    for it in sel:
        c = clone(it)
        if c is None or it.parentItem() is None:
            continue
        c.setParentItem(it.parentItem())
        c.setPos(c.pos() + QPointF(DUP_OFFSET, DUP_OFFSET))  # clamped to the page by the item
        copies.append(c)
    if copies:
        canvas.undo.push(C.AddItems(copies, "Duplicate"))
        canvas.scene().clearSelection()
        for c in copies:
            c.setSelected(True)
    return copies


def select_all_on_page(canvas) -> int:
    pg = canvas.current_page_item()
    if pg is None:
        return 0
    canvas._clear_focus()
    canvas.set_tool("select")
    canvas.scene().clearSelection()
    items = [o for o in pg.overlays() if o.flags() & o.GraphicsItemFlag.ItemIsSelectable]
    for o in items:
        o.setSelected(True)
    return len(items)


# ---------------------------------------------------------------- "?" key
class _HelpKey(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.KeyPress and e.text() == "?" and not self.win.canvas._editing_text() \
                and not e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            shortcuts.show_cheat_sheet(self.win)
            return True
        return False


def _history_dock(win) -> QDockWidget:
    view = QUndoView(win.canvas.undo)
    view.setEmptyLabel("Opened file")
    view.setCleanIcon(win.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
    view.setToolTip("Click a step to go back to it. The disk icon marks the saved state.")
    view.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep keyboard focus (and tool keys) on the page
    dock = QDockWidget("History", win)
    dock.setObjectName("history")
    dock.setWidget(view)
    dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    if getattr(win, "props_dock", None) is not None:
        win.tabifyDockWidget(win.props_dock, dock)
        win.props_dock.raise_()
    dock.hide()
    try:  # empty state: only the opened file, no edits yet
        from ..onboarding import attach_empty

        attach_empty(view, "Your edits will be listed here. Click a step to go back to it.",
                     when=lambda: win.canvas.doc is not None, empty=lambda: view.model().rowCount() <= 1)
    except Exception:  # noqa: BLE001 - a hint only
        pass
    return dock


def install(win) -> None:
    cv = win.canvas
    menus = getattr(win, "menus", {})

    # palette
    pal = _action(win, "command_palette", "Command palette…", lambda: show_palette(win), ["Ctrl+K", "Ctrl+Shift+P"],
                  "Search all commands")
    # edit: duplicate / select all on page / shortcut preferences
    dup = _action(win, "duplicate", "D&uplicate", lambda: duplicate(cv), ["Ctrl+D"], "Duplicate selected items")
    sel_all = _action(win, "select_all_items", "Select all &items on page", lambda: select_all_on_page(cv), ["Ctrl+Shift+A"])
    # Ctrl+, opens Settings (features/settings.py), whose Keyboard page links here
    prefs = _action(win, "shortcut_prefs", "&Keyboard shortcuts…", lambda: shortcuts.open_preferences(win),
                    [] if getattr(win, "a_settings", None) is not None else ["Ctrl+,"], "Customize keyboard shortcuts")

    def update_dup() -> None:
        has = bool(cv.doc is not None and cv.selected_overlays())
        dup.setEnabled(has)

    cv.selectionInfo.connect(update_dup)
    update_dup()
    if "edit" in menus:
        m = menus["edit"]
        _insert_after(m, getattr(win, "a_delete", None), [dup, sel_all])
        settings_act = getattr(win, "a_settings", None)
        if settings_act is not None and settings_act in m.actions():
            m.insertAction(settings_act, prefs)  # next to Settings…
        else:
            m.addSeparator()
            m.addAction(prefs)

    # cheat sheet: reuse the Help menu entry (F1) and the "?" key on the page
    help_act = None
    if "help" in menus:
        help_act = next((a for a in menus["help"].actions() if shortcuts.clean(a.text()).lower() == "keyboard shortcuts"), None)
        if help_act is None:
            help_act = menus["help"].addAction("&Keyboard shortcuts", win.show_shortcuts)
        help_act.setObjectName("a_shortcuts")
        help_act.setShortcut(QKeySequence("F1"))
        win.a_shortcuts = help_act
    cv.installEventFilter(_HelpKey(win))

    # history panel
    win.history_dock = _history_dock(win)
    hist = win.history_dock.toggleViewAction()
    hist.setText("&History panel")
    hist.setObjectName("a_history_panel")
    hist.setShortcut(QKeySequence("F7"))
    win.a_history_panel = hist
    if "view" in menus:
        _insert_after(menus["view"], getattr(win, "a_props", None), [hist])
        menus["view"].addSeparator()
        menus["view"].addAction(pal)

    # crash recovery
    win.recovery = recovery.Recovery(win)
    QTimer.singleShot(600, lambda: _offer(win))

    shortcuts.apply_saved(win)
    QTimer.singleShot(0, lambda: _warn_duplicates(win))


def _offer(win) -> None:
    try:
        if win.isVisible():
            recovery.offer(win)
    except RuntimeError:  # window closed before the timer fired
        pass


def _warn_duplicates(win) -> None:
    try:
        dup = shortcuts.duplicates(win)
    except RuntimeError:  # window already gone
        return
    for seq, acts in dup.items():
        names = ", ".join(a.objectName() for a in acts)
        print(f"Leaf: shortcut {seq} is bound to several actions ({names}); none of them will fire.", file=sys.stderr)
