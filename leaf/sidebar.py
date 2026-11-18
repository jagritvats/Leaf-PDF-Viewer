"""Page thumbnails: lazy rendering, multi-select, block drag-reorder, keyboard, context actions.

The same widget serves as the narrow Pages panel (list) and the full-window organizer (wrapping grid)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QMimeData, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QListView, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate,
)

THUMB_W = 116
UID_ROLE = Qt.ItemDataRole.UserRole + 1
ASPECT_ROLE = Qt.ItemDataRole.UserRole + 2  # display height / width
LABEL_ROLE = Qt.ItemDataRole.UserRole + 3  # PDF page label, if it differs from the number
PAGES_MIME = "application/x-leaf-page-uids"
SIZES = {False: (64, 320, THUMB_W), True: (96, 560, 200)}  # (min, max, default) per mode (list, grid)
TEXT_H = 34
NO_UPDATE = QItemSelectionModel.SelectionFlag.NoUpdate


class ThumbDelegate(QStyledItemDelegate):
    def __init__(self, view: "Sidebar"):
        super().__init__(view)
        self.view = view

    def _box_size(self, index) -> tuple[int, int]:
        tw = self.view.thumb_w
        a = index.data(ASPECT_ROLE) or 1.3
        if self.view.grid:  # fit into a square cell so rows line up
            return (int(tw / a), tw) if a >= 1 else (tw, int(tw * a))
        return tw, int(tw * a)

    def sizeHint(self, opt, index) -> QSize:
        tw = self.view.thumb_w
        if self.view.grid:
            return QSize(tw + 28, tw + TEXT_H + 16)
        _, h = self._box_size(index)
        return QSize(tw + 28, h + TEXT_H + 14)

    def paint(self, p: QPainter, opt, index) -> None:
        p.save()
        r = opt.rect
        pal = opt.palette
        uid = index.data(UID_ROLE)
        w, h = self._box_size(index)
        top = r.y() + 8 + ((self.view.thumb_w - h) // 2 if self.view.grid else 0)
        box = QRect(r.x() + (r.width() - w) // 2, top, w, h)
        hl = pal.color(QPalette.ColorRole.Highlight)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        current = uid == self.view.current_uid
        if selected:
            fill = QColor(hl)
            fill.setAlpha(40)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r.adjusted(3, 2, -3, -2), 6, 6)
            p.setPen(QPen(hl, 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(-4, -4, 4, 4), 4, 4)
        elif current:
            p.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(-4, -4, 4, 4), 4, 4)
        p.fillRect(box.translated(1, 1), QColor(0, 0, 0, 50))
        p.fillRect(box, Qt.GlobalColor.white)
        img = self.view.thumb(uid)
        if img is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(box, img)
        # hairline edge so white pages stand out on a light sidebar
        p.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(box).adjusted(0.5, 0.5, -0.5, -0.5))
        num = str(index.row() + 1)
        f = QFont(opt.font)
        f.setBold(current)
        p.setFont(f)
        tr = QRect(r.x(), box.bottom() + 6, r.width(), 16)
        if current:  # pill behind the current page's number
            fm = p.fontMetrics()
            pw = fm.horizontalAdvance(num) + 14
            pill = QRect(tr.center().x() - pw // 2 + 1, tr.y(), pw, tr.height())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hl)
            p.drawRoundedRect(pill, 8, 8)
            p.setPen(pal.color(QPalette.ColorRole.HighlightedText))
        else:
            p.setPen(pal.color(QPalette.ColorRole.WindowText))
        p.drawText(tr, int(Qt.AlignmentFlag.AlignCenter), num)
        if opt.state & QStyle.StateFlag.State_HasFocus and self.view.hasFocus():  # a11y: visible keyboard focus
            p.setPen(QPen(hl, 1.5, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(r).adjusted(2.5, 1.5, -2.5, -1.5), 6, 6)
        label = index.data(LABEL_ROLE)
        if label:
            f.setBold(False)
            f.setPointSizeF(max(6.0, f.pointSizeF() * 0.85))
            p.setFont(f)
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            lr = QRect(r.x() + 2, tr.bottom() + 1, r.width() - 4, 14)
            p.drawText(lr, int(Qt.AlignmentFlag.AlignCenter), p.fontMetrics().elidedText(label, Qt.TextElideMode.ElideMiddle, lr.width()))
        p.restore()


class Sidebar(QListWidget):
    orderChanged = Signal(list)  # list of uids in new order
    pageActivated = Signal(int)
    filesDropped = Signal(list, int)  # paths, insert row
    contextRequested = Signal(object)  # global pos
    command = Signal(str)  # "delete", "copy", "cut", "paste", "duplicate"
    pagesDropped = Signal(object, int)  # source Sidebar (another document), insert row
    thumbSizeChanged = Signal(int)

    def __init__(self, canvas, parent=None, grid: bool = False):
        super().__init__(parent)
        self.canvas = canvas
        self.grid = grid
        lo, hi, default = SIZES[grid]
        try:
            tw = int(QSettings().value(self._size_key(), default))
        except (TypeError, ValueError):
            tw = default
        self.thumb_w = max(lo, min(hi, tw))
        self._thumbs: dict[int, tuple] = {}  # uid -> (rotation, width px, QImage)
        self._refs: dict = {}
        self.current_uid: int | None = None
        self._drop_row: int | None = None
        self.setItemDelegate(ThumbDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(False)  # drawn by paintEvent, also in grid mode
        self.setAcceptDrops(True)
        self.setUniformItemSizes(grid)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        if grid:
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setMovement(QListView.Movement.Static)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
            self.setResizeMode(QListView.ResizeMode.Adjust)
            self.setSpacing(6)
        else:
            self.setMinimumWidth(THUMB_W + 40)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.contextRequested.emit(self.viewport().mapToGlobal(pos)))
        self.itemClicked.connect(self._clicked)
        self.itemActivated.connect(lambda it: self.pageActivated.emit(self.row(it)))
        self._syncing = False

    def _size_key(self) -> str:
        return "pages/thumb_grid" if self.grid else "pages/thumb_list"

    def _clicked(self, it) -> None:
        if not (self._mods() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.pageActivated.emit(self.row(it))

    @staticmethod
    def _mods():
        return QApplication.keyboardModifiers()


    # ----------------------------------------------------------- population
    def rebuild(self) -> None:
        doc = self.canvas.doc
        self._syncing = True
        sel = set(self.selected_uids())
        cur = self.currentItem().data(UID_ROLE) if self.currentItem() else None
        scroll = self.verticalScrollBar().value()
        self.clear()
        self._refs = {r.uid: r for r in doc.pages} if doc is not None else {}
        for uid in [u for u in self._thumbs if u not in self._refs]:
            del self._thumbs[uid]
        if doc is None:
            self._syncing = False
            return
        from .pageops import page_label, page_tooltip

        for i, ref in enumerate(doc.pages):
            dw, dh = doc.display_size(ref)
            it = QListWidgetItem()
            it.setData(UID_ROLE, ref.uid)
            it.setData(ASPECT_ROLE, dh / max(dw, 1))
            label = page_label(doc, ref, i)
            if label:
                it.setData(LABEL_ROLE, label)
            it.setToolTip(page_tooltip(doc, ref, i))
            # a11y: thumbnails have no display text; screen readers read this instead
            it.setData(Qt.ItemDataRole.AccessibleTextRole, f"Page {i + 1}" + (f" ({label})" if label else ""))
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
            self.addItem(it)
            if ref.uid in sel:
                it.setSelected(True)
            if ref.uid == cur:
                self.setCurrentItem(it, NO_UPDATE)
        self.verticalScrollBar().setValue(scroll)
        self._syncing = False

    def row_of(self, uid: int) -> int:
        for i in range(self.count()):
            if self.item(i).data(UID_ROLE) == uid:
                return i
        return -1

    def set_current(self, row: int) -> None:
        if 0 <= row < self.count():
            it = self.item(row)
            self.current_uid = it.data(UID_ROLE)
            if not it.isSelected() and len(self.selectedItems()) <= 1:
                self._syncing = True
                self.blockSignals(True)
                self.setCurrentItem(it)
                self.blockSignals(False)
                self._syncing = False
            self.scrollToItem(it, QAbstractItemView.ScrollHint.EnsureVisible)
            self.viewport().update()

    def selected_rows(self) -> list[int]:
        return sorted(self.row(it) for it in self.selectedItems())

    def selected_uids(self) -> list[int]:
        return [self.item(r).data(UID_ROLE) for r in self.selected_rows()]

    def select_uids(self, uids) -> None:
        uids = set(uids)
        self._syncing = True
        self.blockSignals(True)
        self.clearSelection()
        first = None
        for i in range(self.count()):
            it = self.item(i)
            if it.data(UID_ROLE) in uids:
                it.setSelected(True)
                if first is None:
                    first = it
                    self.setCurrentItem(it, NO_UPDATE)
        self.blockSignals(False)
        self._syncing = False
        if first is not None:
            self.scrollToItem(first, QAbstractItemView.ScrollHint.EnsureVisible)
        self.viewport().update()

    def uids(self) -> list[int]:
        return [self.item(i).data(UID_ROLE) for i in range(self.count())]

    # ----------------------------------------------------------- thumbnails
    def thumb(self, uid: int) -> QImage | None:
        ref = self._refs.get(uid)
        if ref is None:
            return None
        entry = self._thumbs.get(uid)
        want = self._px_width()
        r = self.canvas.renderer
        key = (id(self), uid)
        if entry is None or entry[0] != (ref.rotation, ref.crop):  # crop: optimize
            if not r.thumb_queued(key, ("t", id(self), uid)):
                r.want_thumb(key, lambda: self._render(ref))
            return None
        if abs(entry[1] - want) > want * 0.15:  # size changed: keep showing the old one until the new arrives
            if not r.thumb_queued(key, ("t", id(self), uid)):
                r.want_thumb(key, lambda: self._render(ref))
        return entry[2]

    def _px_width(self) -> int:
        return max(16, int(self.thumb_w * self.devicePixelRatioF()))

    def _render(self, ref) -> None:
        if self.canvas.doc is None or self._refs.get(ref.uid) is not ref:
            return
        want = self._px_width()
        entry = self._thumbs.get(ref.uid)
        if entry is not None and entry[0] == (ref.rotation, ref.crop) and abs(entry[1] - want) <= want * 0.15:
            return
        dw, dh = self.canvas.doc.display_size(ref)
        scale = want / max(dw, dh, 1) if self.grid else want / max(dw, 1)
        rot = (ref.rotation, ref.crop)

        def done(img: QImage) -> None:
            if self._refs.get(ref.uid) is ref:
                self._thumbs[ref.uid] = (rot, want, img)
                self.viewport().update()

        # off the GUI thread when the render worker is running
        self.canvas.renderer.render_async(("t", id(self), ref.uid), ref, scale, done)

    def clear_thumbs(self) -> None:
        self._thumbs.clear()

    def set_thumb_width(self, w: int) -> None:
        lo, hi, _ = SIZES[self.grid]
        w = max(lo, min(hi, int(w)))
        if w == self.thumb_w:
            return
        self.thumb_w = w
        QSettings().setValue(self._size_key(), w)
        if not self.grid:
            self.setMinimumWidth(min(w, THUMB_W) + 40)
        self.scheduleDelayedItemsLayout()
        self.viewport().update()
        self.thumbSizeChanged.emit(w)

    def wheelEvent(self, e) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            d = e.angleDelta().y()
            if d:
                step = max(8, self.thumb_w // 8)
                self.set_thumb_width(self.thumb_w + (step if d > 0 else -step))
            e.accept()
            return
        super().wheelEvent(e)

    # ------------------------------------------------------------- keyboard
    _KEYS = {
        (Qt.Key.Key_Delete, False): "delete", (Qt.Key.Key_Backspace, False): "delete",
        (Qt.Key.Key_C, True): "copy", (Qt.Key.Key_X, True): "cut", (Qt.Key.Key_V, True): "paste",
        (Qt.Key.Key_D, True): "duplicate",
    }

    def _key_name(self, e) -> str | None:
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if shift:
            return None
        k = e.key()
        name = self._KEYS.get((k, ctrl))
        if name:
            return name
        if ctrl and k in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
            return "move"
        if ctrl and k == Qt.Key.Key_A:
            return "selectall"
        if not ctrl and k in (Qt.Key.Key_Home, Qt.Key.Key_End):
            return "nav"
        return None

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ShortcutOverride and self._key_name(e):
            e.accept()  # these keys mean page operations here, not the window-wide shortcuts
            return True
        return super().event(e)

    def keyPressEvent(self, e) -> None:
        name = self._key_name(e)
        if name in ("delete", "copy", "cut", "paste", "duplicate"):
            self.command.emit(name)
            return
        if name == "selectall":
            self.selectAll()
            return
        if name == "move":
            k = e.key()
            if k == Qt.Key.Key_Home:
                self.move_selected_to(0)
            elif k == Qt.Key.Key_End:
                self.move_selected_to(self.count())
            else:
                self.move_selected_by(-1 if k in (Qt.Key.Key_Up, Qt.Key.Key_Left) else 1)
            return
        if name == "nav":
            row = 0 if e.key() == Qt.Key.Key_Home else self.count() - 1
            if row >= 0:
                self.setCurrentRow(row)
                self.pageActivated.emit(row)
            return
        before = self.currentRow()
        super().keyPressEvent(e)
        row = self.currentRow()
        # arrow/page keys follow in the document; Shift/Ctrl+arrows only extend the selection
        if row != before and row >= 0 and not e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.pageActivated.emit(row)

    def move_selected_by(self, delta: int) -> None:
        from .pageops import move_by

        uids = self.uids()
        new = move_by(uids, set(self.selected_uids()), delta)
        if new != uids:
            self.orderChanged.emit(new)

    def move_selected_to(self, row: int) -> None:
        from .pageops import move_block

        uids = self.uids()
        new = move_block(uids, set(self.selected_uids()), row)
        if new != uids:
            self.orderChanged.emit(new)

    # ------------------------------------------------------------ drag/drop
    def startDrag(self, actions) -> None:
        items = self.selectedItems()
        if not items:
            return
        md = QMimeData()
        md.setData(PAGES_MIME, ",".join(str(it.data(UID_ROLE)) for it in items).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.setPixmap(self._drag_pixmap(items))
        drag.setHotSpot(QPoint(20, 20))
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)
        self._drop_row = None
        self.viewport().update()

    def _drag_pixmap(self, items) -> QPixmap:
        first = min(items, key=self.row)
        img = self.thumb(first.data(UID_ROLE))
        w = 72
        a = first.data(ASPECT_ROLE) or 1.3
        h = int(w * a)
        n = len(items)
        pm = QPixmap(w + 16, h + 16)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for k in range(min(n, 3) - 1, -1, -1):  # a small stack for multi-page drags
            r = QRect(4 + k * 4, 4 + k * 4, w, h)
            p.fillRect(r.translated(1, 1), QColor(0, 0, 0, 60))
            p.fillRect(r, Qt.GlobalColor.white)
            if k == 0 and img is not None:
                p.drawImage(r, img)
            p.setPen(QColor(0, 0, 0, 90))
            p.drawRect(r)
        if n > 1:
            hl = self.palette().color(QPalette.ColorRole.Highlight)
            p.setBrush(hl)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRect(w - 14, 0, 26, 26))
            p.setPen(self.palette().color(QPalette.ColorRole.HighlightedText))
            f = p.font()
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRect(w - 14, 0, 26, 26), int(Qt.AlignmentFlag.AlignCenter), str(n))
        p.end()
        pm.setDevicePixelRatio(1.0)
        return pm

    def drop_row_at(self, pos: QPoint) -> int:
        """Insertion index (0..count) for a drop at viewport position `pos`."""
        n = self.count()
        for i in range(n):
            r = self.visualRect(self.indexFromItem(self.item(i)))
            if self.grid:
                if pos.y() < r.top() or (pos.y() <= r.bottom() and pos.x() < r.center().x()):
                    return i
            elif pos.y() < r.center().y():
                return i
        return n

    def _accepts(self, e) -> bool:
        md = e.mimeData()
        return md.hasUrls() or (md.hasFormat(PAGES_MIME) and isinstance(e.source(), Sidebar))

    def dragEnterEvent(self, e) -> None:
        if self._accepts(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        if not self._accepts(e):
            e.ignore()
            return
        pos = e.position().toPoint()
        self._autoscroll(pos)
        row = self.drop_row_at(pos)
        if row != self._drop_row:
            self._drop_row = row
            self.viewport().update()
        if e.source() is self:
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
        else:
            e.acceptProposedAction()

    def _autoscroll(self, pos: QPoint) -> None:
        sb = self.verticalScrollBar()
        m = 28
        if pos.y() < m:
            sb.setValue(sb.value() - max(4, (m - pos.y()) // 2))
        elif pos.y() > self.viewport().height() - m:
            sb.setValue(sb.value() + max(4, (pos.y() - self.viewport().height() + m) // 2))

    def dragLeaveEvent(self, e) -> None:
        self._drop_row = None
        self.viewport().update()
        super().dragLeaveEvent(e)

    def dropEvent(self, e) -> None:
        row = self.drop_row_at(e.position().toPoint())
        self._drop_row = None
        self.viewport().update()
        md = e.mimeData()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            e.acceptProposedAction()
            self.filesDropped.emit(paths, row)
            return
        src = e.source()
        if not (md.hasFormat(PAGES_MIME) and isinstance(src, Sidebar)):
            e.ignore()
            return
        # Report a copy so the drag source never removes rows itself; the page model is the only truth.
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()
        if src is self or src.canvas is self.canvas:
            moved = {int(u) for u in bytes(md.data(PAGES_MIME)).decode().split(",") if u}
            from .pageops import move_block

            uids = self.uids()
            new = move_block(uids, moved, row)
            if new != uids:
                QTimer.singleShot(0, lambda: self.orderChanged.emit(new))
        else:
            QTimer.singleShot(0, lambda: self.pagesDropped.emit(src, row))

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        if self._drop_row is None:
            return
        n = self.count()
        p = QPainter(self.viewport())
        hl = self.palette().color(QPalette.ColorRole.Highlight)
        p.setPen(QPen(hl, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if n == 0:
            p.drawLine(8, 6, self.viewport().width() - 8, 6)
        else:
            after = self._drop_row >= n
            r = self.visualRect(self.indexFromItem(self.item(n - 1 if after else self._drop_row)))
            if self.grid:
                x = r.right() + 3 if after else r.left() - 3
                p.drawLine(x, r.top() + 6, x, r.bottom() - 6)
            else:
                y = r.bottom() + 1 if after else r.top() - 1
                p.drawLine(r.left() + 10, max(2, y), r.right() - 10, max(2, y))
        p.end()
