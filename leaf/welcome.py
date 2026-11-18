"""Start screen shown while no document is open: open button, drop zone, recent files."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .icons import app_icon, icon

THUMB = QSize(34, 44)
PATH_ROLE = Qt.ItemDataRole.UserRole
_thumbs: dict[tuple, QImage] = {}  # (path, mtime) -> tiny first-page image


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


def _secondary(w: QWidget) -> None:
    w.setProperty("secondary", True)
    w.setPalette(QPalette())
    pal = w.palette()
    pal.setColor(QPalette.ColorRole.WindowText, pal.color(QPalette.ColorRole.PlaceholderText))
    w.setPalette(pal)


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


class WelcomeView(QWidget):
    """`win` needs: settings, open_path(path), open_dialog(), _files_dropped(paths)."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setObjectName("welcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(2)
        col = QWidget()
        col.setMaximumWidth(560)
        col.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(col, 10)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch(3)

        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.logo = QLabel()
        self.logo.setPixmap(app_icon().pixmap(QSize(48, 48), self.devicePixelRatioF()))
        head.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("Leaf")
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 2.1)
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        self.subtitle = QLabel("A fast, local PDF editor. Nothing leaves your computer.")
        _secondary(self.subtitle)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles)
        head.addStretch()
        lay.addLayout(head)
        lay.addSpacing(24)

        self.open_btn = QPushButton(icon("open", QColor("white")), "  Open file…")
        self.open_btn.setObjectName("primary")
        self.open_btn.setMinimumHeight(40)
        self.open_btn.setIconSize(QSize(18, 18))
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(win.open_dialog)
        lay.addWidget(self.open_btn)
        lay.addSpacing(12)

        self.drop = DropZone()
        self.drop.clicked.connect(win.open_dialog)
        self.drop.dropped.connect(win._files_dropped)
        lay.addWidget(self.drop)
        lay.addSpacing(26)

        rh = QHBoxLayout()
        self.recent_title = QLabel("Recent")
        f = self.recent_title.font()
        f.setWeight(QFont.Weight.DemiBold)
        self.recent_title.setFont(f)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setFlat(True)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setToolTip("Clear the recent files list")
        self.clear_btn.clicked.connect(self.clear_recent)
        rh.addWidget(self.recent_title)
        rh.addStretch()
        rh.addWidget(self.clear_btn)
        lay.addLayout(rh)
        lay.addSpacing(4)

        self.recent = QListWidget()
        self.recent.setObjectName("recent")
        self.recent.setFrameShape(QFrame.Shape.NoFrame)
        self.recent.setIconSize(THUMB)
        self.recent.setSpacing(1)
        self.recent.setUniformItemSizes(True)
        self.recent.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.recent.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.recent.customContextMenuRequested.connect(self._recent_menu)
        self.recent.itemActivated.connect(lambda it: self.win.open_path(it.data(PATH_ROLE)))
        self.recent.installEventFilter(self)
        lay.addWidget(self.recent)
        self.empty = QLabel("Files you open will show up here.")
        _secondary(self.empty)
        lay.addWidget(self.empty)
        lay.addSpacing(20)

        self.hint = QLabel()
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _secondary(self.hint)
        lay.addWidget(self.hint)

        self._queue: list[QListWidgetItem] = []
        self._thumb_timer = QTimer(self, singleShot=True, interval=0, timeout=self._next_thumb)
        self._restyle()

    # ------------------------------------------------------------- recents
    def refresh(self) -> None:
        self.recent.clear()
        self._queue = []
        paths = [p for p in recent_paths(self.win.settings) if os.path.exists(p)]
        blank = QPixmap(THUMB * self.devicePixelRatioF())
        blank.fill(Qt.GlobalColor.transparent)
        try:
            from .settings import recent_max

            paths = paths[:recent_max(self.win.settings)]
        except Exception:  # noqa: BLE001 - embedded without the settings module
            pass
        for p in paths[:8]:
            it = QListWidgetItem(QIcon(blank), f"{os.path.basename(p)}\n{_short_dir(p)}")
            it.setData(PATH_ROLE, p)
            it.setToolTip(p)
            it.setSizeHint(QSize(0, THUMB.height() + 10))
            self.recent.addItem(it)
            self._queue.append(it)
        has = bool(paths)
        self.recent.setVisible(has)
        self.clear_btn.setVisible(has)
        self.empty.setVisible(not has)
        n = min(len(paths), 5)
        self.recent.setFixedHeight(n * (THUMB.height() + 12) + 4 if n else 0)
        self._thumb_timer.start()
        sc = lambda a: a.shortcut().toString(QKeySequence.SequenceFormat.NativeText) if a is not None else ""  # noqa: E731
        hints = [(sc(getattr(self.win, "a_open", None)), "Open")]
        pal = getattr(self.win, "a_command_palette", None) or getattr(self.win, "a_palette", None)
        if pal is not None and sc(pal):
            hints.append((sc(pal), "Command palette"))
        self.hint.setText("     ".join(f"{k}  {t}" for k, t in hints if k))

    def _next_thumb(self) -> None:
        """One thumbnail per event-loop turn so the window stays responsive."""
        while self._queue:
            it = self._queue.pop(0)
            if it.listWidget() is not self.recent:
                continue
            dpr = self.devicePixelRatioF()
            img = render_thumb(it.data(PATH_ROLE), int(THUMB.height() * dpr))
            if img is not None:
                it.setIcon(QIcon(self._framed(img, dpr)))
            break
        if self._queue:
            self._thumb_timer.start()

    def _framed(self, img: QImage, dpr: float) -> QPixmap:
        pm = QPixmap(THUMB * dpr)
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = img.width() / dpr, img.height() / dpr
        x, y = (THUMB.width() - w) / 2, (THUMB.height() - h) / 2
        r = QRectF(x, y, w, h)
        p.drawImage(r, img)
        p.setPen(QPen(QColor(0, 0, 0, 60), 1 / dpr))
        p.drawRect(r.adjusted(0, 0, -1 / dpr, -1 / dpr))
        p.end()
        return pm

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
        m = QMenu(self)
        m.addAction(icon("open"), "Open", lambda: self.win.open_path(path))
        m.addAction(icon("close"), "Remove from list", lambda: self.remove(path))
        m.addSeparator()
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
        pal = QApplication.palette()
        acc = pal.color(QPalette.ColorRole.Accent)
        txt = "#000000" if acc.lightness() > 150 else "#ffffff"
        self.open_btn.setIcon(icon("open", QColor(txt)))
        ss = (f"QPushButton#primary {{ background: {acc.name()}; color: {txt}; border: none; border-radius: 6px;"
              f" font-weight: 600; padding: 0 18px; }}"
              f"QPushButton#primary:hover {{ background: {acc.lighter(110).name()}; }}"
              f"QPushButton#primary:pressed {{ background: {acc.darker(110).name()}; }}"
              "QListWidget#recent { background: transparent; }"
              "QListWidget#recent::item { padding: 2px 4px; border-radius: 6px; }")
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
