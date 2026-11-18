"""Right-click menus that were missing: the page canvas (empty page, a text selection, an annotation or other
item, a link), the Comments panel and the Attachments list. Images, form fields, the page lists, the outline and
the start screen have their own menus (features/images.py, forms.py/fields.py, features/pages.py, outline.py,
welcome.py).

The menus reuse the window's QActions (same text, shortcut and enabled state as the main menus); actions that
belong to one spot (Add note here, Copy link address) are made per menu. Page actions from the canvas apply to
the page under the cursor (`win.rows_override` while the menu runs)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QInputDialog, QMenu


def _a(win, name: str) -> QAction | None:
    a = getattr(win, name, None)
    return a if isinstance(a, QAction) and a.isVisible() else None


def _add(menu: QMenu, actions) -> None:
    """Add the actions that exist, with separators between groups (None), never two in a row or at the ends."""
    pending = False
    for a in actions:
        if a is None:
            pending = bool(menu.actions())
            continue
        if pending:
            menu.addSeparator()
            pending = False
        menu.addAction(a)


def _exec(menu: QMenu, at):
    """A seam for tests (a modal menu would block them)."""
    return menu.exec(at)


def _own_menu(item) -> bool:
    """`item` (or a parent) shows its own context menu (form fields: forms.py, fields.py)."""
    while item is not None:
        for cls in type(item).__mro__:
            if cls.__module__.startswith("leaf.") and "contextMenuEvent" in vars(cls):
                return True
        item = item.parentItem()
    return False


class CanvasMenu:
    def __init__(self, win):
        self.win = win
        self.cv = win.canvas
        self.cv.context_menu = self.show

    # ------------------------------------------------------------------ dispatch
    def show(self, e) -> bool:
        cv = self.cv
        if cv.tool not in ("select", "hand"):
            return False
        vp = e.pos()
        sp = cv.mapToScene(vp)
        pg = cv.page_at(sp)
        if pg is None:
            return False
        top = self._overlay_at(vp)
        if top is not None:
            menu = self.item_menu(top)
        elif cv.selected_text() and self._in_selection(pg, sp):
            menu = self.text_menu()
        else:
            hit = cv.link_at(sp)
            menu = self.link_menu(*hit) if hit is not None else self.page_menu(pg, pg.content.mapFromScene(sp))
        if menu is None or not menu.actions():
            return False
        row = cv.order.index(pg) if pg in cv.order else -1
        self.win.rows_override = [row] if row >= 0 else None
        try:
            _exec(menu, e.globalPos())
        finally:
            self.win.rows_override = None
            menu.deleteLater()
        return True

    def _overlay_at(self, vp):
        from ..items import is_overlay

        for it in self.cv.items(vp):
            if _own_menu(it):
                return None
            top = it
            while top is not None and not is_overlay(top):
                top = top.parentItem()
            if top is not None and top.isVisible():
                return top
        return None

    def _in_selection(self, pg, sp) -> bool:
        sel = getattr(self.cv, "_sel_item", None)
        if sel is None or sel.parentItem() is not pg.content:
            return False
        lp = pg.content.mapFromScene(sp)
        return any(r.adjusted(-3, -3, 3, 3).contains(lp) for r in getattr(self.cv, "_sel_rects", []))

    # ------------------------------------------------------------------ menus
    def text_menu(self) -> QMenu:
        win = self.win
        m = QMenu(self.cv)
        text = " ".join(self.cv.selected_text().split())
        find = QAction(f"Find “{text[:30]}{'…' if len(text) > 30 else ''}”", m)
        find.triggered.connect(lambda: self.find(text))
        _add(m, [_a(win, "a_copy"), None, _a(win, "a_hl_sel"), _a(win, "a_redact_selection"), None,
                 _a(win, "a_insert_link"), find])
        return m

    def item_menu(self, item) -> QMenu:
        from .. import annots as A
        from ..items import TextItem

        win, cv = self.win, self.cv
        if not item.isSelected():
            cv.scene().clearSelection()
            item.setSelected(True)
        m = QMenu(cv)
        first = []
        if isinstance(item, TextItem):
            a = QAction("Edit text", m)
            a.triggered.connect(item.start_edit)
            first.append(a)
        if A.is_annotation(item):
            a = QAction("Edit comment…", m)
            a.triggered.connect(lambda: cv.annots.open_editor(item))
            r = QAction("Reply…", m)
            r.triggered.connect(lambda: self.reply(item))
            first += [a, r]
        style = QAction("Style…", m)
        style.setToolTip("Color, width, font: the Style panel")
        style.triggered.connect(self.show_style)
        _add(m, first + [None, style, None, _a(win, "a_duplicate"), _a(win, "a_delete")])
        return m

    def link_menu(self, pg, ln: dict) -> QMenu:
        import pymupdf

        from .. import links as LK

        cv = self.cv
        m = QMenu(cv)
        uri = (ln.get("uri") or "").strip() if ln.get("kind") == pymupdf.LINK_URI else ""
        go = QAction("Open link" if uri else "Go to target", m)
        go.triggered.connect(lambda: cv.follow_link(pg, ln))
        acts = [go]
        if uri:
            cp = QAction("Copy link address", m)
            cp.triggered.connect(lambda: QGuiApplication.clipboard().setText(uri))
            acts.append(cp)
        tool = getattr(getattr(self.win, "structure", None), "tool", None)
        acts.append(None)
        if tool is not None:
            ed = QAction("Edit link…", m)
            ed.triggered.connect(lambda: tool.edit(pg.ref, ln))
            acts.append(ed)
        de = QAction("Delete link", m)
        de.triggered.connect(lambda: LK.delete_link(cv, pg.ref, ln))
        acts.append(de)
        _add(m, acts)
        return m

    def page_menu(self, pg, lp: QPointF) -> QMenu:
        win, cv = self.win, self.cv
        m = QMenu(cv)
        note = QAction("Add sticky note here", m)
        note.triggered.connect(lambda: cv.annots.add_note(pg, lp))
        more = QMenu("Pa&ge", m)
        _add(more, [_a(win, "a_copy_pages"), _a(win, "a_duplicate_pages"), None, _a(win, "a_blank_after"),
                    _a(win, "a_insert_file"), _a(win, "a_extract"), None, _a(win, "a_del_pages")])
        _add(m, [note, _a(win, "a_select_all_items"), None, _a(win, "a_rot_l"), _a(win, "a_rot_r")])
        if more.actions():
            m.addMenu(more)
        _add(m, [None, _a(win, "a_find"), _a(win, "a_print"), None, _a(win, "a_doc_props")])
        return m

    # ------------------------------------------------------------------ helpers
    def find(self, text: str) -> None:
        bar = self.win.find
        bar.open_bar()
        bar.edit.setText(text.split("\n")[0][:200])
        bar._start()

    def reply(self, item) -> None:
        text, ok = QInputDialog.getMultiLineText(self.win, "Reply", "Your reply:")
        if ok and text.strip():
            self.cv.annots.add_reply(item, text.strip())

    def show_style(self) -> None:
        dock = getattr(self.win, "props_dock", None)
        if dock is not None:
            dock.show()
            dock.raise_()


# ====================================================================== panels
def comments_menu(win, panel, pos) -> QMenu | None:
    node = panel.tree.itemAt(pos)
    it = panel.item_of(node) if node is not None else None
    if it is None:
        return None
    panel.tree.setCurrentItem(node)
    m = QMenu(panel)
    go = m.addAction("Show on page", lambda: panel.mgr.select(it))
    m.addAction("Edit comment…", lambda: panel.mgr.open_editor(it))
    m.addAction("Reply", lambda: (panel.reply.setFocus(), panel.reply.selectAll()))
    contents = (it.meta or {}).get("contents") or getattr(it, "marked_text", "")
    cp = m.addAction("Copy text", lambda: QGuiApplication.clipboard().setText(contents))
    cp.setEnabled(bool(contents))
    m.addSeparator()
    m.addAction("Delete", panel.delete_current)
    m.setDefaultAction(go)
    return m


def attachments_menu(win, panel, pos) -> QMenu:
    node = panel.tree.itemAt(pos)
    if node is not None:
        panel.tree.setCurrentItem(node)
        node.setSelected(True)
    row = panel.current_row() if node is not None else None
    m = QMenu(panel)
    if row is not None:
        o = m.addAction("Open", lambda: panel.open_row(row))
        m.addAction("Save as…", lambda: panel.save_row(row))
        m.addAction("Rename…", lambda: panel.edit_row(row))
        m.addSeparator()
        m.addAction("Remove", lambda: panel.remove_row(row))
        m.addSeparator()
        m.setDefaultAction(o)
    m.addAction("Add files…", panel.add_dialog)
    return m


def _hook(widget, build) -> None:
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(pos):
        m = build(pos)
        if m is not None and m.actions():
            _exec(m, widget.viewport().mapToGlobal(pos) if hasattr(widget, "viewport") else widget.mapToGlobal(pos))
            m.deleteLater()

    widget.customContextMenuRequested.connect(show)


def install(win) -> None:
    win.canvas_menu = CanvasMenu(win)
    win.rows_override = None
    dock = getattr(win, "comments_dock", None)
    panel = getattr(dock, "panel", None)
    if panel is not None:
        _hook(panel.tree, lambda pos: comments_menu(win, panel, pos))
    att = getattr(win, "attachments", None)
    if att is not None and hasattr(att, "tree"):
        _hook(att.tree, lambda pos: attachments_menu(win, att, pos))
