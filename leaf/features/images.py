"""Images already in the PDF, and File › Export.

Edit images: with the Select tool, click an image of the page (where there is no text under the pointer; Alt+click
picks an image anyway, also a page-sized background or scan) to select it. Drag to move, drag a corner to resize
(keeps the proportions; Shift resizes freely), arrows nudge, Delete removes it, Ctrl+C copies it, Esc deselects.
Right-click an image (or Edit › Image) for Replace…, Rotate left/right, Save image as…, Copy, Delete and Restore
original. Every change is one undo step and edits exactly one draw of the image (leaf/images.py), so other uses of
the same image object are untouched.

Export (File › Export): pages as images, all images, text, Markdown; from what Save would write (leaf/export.py),
in small steps with a progress dialog and Cancel.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtCore import QEventLoop
from PySide6.QtGui import (
    QAction, QColor, QGuiApplication, QImage, QPainter, QPalette, QPen, QPolygonF, QTransform, QUndoCommand,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGraphicsItem, QGraphicsObject,
    QHBoxLayout, QLineEdit, QMenu, QMessageBox, QProgressDialog, QPushButton, QSpinBox, QWidget,
)

from .. import export as X
from .. import images as I
from .. import osint, pageops

APP = "Leaf"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp *.jxr *.pnm *.jp2 *.jpx)"
DRAG_PX = 3  # screen pixels before a press becomes a move


def _toast(win, text: str, ms: int = 2500) -> None:
    try:
        win.toast(text, ms=ms)
    except Exception:  # noqa: BLE001
        pass


def sync(cv, ref) -> list[str]:
    """Show page `ref` with its current image edits (and text edits) on screen, in thumbnails and search."""
    ref.src.set_image_edits(ref.uid, ref.index, cv.doc.image_edits.get(ref.uid))
    return cv.sync_text(ref)


class EditImage(QUndoCommand):
    """Set (or with None, remove) the edit of one image draw."""

    def __init__(self, tool, ref, key: tuple, old, new, text: str):
        super().__init__(text)
        self.tool, self.ref, self.key, self.old, self.new = tool, ref, key, old, new

    def _set(self, e) -> None:
        cv = self.tool.cv
        per = cv.doc.image_edits.setdefault(self.ref.uid, {})
        if e is None or e.is_noop:
            per.pop(self.key, None)
        else:
            per[self.key] = e
        if not per:
            cv.doc.image_edits.pop(self.ref.uid, None)
        try:
            sync(cv, self.ref)
        except Exception as ex:  # noqa: BLE001  (checked before the push; keep the model and say so)
            _toast(self.tool.win, f"The page couldn't show this image change: {ex}", 4000)
        self.tool.refresh()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def _accent() -> QColor:
    c = QApplication.palette().color(QPalette.ColorRole.Highlight)
    return c if c.isValid() else QColor(0, 120, 215)


class ImageFrame(QGraphicsObject):
    """Selection frame of an image draw (page.rect space, child of the page's content item): outline, corner
    handles and, while dragging, a see-through preview at the new place."""

    def __init__(self, parent, preview: QImage | None):
        super().__init__(parent)
        self.setZValue(1e6)
        self.preview = preview
        self.rect = QRectF()
        self.quad: list[QPointF] = []
        self.ghost: QTransform | None = None  # unit square -> page.rect, while dragging

    def set_shape(self, rect: QRectF, quad, ghost: QTransform | None = None) -> None:
        self.prepareGeometryChange()
        self.rect = QRectF(rect)
        self.quad = [QPointF(x, y) for x, y in quad]
        self.ghost = ghost
        self.update()

    def handle_size(self) -> float:
        s = self.scene()
        return 8.0 / max(getattr(s, "zoom", 1.0) if s else 1.0, 1e-3)

    def boundingRect(self) -> QRectF:
        h = self.handle_size() + 2
        return self.rect.adjusted(-h, -h, h, h)

    def corners(self) -> list[QPointF]:
        r = self.rect
        return [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]

    def corner_at(self, p: QPointF):
        h = self.handle_size()
        for i, c in enumerate(self.corners()):
            if abs(c.x() - p.x()) <= h and abs(c.y() - p.y()) <= h:
                return i
        return None

    def paint(self, p: QPainter, opt, widget=None) -> None:
        acc = _accent()
        if self.ghost is not None and self.preview is not None and not self.preview.isNull():
            p.save()
            p.setOpacity(0.6)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setTransform(self.ghost, True)
            p.drawImage(QRectF(0, 0, 1, 1), self.preview)
            p.restore()
        px = 1.0 / max(abs(p.worldTransform().m11()) + abs(p.worldTransform().m12()), 1e-3)
        pen = QPen(acc, 1.5 * px)
        if len(self.quad) == 4 and self.ghost is None:
            p.setPen(QPen(acc, 1 * px, Qt.PenStyle.DashLine))
            p.drawPolygon(QPolygonF(self.quad))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self.rect)
        h = self.handle_size()
        p.setBrush(QColor("white"))
        for c in self.corners():
            p.drawRect(QRectF(c.x() - h / 2, c.y() - h / 2, h, h))


class ImageTool(QObject):
    """Selection and editing of the page's own images in the Select tool (one per window)."""

    def __init__(self, win):
        super().__init__(win.canvas)
        self.win = win
        self.cv = cv = win.canvas
        self.page = None  # PageItem
        self.draw: I.ImageDraw | None = None
        self.frame: ImageFrame | None = None
        self._drag = None  # (corner or None, press viewport point, start rect, moved)
        self._cursor = False  # the viewport cursor is ours (hovering the frame)
        self._pv_repl = False  # the preview shows a replacement
        self.actions: list[QAction] = []
        cv.viewport().installEventFilter(self)
        cv.installEventFilter(self)
        cv.toolChanged.connect(lambda _t: self.clear())
        cv.documentChanged.connect(self._doc_changed)
        cv.pagesChanged.connect(self._pages_changed)
        cv.scene().selectionChanged.connect(self._scene_selection)
        cv.undo.indexChanged.connect(lambda _i: self.refresh())

    # ------------------------------------------------------------------------------------------- state
    @property
    def ref(self):
        return self.page.ref if self.page is not None else None

    def edit(self) -> I.ImageEdit | None:
        if self.draw is None or self.cv.doc is None:
            return None
        return self.cv.doc.image_edits.get(self.ref.uid, {}).get(self.draw.key)

    def selected(self) -> bool:
        return self.draw is not None and self.frame is not None

    def _alive(self) -> bool:
        try:
            return self.frame is not None and self.frame.scene() is not None and self.page is not None \
                and self.page.scene() is not None
        except RuntimeError:  # deleted with the scene
            return False

    def clear(self) -> None:
        if self.frame is not None:
            try:
                if self.frame.scene() is not None:
                    self.frame.scene().removeItem(self.frame)
            except RuntimeError:
                pass
        self.frame = None
        self.page = None
        self.draw = None
        self._drag = None
        self._sync_actions()

    def _doc_changed(self) -> None:
        self.frame = None  # set_document cleared the scene
        self.clear()

    def _pages_changed(self) -> None:
        if self.frame is not None and not self._alive():
            self.frame = None
            self.clear()

    def _scene_selection(self) -> None:
        if self.frame is not None and self.cv.scene().selectedItems():
            self.clear()  # an overlay got selected

    def select(self, pg, draw: I.ImageDraw) -> None:
        self.clear()
        cv = self.cv
        cv.scene().clearSelection()
        cv._clear_focus()
        cv._end_text_selection()
        self.page, self.draw = pg, draw
        self.frame = ImageFrame(pg.content, self._preview())
        self.refresh()
        self._sync_actions()

    def _preview(self) -> QImage | None:
        try:
            pix = I.pixmap(self.ref.src, self.draw, self.edit())
        except Exception:  # noqa: BLE001
            return None
        if pix.width * pix.height > 4_000_000:  # a preview only: keep it light
            pix = pymupdf_shrink(pix)
        img = QImage.fromData(pix.tobytes("png"))
        return img if not img.isNull() else None

    def refresh(self) -> None:
        """Put the frame where the selected draw is shown now (after an edit, undo or redo)."""
        if self.frame is None:
            return
        if not self._alive():
            self.frame = None
            self.clear()
            return
        e = self.edit()
        if e is not None and e.delete:
            self.clear()
            return
        try:
            q = I.shown_quad(self.draw, e)
        except I.Refuse:
            self.clear()
            return
        r = I._bbox(q)
        if self.frame.preview is None or (e is not None and e.data is not None) != self._pv_repl:
            self.frame.preview = self._preview()
            self._pv_repl = e is not None and e.data is not None
        self.frame.set_shape(QRectF(r.x0, r.y0, r.width, r.height), q)

    def _sync_actions(self) -> None:
        on = self.selected()
        for a in self.actions:
            if a is not None:
                a.setEnabled(on)

    # ------------------------------------------------------------------------------------------- hit-test
    def _page_point(self, vp: QPointF):
        sp = self.cv.mapToScene(vp.toPoint())
        pg = self.cv.page_at(sp)
        return (pg, pg.content.mapFromScene(sp)) if pg is not None else (None, None)

    def _top_item(self, vp: QPointF):
        from ..canvas import PageItem
        from ..forms import FieldItem
        from ..items import is_overlay

        top = self.cv.itemAt(vp.toPoint())
        while top is not None and not (is_overlay(top) or isinstance(top, (FieldItem, PageItem, ImageFrame))):
            top = top.parentItem()
        return top

    @staticmethod
    def _word_at(words, lp: QPointF) -> bool:
        x, y = lp.x(), lp.y()
        return any(w[0] - 1 <= x <= w[2] + 1 and w[1] - 1 <= y <= w[3] + 1 for w in words)

    def image_at(self, pg, lp: QPointF, alt: bool = False, any_size: bool = False) -> I.ImageDraw | None:
        doc = self.cv.doc
        ref = pg.ref
        try:
            words = ref.src.words(ref.index, ref.uid)
        except Exception:  # noqa: BLE001
            words = []
        if not alt and not any_size and self._word_at(words, lp):
            return None
        try:
            return I.draw_at(doc, ref, lp.x(), lp.y(), skip_full=bool(words) and not alt and not any_size)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------------------------------- events
    def eventFilter(self, obj, e) -> bool:
        cv = self.cv
        t = e.type()
        if obj is cv:
            return t == QEvent.Type.KeyPress and self._key(e)
        if cv.doc is None or getattr(cv, "presenting", False) or cv.tool != "select":
            return False
        if t == QEvent.Type.MouseButtonPress and e.button() == Qt.MouseButton.LeftButton and not cv._space:
            return self._press(e)
        if t == QEvent.Type.MouseMove:
            return self._move(e)
        if t == QEvent.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._release(e)
            return True
        if t == QEvent.Type.ContextMenu:
            return self._context(e)
        return False

    def _press(self, e) -> bool:
        cv = self.cv
        vp = e.position()
        if self.frame is not None and self._alive():
            lp = self.frame.mapFromScene(cv.mapToScene(vp.toPoint()))
            c = self.frame.corner_at(lp)
            if c is not None or self.frame.rect.contains(lp):  # drag the selected image (the frame is on top)
                self._start(c, vp)
                return True
        top = self._top_item(vp)
        from ..canvas import PageItem

        if top is not None and not isinstance(top, (PageItem, ImageFrame)):
            self.clear()  # an overlay or a form field gets the click
            return False
        pg, lp = self._page_point(vp)
        if pg is None:
            self.clear()
            return False
        alt = bool(e.modifiers() & Qt.KeyboardModifier.AltModifier)
        d = self.image_at(pg, lp, alt=alt)
        if d is None:
            self.clear()
            return False
        self.select(pg, d)
        self._start(None, vp)
        return True

    def _start(self, corner, vp: QPointF) -> None:
        self._drag = (corner, QPointF(vp), QRectF(self.frame.rect), False)

    def _move(self, e) -> bool:
        cv = self.cv
        if self._drag is None:
            if self.frame is not None and self._alive() and not e.buttons():
                lp = self.frame.mapFromScene(cv.mapToScene(e.position().toPoint()))
                c = self.frame.corner_at(lp)
                if c is not None:
                    cv.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor if c % 2 == 0 else Qt.CursorShape.SizeBDiagCursor)
                    self._cursor = True
                elif self.frame.rect.contains(lp):
                    cv.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
                    self._cursor = True
                elif getattr(self, "_cursor", False):
                    self._cursor = False
                    cv.viewport().setCursor(cv._tool_cursor())
            return False
        if not self._alive():
            self._drag = None
            return False
        corner, start, r0, moved = self._drag
        if not moved and (e.position() - start).manhattanLength() < DRAG_PX:
            return True
        self._drag = (corner, start, r0, True)
        a = self.frame.mapFromScene(cv.mapToScene(start.toPoint()))
        b = self.frame.mapFromScene(cv.mapToScene(e.position().toPoint()))
        if corner is None:
            r = r0.translated(b - a)
        else:
            fixed = [r0.bottomRight(), r0.bottomLeft(), r0.topLeft(), r0.topRight()][corner]
            r = QRectF(fixed, b).normalized()
            if not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) and r0.height() > 0:
                aspect = r0.width() / r0.height()
                w = max(r.width(), r.height() * aspect)
                h = w / aspect
                x = fixed.x() if b.x() >= fixed.x() else fixed.x() - w
                y = fixed.y() if b.y() >= fixed.y() else fixed.y() - h
                r = QRectF(x, y, w, h)
            if r.width() < 2 or r.height() < 2:
                return True
        self._show_ghost(r)
        return True

    def _new_edit(self, rect: QRectF | None = None, turns: int | None = None, data=..., delete: bool = False):
        e = self.edit() or I.ImageEdit()
        cur = I.shown_rect(self.draw, e)
        r = rect if rect is not None else QRectF(cur.x0, cur.y0, cur.width, cur.height)
        return I.ImageEdit(delete=delete, rect=(r.left(), r.top(), r.right(), r.bottom()),
                           turns=(e.turns if turns is None else turns) % 4, data=e.data if data is ... else data)

    def _show_ghost(self, r: QRectF) -> None:
        e = self._new_edit(r)
        try:
            mat = I._UPRIGHT * I.placement(self.draw, e)
        except I.Refuse:
            return
        q = I.shown_quad(self.draw, e)
        self.frame.set_shape(r, q, QTransform(mat.a, mat.b, mat.c, mat.d, mat.e, mat.f))

    def _release(self, e) -> None:
        corner, start, r0, moved = self._drag
        self._drag = None
        if not moved or not self._alive():
            return
        r = QRectF(self.frame.rect)
        if r == r0:
            self.refresh()
            return
        self.apply(self._new_edit(r), "Resize image" if corner is not None else "Move image")

    def _key(self, e) -> bool:
        cv = self.cv
        if not self.selected() or cv._editing_text() or cv.doc is None:
            return False
        mods = e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        k = e.key()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and mods == Qt.KeyboardModifier.NoModifier:
            self.delete()
            return True
        if k == Qt.Key.Key_Escape:
            self.clear()
            return True
        arrows = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if k in arrows and mods in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            step = 10.0 if mods == Qt.KeyboardModifier.ShiftModifier else 1.0
            dx, dy = arrows[k]
            # the arrow moves the image on screen: map the screen direction into page.rect space (Leaf rotation)
            m = self.page.content.transform()
            inv, _ = m.inverted()
            d = inv.map(QPointF(dx, dy)) - inv.map(QPointF(0, 0))
            self.apply(self._new_edit(QRectF(self.frame.rect).translated(d * step)), "Nudge image")
            return True
        return False

    def _context(self, e) -> bool:
        vp = QPointF(e.pos())
        top = self._top_item(vp)
        from ..canvas import PageItem

        if top is not None and not isinstance(top, (PageItem, ImageFrame)):
            return False
        pg, lp = self._page_point(vp)
        if pg is None:
            return False
        if not (self.selected() and self.page is pg and self.frame.rect.contains(lp)):
            d = self.image_at(pg, lp, any_size=True)
            if d is None:
                return False
            self.select(pg, d)
        menu = QMenu(self.cv)
        for a in self.actions:
            if a is None:
                menu.addSeparator()
            else:
                menu.addAction(a)
        menu.exec(e.globalPos())
        return True

    # ------------------------------------------------------------------------------------------- actions
    def _checked(self) -> bool:
        if not self.selected():
            return False
        try:
            I.check(self.ref.src, self.ref.index, self.draw)
        except I.Refuse as ex:
            _toast(self.win, str(ex), 4500)
            return False
        return True

    def apply(self, new: I.ImageEdit, text: str) -> bool:
        if not self._checked():
            self.refresh()
            return False
        old = self.edit()
        if new == old:
            return False
        self.cv.undo.push(EditImage(self, self.ref, self.draw.key, old, new, text))
        return True

    def delete(self) -> None:
        if self.selected() and self.apply(self._new_edit(delete=True), "Delete image"):
            self.clear()

    def rotate(self, d: int) -> None:
        if not self.selected():
            return
        r = QRectF(self.frame.rect)
        c = r.center()
        r = QRectF(c.x() - r.height() / 2, c.y() - r.width() / 2, r.height(), r.width())
        self.apply(self._new_edit(r, turns=(self.edit() or I.ImageEdit()).turns + d), "Rotate image")

    def restore(self) -> None:
        if self.selected() and self.edit() is not None:
            self.cv.undo.push(EditImage(self, self.ref, self.draw.key, self.edit(), None, "Restore original image"))

    def replace_dialog(self) -> None:
        if not self._checked():
            return
        st = getattr(self.win, "settings", None)
        last = st.value("images/lastdir", "") if st is not None else ""
        path, _ = QFileDialog.getOpenFileName(self.win, "Replace image", last or "", IMAGE_FILTER + ";;All files (*)")
        if path:
            if st is not None:
                st.setValue("images/lastdir", os.path.dirname(path))
            self.replace_with(path)

    def replace_with(self, path_or_bytes) -> bool:
        """Replace the selected image with a file, fitted into its current box (keeps the place, upright)."""
        if not self._checked():
            return False
        try:
            if isinstance(path_or_bytes, (bytes, bytearray)):
                data = bytes(path_or_bytes)
            else:
                with open(path_or_bytes, "rb") as f:
                    data = f.read()
            w, h = I.image_info(data)
        except (OSError, I.Refuse) as ex:
            QMessageBox.warning(self.win, APP, f"Couldn't use that image:\n{ex}")
            return False
        box = QRectF(self.frame.rect)
        s = min(box.width() / max(w, 1), box.height() / max(h, 1))
        nw, nh = w * s, h * s
        c = box.center()
        r = QRectF(c.x() - nw / 2, c.y() - nh / 2, nw, nh)
        return self.apply(self._new_edit(r, turns=0, data=data), "Replace image")

    def _default_name(self, ext: str) -> str:
        doc = self.cv.doc
        stem = os.path.splitext(os.path.basename(doc.path))[0] if doc.path else "image"
        row = next((i for i, r in enumerate(doc.pages) if r.uid == self.ref.uid), 0)
        return f"{stem}-p{row + 1}-image{self.draw.index + 1}.{ext}"

    def save_dialog(self) -> None:
        if not self.selected():
            return
        src, e = self.ref.src, self.edit()
        try:
            _, ext = I.extract(src, self.draw, e)
        except I.Refuse:
            ext = "png"
        ext = "jpg" if ext == "jpeg" else ext
        filters = [f"Original format (*.{ext})", "PNG (*.png)", "JPEG (*.jpg *.jpeg)"]
        start = os.path.dirname(self.cv.doc.path) if self.cv.doc.path else ""
        path, flt = QFileDialog.getSaveFileName(self.win, "Save image as", os.path.join(start, self._default_name(ext)),
                                                ";;".join(filters))
        if not path:
            return
        low = path.lower()
        fmt = "png" if low.endswith(".png") and ext != "png" else "jpg" if low.endswith((".jpg", ".jpeg")) \
            and ext not in ("jpg", "jpeg") else "original"
        if flt.startswith("PNG") and not low.endswith(".png"):
            path, fmt = path + ".png", "png"
        elif flt.startswith("JPEG") and not low.endswith((".jpg", ".jpeg")):
            path, fmt = path + ".jpg", "jpg"
        elif fmt == "original" and not os.path.splitext(path)[1]:
            path += "." + ext
        try:
            data = I.save_bytes(src, self.draw, e, fmt)
            with open(path, "wb") as f:
                f.write(data)
        except (OSError, I.Refuse, RuntimeError) as ex:
            QMessageBox.critical(self.win, APP, f"Couldn't save the image:\n{ex}")
            return
        _toast(self.win, f"Saved {os.path.basename(path)}")

    def copy(self) -> bool:
        if not self.selected():
            return False
        try:
            pix = I.pixmap(self.ref.src, self.draw, self.edit())
            img = QImage.fromData(pix.tobytes("png"))
        except Exception as ex:  # noqa: BLE001
            _toast(self.win, f"Couldn't copy the image: {ex}")
            return False
        e = self.edit()
        if e is not None and e.turns % 4:
            img = img.transformed(QTransform().rotate(90 * (e.turns % 4)))
        QGuiApplication.clipboard().setImage(img)
        _toast(self.win, "Copied image", 1600)
        return True

    def copy_if_selected(self) -> None:
        cv = self.cv
        if self.selected() and not cv._editing_text() and not cv.selected_text():
            self.copy()

    def delete_if_selected(self) -> None:
        if self.selected() and not self.cv.selected_overlays():
            self.delete()


def pymupdf_shrink(pix):
    """A smaller copy of a pixmap for on-screen previews."""
    import pymupdf

    f = 1
    while pix.width // (2 ** f) * pix.height // (2 ** f) > 4_000_000:
        f += 1
    p = pymupdf.Pixmap(pix)
    p.shrink(f)
    return p


# ---------------------------------------------------------------------------------------------------- export
class ExportDialog(QDialog):
    """Options for one kind of export: "pages", "images", "text" or "markdown"."""

    TITLES = {"pages": "Export pages as images", "images": "Export all images", "text": "Export text",
              "markdown": "Export as Markdown"}

    def __init__(self, win, kind: str):
        super().__init__(win)
        self.win, self.kind = win, kind
        self.setWindowTitle(self.TITLES[kind])
        s = getattr(win, "settings", None)
        n = len(win.canvas.doc.pages)
        form = QFormLayout(self)
        self.range = QLineEdit("all")
        self.range.setPlaceholderText(f"all, or e.g. 1-3, 7, 10-  (1–{n})")
        form.addRow("Pages:", self.range)
        self.fmt = self.dpi = self.quality = self.folder = None
        if kind == "pages":
            self.fmt = QComboBox()
            for ext, label in X.page_formats().items():
                self.fmt.addItem(label, ext)
            want = s.value("export/format", "png") if s is not None else "png"
            self.fmt.setCurrentIndex(max(0, self.fmt.findData(want)))
            form.addRow("Format:", self.fmt)
            self.dpi = QSpinBox()
            self.dpi.setRange(18, 1200)
            self.dpi.setSuffix(" dpi")
            self.dpi.setValue(int(s.value("export/dpi", 150)) if s is not None else 150)
            form.addRow("Resolution:", self.dpi)
            self.quality = QSpinBox()
            self.quality.setRange(10, 100)
            self.quality.setValue(int(s.value("export/quality", 90)) if s is not None else 90)
            form.addRow("Quality:", self.quality)
            self.fmt.currentIndexChanged.connect(self._fmt_changed)
            self._fmt_changed()
        if kind in ("pages", "images"):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            doc = win.canvas.doc
            start = (s.value("export/folder", "") if s is not None else "") or \
                (os.path.dirname(doc.path) if doc.path else os.path.expanduser("~"))
            self.folder = QLineEdit(start)
            b = QPushButton("Browse…")
            b.clicked.connect(self._browse)
            h.addWidget(self.folder, 1)
            h.addWidget(b)
            form.addRow("Folder:", row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export…" if kind in ("text", "markdown") else "Export")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.rows: list[int] = []

    def _fmt_changed(self) -> None:
        self.quality.setEnabled(self.fmt.currentData() in ("jpg", "webp"))

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export to folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def _ok(self) -> None:
        try:
            self.rows = pageops.parse_ranges(self.range.text(), len(self.win.canvas.doc.pages))
        except ValueError as ex:
            QMessageBox.warning(self, APP, str(ex))
            return
        if not self.rows:
            QMessageBox.warning(self, APP, "No pages in that range.")
            return
        s = getattr(self.win, "settings", None)
        if s is not None:
            if self.fmt is not None:
                s.setValue("export/format", self.fmt.currentData())
                s.setValue("export/dpi", self.dpi.value())
                s.setValue("export/quality", self.quality.value())
            if self.folder is not None:
                s.setValue("export/folder", self.folder.text())
        self.accept()


def _stem(doc) -> str:
    return os.path.splitext(os.path.basename(doc.path))[0] if doc.path else "document"


def run_steps(win, title: str, steps, done_text) -> bool:
    """Drive an exporter generator in small steps on the GUI thread with a progress dialog and Cancel.
    Returns True when it finished."""
    dlg = QProgressDialog(title, "Cancel", 0, 0, win)
    dlg.setWindowTitle(APP)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(400)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    state = {"ok": None, "files": 0, "err": None}
    loop = QEventLoop()

    def finish(ok: bool, err=None) -> None:
        state["ok"], state["err"] = ok, err
        loop.quit()

    def step() -> None:
        if dlg.wasCanceled():
            finish(False)
            return
        try:
            done, total, path = next(steps)
        except StopIteration:
            finish(True)
            return
        except Exception as ex:  # noqa: BLE001
            finish(False, ex)
            return
        if path:
            state["files"] += 1
        dlg.setMaximum(total)
        dlg.setValue(done)
        osint.progress(win, done / total if total else osint.INDETERMINATE, key="export")
        QTimer.singleShot(0, step)

    QTimer.singleShot(0, step)
    loop.exec()
    dlg.close()
    osint.progress(win, None, key="export")
    if state["err"] is not None:
        QMessageBox.critical(win, APP, f"Export failed:\n{state['err']}")
        return False
    if state["ok"]:
        _toast(win, done_text(state["files"]), 3500)
        return True
    _toast(win, f"Export cancelled ({state['files']} file(s) written)")
    return False


def export(win, kind: str, rows=None, target: str | None = None, **opts) -> bool:
    """Export (no dialog when `rows` and `target` are given: a folder for pages/images, a file for text)."""
    cv = win.canvas
    doc = cv.doc
    if doc is None:
        return False
    cv._clear_focus()
    if rows is None or target is None:
        dlg = ExportDialog(win, kind)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        rows = dlg.rows
        if kind == "pages":
            opts = {"fmt": dlg.fmt.currentData(), "dpi": dlg.dpi.value(), "quality": dlg.quality.value()}
        if kind in ("pages", "images"):
            target = dlg.folder.text().strip()
            if not target:
                return False
        else:
            ext = "md" if kind == "markdown" else "txt"
            start = os.path.join(os.path.dirname(doc.path) if doc.path else "", f"{_stem(doc)}.{ext}")
            flt = "Markdown (*.md)" if kind == "markdown" else "Text (*.txt)"
            target, _ = QFileDialog.getSaveFileName(win, ExportDialog.TITLES[kind], start, flt)
            if not target:
                return False
            if not os.path.splitext(target)[1]:
                target += "." + ext
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        snap = X.snapshot(doc, cv.overlays_for)
    except Exception as ex:  # noqa: BLE001
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(win, APP, f"Couldn't prepare the export:\n{ex}")
        return False
    QApplication.restoreOverrideCursor()
    try:
        stem = _stem(doc)
        if kind == "pages":
            steps = X.export_pages(snap.doc, rows, target, stem, **opts)
            msg = lambda n: f"Exported {n} page image(s) to {os.path.basename(target) or target}"  # noqa: E731
        elif kind == "images":
            steps = X.export_images(snap.doc, target, stem, rows=rows)
            msg = lambda n: f"Exported {n} image(s) to {os.path.basename(target) or target}" if n \
                else "No images in those pages"  # noqa: E731
        else:
            steps = X.export_text(snap.doc, rows, target, markdown=kind == "markdown")
            msg = lambda n: f"Exported {os.path.basename(target)}"  # noqa: E731
        return run_steps(win, ExportDialog.TITLES[kind] + "…", steps, msg)
    finally:
        snap.close()


# ---------------------------------------------------------------------------------------------------- install
def _action(win, name: str, text: str, slot, tip: str | None = None, ic: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if ic:
        try:
            from ..icons import icon

            a.setIcon(icon(ic))
            a.setProperty("icon_name", ic)
        except Exception:  # noqa: BLE001
            pass
    a.setProperty("base_tip", tip or text.replace("&", "").rstrip("…"))
    a.setToolTip(tip or text.replace("&", "").rstrip("…"))
    a.triggered.connect(lambda _=False: slot())
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def install(win) -> None:
    cv = win.canvas
    tool = win.image_tool = ImageTool(win)

    replace = _action(win, "image_replace", "&Replace image…", tool.replace_dialog,
                      "Replace the selected image with a file (keeps its place)", "image")
    rot_l = _action(win, "image_rotate_left", "Rotate image &left", lambda: tool.rotate(-1), None, "rotate_left")
    rot_r = _action(win, "image_rotate_right", "Rotate image r&ight", lambda: tool.rotate(1), None, "rotate_right")
    save = _action(win, "image_save", "&Save image as…", tool.save_dialog,
                   "Save the selected image to a file (original format, PNG or JPEG)")
    copy = _action(win, "image_copy", "&Copy image", tool.copy, None, "copy")
    delete = _action(win, "image_delete", "&Delete image", tool.delete, None, "delete")
    restore = _action(win, "image_restore", "Res&tore original image", tool.restore,
                      "Undo every change to the selected image")
    tool.actions = [replace, rot_l, rot_r, None, save, copy, None, delete, restore]
    tool._sync_actions()

    menus = getattr(win, "menus", {})
    em = menus.get("edit")
    if em is not None:
        sub = QMenu("&Image", win)
        sub.setObjectName("menu_image")
        for a in tool.actions:
            if a is None:
                sub.addSeparator()
            else:
                sub.addAction(a)
        anchor = getattr(win, "a_delete", None)
        items = em.actions()
        if anchor in items and items.index(anchor) + 1 < len(items):
            em.insertMenu(items[items.index(anchor) + 1], sub)
        else:
            em.addMenu(sub)
        win.menus["image"] = sub
    # the window's Copy and Delete also act on a selected image
    if getattr(win, "a_copy", None) is not None:
        win.a_copy.triggered.connect(lambda _=False: tool.copy_if_selected())
    if getattr(win, "a_delete", None) is not None:
        win.a_delete.triggered.connect(lambda _=False: tool.delete_if_selected())

    # File › Export
    exp_pages = _action(win, "export_pages", "Pages as &images…", lambda: export(win, "pages"),
                        "Save each page as an image file (PNG, JPEG, …)")
    exp_images = _action(win, "export_images", "&All images…", lambda: export(win, "images"),
                         "Save every image in the document, in its original format")
    exp_text = _action(win, "export_text", "&Text…", lambda: export(win, "text"), "Save the document's text")
    exp_md = _action(win, "export_markdown", "&Markdown…", lambda: export(win, "markdown"),
                     "Save the document's text as Markdown (headings from font sizes)")
    exp_acts = [exp_pages, exp_images, exp_text, exp_md]
    fm = menus.get("file")
    if fm is not None:
        sub = QMenu("E&xport", win)
        sub.setObjectName("menu_export")
        sub.addActions(exp_acts)
        items = fm.actions()
        before = getattr(win, "a_print", None)
        if before not in items:
            before = getattr(win, "a_close", None)
        if before in items:
            k = items.index(before)
            if k and items[k - 1].isSeparator():
                before = items[k - 1]
            fm.insertMenu(before, sub)
            fm.insertSeparator(before)
        else:
            fm.addMenu(sub)
        win.menus["export"] = sub

    def enable() -> None:
        has = cv.doc is not None
        for a in exp_acts:
            a.setEnabled(has)

    cv.documentChanged.connect(enable)
    enable()

    # hint for the Select tool mentions images
    try:
        from .. import window

        h = window.HINTS.get("select", "")
        if "image" not in h:
            window.HINTS["select"] = h + " Click images to edit them."
    except Exception:  # noqa: BLE001
        pass
