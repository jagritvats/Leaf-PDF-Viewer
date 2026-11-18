"""Outline (bookmarks) panel: click to jump, follows the reading position, filterable, and editable.

Editing: add a bookmark at the current view (title from the selected text or the nearest heading), rename (F2),
delete, drag to reorder/nest (or Move in / Move out), set the destination to the current view. The edited outline
is a document-level setting, `PdfDocument.outline` (None = untouched: saving leaves the file's outline exactly as it
is; else a tuple of `Bookmark`), changed only through the undoable `SetOutline`. Bookmarks point at a page UID, so
they follow reordering; at save `write_outline` writes them with `set_toc` at the final page numbers. A bookmark whose
page was deleted is left out (kept as a plain heading, without a target, when it still has children).

Coordinates: points are in the target page's page.rect space (as `get_toc()` reports them); destinations are written
in PDF user space (leaf/links.py: to_pdf_point)."""
from __future__ import annotations

import bisect
import itertools
from dataclasses import dataclass, field, replace

import pymupdf
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QUndoCommand
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLineEdit, QMenu, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

DEST_ROLE = Qt.ItemDataRole.UserRole + 1
_ids = itertools.count(1)
EXTERNAL = (pymupdf.LINK_URI, pymupdf.LINK_GOTOR, pymupdf.LINK_LAUNCH)


@dataclass(frozen=True)
class Bookmark:
    title: str
    level: int = 1
    uid: int | None = None  # target page (PageRef.uid); None: no page target (external target, or none)
    point: tuple | None = None  # (x, y) in the target's source page.rect space; None = top of the page
    dest: dict | None = field(default=None, compare=False)  # external target as get_toc() gives it (URI, file, ...)
    open: bool = True  # children shown expanded in other viewers
    color: tuple | None = None
    bold: bool = False
    italic: bool = False
    id: int = field(default_factory=lambda: next(_ids))


def read_outline(src) -> list[tuple[int, str, int, QPointF | None, dict]]:
    """(level, title, source page index or -1, point in that page's page.rect space or None, dest)."""
    out = []
    try:
        toc = src.doc.get_toc(simple=False)
    except Exception:  # noqa: BLE001
        return out
    for entry in toc:
        lvl, title, pno = entry[0], entry[1], entry[2]
        dest = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
        idx = pno - 1 if isinstance(pno, int) and 0 < pno <= src.doc.page_count else -1
        pt = None
        to = dest.get("to")
        if idx >= 0 and to is not None and (to.x or to.y):
            # get_toc() already reports destinations in the (rotated) page.rect space overlays and pages use
            pt = QPointF(to.x, to.y)
        out.append((lvl, title, idx, pt, dest))
    return out


def source_marks(doc) -> list[Bookmark]:
    """The file's own outline as bookmarks (targets: the first page in the page list showing that source page)."""
    from .links import uid_for

    prim = doc.primary
    out = []
    for lvl, title, idx, pt, dest in read_outline(prim):
        uid = uid_for(doc, prim, idx) if idx >= 0 else None
        ext = dict(dest) if idx < 0 and dest.get("kind") in EXTERNAL else None
        color = dest.get("color")
        out.append(Bookmark(title, lvl, uid, (pt.x(), pt.y()) if pt is not None else None, ext,
                            not dest.get("collapse", False), tuple(color) if color else None,
                            bool(dest.get("bold")), bool(dest.get("italic"))))
    return normalized(out)


def _content(m: Bookmark) -> tuple:
    return m.title, m.level, m.uid, m.point, m.open, m.color, m.bold, m.italic, id(m.dest) if m.dest else None


def marks_of(doc) -> list[Bookmark]:
    return list(doc.outline) if doc.outline is not None else source_marks(doc)


def normalized(marks) -> list[Bookmark]:
    """Levels that make a valid tree: the first is 1, each at most one deeper than the one before."""
    out, prev = [], 0
    for m in marks:
        lvl = max(1, min(m.level, prev + 1))
        out.append(m if lvl == m.level else replace(m, level=lvl))
        prev = lvl
    return out


def subtree_end(marks, k: int) -> int:
    """Index after bookmark k's children."""
    j = k + 1
    while j < len(marks) and marks[j].level > marks[k].level:
        j += 1
    return j


def orphaned(doc) -> int:
    """Bookmarks whose target page is no longer in the document (left out when saving)."""
    if doc.outline is None:
        return 0
    have = {r.uid for r in doc.pages}
    return sum(1 for m in doc.outline if m.uid is not None and m.uid not in have)


# ------------------------------------------------------------------------------------------------------ save
def _external_action(dest: dict) -> str | None:
    kind = dest.get("kind")
    if kind == pymupdf.LINK_URI and dest.get("uri"):
        from .links import _ascii_uri
        return f"<</S/URI/URI{pymupdf.get_pdf_str(_ascii_uri(dest['uri']))}>>"
    if kind in (pymupdf.LINK_GOTOR, pymupdf.LINK_LAUNCH) and dest.get("file"):
        f = dest["file"]
        spec = f"<</Type/Filespec/F{pymupdf.get_pdf_str(f)}/UF{pymupdf.get_pdf_str(f)}>>"
        if kind == pymupdf.LINK_GOTOR:
            page = dest.get("page", 0)
            return f"<</S/GoToR/F{spec}/D[{max(0, int(page) if isinstance(page, int) else 0)}/Fit]>>"
        return f"<</S/Launch/F{spec}>>"
    return None


def write_outline(doc, out, pages: list) -> None:
    """Replace the output's outline with `doc.outline`, targets at their final page numbers."""
    from .links import goto_action

    pos: dict = {}
    for i, r in enumerate(pages):
        pos.setdefault(r.uid, i)
    marks = normalized(doc.outline or ())
    keep: list[tuple[Bookmark, int | None]] = []
    next_level = 0  # level of the next kept bookmark (walking backwards)
    for m in reversed(marks):
        gone = m.uid is not None and m.uid not in pos
        if gone and next_level <= m.level:  # deleted page and no children left: leave it out
            continue
        keep.append((m, None if gone or m.uid is None else pos[m.uid]))
        next_level = m.level
    keep.reverse()
    # dropping childless entries keeps the levels valid, but re-check (a dropped first entry, ...)
    rows, prev = [], 0
    for m, i in keep:
        lvl = max(1, min(m.level, prev + 1))
        prev = lvl
        d = {"kind": pymupdf.LINK_GOTO if i is not None else pymupdf.LINK_NONE, "collapse": not m.open}
        if m.color:
            d["color"] = m.color
        if m.bold:
            d["bold"] = True
        if m.italic:
            d["italic"] = True
        rows.append([lvl, m.title, i + 1 if i is not None else -1, d])
    out.set_toc(rows, collapse=0)
    if not rows:
        return
    xrefs = out.get_outline_xrefs()
    for (m, i), x in zip(keep, xrefs):
        out.xref_set_key(x, "Dest", "null")
        if i is not None:
            action = goto_action(doc, out, pages[i], i, m.point)
        else:
            action = _external_action(m.dest) if m.dest else None
        out.xref_set_key(x, "A", action or "null")


# --------------------------------------------------------------------------------------------------- command
class SetOutline(QUndoCommand):
    def __init__(self, panel, old, new, text: str):
        super().__init__(text)
        self.panel, self.old, self.new = panel, old, new

    def _set(self, v) -> None:
        self.panel.canvas.doc.outline = v
        self.panel.rebuild()
        self.panel.changed.emit()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


class _Tree(QTreeWidget):
    dropped = Signal()

    def dropEvent(self, e) -> None:
        super().dropEvent(e)
        self.dropped.emit()


# ----------------------------------------------------------------------------------------------------- panel
class OutlinePanel(QWidget):
    changed = Signal()  # the outline was edited (or an edit undone)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter outline")
        self.filter.setClearButtonEnabled(True)
        self.add_button = QToolButton()
        self.add_button.setText("+")
        self.add_button.setToolTip("Add a bookmark for the current view  (Ctrl+Shift+B)")
        self.add_button.setAutoRaise(True)
        self.add_button.clicked.connect(lambda: self.add_here(edit=True))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(2)
        top.addWidget(self.filter, 1)
        top.addWidget(self.add_button)
        self.tree = _Tree()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        lay.addLayout(top)
        lay.addWidget(self.tree)
        self.entries: list[tuple[QTreeWidgetItem, int, QPointF | None, dict]] = []
        self.marks: list[Bookmark] = []  # parallel to entries
        self._pos: list[tuple[float, int]] = []  # (scene y, entry index), sorted
        self._pos_dirty = True
        self._building = False
        self._track = QTimer(self, singleShot=True, interval=60, timeout=self._update_current)
        self.filter.textChanged.connect(self._apply_filter)
        self.tree.itemClicked.connect(self._activate)
        self.tree.itemActivated.connect(self._activate)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.dropped.connect(lambda: QTimer.singleShot(0, self._tree_changed))
        self.tree.customContextMenuRequested.connect(self._context)
        self.tree.installEventFilter(self)
        canvas.verticalScrollBar().valueChanged.connect(self._kick)  # (bound methods: disconnected when the
        canvas.pageChanged.connect(self._kick)  # panel is deleted before the canvas)
        canvas.layoutChanged.connect(self._layout_changed)
        canvas.pagesChanged.connect(self._pages_changed)

    def _kick(self, *_) -> None:
        self._track.start()

    def has_outline(self) -> bool:
        return bool(self.entries)

    def rebuild(self) -> None:
        open_ids = {self.marks[n].id for n in range(len(self.marks))
                    if n < len(self.entries) and self.entries[n][0].isExpanded()}
        edited_before = bool(self.marks) and self.canvas.doc is not None and self.canvas.doc.outline is not None
        self._building = True
        self.tree.clear()
        self.entries = []
        self.marks = []
        self._pos_dirty = True
        doc = self.canvas.doc
        if doc is None:
            self._building = False
            return
        stack: list[tuple[int, QTreeWidgetItem | None]] = [(0, None)]
        have = {r.uid: r for r in doc.pages}
        for m in marks_of(doc):
            while len(stack) > 1 and stack[-1][0] >= m.level:
                stack.pop()
            parent = stack[-1][1]
            it = QTreeWidgetItem([m.title.strip() or "(untitled)"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
                        | Qt.ItemFlag.ItemIsDropEnabled)
            ref = have.get(m.uid) if m.uid is not None else None
            tip = m.title
            if m.uid is not None and ref is None:
                tip += "\n(its page was deleted: left out when saving)"
                it.setForeground(0, QColor("#d93025"))
            it.setToolTip(0, tip)
            if m.bold or m.italic:
                f = QFont(it.font(0))
                f.setBold(m.bold)
                f.setItalic(m.italic)
                it.setFont(0, f)
            it.setData(0, DEST_ROLE, len(self.entries))
            if parent is None:
                self.tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            if (m.id in open_ids) if edited_before else (m.open and m.level <= 1):
                it.setExpanded(True)
            stack.append((m.level, it))
            idx = ref.index if ref is not None and ref.src is doc.primary else -1
            pt = QPointF(*m.point) if m.point is not None else None
            self.entries.append((it, idx, pt, m.dest or {}))
            self.marks.append(m)
        if not edited_before:
            for i in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(i).setExpanded(True)
        self._building = False
        self._apply_filter(self.filter.text())
        self._track.start()

    # ------------------------------------------------------------ navigate
    def _activate(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        n = item.data(0, DEST_ROLE)
        if n is None:
            return
        self.navigate(n)

    def navigate(self, n: int) -> bool:
        m = self.marks[n]
        cv = self.canvas
        if m.uid is None:
            dest = m.dest or {}
            if dest.get("kind") in EXTERNAL:
                cv.follow_link(None, dest)
                return True
            return False
        i = next((k for k, it in enumerate(cv.order) if it.ref.uid == m.uid), None)
        if i is None:
            cv.message.emit("That section's page isn't in this document any more.")
            return False
        cv.go_to_point(i, QPointF(*m.point) if m.point is not None else None)
        cv.setFocus()
        return True

    # ------------------------------------------------------------- filter
    def _apply_filter(self, text: str) -> None:
        q = text.strip().casefold()
        self.tree.setDragEnabled(not q)  # moving within a filtered tree would be guesswork

        def walk(it: QTreeWidgetItem) -> bool:
            child_hit = False
            for k in range(it.childCount()):
                child_hit = walk(it.child(k)) or child_hit
            hit = not q or q in it.text(0).casefold()
            it.setHidden(not (hit or child_hit))
            if q and child_hit:
                it.setExpanded(True)
            return hit or child_hit

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    # ------------------------------------------------------ current section
    def _layout_changed(self) -> None:
        self._pos_dirty = True
        self._track.start()

    def _pages_changed(self) -> None:
        if self.canvas.doc is not None and self.canvas.doc.outline is not None:
            self.rebuild()  # deleted/restored target pages change how entries look
        else:
            self._layout_changed()

    def _positions(self) -> list[tuple[float, int]]:
        if self._pos_dirty:
            cv = self.canvas
            where = {}
            if cv.doc is not None:
                where = {it.ref.uid: i for i, it in reversed(list(enumerate(cv.order)))}
            pos = []
            for n, m in enumerate(self.marks):
                i = where.get(m.uid) if m.uid is not None else None
                if i is None:
                    continue
                pg = cv.order[i]
                y = pg.content.mapToScene(QPointF(*m.point)).y() if m.point is not None else pg.y()
                pos.append((y, n))
            pos.sort()
            self._pos = pos
            self._pos_dirty = False
        return self._pos

    def current_entry(self) -> int | None:
        cv = self.canvas
        pos = self._positions()
        if not pos or not cv.order:
            return None
        vr = cv.mapToScene(cv.viewport().rect()).boundingRect()
        y = vr.top() + min(vr.height() * 0.25, 120 / max(cv.zoom, 1e-3))
        k = bisect.bisect_right(pos, (y, len(self.entries))) - 1
        return pos[k][1] if k >= 0 else None

    def _update_current(self) -> None:
        if not self.entries or not self.isVisible() or self.tree.state() == QAbstractItemView.State.EditingState:
            return
        n = self.current_entry()
        item = self.entries[n][0] if n is not None else None
        if item is self.tree.currentItem():
            return
        self.tree.blockSignals(True)
        if item is None:
            self.tree.clearSelection()
        else:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        self.tree.blockSignals(False)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._track.start()

    # ------------------------------------------------------------- editing
    def _push(self, new: list[Bookmark], text: str) -> None:
        doc = self.canvas.doc
        if doc is None:
            return
        new = tuple(normalized(new))
        if [_content(m) for m in new] == [_content(m) for m in self.marks]:
            return  # nothing changed (the file's own outline stays untouched)
        self.canvas.undo.push(SetOutline(self, doc.outline, new, text))

    def current_index(self) -> int | None:
        it = self.tree.currentItem()
        if it is None or not it.isSelected():
            return None
        n = it.data(0, DEST_ROLE)
        return n if n is not None and 0 <= n < len(self.marks) else None

    def view_target(self):
        """(page uid, point or None) of the top of the current view."""
        cv = self.canvas
        if cv.doc is None or not cv.order:
            return None
        vr = cv.mapToScene(cv.viewport().rect()).boundingRect()
        y = vr.top() + 2
        pg = None
        for it in cv.order:
            r = it.sceneBoundingRect()
            if it.isVisible() and r.top() - 12 <= y <= r.bottom() and r.right() >= vr.left() and r.left() <= vr.right():
                pg = it
                break
        if pg is None:
            pg = cv.current_page_item()
        if pg is None:
            return None
        sr = pg.sceneBoundingRect()
        lp = pg.content.mapFromScene(QPointF(sr.center().x(), max(y, sr.top())))
        cr = pg.content.rect()
        if lp.y() <= cr.top() + 4:
            return pg.ref.uid, None
        return pg.ref.uid, (cr.left(), min(lp.y(), cr.bottom()))

    def suggested_title(self, uid, point) -> str:
        cv = self.canvas
        text = (cv.selected_text() or "").strip().splitlines()
        if text and text[0].strip():
            return text[0].strip()[:120]
        ref = next((r for r in cv.doc.pages if r.uid == uid), None)
        if ref is None:
            return "Bookmark"
        h = nearest_heading(cv.doc, ref, point[1] if point else None)
        if h:
            return h
        return f"Page {cv.doc.pages.index(ref) + 1}"

    def add_here(self, title: str | None = None, child: bool = False, edit: bool = False) -> int | None:
        """Add a bookmark for the current view after the selected one (or at the end). Returns its index."""
        t = self.view_target()
        if t is None:
            return None
        uid, point = t
        marks = list(self.marks)
        k = self.current_index()
        title = title or self.suggested_title(uid, point)
        if k is None:
            at, level = len(marks), 1
        elif child:
            at, level = subtree_end(marks, k), marks[k].level + 1
        else:
            at, level = subtree_end(marks, k), marks[k].level
        m = Bookmark(title, level, uid, point)
        marks.insert(at, m)
        self._push(marks, "Add bookmark")
        n = next((i for i, x in enumerate(self.marks) if x.id == m.id), None)
        if n is not None:
            it = self.entries[n][0]
            self.tree.setCurrentItem(it)
            self.tree.scrollToItem(it)
            if edit:
                self.tree.editItem(it, 0)
        return n

    def rename(self, n: int, title: str) -> None:
        title = title.strip()
        if not title or title == self.marks[n].title:
            self.rebuild()  # puts the shown text back
            return
        marks = list(self.marks)
        marks[n] = replace(marks[n], title=title)
        self._push(marks, "Rename bookmark")

    def delete(self, n: int) -> None:
        marks = list(self.marks)
        end = subtree_end(marks, n)
        k = end - n
        del marks[n:end]
        self._push(marks, "Delete bookmark" if k == 1 else f"Delete {k} bookmarks")

    def set_destination(self, n: int) -> None:
        t = self.view_target()
        if t is None:
            return
        marks = list(self.marks)
        marks[n] = replace(marks[n], uid=t[0], point=t[1], dest=None)
        self._push(marks, "Set bookmark destination")

    def indent(self, n: int, d: int) -> None:
        """Move bookmark n (with its children) one level in (d=1, under the bookmark above) or out (d=-1)."""
        marks = list(self.marks)
        end = subtree_end(marks, n)
        lvl = marks[n].level
        if d > 0 and (n == 0 or marks[n - 1].level < lvl):
            return  # no sibling above to nest under
        if d < 0 and lvl <= 1:
            return
        for j in range(n, end):
            marks[j] = replace(marks[j], level=marks[j].level + d)
        self._push(marks, "Move bookmark in" if d > 0 else "Move bookmark out")

    def move(self, n: int, to: int) -> None:
        """Move bookmark n with its children so it becomes the `to`-th bookmark among the others (same level)."""
        marks = list(self.marks)
        end = subtree_end(marks, n)
        block = marks[n:end]
        rest = marks[:n] + marks[end:]
        to = max(0, min(to, len(rest)))
        self._push(rest[:to] + block + rest[to:], "Move bookmark")

    def _tree_changed(self) -> None:
        """After a drag in the tree: read the new order and nesting back."""
        if self.canvas.doc is None:
            return
        marks = []

        def walk(it: QTreeWidgetItem, depth: int) -> None:
            n = it.data(0, DEST_ROLE)
            if n is not None and 0 <= n < len(self.marks):
                marks.append(replace(self.marks[n], level=depth))
            for k in range(it.childCount()):
                walk(it.child(k), depth + 1)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), 1)
        if [(m.id, m.level) for m in marks] == [(m.id, m.level) for m in self.marks]:
            return
        self._push(marks, "Move bookmark")
        self.rebuild()  # (no-op pushes still need the tree back in model order)

    def _item_changed(self, it: QTreeWidgetItem, _col: int) -> None:
        if self._building:
            return
        n = it.data(0, DEST_ROLE)
        if n is None or not 0 <= n < len(self.marks):
            return
        text = it.text(0)
        if text.strip() != self.marks[n].title.strip():
            QTimer.singleShot(0, lambda n=n, text=text: self.rename(n, text) if n < len(self.marks) else None)

    def eventFilter(self, obj, e) -> bool:
        if obj is self.tree and e.type() == QEvent.Type.KeyPress and \
                self.tree.state() != QAbstractItemView.State.EditingState:
            n = self.current_index()
            if n is not None and e.key() == Qt.Key.Key_Delete:
                self.delete(n)
                return True
            if n is not None and e.key() == Qt.Key.Key_F2:
                self.tree.editItem(self.entries[n][0], 0)
                return True
        return super().eventFilter(obj, e)

    def _context(self, pos) -> None:
        it = self.tree.itemAt(pos)
        if it is not None:
            self.tree.setCurrentItem(it)
        n = self.current_index() if it is not None else None
        m = QMenu(self)
        a_add = m.addAction("Add bookmark here", lambda: self.add_here(edit=True))
        a_add.setShortcut(QKeySequence("Ctrl+Shift+B"))
        if n is not None:
            m.addAction("Add child bookmark here", lambda: self.add_here(child=True, edit=True))
            m.addSeparator()
            r = m.addAction("Rename", lambda: self.tree.editItem(self.entries[n][0], 0))
            r.setShortcut(QKeySequence("F2"))
            m.addAction("Set destination to current view", lambda: self.set_destination(n))
            m.addSeparator()
            a_in = m.addAction("Move in", lambda: self.indent(n, 1))
            a_in.setEnabled(n > 0 and self.marks[n - 1].level >= self.marks[n].level)
            a_out = m.addAction("Move out", lambda: self.indent(n, -1))
            a_out.setEnabled(self.marks[n].level > 1)
            m.addSeparator()
            d = m.addAction("Delete", lambda: self.delete(n))
            d.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        for a in m.actions():
            a.setShortcutVisibleInContextMenu(True)
        m.exec(self.tree.viewport().mapToGlobal(pos))


def nearest_heading(doc, ref, y: float | None) -> str:
    """The most heading-like line at or below `y` (page.rect space; None = top) in the upper part of the view:
    the biggest (bold counts a little) text, nearest first. Empty if the page has no text there."""
    try:
        page = doc.text_page(ref)
        m = page.rotation_matrix
        data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    except Exception:  # noqa: BLE001
        return ""
    y0 = (y or 0.0) - 2
    span = page.rect.height * 0.5
    lines, sizes = [], []
    for b in data.get("blocks", []):
        for ln in b.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = " ".join("".join(s["text"] for s in spans).split())
            size = max(s["size"] for s in spans)
            sizes.append(size)
            r = pymupdf.Rect(ln["bbox"]) * m
            if r.y1 < y0 or r.y0 > y0 + span or len(text) > 120:
                continue
            bold = any(s.get("flags", 0) & 16 for s in spans)
            lines.append((size + (1.5 if bold else 0), r.y0, text))
    if not lines:
        return ""
    sizes.sort()
    body = sizes[len(sizes) // 2]
    big = [ln for ln in lines if ln[0] > body * 1.15]
    if big:
        best = max(big, key=lambda ln: (ln[0], -ln[1]))
    else:
        best = min(lines, key=lambda ln: ln[1])
    return best[2][:120]
