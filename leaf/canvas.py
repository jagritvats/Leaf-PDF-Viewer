"""Continuous page view: lazy prioritized rendering, tools, selection, forms."""
from __future__ import annotations

import bisect
import math
import os
import time
from collections import OrderedDict

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QGuiApplication, QImage, QPainter, QPainterPath, QPalette, QTransform, QUndoStack,
)
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QFrame, QMessageBox, QToolTip,
)

from . import annots as A
from . import commands as C
from .document import PageRef, PdfDocument
from .forms import FieldItem, build_fields
from .items import (
    DEFAULTS, BoxItem, HighlightItem, ImageItem, InkItem, LineItem, ShapeItem, TextItem, editing_text_item, is_overlay,
)

GAP = 14.0
MARGIN = 28.0
MAX_FULL_PIXELS = 10_000_000  # beyond this a full-page bitmap is capped; a sharp tile covers the viewport
IMAGE_BUDGET = 384 * 2**20
ZOOM_MIN, ZOOM_MAX = 0.08, 16.0
CREATE_TOOLS = {"rect", "ellipse", "whiteout", "line", "arrow", "ink"}
VIEW_MODES = ("continuous", "single", "spread")
MAX_INFLIGHT = 2  # worker requests in flight; the rest stay queued here so they can be dropped cheaply


def to_qimage(pix) -> QImage:
    return QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()


_channel_rotation = None


def _rotate_channels():
    """A colour transform (r, g, b) -> (g, b, r): between two linear colour spaces whose primaries are the same three
    in rotated order, i.e. a permutation matrix. Exact for 8-bit values and runs in C (~3.5 ms per 2 MP)."""
    global _channel_rotation
    if _channel_rotation is None:
        from PySide6.QtGui import QColorSpace

        wp, r, g, b = QPointF(0.3127, 0.3290), QPointF(0.64, 0.33), QPointF(0.30, 0.60), QPointF(0.15, 0.06)
        lin = QColorSpace.TransferFunction.Linear
        _channel_rotation = QColorSpace(wp, r, g, b, lin).transformationToColorSpace(QColorSpace(wp, g, b, r, lin))
    return _channel_rotation


def night_image(img: QImage) -> QImage:
    """Night-mode version of a rendered page: lightness inverted, hue and saturation kept (white paper -> black,
    black text -> white, a green figure stays green instead of turning pink). Per pixel c' = c + 255 - max - min
    (HSL: L -> 1 - L), which is its own inverse, so toggling night mode back restores the exact pixels.

    No numpy: max/min over each pixel's channels with Lighten/Darken against the two channel rotations, then
    c' = |(max - c) - (255 - min)| with Difference, all QPainter/QImage work in C (~19 ms per 2 MP)."""
    if img.isNull():
        return img
    if img.hasAlphaChannel():  # not a rendered page (those are opaque): plain inversion, also an involution
        out = QImage(img)
        out.invertPixels()
        return out
    fmt = img.format()
    s = img.convertToFormat(QImage.Format.Format_RGB32)
    rot = _rotate_channels()
    p1 = s.colorTransformed(rot)  # (g, b, r)
    p2 = p1.colorTransformed(rot)  # (b, r, g)
    mode = QPainter.CompositionMode
    mn = QImage(p1)
    p = QPainter(mn)
    p.setCompositionMode(mode.CompositionMode_Darken)
    p.drawImage(0, 0, s)
    p.drawImage(0, 0, p2)
    p.end()
    mn.invertPixels()  # 255 - min, in every channel
    p = QPainter(p1)
    p.setCompositionMode(mode.CompositionMode_Lighten)
    p.drawImage(0, 0, s)
    p.drawImage(0, 0, p2)
    p.end()  # p1 = max, in every channel
    p = QPainter(s)
    p.setCompositionMode(mode.CompositionMode_Difference)
    p.drawImage(0, 0, p1)  # max - c
    p.drawImage(0, 0, mn)  # (255 - min) - (max - c) = c + 255 - max - min (never negative)
    p.end()
    return s.convertToFormat(fmt) if fmt != s.format() else s


def _page_of(item) -> "PageItem | None":
    while item is not None and not isinstance(item, PageItem):
        item = item.parentItem()
    return item


class PageItem(QGraphicsItem):
    def __init__(self, canvas: "Canvas", ref: PageRef):
        super().__init__()
        self.canvas = canvas
        self.ref = ref
        self.content = QGraphicsRectItem(self)
        self.content.setPen(Qt.PenStyle.NoPen)
        self.content.setFlag(QGraphicsItem.GraphicsItemFlag.ItemHasNoContents)
        self.img: QImage | None = None
        self.img_scale = 0.0
        self.preview: QImage | None = None
        self.detail = None  # (QImage, QRectF, scale)
        self.job = None  # worker request in flight: (rid, kind, scale, rotation, display rect or None)
        self.rot = None
        self.fields: list[FieldItem] | None = None
        self.adopted: list | None = None  # the PDF's own annotations as items (leaf/annots.py); None = not yet
        self._adopting = False
        self.w = self.h = 1.0
        self.sync_geometry()

    def sync_geometry(self) -> None:
        doc = self.canvas.doc
        pr = doc.view_rect(self.ref)  # the crop (optimize) or the whole page.rect
        e = doc.extra_matrix(self.ref)
        self.prepareGeometryChange()
        self.w, self.h = doc.display_size(self.ref)
        self.content.setRect(pr.x0, pr.y0, pr.width, pr.height)
        # a cropped page hides everything outside the crop, overlays included, like the CropBox written at save
        self.content.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, self.ref.crop is not None)
        self.content.setTransform(QTransform(e.a, e.b, e.c, e.d, e.e, e.f))
        if self.rot != (self.ref.rotation, self.ref.crop):
            self.rot = (self.ref.rotation, self.ref.crop)
            self.img = self.preview = self.detail = None
            self.img_scale = 0.0
            self.job = None  # a render in flight is for the old rotation/crop

    SHADOW = 9.0  # room for the page shadow around the page, in page units (only painting sees it)

    def page_rect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def scene_page_rect(self) -> QRectF:
        """The page itself in scene coordinates (sceneBoundingRect also holds the shadow margin)."""
        return self.mapRectToScene(self.page_rect())

    def boundingRect(self) -> QRectF:
        m = self.SHADOW
        return QRectF(-m, -m + 2, self.w + 2 * m, self.h + 2 * m)

    def shape(self) -> QPainterPath:
        path = QPainterPath()  # hits: the page only, not its shadow
        path.addRect(self.page_rect())
        return path

    def paint(self, p: QPainter, opt, widget=None) -> None:
        r = self.page_rect()
        # soft layered shadow: screen-pixel sized (page-space capped, so it shrinks when zoomed far out), three
        # bands around the page only, never under it: plain fills, no antialiasing, no clipping
        px = 1.0 / max(abs(p.worldTransform().m11()), 1e-3)
        dark = self.canvas.backgroundBrush().color().lightness() < 100
        for i in (3, 2, 1):
            spread = min(i * 2.6 * px, self.SHADOW - 1)
            dy = min((0.8 + i * 0.9) * px, 4.0)
            a = (0.07 if dark else 0.036) * (1.0 - (i - 1) / 3) + (0.025 if dark else 0.01)
            c = QColor(0, 0, 0, round(255 * a))
            o = r.adjusted(-spread, -spread + dy, spread, spread + dy)
            p.fillRect(QRectF(o.left(), o.top(), o.width(), r.top() - o.top()), c)  # above
            p.fillRect(QRectF(o.left(), r.bottom(), o.width(), o.bottom() - r.bottom()), c)  # below
            p.fillRect(QRectF(o.left(), r.top(), r.left() - o.left(), r.height()), c)  # left
            p.fillRect(QRectF(r.right(), r.top(), o.right() - r.right(), r.height()), c)  # right
        p.fillRect(r, QColor(24, 24, 24) if self.canvas.night else Qt.GlobalColor.white)  # no white flash in night mode
        img = self.img or self.preview
        if img is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, abs(self.img_scale - self.canvas.target_scale()) > 1e-3)
            p.drawImage(r, img)
        ts = self.canvas.target_scale()
        if self.detail is not None and abs(self.detail[2] - ts) < 1e-3:
            p.drawImage(self.detail[1], self.detail[0])
        if self.needs_work():
            self.canvas.renderer.want(self)
        if self.fields is None:
            self.fields = []
            QTimer.singleShot(0, self._build_fields)
        if self.adopted is None and not self._adopting:
            self._adopting = True
            QTimer.singleShot(0, self._adopt)

    def _build_fields(self) -> None:
        if self.scene() is not None:
            self.fields = build_fields(self.canvas, self)

    def _adopt(self) -> None:
        self._adopting = False
        if self.scene() is not None:
            self.canvas.annots.ensure(self)

    def invalidate(self) -> None:
        """Re-render (keeping the current image on screen until the new one arrives)."""
        self.img_scale = 0.0
        self.detail = None
        self.job = None
        self.update()

    def full_scale(self) -> float:
        cap = math.sqrt(MAX_FULL_PIXELS / max(self.w * self.h, 1.0))
        return min(self.canvas.target_scale(), cap)

    def visible_rect(self) -> QRectF:
        v = self.canvas
        vr = v.mapToScene(v.viewport().rect()).boundingRect()
        return self.mapFromScene(vr).boundingRect() & self.page_rect()

    def _wanted(self, with_clip: bool = False):
        """What to render next: ("full", scale, None), ("detail", scale, vis/clip rect) or None."""
        fs = self.full_scale()
        if self.img is None or abs(self.img_scale - fs) > 1e-3:
            return ("full", fs, None)
        ts = self.canvas.target_scale()
        if ts > self.img_scale + 1e-3:
            vis = self.visible_rect()
            if vis.isEmpty():
                return None
            d = self.detail
            if d is not None and abs(d[2] - ts) < 1e-3 and d[1].contains(vis):
                return None
            if with_clip:
                pad_x, pad_y = vis.width() * 0.25, vis.height() * 0.25
                vis = vis.adjusted(-pad_x, -pad_y, pad_x, pad_y) & self.page_rect()
            return ("detail", ts, vis)
        return None

    def needs_work(self) -> bool:
        want = self._wanted()
        if want is None:
            return False
        return not self.job_covers(want)

    def job_covers(self, want) -> bool:
        j = self.job
        if j is None or j[1] != want[0] or abs(j[2] - want[1]) > 1e-3 or j[3] != self.ref.rotation:
            return False
        return want[0] == "full" or j[4].contains(want[2])

    def do_work(self) -> None:
        want = self._wanted(with_clip=True)
        if want is None:
            return
        kind, scale, clip = want
        c = (clip.left(), clip.top(), clip.right(), clip.bottom()) if clip is not None else None
        pix, (x, y, w, h) = self.canvas.doc.render(self.ref, scale, clip=c)
        self.apply_image(kind, scale, to_qimage(pix), QRectF(x, y, w, h))

    def apply_image(self, kind: str, scale: float, img: QImage, rect: QRectF) -> None:
        if self.canvas.night:
            img = night_image(img)
        if kind == "full":
            self.img = img
            self.img_scale = scale
            if self.preview is None or self.preview.width() < 64:
                self.preview = img.scaledToWidth(max(64, int(self.w * 0.25)), Qt.TransformationMode.SmoothTransformation)
        else:
            self.detail = (img, rect, scale)
        self.canvas.renderer.account(self)
        self.update()

    def invert_images(self) -> None:
        """Night mode toggled: night_image is its own inverse, so the same call turns it on and off."""
        if self.img is not None:
            self.img = night_image(self.img)
        if self.preview is not None:
            self.preview = night_image(self.preview)
        if self.detail is not None:
            self.detail = (night_image(self.detail[0]),) + tuple(self.detail[1:])
        self.update()

    def bytes_used(self) -> int:
        n = self.img.sizeInBytes() if self.img is not None else 0
        if self.detail is not None:
            n += self.detail[0].sizeInBytes()
        return n

    def evict(self) -> None:
        self.img = None
        self.detail = None
        self.img_scale = 0.0

    def overlays(self) -> list:
        return sorted((c for c in self.content.childItems() if is_overlay(c)), key=lambda c: c.zValue())


class Renderer:
    """Renders visible pages first, nearest the center first.

    Pages go to a worker process when one is enabled (the GUI never blocks on MuPDF); otherwise,
    and always for thumbnails, rendering runs on the GUI thread in small time slices.
    """

    def __init__(self, canvas: "Canvas"):
        self.canvas = canvas
        self.pending: set[PageItem] = set()
        self.thumbs: OrderedDict = OrderedDict()
        self.lru: OrderedDict = OrderedDict()
        self.total = 0
        self.timer = QTimer(canvas)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._tick)
        self._defer_until = 0.0
        self.worker = None  # render_worker.RenderWorker
        self.worker_error = ""  # why the last worker was dropped, if it failed
        self.inflight: dict[int, PageItem] = {}
        self.asyncs: OrderedDict = OrderedDict()  # key -> (ref, scale, done)
        self.async_inflight: dict[int, tuple] = {}  # rid -> (key, done)
        self.poll_timer = QTimer(canvas)
        self.poll_timer.setInterval(5)
        self.poll_timer.timeout.connect(self._poll)

    # ------------------------------------------------------------ worker
    def enable_worker(self, on: bool = True) -> bool:
        """Start (or stop) out-of-process rendering. Returns whether a worker is running."""
        if on and self.worker is None and os.environ.get("LEAF_INPROCESS_RENDER") != "1":
            from .render_worker import RenderWorker

            self.worker = RenderWorker()
            if self.worker.failed:
                self.worker_error = self.worker.error
                self.worker = None
            else:
                self.poll_timer.start()  # also picks up the "ready" message
                w = self.worker
                self.canvas.destroyed.connect(lambda *_: w.close())
        elif not on and self.worker is not None:
            self._drop_worker()
        return self.worker is not None

    def _remote(self) -> bool:
        w = self.worker
        if w is None:
            return False
        if w.usable:
            return True
        if w.failed:
            self._drop_worker()
        return False

    def _drop_worker(self) -> None:
        w, self.worker = self.worker, None
        if w is not None:
            self.worker_error = w.error
            w.close()
        self.poll_timer.stop()
        for it in self.inflight.values():
            it.job = None
        self.inflight.clear()
        for key, done, ref, scale in self.async_inflight.values():
            self.asyncs.setdefault(key, (ref, scale, done))
        self.async_inflight.clear()
        self._kick()

    def reset_worker(self) -> None:
        for it in self.inflight.values():
            it.job = None
        self.inflight.clear()
        self.async_inflight.clear()
        if self.worker is not None:
            self.worker.reset()

    def _dispatch(self) -> None:
        """Cancel requests that are no longer useful, then keep up to MAX_INFLIGHT going."""
        w = self.worker
        vis = self.canvas.visible_pages()
        live = set(vis)
        stale = []
        for rid, it in list(self.inflight.items()):
            j = it.job
            want = it._wanted() if it in live else None
            if j is None or j[0] != rid or want is None or not it.job_covers(want):
                stale.append(rid)
                del self.inflight[rid]
                if j is not None and j[0] == rid:
                    it.job = None
        w.cancel(stale)
        for it in vis:
            if len(self.inflight) >= MAX_INFLIGHT:
                break
            if it.job is not None:
                continue
            want = it._wanted(with_clip=True)
            if want is None:
                continue
            kind, scale, clip = want
            doc, ref = self.canvas.doc, it.ref
            pclip = None
            if clip is not None:
                pclip = tuple(pymupdf.Rect(clip.left(), clip.top(), clip.right(), clip.bottom()) * ~doc.extra_matrix(ref))
            if ref.crop is not None:  # optimize: render only the cropped area
                vr = doc.view_rect(ref)
                pclip = tuple(vr if pclip is None else pymupdf.Rect(pclip) & vr)
            rid = w.submit(ref.src, ref.index, scale, ref.rotation, pclip, uid=ref.uid)
            if not rid:
                return
            it.job = (rid, kind, scale, ref.rotation, clip if clip is not None else it.page_rect())
            self.inflight[rid] = it
        self.pending.clear()
        if any(it.job is None and it._wanted() is not None for it in vis):
            return  # visible pages first
        while self.asyncs and len(self.inflight) + len(self.async_inflight) < MAX_INFLIGHT:
            key, (ref, scale, done) = self.asyncs.popitem(last=False)
            rid = w.submit(ref.src, ref.index, scale, ref.rotation, ref.crop, uid=ref.uid)  # crop: page.rect space, or None
            if not rid:
                return
            self.async_inflight[rid] = (key, done, ref, scale)

    def _poll(self) -> None:
        w = self.worker
        if w is None:
            self.poll_timer.stop()
            return
        if not w.ready:
            if not self._remote():
                if not w.starting and self.pending:
                    self._kick()  # stopped waiting for the worker: render here meanwhile
                return
            self._kick()
        got = False
        for res in w.poll():
            aj = self.async_inflight.pop(res[1], None)
            if aj is not None:
                got = True
                if res[0] == "img" and self.canvas.doc is not None:
                    _, _, _, _, pw, ph, stride, data = res
                    aj[1](QImage(data, pw, ph, stride, QImage.Format.Format_RGB888).copy())
                continue
            it = self.inflight.pop(res[1], None)
            if it is None or it.job is None or it.job[0] != res[1]:
                continue
            _, kind, scale, rot, _ = it.job
            it.job = None
            got = True
            if it.scene() is None or rot != it.ref.rotation or self.canvas.doc is None:
                continue
            if res[0] != "img":
                try:  # same input in-process; stop retrying if MuPDF can't render it either
                    it.do_work()
                except Exception:  # noqa: BLE001
                    it.img, it.img_scale = QImage(1, 1, QImage.Format.Format_RGB888), it.full_scale()
                    it.img.fill(Qt.GlobalColor.white)
                continue
            _, _, px, py, pw, ph, stride, data = res
            if kind == "full" and it.img is not None:
                fs = it.full_scale()
                if abs(scale - fs) >= abs(it.img_scale - fs):
                    continue
            if kind == "detail" and abs(scale - self.canvas.target_scale()) > 1e-3:
                continue
            img = QImage(data, pw, ph, stride, QImage.Format.Format_RGB888).copy()
            rs = self.canvas.doc.view_rect(it.ref) * pymupdf.Matrix(scale, scale).prerotate(rot)
            it.apply_image(kind, scale, img, QRectF((px - rs.x0) / scale, (py - rs.y0) / scale, pw / scale, ph / scale))
        if w.failed:
            self._drop_worker()
            return
        if got:
            self._kick()
        if not self.inflight and not self.async_inflight:
            self.poll_timer.stop()

    # ------------------------------------------------------------- queue
    def want(self, item: PageItem) -> None:
        self.pending.add(item)
        self._kick()

    def want_thumb(self, key, fn) -> None:
        self.thumbs[key] = fn
        self._kick()

    def thumb_queued(self, key, async_key) -> bool:
        """A thumbnail for `key` is already waiting (or its async render is queued or running): asking again from
        every paint would re-arm the render timer each frame."""
        return (key in self.thumbs or async_key in self.asyncs
                or any(v[0] == async_key for v in self.async_inflight.values()))

    def render_async(self, key, ref: PageRef, scale: float, done) -> None:
        """Queue a whole-page render of `ref` at `scale`; `done(QImage)` is called on the GUI thread.

        Goes to the worker process when one is running. Same queue semantics as want_thumb
        (key dedupes, drop_thumb cancels)."""
        self.asyncs[key] = (ref, scale, done)
        self._kick()

    def drop_thumb(self, key) -> None:
        self.thumbs.pop(key, None)
        self.asyncs.pop(key, None)

    def defer(self, ms: int) -> None:
        self._defer_until = time.perf_counter() + ms / 1000
        self.timer.start(ms)

    def _kick(self) -> None:
        if not self.timer.isActive():
            delay = max(0, int((self._defer_until - time.perf_counter()) * 1000))
            self.timer.start(delay)

    def clear(self) -> None:
        self.pending.clear()
        self.thumbs.clear()
        self.asyncs.clear()
        self.lru.clear()
        self.total = 0
        self.reset_worker()

    def _tick(self) -> None:
        t0 = time.perf_counter()
        remote = self._remote() and self.canvas.doc is not None
        if remote:
            if self.inflight or self.async_inflight:
                self._poll()  # collect finished pages here too: a busy GUI must never starve the 5 ms poll timer
                remote = self._remote()  # (the poll drops a worker that died)
        if remote:
            self._dispatch()
            if self.inflight or self.async_inflight:
                self.poll_timer.start()
        # a worker that is still starting up gets a moment before pages fall back to this thread
        skip_pages = remote or (self.worker is not None and self.worker.starting)
        while True:
            job = self._next(skip_pages)
            if job is None:
                return
            job()
            if time.perf_counter() - t0 > 0.014:
                break
        if (self.pending and not skip_pages) or self.thumbs or (self.asyncs and not remote):
            self.timer.start(0)

    def _next(self, remote: bool = False):
        if self.canvas.doc is None:
            return None
        if not remote:
            for it in self.canvas.visible_pages():
                if it in self.pending:
                    if it.needs_work():
                        return it.do_work
                    self.pending.discard(it)
            self.pending.clear()
        if self.thumbs:
            _, fn = self.thumbs.popitem(last=False)
            return fn
        if self.asyncs and not remote:
            key, (ref, scale, done) = self.asyncs.popitem(last=False)
            return lambda: done(to_qimage(self.canvas.doc.render(ref, scale)[0]))
        return None

    def account(self, item: PageItem) -> None:
        old = self.lru.pop(item, 0)
        n = item.bytes_used()
        self.lru[item] = n
        self.total += n - old
        if self.total <= IMAGE_BUDGET:
            return
        visible = set(self.canvas.visible_pages())
        for it in list(self.lru):
            if self.total <= IMAGE_BUDGET * 0.8:
                break
            if it in visible:
                continue
            self.total -= self.lru.pop(it)
            it.evict()


class SelectionItem(QGraphicsPathItem):
    """Transient text-selection highlight (never saved)."""

    def __init__(self, parent):
        super().__init__(parent)
        c = QGuiApplication.palette().color(QPalette.ColorRole.Highlight)
        c.setAlpha(80)
        self.setBrush(c)
        self.setPen(Qt.PenStyle.NoPen)
        self.setZValue(50)


class Canvas(QGraphicsView):
    pageChanged = Signal(int)
    zoomChanged = Signal(float)
    toolChanged = Signal(str)
    selectionInfo = Signal()
    pagesChanged = Signal()
    filesDropped = Signal(list)
    documentChanged = Signal()
    layoutChanged = Signal()
    historyChanged = Signal()
    message = Signal(str)
    presentationExitRequested = Signal()
    presentingChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        sc = QGraphicsScene(self)
        sc.canvas = self
        sc.zoom = 1.0
        self.setScene(sc)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setOptimizationFlags(QGraphicsView.OptimizationFlag.DontSavePainterState)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self._update_bg()

        self.doc: PdfDocument | None = None
        self.items_by_uid: dict[int, PageItem] = {}
        self.order: list[PageItem] = []
        self.tops: list[float] = []
        self.zoom = 1.0
        self.fit_mode: str | None = "width"
        self.tool = "select"
        self.defaults = {k: dict(v) for k, v in DEFAULTS.items()}
        self.undo = QUndoStack(self)
        self.renderer = Renderer(self)
        self.annots = A.AnnotManager(self)
        self._cur_page = -1
        self._space = False
        self._pan = None
        self._create = None
        self._sel = None  # (page_item, start_pt, highlight_mode)
        self._sel_item: SelectionItem | None = None
        self._sel_text = ""
        self._sel_rects: list[QRectF] = []
        self._move_snapshot = None
        self.tool_keys: dict = {}
        # viewing state
        self.view_mode = "continuous"
        self.cover_alone = True  # spread mode: first page alone on the right, like a book
        self.night = False
        self.presenting = False
        self.rows: list[list[PageItem]] = []
        self.row_of: dict[PageItem, int] = {}
        self.index_of: dict[PageItem, int] = {}
        self._single = 0
        self._back: list[tuple] = []
        self._fwd: list[tuple] = []
        self._links: dict[tuple, list] = {}
        self._link_hover = None
        self._link_press = None
        self._flip_at = 0.0
        self.confirm_external = self._confirm_external
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        sc.selectionChanged.connect(self.selectionInfo)

    # ------------------------------------------------------------- document
    def set_document(self, doc: PdfDocument) -> None:
        self.scene().clear()
        self.renderer.clear()
        self.undo.clear()
        self.doc = doc
        self.items_by_uid.clear()
        self.order = []
        self._sel_item = None
        self._links.clear()
        self._link_hover = self._link_press = None
        self._back.clear()
        self._fwd.clear()
        self._single = 0
        self.annots.reset()
        self.sync_pages()
        self.fit_width()
        self.verticalScrollBar().setValue(0)
        self.historyChanged.emit()
        self.documentChanged.emit()

    def page_item(self, ref: PageRef) -> PageItem:
        it = self.items_by_uid.get(ref.uid)
        if it is None:
            it = PageItem(self, ref)
            self.items_by_uid[ref.uid] = it
        return it

    def sync_pages(self) -> None:
        """Re-sync scene with doc.pages (order/rotation/deletions)."""
        sc = self.scene()
        wanted = [self.page_item(r) for r in self.doc.pages]
        keep = set(wanted)
        for it in self.order:
            if it not in keep and it.scene() is sc:
                sc.removeItem(it)
        for it in wanted:
            if it.scene() is not sc:
                sc.addItem(it)
            it.sync_geometry()
        self.order = wanted
        self._layout()
        self._cur_page = -1
        self._scrolled()
        self.pagesChanged.emit()
        self.viewport().update()

    def _make_rows(self) -> list[list[PageItem]]:
        if self.view_mode != "spread":
            return [[it] for it in self.order]
        items = list(self.order)
        rows = [[items.pop(0)]] if self.cover_alone and items else []
        rows += [items[i:i + 2] for i in range(0, len(items), 2)]
        return rows

    def _layout(self) -> None:
        y = MARGIN
        maxw = 1.0
        self.tops = []
        self.rows = self._make_rows()
        self.row_of = {}
        self.index_of = {it: i for i, it in enumerate(self.order)}
        spread = self.view_mode == "spread"
        for r, row in enumerate(self.rows):
            h = max(it.h for it in row)
            if spread:  # the gutter sits at x=0: left pages end at -GAP/2, right pages start at +GAP/2
                right_side = len(row) == 1 and r == 0 and self.cover_alone
                for k, it in enumerate(row):
                    left = k == 0 and len(row) == 2 or (len(row) == 1 and not right_side)
                    it.setPos(-GAP / 2 - it.w if left else GAP / 2, y + (h - it.h) / 2)
                maxw = max(maxw, 2 * max(it.w for it in row) + GAP)
            else:
                it = row[0]
                it.setPos(-it.w / 2, y)
                maxw = max(maxw, it.w)
            for it in row:
                self.tops.append(y)
                self.row_of[it] = r
                it.setVisible(True)
            y += h + GAP
        self.max_w = maxw
        if self.view_mode == "single" and self.order:
            self._single = max(0, min(self._single, len(self.order) - 1))
            cur = self.order[self._single]
            for it in self.order:
                it.setVisible(it is cur)
            m = 0.0 if self.presenting else MARGIN
            self.scene().setSceneRect(QRectF(-cur.w / 2 - m, cur.y() - m, cur.w + 2 * m, cur.h + 2 * m))
        else:
            self.scene().setSceneRect(QRectF(-maxw / 2 - MARGIN, 0, maxw + 2 * MARGIN, y - GAP + MARGIN))
        self.layoutChanged.emit()

    def set_view_mode(self, mode: str | None = None, cover_alone: bool | None = None) -> None:
        """Continuous scroll, one page at a time, or two-page spreads; keeps the current page."""
        mode = mode or self.view_mode
        if mode not in VIEW_MODES:
            return
        if mode == self.view_mode and (cover_alone is None or cover_alone == self.cover_alone):
            return
        i = self.current_index()
        self.view_mode = mode
        if cover_alone is not None:
            self.cover_alone = cover_alone
        if not self.order:
            return
        self._single = max(0, i)
        self._layout()
        self._refit()
        self.go_to(max(0, i), record=False)
        self.viewport().update()

    def _refit(self) -> None:
        if self.fit_mode == "width":
            self.fit_width()
        elif self.fit_mode == "page":
            self.fit_page()

    def overlays_for(self, ref: PageRef):
        """What saving writes for a page: Leaf's items, changed/deleted existing annotations (untouched ones are
        left exactly as they are in the file)."""
        it = self.items_by_uid.get(ref.uid)
        return self.annots.for_save(it) if it else []

    # ------------------------------------------------------------- geometry
    def target_scale(self) -> float:
        return self.zoom * self.devicePixelRatioF()

    def visible_pages(self) -> list[PageItem]:
        if not self.order:
            return []
        if self.view_mode == "single":
            it = self.order[max(0, min(self._single, len(self.order) - 1))]
            return [it] if it.isVisible() else []
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        i0 = self._row_start(max(0, bisect.bisect_right(self.tops, vr.top()) - 1))
        out = []
        for i in range(i0, len(self.order)):
            if self.tops[i] > vr.bottom():
                break
            it = self.order[i]
            if it.y() + it.h >= vr.top() and it.y() <= vr.bottom():
                out.append(it)
        cy = vr.center().y()
        out.sort(key=lambda it: abs(it.y() + it.h / 2 - cy))
        return out

    def _row_start(self, i: int) -> int:
        while 0 < i < len(self.tops) and self.tops[i - 1] == self.tops[i]:
            i -= 1
        return i

    def page_at(self, sp: QPointF) -> PageItem | None:
        i = bisect.bisect_right(self.tops, sp.y()) - 1
        if 0 <= i < len(self.order):
            for j in range(self._row_start(i), i + 1):
                it = self.order[j]
                if it.isVisible() and it.scene_page_rect().contains(sp):
                    return it
        return None

    def current_index(self) -> int:
        if not self.order:
            return -1
        if self.view_mode == "single":
            return max(0, min(self._single, len(self.order) - 1))
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        # the row at the top of the view is current while enough of it is still visible: 35% of the view (tall
        # pages, zoomed in) or half the row (short pages, zoomed out: several fit, and right after "next page" the
        # top one must count); otherwise the next row
        n = len(self.order) - 1
        sb = self.verticalScrollBar()
        if sb.maximum() > 0 and sb.value() >= sb.maximum():
            # scrolled to the end: the last rows can't reach the top, so probe 35% down the view instead
            y = vr.top() + vr.height() * 0.35
            return self._row_start(max(0, min(n, bisect.bisect_right(self.tops, y + GAP / 2) - 1)))
        i0 = max(0, min(n, bisect.bisect_right(self.tops, vr.top()) - 1))
        j = bisect.bisect_right(self.tops, self.tops[i0])
        if j > n:
            return self._row_start(i0)
        bottom = self.tops[j] - GAP
        visible = bottom - max(vr.top(), self.tops[i0])
        if visible < min(vr.height() * 0.35, (bottom - self.tops[i0]) * 0.5):
            return self._row_start(j)
        return self._row_start(i0)

    def current_page_item(self) -> PageItem | None:
        i = self.current_index()
        return self.order[i] if i >= 0 else None

    def _scrolled(self, *_):
        i = self.current_index()
        if i != self._cur_page:
            self._cur_page = i
            self.pageChanged.emit(i)

    def go_to(self, i: int, record: bool | None = None) -> None:
        """Scroll page `i` to the top. Jumps of more than one page go into back/forward history
        unless `record` says otherwise."""
        if not 0 <= i < len(self.order):
            return
        cur = self.current_index()
        if record or (record is None and abs(i - cur) > 1):
            self.push_history()
        sb = self.verticalScrollBar()
        if self.view_mode == "single":
            if i != self._single:
                self._single = i
                self._layout()
                self.viewport().update()
            sb.setValue(sb.minimum())
        else:
            y = self.tops[i] - GAP / 2
            dy = self.mapFromScene(QPointF(0, y)).y()
            sb.setValue(sb.value() + dy)
        self._scrolled()

    def go_to_point(self, i: int, pt: QPointF | None, record: bool = True) -> None:
        """Show page `i` with `pt` (source page.rect space) near the top of the viewport."""
        if not 0 <= i < len(self.order):
            return
        if pt is None:
            self.go_to(i, record=record)
            return
        if record:
            self.push_history()
        self.go_to(i, record=False)
        it = self.order[i]
        r = it.content.rect()
        pt = QPointF(min(max(pt.x(), r.left()), r.right()), min(max(pt.y(), r.top()), r.bottom()))
        sp = it.content.mapToScene(pt)
        dy = self.mapFromScene(sp).y() - 24
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + dy)
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        if not vis.left() <= sp.x() <= vis.right():
            self.centerOn(sp.x(), vis.center().y())
        self._scrolled()

    def next_page(self, d: int = 1) -> None:
        """Next/previous page; moves by whole spreads in spread mode."""
        i = self.current_index()
        if i < 0:
            return
        if self.view_mode == "spread" and self.rows:
            r = max(0, min(len(self.rows) - 1, self.row_of[self.order[i]] + d))
            self.go_to(self.index_of[self.rows[r][0]], record=False)
        else:
            self.go_to(max(0, min(len(self.order) - 1, i + d)), record=False)

    def ensure_rect_visible(self, item_rect: QRectF, item: QGraphicsItem) -> None:
        pg = _page_of(item)
        if self.view_mode == "single" and pg is not None and pg in self.index_of and not pg.isVisible():
            self.go_to(self.index_of[pg], record=False)
        self.ensureVisible(item.mapRectToScene(item_rect), 60, 80)

    # ----------------------------------------------------------- history
    def _nav_state(self) -> tuple | None:
        it = self.current_page_item()
        if it is None:
            return None
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        return (it.ref.uid, round((vr.top() - it.y()) / max(it.h, 1.0), 4))

    def _restore_nav(self, state) -> bool:
        uid, frac = state
        it = self.items_by_uid.get(uid)
        if it is None or it not in self.index_of:
            return False
        self.go_to(self.index_of[it], record=False)
        y = it.y() + frac * it.h
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + self.mapFromScene(QPointF(0, y)).y())
        self._scrolled()
        return True

    def push_history(self) -> None:
        s = self._nav_state()
        if s is None or (self._back and self._back[-1] == s):
            return
        self._back.append(s)
        del self._back[:-100]
        self._fwd.clear()
        self.historyChanged.emit()

    def can_go_back(self) -> bool:
        return bool(self._back)

    def can_go_forward(self) -> bool:
        return bool(self._fwd)

    def nav_back(self) -> None:
        self._nav(self._back, self._fwd)

    def nav_forward(self) -> None:
        self._nav(self._fwd, self._back)

    def _nav(self, src: list, dst: list) -> None:
        while src:
            s = src.pop()
            cur = self._nav_state()
            if self._restore_nav(s):
                if cur is not None:
                    dst.append(cur)
                break
        self.historyChanged.emit()

    # ------------------------------------------------------------- links
    def links_on(self, it: PageItem) -> list[tuple[QRectF, dict]]:
        """Links of a page; get_links() "from" rects are already in page.rect (display) space."""
        key = (it.ref.src.id, it.ref.index)
        out = self._links.get(key)
        if out is None:
            out = []
            try:
                for ln in self.doc.page(it.ref).get_links():
                    r = ln["from"]
                    out.append((QRectF(r.x0, r.y0, r.width, r.height), ln))
            except Exception:  # noqa: BLE001
                pass
            self._links[key] = out
        edited = getattr(self.doc, "links", None)
        if edited and it.ref.uid in edited:  # structure (leaf/links.py): this page's links were edited
            from .links import shown_links
            return shown_links(out, edited[it.ref.uid])
        return out

    def link_at(self, sp: QPointF):
        """(page item, link dict) under scene point `sp`, or None."""
        pg = self.page_at(sp)
        if pg is None or self.doc is None:
            return None
        lp = pg.content.mapFromScene(sp)
        for r, ln in self.links_on(pg):
            if r.contains(lp):
                return pg, ln
        return None

    def page_index_for(self, src, index: int) -> int | None:
        """Position in the current page list of page `index` of `src` (respects reordering)."""
        for i, it in enumerate(self.order):
            if it.ref.src is src and it.ref.index == index:
                return i
        return None

    def _link_target(self, pg: PageItem, ln: dict) -> int | None:
        uid = ln.get("target_uid")  # structure (leaf/links.py): links made in Leaf point at a page uid
        if uid is not None or "leaf_link" in ln:
            return next((i for i, it in enumerate(self.order) if it.ref.uid == uid), None)
        return self.page_index_for(pg.ref.src, ln["page"])

    def link_target_text(self, pg: PageItem, ln: dict) -> str:
        kind = ln.get("kind")
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and ln.get("page", -1) >= 0:
            i = self._link_target(pg, ln)
            return f"Go to page {i + 1}" if i is not None else "Page not in this document"
        if kind == pymupdf.LINK_URI:
            return ln.get("uri", "")
        if kind in (pymupdf.LINK_GOTOR, pymupdf.LINK_LAUNCH):
            return ln.get("file", "")
        return ""

    def follow_link(self, pg: PageItem, ln: dict) -> None:
        kind = ln.get("kind")
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and ln.get("page", -1) >= 0:
            i = self._link_target(pg, ln)
            if i is None:
                self.message.emit("The link's target page isn't in this document.")
                return
            to = ln.get("to")
            # LINK_GOTO targets are already in the target page.rect space; (0, 0) means "the page"
            pt = QPointF(to.x, to.y) if to is not None and (to.x or to.y) else None
            self.go_to_point(i, pt)
            return
        if kind == pymupdf.LINK_URI:
            uri = (ln.get("uri") or "").strip()
            if not uri or uri.lower().startswith("javascript:"):
                return
            if self.confirm_external(uri):
                QDesktopServices.openUrl(QUrl(uri))
            return
        if kind in (pymupdf.LINK_GOTOR, pymupdf.LINK_LAUNCH):
            f = ln.get("file") or ""
            if not f:
                return
            if not os.path.isabs(f) and self.doc.path:
                f = os.path.join(os.path.dirname(self.doc.path), f)
            if self.confirm_external(f):
                QDesktopServices.openUrl(QUrl.fromLocalFile(f))

    def _confirm_external(self, target: str) -> bool:
        r = QMessageBox.question(self, "Open link", f"Open this link outside Leaf?\n\n{target}",
                                 QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
                                 QMessageBox.StandardButton.Open)
        return r == QMessageBox.StandardButton.Open

    def _hover_link(self, e) -> None:
        sp = self.mapToScene(e.position().toPoint())
        hit = self.link_at(sp) if self.tool == "select" and not self._space else None
        if hit is not None:
            top = self.itemAt(e.position().toPoint())
            while top is not None and not (is_overlay(top) or isinstance(top, (FieldItem, PageItem))):
                top = top.parentItem()
            if top is not None and not isinstance(top, PageItem):
                hit = None
        ln = hit[1] if hit else None
        if ln is self._link_hover:
            return
        self._link_hover = ln
        if ln is not None:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            QToolTip.showText(e.globalPosition().toPoint(), self.link_target_text(*hit), self.viewport())
        else:
            self.viewport().setCursor(self._tool_cursor())
            QToolTip.hideText()

    # -------------------------------------------------------- night mode
    def set_night(self, on: bool) -> None:
        """Invert rendered page pixels for dark-room reading; overlays are untouched."""
        if on == self.night:
            return
        self.night = on
        for it in self.items_by_uid.values():
            it.invert_images()

    # ----------------------------------------------------------------- zoom
    def zoom_to(self, z: float, anchor: QPointF | None = None, keep_fit: bool = False) -> None:
        if not keep_fit:
            self.fit_mode = None
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if abs(z - self.zoom) < 1e-6:
            return
        if anchor is None:
            anchor = QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        before = self.mapToScene(anchor.toPoint())
        self.zoom = z
        self.scene().zoom = z
        self.setTransform(QTransform.fromScale(z, z))
        after = self.mapFromScene(before)
        delta = after - anchor.toPoint()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + delta.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + delta.y())
        self.renderer.defer(110)
        self.zoomChanged.emit(z)
        self._scrolled()

    def zoom_step(self, direction: int) -> None:
        steps = [0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0, 16.0]
        if direction > 0:
            nxt = next((s for s in steps if s > self.zoom + 1e-3), steps[-1])
        else:
            nxt = next((s for s in reversed(steps) if s < self.zoom - 1e-3), steps[0])
        self.zoom_to(nxt)

    def fit_width(self) -> None:
        """Fit page width; sticks across window resizes until the user zooms."""
        if self.order:
            vw = self.viewport().width() - 2
            z = min(max(ZOOM_MIN, vw / (self.max_w + 2 * MARGIN)), 2.5)
            if abs(z - self.zoom) > 1e-6:
                # keep the reading position (page + offset) rather than the viewport center: a resize while
                # hidden (e.g. behind another central view) would otherwise drift far down the document
                st = self._nav_state()
                self.zoom_to(z, keep_fit=True)
                if st is not None:
                    self._restore_nav(st)
            self.fit_mode = "width"

    def fit_page(self) -> None:
        it = self.current_page_item()
        if it:
            vw, vh = self.viewport().width(), self.viewport().height()
            w, h = it.w, it.h
            if self.view_mode == "spread" and it in self.row_of:
                row = self.rows[self.row_of[it]]
                w, h = 2 * max(p.w for p in row) + GAP, max(p.h for p in row)
            if self.presenting:
                z = min((vw - 1) / w, (vh - 1) / h)
            else:
                z = min((vw - 40) / w, (vh - 2 * GAP) / h)
            self.zoom_to(z, keep_fit=True)
            self.fit_mode = "page"
            self.go_to(self.order.index(it), record=False)

    def wheelEvent(self, e) -> None:
        mods = e.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            dy = e.angleDelta().y() or e.pixelDelta().y()
            self.zoom_to(self.zoom * (1.0015 ** dy), e.position())
            e.accept()
            return
        if mods & Qt.KeyboardModifier.ShiftModifier and not e.angleDelta().x():
            d = e.pixelDelta().y() or e.angleDelta().y()
            hb = self.horizontalScrollBar()
            hb.setValue(hb.value() - (d if e.pixelDelta().y() else int(d / 120 * hb.singleStep() * 3)))
            e.accept()
            return
        if self.view_mode == "single" or self.presenting:
            dy = e.angleDelta().y() or e.pixelDelta().y()
            sb = self.verticalScrollBar()
            edge = (dy < 0 and sb.value() >= sb.maximum()) or (dy > 0 and sb.value() <= sb.minimum())
            if dy and edge:
                now = time.monotonic()
                if now - self._flip_at > 0.35:
                    self._flip_at = now
                    self.next_page(-1 if dy > 0 else 1)
                    if dy > 0 and not self.presenting:
                        sb.setValue(sb.maximum())
                e.accept()
                return
        super().wheelEvent(e)

    def viewportEvent(self, e) -> bool:
        if e.type() == QEvent.Type.NativeGesture and e.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.zoom_to(self.zoom * (1 + e.value()), e.position())
            return True
        return super().viewportEvent(e)

    # ---------------------------------------------------------------- tools
    def set_tool(self, tool: str) -> None:
        if tool == self.tool:
            return
        self.tool = tool
        self._end_text_selection()
        self.viewport().setCursor(self._tool_cursor())
        self.toolChanged.emit(tool)
        self.selectionInfo.emit()

    def _tool_cursor(self) -> Qt.CursorShape:
        t = self.tool
        if t in ("text", "highlight"):
            return Qt.CursorShape.IBeamCursor
        if t in CREATE_TOOLS or t in ("note", "redact"):
            return Qt.CursorShape.CrossCursor
        if t == "hand":
            return Qt.CursorShape.OpenHandCursor
        return Qt.CursorShape.ArrowCursor

    def selected_overlays(self) -> list:
        return [i for i in self.scene().selectedItems() if is_overlay(i)]

    def _editing_text(self) -> bool:
        fi = self.scene().focusItem()
        return fi is not None and hasattr(fi, "textInteractionFlags") and \
            fi.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def mousePressEvent(self, e) -> None:
        pos = e.position().toPoint()
        sp = self.mapToScene(pos)
        btn = e.button()
        if self.presenting:
            hit = self.link_at(sp) if btn == Qt.MouseButton.LeftButton else None
            if hit is not None:
                self.follow_link(*hit)
            elif btn in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                self.next_page(1 if btn == Qt.MouseButton.LeftButton else -1)
            return
        if btn == Qt.MouseButton.MiddleButton or (btn == Qt.MouseButton.LeftButton and (self._space or self.tool == "hand")):
            self._pan = e.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if btn != Qt.MouseButton.LeftButton or self.doc is None:
            super().mousePressEvent(e)
            return
        hit = self.itemAt(pos)
        top = hit
        while top is not None and not (is_overlay(top) or isinstance(top, (FieldItem, PageItem))):
            top = top.parentItem()
        # form fields and editing text always get the click in non-drawing tools
        if isinstance(top, FieldItem) and self.tool not in CREATE_TOOLS and self.tool != "redact":
            super().mousePressEvent(e)
            return
        if self.tool == "select":
            if top is not None and is_overlay(top):
                super().mousePressEvent(e)
                self._move_snapshot = {i: QPointF(i.pos()) for i in self.scene().selectedItems() if is_overlay(i)}
                return
            self.scene().clearSelection()
            self._clear_focus()
            pg = self.page_at(sp)
            self._end_text_selection()
            if pg is not None:
                self._sel = (pg, pg.content.mapFromScene(sp), False)
                hit = self.link_at(sp)
                self._link_press = (hit, e.position()) if hit else None
            return
        pg = self.page_at(sp)
        if self.tool == "text":
            if isinstance(top, TextItem):
                super().mousePressEvent(e)
                if not top.editing():
                    top.start_edit()
                return
            if self._editing_text():  # first click just finishes the current box
                self._clear_focus()
                return
            if pg is None:
                return
            lp = pg.content.mapFromScene(sp)
            item = TextItem(QPointF(0, 0), self.defaults["text"])
            item.setParentItem(pg.content)
            item.setPos(lp - QPointF(TextItem.MARGIN, item.boundingRect().height() / 2))
            self.scene().clearSelection()
            item.setSelected(True)
            item.start_edit()
            return
        if pg is None:
            return
        lp = pg.content.mapFromScene(sp)
        if self.tool == "note":
            if top is not None and A.is_annotation(top):
                self.scene().clearSelection()
                top.setSelected(True)
                self.annots.open_editor(top)
            else:
                self.annots.add_note(pg, lp)
            return
        if self.tool == "highlight":
            self._end_text_selection()
            self._sel = (pg, lp, True)
            return
        if self.tool == "redact":  # leaf/redact.py: drag across text (word-snapped) or draw a box
            from .redact import canvas_press
            canvas_press(self, pg, lp, e)
            return
        if self.tool in CREATE_TOOLS:
            props = self.defaults[self.tool]
            if self.tool in ("rect", "ellipse", "whiteout"):
                item = ShapeItem(self.tool, QRectF(lp, QSizeF(0, 0)), props)
            elif self.tool in ("line", "arrow"):
                item = LineItem(self.tool, lp, lp, props)
            else:
                item = InkItem(QPointF(0, 0), props)
                item.setPos(lp)
            item.setParentItem(pg.content)
            self.scene().clearSelection()
            self._create = (pg, item, lp)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._pan is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        sp = self.mapToScene(e.position().toPoint())
        if self._create is not None:
            pg, item, start = self._create
            lp = pg.content.mapFromScene(sp)
            r = pg.content.rect()
            lp = QPointF(min(max(lp.x(), r.left()), r.right()), min(max(lp.y(), r.top()), r.bottom()))
            item.drag_create(start, lp, bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        if self._sel is not None:
            pg, start, hl = self._sel
            if self._link_press is not None:
                if (e.position() - self._link_press[1]).manhattanLength() < 5:
                    return
                self._link_press = None  # dragging from a link selects text instead
            self._update_text_selection(pg, start, pg.content.mapFromScene(sp))
            return
        if self.doc is not None and not self.presenting and not e.buttons():
            self._hover_link(e)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._pan is not None:
            self._pan = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space else self._tool_cursor())
            return
        if self._create is not None:
            pg, item, start = self._create
            self._create = None
            if item.is_trivial():
                if isinstance(item, ShapeItem):  # click = default-sized shape
                    w, h = (140, 90) if item.kind != "whiteout" else (120, 24)
                    item.set_geometry((start - QPointF(w / 2, h / 2), QRectF(0, 0, w, h)))
                else:
                    self.scene().removeItem(item)
                    return
            self.undo.push(C.AddItems([item], f"Add {item.kind}"))
            if self.tool != "ink":
                item.setSelected(True)
            return
        if self._sel is not None:
            pg, start, hl = self._sel
            self._sel = None
            press, self._link_press = self._link_press, None
            if press is not None and not hl:
                self._end_text_selection()
                self.follow_link(*press[0])
                return
            if hl == "redact":  # leaf/redact.py: one mark per selected line
                from .redact import canvas_text_done
                canvas_text_done(self, pg, list(self._sel_rects), self._sel_text)
                self._end_text_selection()
                return
            if hl:
                rects, text = list(self._sel_rects), self._sel_text
                self._end_text_selection()
                if rects:
                    item = HighlightItem(rects, self.defaults["highlight"])
                    item.marked_text = text
                    item.setParentItem(pg.content)
                    self.undo.push(C.AddItems([item], "Highlight"))
            return
        super().mouseReleaseEvent(e)
        if self._move_snapshot:
            moves = {i: (old, QPointF(i.pos())) for i, old in self._move_snapshot.items() if i.pos() != old}
            if moves:
                self.undo.push(C.MoveItems(moves))
        self._move_snapshot = None

    def contextMenuEvent(self, e) -> None:
        """Items with their own menu (form fields) get it first; then `context_menu(event)` (features/menus.py)
        shows the page's menu. Viewport event filters (an image under the cursor) run before both."""
        super().contextMenuEvent(e)
        handler = getattr(self, "context_menu", None)
        if not e.isAccepted() and handler is not None and self.doc is not None and not self.presenting:
            if handler(e):
                e.accept()

    def mouseDoubleClickEvent(self, e) -> None:
        pos = e.position().toPoint()
        hit = self.itemAt(pos)
        if isinstance(hit, TextItem) and self.tool in ("select", "text"):
            hit.start_edit()
        elif hit is not None and A.is_annotation(hit) and self.tool in ("select", "note"):
            self.annots.open_editor(hit)  # the comment of a note or any annotation
            return
        super().mouseDoubleClickEvent(e)

    # ------------------------------------------------------- text selection
    def _word_index(self, words, pt: QPointF) -> int:
        best, bi = 1e18, 0
        x, y = pt.x(), pt.y()
        for i, w in enumerate(words):
            dx = 0 if w[0] <= x <= w[2] else min(abs(x - w[0]), abs(x - w[2]))
            dy = 0 if w[1] <= y <= w[3] else min(abs(y - w[1]), abs(y - w[3]))
            d = dx + dy * 4
            if d < best:
                best, bi = d, i
                if d == 0:
                    break
        return bi

    def _update_text_selection(self, pg: PageItem, a: QPointF, b: QPointF) -> None:
        words = pg.ref.src.words(pg.ref.index, pg.ref.uid)  # text edits: the page as shown
        if not words:
            return
        i, j = sorted((self._word_index(words, a), self._word_index(words, b)))
        lines: OrderedDict = OrderedDict()
        for w in words[i:j + 1]:
            k = (w[5], w[6])
            r = QRectF(QPointF(w[0], w[1]), QPointF(w[2], w[3]))
            if k in lines:
                lines[k][0] = lines[k][0].united(r)
                lines[k][1].append(w[4])
            else:
                lines[k] = [r, [w[4]]]
        self._sel_rects = [v[0] for v in lines.values()]
        self._sel_text = "\n".join(" ".join(v[1]) for v in lines.values())
        if self._sel_item is None or self._sel_item.parentItem() is not pg.content:
            self._end_text_selection(keep_state=True)
            self._sel_item = SelectionItem(pg.content)
        path = QPainterPath()
        for r in self._sel_rects:
            path.addRect(r)
        self._sel_item.setPath(path)

    def _end_text_selection(self, keep_state: bool = False) -> None:
        if self._sel_item is not None and self._sel_item.scene() is not None:
            self.scene().removeItem(self._sel_item)
        self._sel_item = None
        if not keep_state:
            self._sel_rects = []
            self._sel_text = ""

    def selected_text(self) -> str:
        return self._sel_text

    def highlight_selection(self) -> bool:
        if not self._sel_rects or self._sel_item is None:
            return False
        content = self._sel_item.parentItem()
        item = HighlightItem(self._sel_rects, self.defaults["highlight"])
        item.marked_text = self._sel_text
        item.setParentItem(content)
        self.undo.push(C.AddItems([item], "Highlight"))
        self._end_text_selection()
        return True

    def _clear_focus(self) -> None:
        fi = self.scene().focusItem()
        if fi is not None:
            fi.clearFocus()

    def text_edit_finished(self, item: TextItem, old: str) -> None:
        new = item.content()  # text + run formatting
        if not item.committed:
            if item.toPlainText().strip():
                item.committed = True
                self.undo.push(C.AddItems([item], "Add text"))
            else:
                item.setSelected(False)
                if item.scene() is not None:
                    self.scene().removeItem(item)
        elif new != old:
            if item.toPlainText().strip():
                self.undo.push(C.EditText(item, old, new))
            else:
                item.set_content(old)
                self.undo.push(C.RemoveItems([item], "Delete text"))

    # ------------------------------------------------------------- keyboard
    def keyPressEvent(self, e) -> None:
        if self.presenting:
            self._presentation_key(e)
            return
        if self._editing_text():
            super().keyPressEvent(e)
            return
        k = e.key()
        if self.doc is not None and k in self.tool_keys and e.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier):
            self.set_tool(self.tool_keys[k])
            return
        if self.doc is not None and e.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier) \
                and self._navigation_key(k):
            return
        if k == Qt.Key.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return
        sel = self.selected_overlays()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and sel:
            self.delete_selected()
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and len(sel) == 1 and A.is_annotation(sel[0]):
            self.annots.open_editor(sel[0])
            return
        if k == Qt.Key.Key_Escape:
            if sel:
                self.scene().clearSelection()
            elif self._sel_rects:
                self._end_text_selection()
            else:
                self.set_tool("select")
            return
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        movable = [i for i in sel if i.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable]
        if k in arrows and movable:
            shift = e.modifiers() & Qt.KeyboardModifier.ShiftModifier  # steps: Settings › Editing
            step = getattr(self, "nudge_big", 10.0) if shift else getattr(self, "nudge_step", 1.0)
            dx, dy = arrows[k]
            moves = {}
            for it in movable:
                old = QPointF(it.pos())
                it.setPos(old + QPointF(dx * step, dy * step))
                moves[it] = (old, QPointF(it.pos()))
            self.undo.push(C.MoveItems(moves, "Nudge", nudge=True))
            return
        super().keyPressEvent(e)

    def _navigation_key(self, k) -> bool:
        if not self.order:
            return False
        sb = self.verticalScrollBar()
        page_keys = {Qt.Key.Key_PageUp: -1, Qt.Key.Key_PageDown: 1}
        if k in page_keys:
            d = page_keys[k]
            if self.view_mode == "single":
                self.next_page(d)
            else:
                at_edge = sb.value() >= sb.maximum() if d > 0 else sb.value() <= sb.minimum()
                if not at_edge:
                    sb.setValue(sb.value() + d * max(40, self.viewport().height() - 40))
            return True
        if k == Qt.Key.Key_Home:
            self.go_to(0, record=True)
            sb.setValue(sb.minimum())
            return True
        if k == Qt.Key.Key_End:
            self.go_to(len(self.order) - 1, record=True)
            sb.setValue(sb.maximum())
            return True
        return False

    def _presentation_key(self, e) -> None:
        k = e.key()
        fwd = {Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space, Qt.Key.Key_PageDown, Qt.Key.Key_Return,
               Qt.Key.Key_Enter, Qt.Key.Key_N}
        back = {Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace, Qt.Key.Key_P}
        if k in fwd:
            self.next_page(-1 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier and k == Qt.Key.Key_Space else 1)
        elif k in back:
            self.next_page(-1)
        elif k == Qt.Key.Key_Home:
            self.go_to(0, record=False)
        elif k == Qt.Key.Key_End:
            self.go_to(len(self.order) - 1, record=False)
        elif k == Qt.Key.Key_Escape:
            self.presentationExitRequested.emit()

    def set_presenting(self, on: bool) -> None:
        """Presentation look: one page fit to the screen on black; clicks/keys advance."""
        if on == self.presenting:
            return
        i = self.current_index()
        if on:
            self._pre_present = (self.view_mode, self.fit_mode, self.zoom, self.tool)
            self.set_tool("select")
            self.presenting = True
            self.view_mode = "single"
            self._single = max(0, i)
            self.setBackgroundBrush(Qt.GlobalColor.black)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._layout()
            self.fit_page()
        else:
            mode, fit, zoom, tool = self._pre_present
            self.presenting = False
            self.view_mode = mode
            self._single = max(0, i)
            self._update_bg()
            self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._layout()
            self.fit_mode = fit
            if fit:
                self._refit()
            else:
                self.zoom_to(zoom)
            self.go_to(max(0, i), record=False)
            self.set_tool(tool)
        self.viewport().update()
        self.presentingChanged.emit(on)

    def keyReleaseEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Space and not e.isAutoRepeat() and self._space:
            self._space = False
            self.viewport().setCursor(self._tool_cursor())
            return
        super().keyReleaseEvent(e)

    def delete_selected(self) -> None:
        sel = self.annots.with_replies(self.selected_overlays())  # a comment goes with its replies
        if sel:
            self.undo.push(C.RemoveItems(sel, "Delete"))

    # ---------------------------------------------------------- props/forms
    def apply_props(self, delta: dict) -> None:
        ed = editing_text_item(self.scene())
        if ed is not None:  # character props go to the text selection (document undo); box props continue below
            self.defaults["text"].update({k: v for k, v in delta.items() if k in self.defaults["text"]})
            delta = ed.format_selection(delta)
            if not delta:
                return
        sel = self.selected_overlays()
        if sel:
            changes = {}
            for it in sel:
                new = dict(it.props)
                new.update({k: v for k, v in delta.items() if k in it.props})
                if new != it.props:
                    changes[it] = (dict(it.props), new)
                if it.kind in self.defaults and not isinstance(it, A.Adopted):  # other apps' styles aren't defaults
                    self.defaults[it.kind].update({k: v for k, v in delta.items() if k in self.defaults[it.kind]})
            if changes:
                self.undo.push(C.SetProps(changes))
        elif self.tool in self.defaults:
            d = self.defaults[self.tool]
            d.update({k: v for k, v in delta.items() if k in d})

    def set_form_values(self, values: dict, text: str = "Fill form") -> None:
        """values: key -> value (C.UNSET = back to the PDF's own value); one undo step."""
        changes = {}
        for key, v in values.items():
            old = self.doc.form_values.get(key, C.UNSET)
            if old is not v and old != v:
                changes[key] = (old, v)
        if changes:
            self.undo.push(C.SetFormValues(self, changes, text))

    # ------------------------------------------------------- text edits (leaf/textedit.py)
    def preview_text_hidden(self, ref: PageRef, unit, sync: bool = True) -> None:
        """While the inline text editor is open: show page `ref` without `unit`'s glyphs (original or edited);
        None shows it normally again. `sync=False` only drops the preview state (the caller syncs, e.g. by
        pushing an EditPageText, so the page re-renders once instead of twice)."""
        hidden = self.__dict__.setdefault("_text_hidden", {})
        if unit is None:
            if hidden.pop(ref.uid, None) is None:
                return
        else:
            hidden[ref.uid] = unit.key
        if sync:
            self.sync_text(ref)

    def sync_text(self, ref: PageRef) -> list[str]:
        """Show page `ref` with its current text edits (doc.text_edits) on screen, in thumbnails, search and
        selection. Returns the edits' warnings."""
        hidden = self.__dict__.get("_text_hidden", {}).get(ref.uid)
        warnings = ref.src.set_text_edits(ref.uid, ref.index, dict(self.doc.text_edits.get(ref.uid, {})), hidden)
        it = self.items_by_uid.get(ref.uid)
        if it is not None:
            it.invalidate()
        if it is not None and self._sel_item is not None and _page_of(self._sel_item) is it:
            self._end_text_selection()  # its words changed
        self.annots.thumbsStale.emit([ref.uid])  # sidebar/organizer thumbnails re-render
        return warnings

    def _all_fields(self):
        for it in self.order:
            for f in it.fields or []:
                yield f

    def refresh_fields(self, keys) -> None:
        keys = set(keys)
        for f in self._all_fields():
            if f.key in keys:
                f.refresh()

    def fields_named(self, uid: int, name: str) -> list[FieldItem]:
        it = self.items_by_uid.get(uid)
        return [f for f in (it.fields or []) if f.name == name] if it else []

    def focus_next_field(self, field: FieldItem | None, back: bool = False) -> FieldItem | None:
        """Move keyboard focus along the document's widget order (page by page, wrapping)."""
        from .forms import focus_field, focusable, iter_fields

        for f in iter_fields(self, field, back):
            if focusable(f):
                focus_field(self, f)
                return f
        return None

    # ----------------------------------------------------------- insertion
    def insert_image(self, image: QImage, kind: str = "image") -> None:
        pg = self.current_page_item()
        if pg is None or image.isNull():
            return
        pr = pg.content.rect()
        vis = pg.content.mapFromScene(self.mapToScene(self.viewport().rect())).boundingRect() & pr
        center = vis.center() if not vis.isEmpty() else pr.center()
        target_w = 160.0 if kind == "signature" else min(pr.width() * 0.45, image.width() * 0.75)
        w = min(target_w, pr.width() * 0.9)
        h = w * image.height() / max(1, image.width())
        if h > pr.height() * 0.9:
            h = pr.height() * 0.9
            w = h * image.width() / max(1, image.height())
        item = ImageItem(kind, QRectF(center.x() - w / 2, center.y() - h / 2, w, h), image, self.defaults.get(kind))
        item.setParentItem(pg.content)
        self.undo.push(C.AddItems([item], f"Insert {kind}"))
        self.scene().clearSelection()
        item.setSelected(True)
        self.set_tool("select")

    # ---------------------------------------------------------------- misc
    def _update_bg(self) -> None:
        from . import style

        if getattr(self, "presenting", False):  # black while presenting (set_presenting)
            return
        if style.active():  # the style's backdrop token
            self.setBackgroundBrush(style.current().canvas)
            return
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Window)
        # a calm backdrop one step away from the window color, so white pages stand out in light and dark
        bg = base.darker(108) if base.lightness() > 128 else base.lighter(118)
        self.setBackgroundBrush(bg)

    def changeEvent(self, e) -> None:
        if e.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._update_bg()
        super().changeEvent(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.doc is not None and self.fit_mode == "width":
            self.fit_width()
        elif self.doc is not None and self.fit_mode == "page":
            self.fit_page()
        self._scrolled()

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            e.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            super().dropEvent(e)
