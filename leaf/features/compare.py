"""Compare feature: Document › Compare with › Another PDF… / Saved version; the compare view.

The view (`CompareView`, registered as the "compare" central view) shows both documents side by side with
synchronized scrolling and zoom. Pages are aligned by text similarity (leaf/compare.py), so an inserted or deleted
page leaves a gap on the other side instead of shifting everything after it. Word changes are highlighted on each
side (deleted/old red on the left, inserted/new green on the right), changes in graphics and images in orange.
Overlay mode shows both pages blended with every changed pixel in magenta. The change list on the right
(page, kind, text) navigates with Alt+Down / Alt+Up (WinMerge's keys; F7 is the History panel).

Everything is computed in small time-boxed steps on the GUI thread: page fingerprints, then the row diffs, the
rows on screen first. Pages render lazily at the current zoom, one per timer tick.
"""
from __future__ import annotations

import bisect
import os
import time

import pymupdf
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QImage, QKeySequence, QPainter, QPalette, QPen, qRgb, qRgba
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFileDialog, QFrame, QGraphicsItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QSizePolicy, QSplitter, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import compare as K

ROW_GAP = 18.0
SIDE_MARGIN = 16.0
STEP_SECONDS = 0.03
COLORS = {  # (brush rgba, pen rgb) per highlight role; both sides keep an outline so it stays visible in any theme
    "del": ((220, 40, 40, 70), (200, 30, 30)),
    "ins": ((30, 160, 60, 70), (20, 130, 50)),
    "vis": ((240, 140, 0, 50), (215, 110, 0)),
}
DIFF_RGBA = (230, 20, 140, 190)


def _kind_roles(kind: str) -> tuple[str | None, str | None]:
    """Highlight role on side A and side B for a change kind."""
    if kind in ("insert", "page_insert"):
        return None, "ins"
    if kind in ("delete", "page_delete"):
        return "del", None
    if kind == "replace":
        return "del", "ins"
    return "vis", "vis"


# --------------------------------------------------------------------------------------------- scene items
class _Page(QGraphicsItem):
    """One page (or the gap where the other document has a page) on one side of the compare view."""

    def __init__(self, view: "CompareView", side: str, row: int, index: int | None, w: float, h: float):
        super().__init__()
        self.view, self.side, self.row, self.index = view, side, row, index
        self.w, self.h = w, h
        self.img: QImage | None = None
        self.img_key = None
        self.queued = False

    def boundingRect(self) -> QRectF:
        return QRectF(-1, -1, self.w + 2, self.h + 2)

    def paint(self, p: QPainter, opt, widget=None) -> None:
        r = QRectF(0, 0, self.w, self.h)
        pal = self.view.palette()
        if self.index is None:  # gap
            p.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 0, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            f = p.font()
            f.setPointSizeF(14)
            p.setFont(f)
            other = "B" if self.side == "a" else "A"
            p.drawText(r, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       f"No page here\n(page only in {other})")
            return
        p.fillRect(r, Qt.GlobalColor.white)
        if self.img is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self.img)
        p.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        if self.img_key != self.view.render_key(self):
            self.view.request(self)


class _View(QGraphicsView):
    def __init__(self, owner: "CompareView", name: str, desc: str):
        super().__init__()
        self.owner = owner
        self.setScene(QGraphicsScene(self))
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(name)
        self.setAccessibleDescription(desc)
        self.setBackgroundBrush(self.palette().color(QPalette.ColorRole.Dark))

    def wheelEvent(self, e) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            dy = e.angleDelta().y() or e.pixelDelta().y()
            self.owner.set_zoom(self.owner.zoom * (1.0015 ** dy), self)
            e.accept()
            return
        super().wheelEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.owner.close_view()
            return
        super().keyPressEvent(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.owner.schedule()


# ----------------------------------------------------------------------------------------------- the view
class CompareView(QWidget):
    def __init__(self, win, a, b, a_name: str, b_name: str, visual: bool = True):
        super().__init__()
        self.win = win
        self.setObjectName("compareView")
        self.setAccessibleName(f"Comparison of {a_name} and {b_name}")
        self.cmp = K.Comparison(a, b, visual)
        self.a_name, self.b_name = a_name, b_name
        self.zoom = 0.0  # 0 = fit width once the layout is known
        self.mode = "side"
        self.changes: list[K.Change] = []
        self._keys: list[tuple[int, int]] = []  # (row, n) per change, sorted
        self.cur = -1
        self.items_a: list[_Page] = []
        self.items_b: list[_Page] = []
        self.row_y: list[float] = []
        self._marks: dict[int, list[QGraphicsRectItem]] = {}  # change id -> highlight items
        self._cur_marks: list[QGraphicsRectItem] = []
        self._prep = self.cmp.prepare()
        self._todo_render: list[_Page] = []
        self._next_row = 0
        self._syncing = False
        self._closed = False
        self._build()
        self._work = QTimer(self, singleShot=True, interval=0, timeout=self._step)
        self._paint = QTimer(self, singleShot=True, interval=15, timeout=self._render_one)
        self._work.start()

    # ------------------------------------------------------------------ widgets
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("compareBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 4, 8, 4)
        self.title = QLabel(f"<b>A</b> {_esc(self.a_name)} &nbsp;→&nbsp; <b>B</b> {_esc(self.b_name)}")
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setAccessibleName(f"Comparing {self.a_name} (A) with {self.b_name} (B)")
        self.title.setToolTip(f"A: {self.a_name}\nB: {self.b_name}")
        # long names (or translations) must not widen the window: the title gives way, the buttons are icons
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setMinimumWidth(40)
        bl.addWidget(self.title, 1)
        self.side_btn = QToolButton(text="Side by side", checkable=True, checked=True)
        self.side_btn.setToolTip("Both documents next to each other, word changes highlighted")
        self.overlay_btn = QToolButton(text="Overlay", checkable=True)
        self.overlay_btn.setToolTip("Both pages blended, changed pixels in magenta (images and graphics)")
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for btn in (self.side_btn, self.overlay_btn):
            grp.addButton(btn)
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            bl.addWidget(btn)
        self.side_btn.toggled.connect(lambda on: self.set_mode("side" if on else "overlay"))
        bl.addSpacing(12)
        self.prev_btn = _icon_button("chevron_up", "Previous change", "Previous change (Alt+Up)")
        self.next_btn = _icon_button("chevron_down", "Next change", "Next change (Alt+Down)")
        self.counter = QLabel()
        self.counter.setAccessibleName("Current change")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        zo = QToolButton(text="−")
        zo.setToolTip("Zoom out (Ctrl+wheel)")
        zo.setAccessibleName("Zoom out")
        zo.clicked.connect(lambda: self.set_zoom(self.zoom / 1.25))
        zi = QToolButton(text="+")
        zi.setToolTip("Zoom in (Ctrl+wheel)")
        zi.setAccessibleName("Zoom in")
        zi.clicked.connect(lambda: self.set_zoom(self.zoom * 1.25))
        fw = _icon_button("fit_width", "Fit width", "Fit width")
        fw.clicked.connect(self.fit_width)
        close = _icon_button("close", "Close comparison", "Close comparison: back to the document (Esc)")
        close.clicked.connect(self.close_view)
        for w in (self.prev_btn, self.counter, self.next_btn, zo, zi, fw, close):
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            bl.addWidget(w)
        lay.addWidget(bar)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.view_a = _View(self, f"Document A: {self.a_name}", "Older document. Deleted and replaced words are marked red.")
        self.view_b = _View(self, f"Document B: {self.b_name}", "Newer document. Inserted and replaced words are marked green.")
        self.split.addWidget(self.view_a)
        self.split.addWidget(self.view_b)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 6, 6)
        self.summary = QLabel("Comparing…")
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Comparison summary")
        sl.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Changes")
        self.tree.setAccessibleDescription("Every difference found; select one to show it in both documents")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Page", "Change", "Text"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(True)
        self.tree.currentItemChanged.connect(self._tree_current)
        sl.addWidget(self.tree, 1)
        self.split.addWidget(side)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 3)
        self.split.setStretchFactor(2, 2)
        side.setMinimumWidth(160)
        self.tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.split, 1)
        # tab order: toolbar buttons → A → B → change list
        chain = [self.side_btn, self.overlay_btn, self.prev_btn, self.next_btn, zo, zi, fw, close, self.view_a, self.view_b, self.tree]
        for x, y in zip(chain, chain[1:]):
            QWidget.setTabOrder(x, y)
        for v, other in ((self.view_a, self.view_b), (self.view_b, self.view_a)):
            v.verticalScrollBar().valueChanged.connect(lambda val, o=other: self._sync_scroll(o, val, True))
            v.horizontalScrollBar().valueChanged.connect(lambda val, o=other: self._sync_scroll(o, val, False))
        self._update_nav()

    def _sync_scroll(self, other: _View, value: int, vertical: bool) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            (other.verticalScrollBar() if vertical else other.horizontalScrollBar()).setValue(value)
        finally:
            self._syncing = False
        self.schedule()

    # --------------------------------------------------------------- background
    def schedule(self) -> None:
        if not self._closed and not self._work.isActive():
            self._work.start()

    def _step(self) -> None:
        if self._closed:
            return
        t0 = time.perf_counter()
        if not self.cmp.ready:
            try:
                while time.perf_counter() - t0 < STEP_SECONDS:
                    done, total = next(self._prep)
                    self.summary.setText(f"Reading pages… {done} of {total}")
            except StopIteration:
                self._layout()
            self._work.start()
            return
        rows = self._visible_rows() + list(range(self._next_row, len(self.cmp.rows)))
        for row in rows:
            if time.perf_counter() - t0 > STEP_SECONDS:
                break
            if self.cmp.computed(row):
                if row == self._next_row:
                    self._next_row += 1
                continue
            self._add_changes(row, self.cmp.row_changes(row))
        while self._next_row < len(self.cmp.rows) and self.cmp.computed(self._next_row):
            self._next_row += 1
        self._update_summary()
        if self._next_row < len(self.cmp.rows):
            self._work.start()

    def finish(self) -> None:
        """Compute everything now (tests, CLI-like use)."""
        while not self.cmp.ready:
            self._step()
        for row in range(len(self.cmp.rows)):
            if not self.cmp.computed(row):
                self._add_changes(row, self.cmp.row_changes(row))
        self._next_row = len(self.cmp.rows)
        self._update_summary()

    def done(self) -> bool:
        return self.cmp.ready and self._next_row >= len(self.cmp.rows)

    def _update_summary(self) -> None:
        text = K.summary_text(self.changes)
        if not self.done():
            text = f"{text} — comparing… page {self._next_row + 1} of {len(self.cmp.rows)}"
        self.summary.setText(text)
        self._update_nav()

    # ------------------------------------------------------------------ layout
    def _layout(self) -> None:
        width = SIDE_MARGIN * 2 + max([self.cmp.a[i].rect.width for i, _ in self.cmp.rows if i is not None]
                                      + [self.cmp.b[j].rect.width for _, j in self.cmp.rows if j is not None] + [100])
        y = SIDE_MARGIN
        for row, (i, j) in enumerate(self.cmp.rows):
            ra = self.cmp.a[i].rect if i is not None else None
            rb = self.cmp.b[j].rect if j is not None else None
            ha = ra.height if ra else rb.height
            hb = rb.height if rb else ra.height
            wa = ra.width if ra else rb.width
            wb = rb.width if rb else ra.width
            self.row_y.append(y)
            for scene, items, idx, w, h, side in ((self.view_a.scene(), self.items_a, i, wa, ha, "a"),
                                                  (self.view_b.scene(), self.items_b, j, wb, hb, "b")):
                it = _Page(self, side, row, idx, w, h)
                it.setPos((width - w) / 2, y)
                scene.addItem(it)
                items.append(it)
            y += max(ha, hb) + ROW_GAP
        rect = QRectF(0, 0, width, y + SIDE_MARGIN - ROW_GAP)
        for v in (self.view_a, self.view_b):
            v.scene().setSceneRect(rect)
        if self.zoom <= 0:
            w = max(self.split.width(), 600)
            self.split.setSizes([int(w * 0.39), int(w * 0.39), w - 2 * int(w * 0.39)])
            self.fit_width()

    def _visible_rows(self) -> list[int]:
        if not self.row_y:
            return []
        v = self.view_b if self.view_b.isVisible() or not self.view_a.isVisible() else self.view_a
        r = v.mapToScene(v.viewport().rect()).boundingRect()
        lo = max(0, bisect.bisect_right(self.row_y, r.top()) - 1)
        hi = bisect.bisect_right(self.row_y, r.bottom())
        return list(range(lo, min(hi, len(self.row_y))))

    # -------------------------------------------------------------------- zoom
    def set_zoom(self, z: float, anchor_view: _View | None = None) -> None:
        z = max(0.1, min(8.0, z))
        if abs(z - self.zoom) < 1e-4:
            return
        self.zoom = z
        for v in (self.view_a, self.view_b):
            if v is not anchor_view:
                v.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            v.resetTransform()
            v.scale(z, z)
            v.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        if anchor_view is not None:  # follow the view zoomed under the mouse
            other = self.view_b if anchor_view is self.view_a else self.view_a
            self._sync_scroll(other, anchor_view.verticalScrollBar().value(), True)
            self._sync_scroll(other, anchor_view.horizontalScrollBar().value(), False)
        self.schedule()

    def fit_width(self) -> None:
        w = self.view_b.scene().sceneRect().width()
        vw = self.view_b.viewport().width() or 400
        if w > 0:
            self.set_zoom((vw - 4) / w)

    # --------------------------------------------------------------- rendering
    def render_key(self, it: _Page):
        scale = self.zoom * self.devicePixelRatioF()
        return round(min(scale, 4.0), 3), (self.mode if it.side == "b" else "side")

    def request(self, it: _Page) -> None:
        if not it.queued:
            it.queued = True
            self._todo_render.append(it)
        if not self._paint.isActive():
            self._paint.start()

    def _render_one(self) -> None:
        if self._closed or not self._todo_render:
            return
        vis = set(self._visible_rows())
        self._todo_render.sort(key=lambda it: it.row not in vis)
        it = self._todo_render.pop(0)
        it.queued = False
        if it.row in vis or not vis:
            key = self.render_key(it)
            scale, mode = key
            doc = self.cmp.a if it.side == "a" else self.cmp.b
            img = _render(doc[it.index], scale)
            if mode == "overlay":
                i, _j = self.cmp.rows[it.row]
                img = overlay(_render(self.cmp.a[i], scale) if i is not None else None, img)
            it.img, it.img_key = img, key
            it.update()
            self._evict(vis)
        if self._todo_render:
            self._paint.start()

    def _evict(self, vis: set[int]) -> None:
        """Keep rendered images only for rows near the visible ones (memory stays flat on long documents)."""
        if not vis:
            return
        lo, hi = min(vis) - 4, max(vis) + 4
        for items in (self.items_a, self.items_b):
            for it in items:
                if it.img is not None and not lo <= it.row <= hi:
                    it.img = it.img_key = None

    # ----------------------------------------------------------------- changes
    def _add_changes(self, row: int, changes: list[K.Change]) -> None:
        for n, ch in enumerate(changes):
            key = (row, n)
            pos = bisect.bisect_left(self._keys, key)
            self._keys.insert(pos, key)
            self.changes.insert(pos, ch)
            if self.cur >= pos:
                self.cur += 1
            item = QTreeWidgetItem([ch.page_label(), ch.label, ch.snippet()])
            item.setToolTip(2, ch.snippet(400))
            self.tree.insertTopLevelItem(pos, item)
            self._mark(ch)

    def _mark(self, ch: K.Change) -> None:
        ra, rb = _kind_roles(ch.kind)
        marks = []
        for role, items, rects in ((ra, self.items_a, ch.a_rects), (rb, self.items_b, ch.b_rects)):
            if role is None or not items:
                continue
            page = items[ch.row]
            brush, pen = COLORS[role]
            for x0, y0, x1, y1 in rects:
                m = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-1, -1, 1, 1), page)
                m.setBrush(QBrush(QColor(*brush)))
                p = QPen(QColor(*pen), 1.2, Qt.PenStyle.DashLine if role == "vis" else Qt.PenStyle.SolidLine)
                p.setCosmetic(True)
                m.setPen(p)
                m.setZValue(5)
                marks.append(m)
        self._marks[id(ch)] = marks

    def _tree_current(self, cur, _prev) -> None:
        if cur is None:
            return
        i = self.tree.indexOfTopLevelItem(cur)
        if i != self.cur:
            self.go_to_change(i)

    def step(self, d: int) -> None:
        if not self.changes:
            return
        self.go_to_change((self.cur + d) % len(self.changes) if self.cur >= 0 else (0 if d > 0 else len(self.changes) - 1))

    def go_to_change(self, i: int) -> None:
        if not 0 <= i < len(self.changes):
            return
        self.cur = i
        ch = self.changes[i]
        it = self.tree.topLevelItem(i)
        if it is not None and self.tree.currentItem() is not it:
            self.tree.setCurrentItem(it)
        for m in self._cur_marks:
            if m.scene() is not None:
                m.scene().removeItem(m)
        self._cur_marks = []
        accent = self.palette().color(QPalette.ColorRole.Highlight)
        target = None
        for items, rects in ((self.items_a, ch.a_rects), (self.items_b, ch.b_rects)):
            page = items[ch.row] if items else None
            if page is None:
                continue
            for x0, y0, x1, y1 in rects or ([(0, 0, page.w, page.h)] if page.index is None else []):
                m = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-3, -3, 3, 3), page)
                p = QPen(accent, 2.5)
                p.setCosmetic(True)
                m.setPen(p)
                m.setZValue(6)
                self._cur_marks.append(m)
                if target is None or items is self.items_b:
                    target = m
        if target is None and self.items_b:
            target = self.items_b[ch.row]
        if target is not None:
            v = self.view_b if self.view_b.isVisible() else self.view_a
            v.ensureVisible(target.sceneBoundingRect(), 40, 60)
        self._update_nav()
        from .. import a11y

        a11y.announce(self.summary, f"Change {i + 1} of {len(self.changes)}: {ch.label}, page {ch.page_label()}. {ch.snippet()}")

    def _update_nav(self) -> None:
        n = len(self.changes)
        for b in (self.prev_btn, self.next_btn):
            b.setEnabled(n > 0)
        self.counter.setText(f"{self.cur + 1} / {n}" if n and self.cur >= 0 else (f"{n}" if n else ""))

    # -------------------------------------------------------------------- modes
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self.view_a.setVisible(mode == "side")
        for it in self.items_b:
            it.update()
        self.schedule()

    def close_view(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._work.stop()
        self._paint.stop()
        win = self.win
        win.show_view("document" if win.canvas.doc is not None else "welcome")
        for v in (self.view_a, self.view_b):
            v.scene().clear()
        self.cmp.close()
        if getattr(win, "compare_view", None) is self:
            win.compare_view = None
        self.deleteLater()


def _icon_button(ic: str, name: str, tip: str) -> QToolButton:
    from ..icons import icon

    b = QToolButton()
    b.setIcon(icon(ic))
    b.setProperty("icon_name", ic)
    b.setAutoRaise(True)
    b.setToolTip(tip)
    b.setAccessibleName(name)
    return b


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render(page: pymupdf.Page, scale: float) -> QImage:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
    return QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()


def overlay(a: QImage | None, b: QImage) -> QImage:
    """Both pages blended (A and B at half strength each) with every pixel that differs painted in magenta.
    A page with no counterpart is shown as is."""
    out = QImage(b.size(), QImage.Format.Format_RGB32)
    out.fill(qRgb(255, 255, 255))
    p = QPainter(out)
    if a is None:
        p.drawImage(0, 0, b)
        p.end()
        return out
    if a.size() != b.size():
        a = a.scaled(b.size())
    p.setOpacity(0.5)
    p.drawImage(0, 0, a)
    p.drawImage(0, 0, b)
    p.setOpacity(1.0)
    p.end()
    diff = a.convertToFormat(QImage.Format.Format_RGB32)
    q = QPainter(diff)
    q.setCompositionMode(QPainter.CompositionMode.CompositionMode_Difference)
    q.drawImage(0, 0, b)
    q.end()
    mask = diff.createMaskFromColor(qRgb(0, 0, 0), Qt.MaskMode.MaskOutColor)  # 1 = differs
    mask.setColorTable([qRgba(0, 0, 0, 0), qRgba(*DIFF_RGBA)])
    p = QPainter(out)
    p.drawImage(0, 0, mask.convertToFormat(QImage.Format.Format_ARGB32))
    p.end()
    return out


# ------------------------------------------------------------------------------------------- entry points
def open_compare(win, a, b, a_name: str, b_name: str, visual: bool = True) -> CompareView:
    """Show the compare view for documents a (old) and b (new): bytes, paths or pymupdf.Documents."""
    old = getattr(win, "compare_view", None)
    if old is not None:
        old.close_view()
    view = CompareView(win, a, b, a_name, b_name, visual)
    win.compare_view = view
    win.add_view("compare", view)
    win.show_view("compare")
    view.view_b.setFocus()
    return view


def _open_file(win, path: str) -> pymupdf.Document | None:
    from PySide6.QtWidgets import QInputDialog, QLineEdit

    try:
        d = pymupdf.open(path)
    except Exception as ex:  # noqa: BLE001
        QMessageBox.warning(win, "Compare", f"Couldn't open {os.path.basename(path)}:\n{ex}")
        return None
    while d.needs_pass:
        pw, ok = QInputDialog.getText(win, "Password", f"{os.path.basename(path)} is protected. Password:",
                                      QLineEdit.EchoMode.Password)
        if not ok:
            d.close()
            return None
        d.authenticate(pw)
    if not d.is_pdf:  # images etc.: convert so text/visual comparison works the same
        d = pymupdf.open("pdf", d.convert_to_pdf())
    return d


def current_bytes(win) -> bytes:
    """What Save would write for the open document (the pristine bytes when nothing changed)."""
    from PySide6.QtWidgets import QApplication

    doc = win.canvas.doc
    # untouched (no edit made, or all undone; a save doesn't count: the edits still replay from the sources)
    if (win.canvas.undo.index() == 0 and doc.is_identity(doc.pages) and not doc.primary.password
            and not getattr(doc, "converted", False)):
        return doc.primary.data
    from ..export import snapshot

    commit = getattr(win, "_commit_edits", None)
    if commit is not None:
        commit()
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        with snapshot(doc, win.canvas.overlays_for) as s:
            if s.doc.is_encrypted or s.doc.needs_pass:
                return s.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
            with open(s.path, "rb") as f:
                return f.read()
    finally:
        QApplication.restoreOverrideCursor()


def _saved_doc(win) -> tuple[pymupdf.Document, str]:
    """The saved file on disk (else the document as opened), decrypted with the password it was opened with."""
    doc = win.canvas.doc
    data, label = doc.primary.data, "as opened"
    if doc.path and os.path.exists(doc.path) and not getattr(doc, "converted", False):
        try:
            with open(doc.path, "rb") as f:
                data, label = f.read(), "saved file"
        except OSError:
            pass
    d = pymupdf.open(stream=data, filetype="pdf")
    if d.needs_pass:
        for pw in (doc.primary.password, getattr(doc, "output_password", lambda: None)()):
            if pw and d.authenticate(pw):
                break
    if d.needs_pass:  # saved with a password we don't know: fall back to the bytes as opened
        d = pymupdf.open(stream=doc.primary.data, filetype="pdf")
        d.authenticate(doc.primary.password or "")
        label = "as opened"
    return d, label


def compare_with_file(win, path: str | None = None) -> CompareView | None:
    if path is None:
        start = win.settings.value("lastdir", "")
        path, _ = QFileDialog.getOpenFileName(win, "Compare with", start, "PDF files (*.pdf);;All files (*)")
        if not path:
            return None
    other = _open_file(win, path)
    if other is None:
        return None
    if win.canvas.doc is None:  # no document open: pick the second (newer) file too
        p2, _ = QFileDialog.getOpenFileName(win, "Compare with (newer version)", os.path.dirname(path),
                                            "PDF files (*.pdf);;All files (*)")
        b = _open_file(win, p2) if p2 else None
        if b is None:
            other.close()
            return None
        return open_compare(win, other, b, os.path.basename(path), os.path.basename(p2))
    name = os.path.basename(win.canvas.doc.path or "Untitled")
    mine = pymupdf.open(stream=current_bytes(win), filetype="pdf")
    return open_compare(win, mine, other, name, os.path.basename(path))


def compare_with_saved(win) -> CompareView | None:
    if win.canvas.doc is None:
        return None
    if getattr(win.canvas.doc, "converted", False):
        win.toast("This document hasn't been saved as a PDF yet: nothing to compare with")
        return None
    saved, label = _saved_doc(win)
    mine = pymupdf.open(stream=current_bytes(win), filetype="pdf")
    name = os.path.basename(win.canvas.doc.path or "Untitled")
    return open_compare(win, saved, mine, f"{name} ({label})", f"{name} (with your changes)")


def _action(win, name: str, text: str, slot, keys=(), tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    a.setProperty("base_tip", tip or text.replace("&", "").rstrip("…"))
    a.setToolTip(tip or text.replace("&", "").rstrip("…"))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def install(win) -> None:
    a_file = _action(win, "compare_file", "&Another PDF…", lambda: compare_with_file(win),
                     tip="Compare this document with another PDF: text, pages and graphics")
    a_saved = _action(win, "compare_saved", "&Saved Version (Review Changes)", lambda: compare_with_saved(win),
                      tip="Compare what Save would write with the saved file")
    a_next = _action(win, "compare_next", "Next Change", lambda: _cv_step(win, 1), ["Alt+Down"],
                     "Next difference in the comparison")
    a_prev = _action(win, "compare_prev", "Previous Change", lambda: _cv_step(win, -1), ["Alt+Up"],
                     "Previous difference in the comparison")
    win.compare_view = None
    menus = getattr(win, "menus", {})
    host = menus.get("document") or menus.get("file")
    if host is not None:
        sub = QMenu("Co&mpare with", win)
        sub.setObjectName("menu_compare")
        sub.addActions([a_file, a_saved])
        sub.addSeparator()
        sub.addActions([a_next, a_prev])
        host.addSeparator()
        host.addMenu(sub)
        menus["compare"] = sub

    def sync(*_) -> None:
        has = win.canvas.doc is not None
        a_saved.setEnabled(has)
        on = win.current_view() == "compare"
        a_next.setEnabled(on)  # only while comparing: Alt+Up/Down stay free for combo boxes elsewhere
        a_prev.setEnabled(on)

    win.canvas.documentChanged.connect(sync)
    win.views.currentChanged.connect(sync)
    sync()
    # accessibility + high contrast for the whole window (leaf/a11y.py); lives here so no shared file has to call it
    from .. import a11y

    a11y.install(win)


def _cv_step(win, d: int) -> None:
    v = getattr(win, "compare_view", None)
    if v is not None and win.current_view() == "compare":
        v.step(d)
