"""Comments panel: every annotation (the file's own and new ones) in page order, threaded replies, filters,
inline comment editing, replies and delete. Lives in a dock; follows the undo stack live."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QStyledItemDelegate,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import annots as A
from .icons import icon
from .items import ReplyItem

ROLE = Qt.ItemDataRole.UserRole
ICONS = {
    "note": "note", "reply": "comment", "highlight": "highlight", "underline": "highlight", "strikeout": "highlight",
    "squiggly": "highlight", "rect": "rect", "ellipse": "ellipse", "line": "line", "arrow": "arrow", "ink": "ink",
    "freetext": "text", "stamp": "signature",
}


def _one_line(s: str, n: int = 140) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class _WrapDelegate(QStyledItemDelegate):
    """Row height for word-wrapped text at the width the row really gets. The default size hint measures with no
    width (one unwrapped line per paragraph), so when "Type · author   date" wraps in a narrow panel the comment
    line under it is pushed out of the row and hidden."""

    def sizeHint(self, opt, idx):
        view = opt.widget
        if view is None or not isinstance(view, QTreeWidget):
            return super().sizeHint(opt, idx)
        depth, p = 0, idx.parent()
        while p.isValid():
            depth, p = depth + 1, p.parent()
        width = view.viewport().width() - view.indentation() * (depth + (1 if view.rootIsDecorated() else 0))
        o = type(opt)(opt)
        o.rect = QRect(0, 0, max(40, width), 1)  # a valid rect: the style wraps the text to its width
        return super().sizeHint(o, idx)


class CommentsPanel(QWidget):
    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.mgr = canvas.annots
        self._objs: dict[int, object] = {}  # id(item) -> annotation item shown in the tree
        self._syncing = False
        self._current = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.type_box = QComboBox()
        self.type_box.setToolTip("Show one kind of annotation")
        self.author_box = QComboBox()
        self.author_box.setToolTip("Show one author's comments")
        for b in (self.type_box, self.author_box):
            b.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            b.setMinimumContentsLength(6)
            b.currentIndexChanged.connect(lambda _=0: self.rebuild())
            row.addWidget(b, 1)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Comments")  # a11y: the header is hidden, so the list has no other name
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setWordWrap(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setItemDelegate(_WrapDelegate(self.tree))
        self.tree.viewport().installEventFilter(self)  # re-measure wrapped rows when the panel's width changes
        self._last_w = -1
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemClicked.connect(lambda it, _c: self._show(it))
        self.tree.itemActivated.connect(self._activated)
        self.tree.installEventFilter(self)
        lay.addWidget(self.tree, 1)

        self.count = QLabel()
        self.count.setEnabled(False)
        lay.addWidget(self.count)

        # details of the current annotation: comment, reply, delete
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(4)
        head = QHBoxLayout()
        self.head = QLabel()
        self.head.setWordWrap(True)
        head.addWidget(self.head, 1)
        self.del_btn = QToolButton()
        self.del_btn.setIcon(icon("delete"))
        self.del_btn.setAutoRaise(True)
        self.del_btn.setToolTip("Delete this annotation and its replies")
        self.del_btn.clicked.connect(self.delete_current)
        head.addWidget(self.del_btn)
        dl.addLayout(head)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Comment")
        self.edit.setToolTip("Edit the comment; saved when you leave the box or press Ctrl+Enter")
        self.edit.setMaximumHeight(96)
        self.edit.installEventFilter(self)
        dl.addWidget(self.edit)
        for k in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(k), self.edit, self.commit, context=Qt.ShortcutContext.WidgetShortcut)
        rr = QHBoxLayout()
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("Reply…")
        self.reply.returnPressed.connect(self.send_reply)
        self.reply_btn = QPushButton("Reply")
        self.reply_btn.clicked.connect(self.send_reply)
        rr.addWidget(self.reply, 1)
        rr.addWidget(self.reply_btn)
        dl.addLayout(rr)
        lay.addWidget(self.detail)

        self._timer = QTimer(self, singleShot=True, interval=0, timeout=self.rebuild)
        self.mgr.changed.connect(self._timer.start)
        canvas.documentChanged.connect(self._timer.start)
        canvas.selectionInfo.connect(self._canvas_selection)
        self.rebuild()

    # -------------------------------------------------------------- model
    def entries(self) -> list[tuple[int, object]]:
        return self.mgr.items() if self.canvas.doc is not None else []

    def _fill_filters(self, entries) -> None:
        kinds = sorted({A.type_name(it) for _, it in entries if not isinstance(it, ReplyItem)})
        authors = sorted({A.author_of(it, self.mgr.author) for _, it in entries} - {""}, key=str.lower)
        for box, values, everything in ((self.type_box, kinds, "All types"), (self.author_box, authors, "All authors")):
            cur = box.currentData()
            box.blockSignals(True)
            box.clear()
            box.addItem(everything, None)
            for v in values:
                box.addItem(v, v)
            i = box.findData(cur) if cur is not None else 0
            box.setCurrentIndex(max(0, i))
            box.blockSignals(False)

    def _text(self, it) -> str:
        meta = it.meta or {}
        who = A.author_of(it, self.mgr.author) or "Unknown"
        when = A.pdf_date_text(meta.get("modified") or meta.get("created") or "")
        first = f"{A.type_name(it)} · {who}" + (f"   {when}" if when else "")
        snip = _one_line(A.snippet_of(it))
        return f"{first}\n{snip}" if snip else first

    def rebuild(self) -> None:
        entries = self.entries()
        self._fill_filters(entries)
        want_type, want_author = self.type_box.currentData(), self.author_box.currentData()
        sel = self._current
        bar = self.tree.verticalScrollBar().value()
        self._syncing = True
        self.tree.clear()
        self._objs.clear()
        nodes: dict[int, QTreeWidgetItem] = {}
        pages: dict[int, QTreeWidgetItem] = {}
        shown = 0
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        roots = [(i, it) for i, it in entries if not isinstance(it, ReplyItem)]
        replies = [it for _, it in entries if isinstance(it, ReplyItem)]
        roots.sort(key=lambda e: (e[0], round(e[1].mapRectToParent(e[1].boundingRect()).top()), e[1].zValue()))
        for i, it in roots:
            if want_type is not None and A.type_name(it) != want_type:
                continue
            if want_author is not None and A.author_of(it, self.mgr.author) != want_author:
                continue
            page = pages.get(i)
            if page is None:
                page = pages[i] = QTreeWidgetItem(self.tree, [f"Page {i + 1}"])
                page.setFont(0, bold)
                page.setFlags(Qt.ItemFlag.ItemIsEnabled)
                page.setFirstColumnSpanned(True)
            nodes[id(it)] = self._node(page, it)
            shown += 1
        pending = list(replies)
        while pending:  # replies under what they answer (which may itself be a reply)
            rest = [r for r in pending if id(r.target) not in nodes]
            for r in pending:
                if id(r.target) in nodes:
                    nodes[id(r)] = self._node(nodes[id(r.target)], r)
            if len(rest) == len(pending):
                break
            pending = rest
        self.tree.expandAll()
        self.tree.verticalScrollBar().setValue(bar)
        if sel is not None and id(sel) in nodes:
            self.tree.setCurrentItem(nodes[id(sel)])
        else:
            self._current = None
        self._syncing = False
        if self.mgr.busy():
            self.count.setText("Looking for comments…")
        else:
            total = sum(1 for _, it in entries if not isinstance(it, ReplyItem))
            self.count.setText(f"{shown} of {total} annotations" if shown != total else
                               f"{total} annotation{'s' if total != 1 else ''}")
        self._show_detail()

    def _node(self, parent, it) -> QTreeWidgetItem:
        n = QTreeWidgetItem(parent, [self._text(it)])
        n.setIcon(0, icon(ICONS.get(it.kind, "comment")))
        n.setData(0, ROLE, id(it))
        full = A.snippet_of(it)
        if full:
            n.setToolTip(0, full)
        self._objs[id(it)] = it
        return n

    def item_of(self, node) -> object | None:
        return self._objs.get(node.data(0, ROLE)) if node is not None else None

    def node_of(self, it):
        for n in self._walk():
            if n.data(0, ROLE) == id(it):
                return n
        return None

    def _walk(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.child(i) for i in range(n.childCount()))

    # --------------------------------------------------------- interaction
    def _current_changed(self, cur, _prev) -> None:
        if self._syncing:
            return
        self.commit()
        self._current = self.item_of(cur)
        self._show_detail()
        if self._current is not None:
            self._show(cur)

    def _show(self, node) -> None:
        it = self.item_of(node)
        if it is None or self._syncing:
            return
        self._syncing = True
        try:
            self.mgr.select(it)
        finally:
            self._syncing = False

    def _activated(self, node, _col=0) -> None:
        it = self.item_of(node)
        if it is not None:
            self.mgr.open_editor(it)

    def _canvas_selection(self) -> None:
        """Follow the page: selecting an annotation there selects it here."""
        if self._syncing or self.canvas.doc is None:
            return
        try:
            sel = [i for i in self.canvas.scene().selectedItems() if A.is_annotation(i)]
        except RuntimeError:
            return
        if len(sel) != 1 or sel[0] is self._current:
            return
        n = self.node_of(sel[0])
        if n is not None:
            self._syncing = True
            self.commit()
            self.tree.setCurrentItem(n)
            self._current = sel[0]
            self._syncing = False
            self._show_detail()

    def _show_detail(self) -> None:
        it = self._current
        on = it is not None
        self.detail.setEnabled(on)
        if not on:
            self.head.setText("Select a comment")
            self.edit.blockSignals(True)
            self.edit.setPlainText("")
            self.edit.blockSignals(False)
            return
        meta = it.meta or {}
        who = A.author_of(it, self.mgr.author) or "Unknown"
        self.head.setText(f"<b>{A.type_name(it)}</b> · {who}")
        if not self.edit.hasFocus():
            self.edit.setPlainText(meta.get("contents") or "")
        mark = getattr(it, "marked_text", "")
        self.edit.setPlaceholderText(f"“{_one_line(mark, 60)}”" if mark else "Comment")

    def commit(self) -> None:
        """Write the comment box into the current annotation (one undo step)."""
        it = self._current
        if it is None or it.parentItem() is None:
            return
        text = self.edit.toPlainText()
        if text != (it.meta.get("contents") or ""):
            self.mgr.set_comment(it, text)

    def send_reply(self) -> None:
        text = self.reply.text().strip()
        if not text or self._current is None:
            return
        self.commit()
        r = self.mgr.add_reply(self._current, text)
        if r is not None:
            self.reply.clear()
            self._current = r
            self._timer.start()

    def delete_current(self) -> None:
        it = self._current
        if it is not None:
            self._current = None
            self.mgr.delete([it])

    def eventFilter(self, obj, e) -> bool:
        if not hasattr(self, "edit"):  # still building
            return False
        if obj is self.tree.viewport() and e.type() == QEvent.Type.Resize:
            if e.size().width() != self._last_w:
                self._last_w = e.size().width()
                self.tree.scheduleDelayedItemsLayout()
            return False
        if obj is self.edit and e.type() == QEvent.Type.FocusOut:
            self.commit()
        elif obj is self.tree and e.type() == QEvent.Type.KeyPress and e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_current()
            return True
        return super().eventFilter(obj, e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.mgr.adopt_all()
        self._timer.start()
