"""Start screen shown while no document is open: "Pick up where you left off", recent files as large page covers,
the Open button and the keyboard shortcuts that matter here. The whole window is the drop target; a first run (no
recent files yet) gets a drop zone instead of the covers."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu, QPushButton, QSizePolicy,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .icons import app_icon, icon

PATH_ROLE = Qt.ItemDataRole.UserRole
IMG_ROLE = Qt.ItemDataRole.UserRole + 7  # the cover image (QImage at the screen's pixel ratio)
AGO_ROLE = Qt.ItemDataRole.UserRole + 8  # "3 h ago": read once per refresh, not on every paint (no disk access there)
COVER_W = 188  # one recent file: page cover, name, when (height: cover_size())
PAGE_BOX = QSize(156, 204)  # the page inside it
SHELF = 5  # covers visible side by side; more scroll sideways
MAX_RECENT = 8
_thumbs: dict[tuple, QImage] = {}  # (path, mtime, px) -> first-page image


def recent_paths(settings) -> list[str]:
    v = settings.value("recent") or []
    return [v] if isinstance(v, str) else [str(p) for p in v]


def render_thumb(path: str, px: int) -> QImage | None:
    """First page at roughly `px` pixels on its long side; None for missing/protected/broken files."""
    try:
        key = (path, os.path.getmtime(path), px)
    except OSError:
        return None
    if key in _thumbs:
        return _thumbs[key]
    img = None
    try:
        import pymupdf

        with pymupdf.open(path) as d:
            if not d.needs_pass and d.page_count:
                page = d[0]
                s = px / max(page.rect.width, page.rect.height, 1)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
    except Exception:  # noqa: BLE001 - a thumbnail is never worth an error
        img = None
    if len(_thumbs) > 64:
        _thumbs.clear()
    _thumbs[key] = img
    return img


def _short_dir(path: str) -> str:
    d = os.path.normpath(os.path.dirname(path))
    home = os.path.normpath(os.path.expanduser("~"))
    if os.name != "nt" and (d + os.sep).startswith(home + os.sep):  # "~" means nothing to most Windows users
        d = "~" + d[len(home):]
    return d


def ago(path: str, now: float | None = None) -> str:
    """When the file last changed, the way people say it ("Just now", "3 h ago", "Yesterday", "12 Mar 2026")."""
    try:
        dt = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        return ""
    if dt < 90:
        return "Just now"
    if dt < 3600:
        return f"{int(dt // 60)} min ago"
    if dt < 86400:
        return f"{int(dt // 3600)} h ago"
    if dt < 86400 * 7:
        d = int(dt // 86400)
        return "Yesterday" if d == 1 else f"{d} days ago"
    return time.strftime("%d %b %Y", time.localtime((now or time.time()) - dt))


def cover_size(font: QFont) -> QSize:
    line = QFontMetrics(font).height() + 2
    return QSize(COVER_W, 12 + PAGE_BOX.height() + 10 + 2 * line + 12)


def display_name(path: str) -> str:
    name = os.path.basename(path)
    return name[:-4] if name.lower().endswith(".pdf") else name


def _secondary(w: QWidget) -> None:
    w.setProperty("secondary", True)
    w.setPalette(QPalette())
    pal = w.palette()
    pal.setColor(QPalette.ColorRole.WindowText, pal.color(QPalette.ColorRole.PlaceholderText))
    w.setPalette(pal)


def _tokens():
    from . import style

    return style.current()


class DropZone(QFrame):
    clicked = Signal()
    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hot = False
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(6)
        self.ico = QLabel()
        self.ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel("Drop a PDF or image here")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub = QLabel("or click to browse")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _secondary(self.sub)
        for w in (self.ico, self.text, self.sub):
            lay.addWidget(w)
        self._restyle()

    def _restyle(self) -> None:
        self.ico.setPixmap(icon("page_add").pixmap(QSize(28, 28), self.devicePixelRatioF()))

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        acc = pal.color(QPalette.ColorRole.Accent)
        if self._hot:
            fill = QColor(acc)
            fill.setAlpha(28)
            p.setBrush(fill)
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        col = acc if self._hot else pal.color(QPalette.ColorRole.Mid)
        pen = QPen(col, 1.4 if self._hot else 1.2, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1).toRectF(), 8, 8)

    def _set_hot(self, hot: bool) -> None:
        if hot != self._hot:
            self._hot = hot
            self.update()

    def enterEvent(self, e) -> None:
        self._set_hot(True)

    def leaveEvent(self, e) -> None:
        self._set_hot(False)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_hot(True)

    def dragLeaveEvent(self, e) -> None:
        self._set_hot(False)

    def dropEvent(self, e) -> None:
        self._set_hot(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        if paths:
            self.dropped.emit(paths)

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self._restyle()
            for w in self.findChildren(QLabel):
                if w.property("secondary"):
                    _secondary(w)
        super().changeEvent(e)


class CoverDelegate(QStyledItemDelegate):
    """A recent file as its first page (soft shadow), the name and when it was last changed. Hover lifts a card
    behind it; keyboard focus draws the accent ring."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def sizeHint(self, opt, idx) -> QSize:
        return cover_size(opt.font)

    def paint(self, p: QPainter, opt, idx) -> None:
        t = _tokens()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(opt.rect).adjusted(2.5, 2.5, -2.5, -2.5)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        focus = bool(opt.state & QStyle.StateFlag.State_HasFocus) and self.view.hasFocus()
        if hover or focus:
            p.setPen(QPen(t.accent, 2) if focus else QPen(t.stroke, 1))
            p.setBrush(t.layer)
            p.drawRoundedRect(r, t.r_l, t.r_l)
        box = QRectF(r.center().x() - PAGE_BOX.width() / 2, r.top() + 12, PAGE_BOX.width(), PAGE_BOX.height())
        img = idx.data(IMG_ROLE)
        if isinstance(img, QImage) and not img.isNull():
            dpr = img.devicePixelRatio()
            iw, ih = img.width() / dpr, img.height() / dpr
            s = min(box.width() / iw, box.height() / ih)
            w, h = iw * s, ih * s
            pr = QRectF(box.center().x() - w / 2, box.bottom() - h, w, h)  # pages stand on one baseline
            p.setPen(Qt.PenStyle.NoPen)
            for i, a in ((4, 10), (3, 14), (2, 20), (1, 30)):
                p.setBrush(QColor(0, 0, 0, a + (16 if t.dark else 0)))
                p.drawRoundedRect(pr.adjusted(-i, -i + 2, i, i + 2), i + 1, i + 1)
            p.drawImage(pr, img)
            if not t.dark:
                p.setPen(QPen(QColor(0, 0, 0, 28), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(pr.adjusted(0.5, 0.5, -0.5, -0.5))
        else:  # not rendered yet (or unreadable): a quiet placeholder page
            pr = QRectF(box.center().x() - 72, box.bottom() - 196, 144, 196)
            p.setPen(QPen(t.stroke2, 1))
            p.setBrush(t.layer2)
            p.drawRoundedRect(pr, 2, 2)
        path = idx.data(PATH_ROLE)
        line = QFontMetrics(opt.font).height() + 2
        tx = QRectF(r.left() + 10, box.bottom() + 10, r.width() - 20, line)
        f = QFont(opt.font)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(t.text)
        center = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        p.drawText(tx, center, QFontMetrics(f).elidedText(display_name(path), Qt.TextElideMode.ElideMiddle, int(tx.width())))
        p.setFont(opt.font)
        p.setPen(t.text2)
        p.drawText(QRectF(tx.left(), tx.bottom(), tx.width(), line), center, idx.data(AGO_ROLE) or "")
        p.restore()


class KeyHint(QWidget):
    """A shortcut hint: key caps, then what it does."""

    def __init__(self, keys: str, text: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for k in keys.split("+"):
            cap = QLabel(k)
            cap.setObjectName("kbd")
            lay.addWidget(cap)
        lab = QLabel(text)
        lab.setObjectName("muted")
        _secondary(lab)
        lay.addSpacing(4)
        lay.addWidget(lab)
        self.setAccessibleName(f"{keys}: {text}")


class WelcomeView(QWidget):
    """`win` needs: settings, open_path(path), open_dialog(), _files_dropped(paths)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("welcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 28)
        outer.addStretch(3)
        self.col = col = QWidget()
        col.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(col, 10)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch(4)

        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.logo.setPixmap(app_icon().pixmap(QSize(40, 40), self.devicePixelRatioF()))
        lay.addWidget(self.logo)
        lay.addSpacing(14)
        self.title = QLabel("Pick up where you left off")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.title.setWordWrap(True)  # (no label may set the window's minimum width: the view stack takes the widest)
        self.title.setAccessibleName("Leaf")
        lay.addWidget(self.title)
        lay.addSpacing(6)
        self.subtitle = QLabel()
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.subtitle.setWordWrap(True)
        _secondary(self.subtitle)
        lay.addWidget(self.subtitle)
        lay.addSpacing(28)

        # first run: a drop zone (the first-run tour card goes right below it: onboarding.show_tour)
        self.drop = DropZone()
        self.drop.clicked.connect(win.open_dialog)
        self.drop.dropped.connect(win._files_dropped)
        lay.addWidget(self.drop)

        self.recent_title = QLabel("Recent files")  # the covers speak for themselves; kept for screen readers
        self.recent_title.hide()
        self.recent = QListWidget()
        self.recent.setObjectName("recent")
        self.recent.setAccessibleName("Recent files")
        self.recent.setItemDelegate(CoverDelegate(self.recent))
        self.recent.setViewMode(QListView.ViewMode.IconMode)
        self.recent.setFlow(QListView.Flow.LeftToRight)
        self.recent.setWrapping(False)  # one shelf; more files scroll sideways
        self.recent.setMovement(QListView.Movement.Static)
        self.recent.setResizeMode(QListView.ResizeMode.Adjust)
        self.recent.setSpacing(6)
        self.recent.setUniformItemSizes(True)
        self.recent.setFrameShape(QFrame.Shape.NoFrame)
        self.recent.setMouseTracking(True)
        self.recent.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.recent.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.recent.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.recent.customContextMenuRequested.connect(self._recent_menu)
        self.recent.itemActivated.connect(lambda it: self.win.open_path(it.data(PATH_ROLE)))
        self.recent.installEventFilter(self)
        shelf = QHBoxLayout()
        shelf.addStretch(1)
        shelf.addWidget(self.recent, 100)
        shelf.addStretch(1)
        lay.addLayout(shelf)
        self.empty = QLabel("Files you open will show up here.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.empty.setWordWrap(True)
        _secondary(self.empty)
        lay.addSpacing(14)
        lay.addWidget(self.empty)
        lay.addSpacing(24)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.open_btn = QPushButton(icon("open", QColor("white")), "  Open file…")
        self.open_btn.setObjectName("primary")
        self.open_btn.setMinimumSize(168, 40)
        self.open_btn.setIconSize(QSize(18, 18))
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(win.open_dialog)
        buttons.addWidget(self.open_btn)
        buttons.addSpacing(8)
        self.clear_btn = QPushButton("Clear recent")
        self.clear_btn.setFlat(True)
        self.clear_btn.setMinimumHeight(40)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("Clear the recent files list")
        self.clear_btn.clicked.connect(self.clear_recent)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch()
        lay.addLayout(buttons)
        lay.addSpacing(22)

        self.hint = QWidget()  # key caps: Open, Command palette, Shortcuts
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint_row = QHBoxLayout(self.hint)
        self.hint_row.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.hint)

        self._queue: list[QListWidgetItem] = []
        self._thumb_timer = QTimer(self, singleShot=True, interval=0, timeout=self._next_thumb)
        self._restyle()

    # ------------------------------------------------------------- recents
    def refresh(self) -> None:
        self.recent.clear()
        self._queue = []
        paths = [p for p in recent_paths(self.win.settings) if os.path.exists(p)]
        try:
            from .settings import recent_max

            paths = paths[:recent_max(self.win.settings)]
        except Exception:  # noqa: BLE001 - embedded without the settings module
            pass
        paths = paths[:MAX_RECENT]
        for p in paths:
            it = QListWidgetItem()
            when = ago(p)
            it.setData(PATH_ROLE, p)
            it.setData(AGO_ROLE, when)
            it.setData(Qt.ItemDataRole.AccessibleTextRole, os.path.basename(p))
            it.setData(Qt.ItemDataRole.AccessibleDescriptionRole, f"{_short_dir(p)}, {when}")
            it.setToolTip(p)
            it.setSizeHint(cover_size(self.recent.font()))
            self.recent.addItem(it)
            self._queue.append(it)
        has = bool(paths)
        self.recent.setVisible(has)
        self.clear_btn.setVisible(has)
        self.empty.setVisible(not has)
        self.drop.setVisible(not has)
        if has:
            self.title.setText("Pick up where you left off")
            self.subtitle.setText("Open a PDF, or drop one anywhere in this window.")
            sp = self.recent.spacing()
            cell = COVER_W + 2 * sp
            scroll = len(paths) > SHELF
            self.recent.setFixedHeight(cover_size(self.recent.font()).height() + 2 * sp + (self.recent.horizontalScrollBar().sizeHint().height() if scroll else 2))
            self.recent.setMinimumWidth(cell + 2)  # a narrow window scrolls the shelf instead of growing
            self.recent.setMaximumWidth(min(len(paths), SHELF) * cell + 2)
            self.recent.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.col.setMaximumWidth(SHELF * cell + 40)
        else:
            self.title.setText("Welcome to Leaf")
            self.subtitle.setText("A fast, local PDF editor. Nothing leaves your computer.")
            self.recent.setFixedHeight(0)
            self.col.setMaximumWidth(560)
        self._thumb_timer.start()
        self._hints()

    def _hints(self) -> None:
        lay = self.hint_row
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()

        def sc(a) -> str:
            return a.shortcut().toString(QKeySequence.SequenceFormat.NativeText) if a is not None else ""
        hints = [(sc(getattr(self.win, "a_open", None)), "Open")]
        pal = getattr(self.win, "a_command_palette", None) or getattr(self.win, "a_palette", None)
        if pal is not None and sc(pal):
            hints.append((sc(pal), "Command palette"))
        help_ = getattr(self.win, "a_shortcuts", None)
        if help_ is not None and sc(help_):
            hints.append((sc(help_), "Keyboard shortcuts"))
        lay.addStretch()
        for i, (k, txt) in enumerate(h for h in hints if h[0]):
            if i:
                lay.addSpacing(24)
            lay.addWidget(KeyHint(k, txt))
        lay.addStretch()

    def _next_thumb(self) -> None:
        """One cover per event-loop turn so the window stays responsive."""
        while self._queue:
            it = self._queue.pop(0)
            if it.listWidget() is not self.recent:
                continue
            dpr = self.devicePixelRatioF()
            img = render_thumb(it.data(PATH_ROLE), int(PAGE_BOX.height() * dpr))
            if img is not None:
                img = QImage(img)
                img.setDevicePixelRatio(dpr)
                it.setData(IMG_ROLE, img)
                it.setIcon(QIcon(QPixmap.fromImage(img)))  # the delegate paints IMG_ROLE; the icon keeps the item API
            break
        if self._queue:
            self._thumb_timer.start()

    def remove(self, path: str) -> None:
        self.win.settings.setValue("recent", [p for p in recent_paths(self.win.settings) if p != path])
        self.refresh()

    def clear_recent(self) -> None:
        self.win.settings.setValue("recent", [])
        self.refresh()

    def _recent_menu(self, pos) -> None:
        it = self.recent.itemAt(pos)
        if it is None:
            return
        path = it.data(PATH_ROLE)
        from .shellint import reveal

        m = QMenu(self)
        m.setDefaultAction(m.addAction(icon("open"), "Open", lambda: self.win.open_path(path)))
        m.addAction("Show in folder", lambda: reveal(path))
        m.addAction("Copy path", lambda: QApplication.clipboard().setText(os.path.normpath(path)))
        m.addSeparator()
        m.addAction(icon("close"), "Remove from list", lambda: self.remove(path))
        m.addAction("Clear list", self.clear_recent)
        m.exec(self.recent.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, e) -> bool:
        if obj is self.recent and e.type() == QEvent.Type.KeyPress and e.key() == Qt.Key.Key_Delete:
            it = self.recent.currentItem()
            if it is not None:
                self.remove(it.data(PATH_ROLE))
            return True
        return super().eventFilter(obj, e)

    # --------------------------------------------------------------- style
    def _restyle(self) -> None:
        from . import style

        t = _tokens()
        f = QFont(t.font_display) if t.font_display else QFont(self.font())
        f.setPointSizeF(t.pt_display if t.pt else QFont(self.font()).pointSizeF() * 2.3)
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        from .style.tokens import rgba

        kbd = (f"QLabel#kbd {{ background: {rgba(t.layer)}; color: {rgba(t.text2)}; border: 1px solid {rgba(t.stroke2)};"
               f" border-bottom-width: 2px; border-radius: {t.r_s}px; padding: 1px 6px; }}")
        lists = ("QListWidget#recent { background: transparent; border: none; }"
                 "QListWidget#recent::item { background: transparent; border: none; padding: 0; }")
        if style.active():  # the app style sheet draws the primary button (accent, on_accent text)
            self.open_btn.setIcon(icon("open", t.on_accent))
            ss = lists + kbd + f"QPushButton:flat {{ color: {rgba(t.text2)}; padding: 0 16px; }}"
        else:
            pal = QApplication.palette()
            acc = pal.color(QPalette.ColorRole.Accent)
            txt = "#000000" if acc.lightness() > 150 else "#ffffff"
            self.open_btn.setIcon(icon("open", QColor(txt)))
            ss = (f"QPushButton#primary {{ background: {acc.name()}; color: {txt}; border: none; border-radius: 6px;"
                  f" font-weight: 600; padding: 0 18px; }}"
                  f"QPushButton#primary:hover {{ background: {acc.lighter(110).name()}; }}"
                  f"QPushButton#primary:pressed {{ background: {acc.darker(110).name()}; }}" + lists + kbd)
        if ss != self.styleSheet():  # setting a style sheet fires PaletteChange again
            self.setStyleSheet(ss)

    def showEvent(self, e) -> None:
        self.refresh()
        super().showEvent(e)

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self._restyle()
            for w in self.findChildren(QLabel):
                if w.property("secondary"):
                    _secondary(w)
        super().changeEvent(e)
