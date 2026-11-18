"""First-run welcome tour, one-time contextual tips and empty-state hints for panels.

Calm by design: the tour is a small card on the welcome screen (never a modal), it can be skipped with one click
(or Esc) and doesn't come back unless Help › Show tips again is used. Tips are toasts that show once.

Settings: "onboarding/tour_done" (bool), "onboarding/tip/<name>" (bool, tip already shown)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

TOUR_DONE = "onboarding/tour_done"
TIP_PREFIX = "onboarding/tip/"


def _truthy(v) -> bool:
    return v in (True, "true", "1", 1, "True")


def _shortcut(win, name: str, fallback: str) -> str:
    a = getattr(win, name, None)
    if a is None:
        return fallback
    try:
        from . import shortcuts

        seqs = shortcuts.current(a)
    except Exception:  # noqa: BLE001
        seqs = list(a.shortcuts())
    return seqs[0].toString(QKeySequence.SequenceFormat.NativeText) if seqs else fallback


def cards(win) -> list[tuple[str, str]]:
    """(title, text) of each tour card, with the shortcuts as currently bound."""
    edit_key = _shortcut(win, "a_tool_edittext", "I")
    palette = _shortcut(win, "a_command_palette", "Ctrl+K")
    return [
        ("Open a PDF", "Click Open file… or drop a PDF anywhere on this window. Images open as PDFs, too. "
                       "Recent files stay listed below."),
        ("Edit the text itself", f"Press {edit_key} (Edit text) and click a line to change it in its own font. "
                                 "T adds a new text box; V selects."),
        ("Every command, one shortcut away", f"Press {palette} and type what you want to do: rotate, redact, sign, "
                                             "reduce file size…"),
        ("Private by design", "Leaf works offline. Your documents stay on this computer: no account, no upload."),
    ]


# ------------------------------------------------------------------ tour
class Tour(QFrame):
    """A small card carousel. `finished(skipped)` fires once, when it's done or skipped."""

    finished = Signal(bool)

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("leafTour")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.setAccessibleName("Welcome tour")
        self.cards = cards(win)
        self.index = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.step = QLabel()
        self._secondary(self.step)
        self.skip = QPushButton("Skip tour")
        self.skip.setFlat(True)
        self.skip.setAutoDefault(False)
        self.skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip.setToolTip("Hide the tour (Help › Show tips again brings it back)")
        self.skip.clicked.connect(lambda: self.finish(True))
        top.addWidget(self.step)
        top.addStretch()
        top.addWidget(self.skip)
        lay.addLayout(top)
        self.title = QLabel()
        f = QFont(self.title.font())
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSizeF(f.pointSizeF() * 1.12)
        self.title.setFont(f)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.text.setMinimumHeight(self.text.fontMetrics().lineSpacing() * 3)  # wrapped labels: no clipping
        lay.addWidget(self.title)
        lay.addWidget(self.text)
        nav = QHBoxLayout()
        nav.addStretch()
        self.back = QPushButton("Back")
        self.back.setAutoDefault(False)
        self.back.clicked.connect(lambda: self.go(self.index - 1))
        self.next = QPushButton("Next")
        self.next.setAutoDefault(False)
        self.next.clicked.connect(self._next)
        nav.addWidget(self.back)
        nav.addWidget(self.next)
        lay.addLayout(nav)
        self.go(0)

    @staticmethod
    def _secondary(w: QWidget) -> None:
        pal = w.palette()
        pal.setColor(QPalette.ColorRole.WindowText, pal.color(QPalette.ColorRole.PlaceholderText))
        w.setPalette(pal)

    def go(self, i: int) -> None:
        self.index = max(0, min(len(self.cards) - 1, i))
        title, text = self.cards[self.index]
        self.title.setText(title)
        self.text.setText(text)
        self.step.setText(f"Welcome · {self.index + 1} of {len(self.cards)}")
        self.back.setVisible(self.index > 0)
        self.next.setText("Get started" if self.index == len(self.cards) - 1 else "Next")
        self.setAccessibleDescription(f"{title}. {text}")

    def _next(self) -> None:
        if self.index >= len(self.cards) - 1:
            self.finish(False)
        else:
            self.go(self.index + 1)

    def finish(self, skipped: bool) -> None:
        s = getattr(self.win, "settings", None)
        if s is not None:
            s.setValue(TOUR_DONE, True)
        self.hide()
        self.finished.emit(skipped)
        self.deleteLater()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.finish(True)
            return
        if e.key() in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self._next()
            return
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.go(self.index - 1)
            return
        super().keyPressEvent(e)

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self.step.setPalette(QPalette())
            self._secondary(self.step)
        super().changeEvent(e)


def tour_of(win) -> Tour | None:
    t = getattr(win, "_tour", None)
    try:
        return t if t is not None and not t.isHidden() else None
    except RuntimeError:  # already deleted
        return None


def show_tour(win, force: bool = False) -> Tour | None:
    """Put the tour on the welcome screen (first launch, or `force`). Returns it, or None when not shown."""
    s = getattr(win, "settings", None)
    welcome = getattr(win, "welcome", None)
    if welcome is None or s is None or (not force and _truthy(s.value(TOUR_DONE, False))):
        return None
    old = tour_of(win)
    if old is not None:
        return old
    anchor = getattr(welcome, "drop", None)
    col = anchor.parentWidget() if anchor is not None else None
    lay = col.layout() if col is not None else None
    if lay is None:
        return None
    tour = Tour(win, col)
    lay.insertWidget(lay.indexOf(anchor) + 1, tour)
    lay.insertSpacing(lay.indexOf(tour), 12)
    tour.show()  # (a child added to a visible parent stays hidden until shown)
    win._tour = tour
    tour.finished.connect(lambda _skipped: setattr(win, "_tour", None))
    return tour


# ------------------------------------------------------------------ tips
def tip_seen(settings, name: str) -> bool:
    return _truthy(settings.value(TIP_PREFIX + name, False))


def tip_once(win, name: str, text: str, action_text: str | None = None, callback=None, ms: int = 7000) -> bool:
    """Show a toast the first time only. Returns True if it was shown now."""
    s = getattr(win, "settings", None)
    if s is None or tip_seen(s, name):
        return False
    s.setValue(TIP_PREFIX + name, True)
    win.toast(text, action_text, callback, ms=ms)
    return True


EDIT_TEXT_WHY = (
    "Leaf edits the page's own text by rewriting it with the font that is already in the PDF, so the result looks "
    "exactly like the original. It refuses when it can't do that faithfully:\n\n"
    "• the font has no reliable Unicode mapping (the text would come out as the wrong letters),\n"
    "• scripts that need shaping (Arabic, Hebrew, Indic, Thai, …) or vertical writing,\n"
    "• invisible OCR text, outlined or clipping text.\n\n"
    "You can still cover it with Whiteout (W) and add a new text box (T), or use Redact (X) to remove it for real.")


def explain_edit_refusal(win) -> None:
    box = QMessageBox(QMessageBox.Icon.Information, "Why some text can't be edited", EDIT_TEXT_WHY,
                      QMessageBox.StandardButton.Ok, win)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.open()  # window-modal sheet, doesn't block the event loop


def wrap_edit_text_toast(win, tool) -> None:
    """The Edit text tool's toasts are refusals: the first one gets a "Why?" link (once per user)."""
    orig = getattr(tool, "toast", None)
    if orig is None or getattr(orig, "_leaf_tip", False):
        return

    def toast(text, action_text=None, callback=None, ms: int = 2500, **kw):
        s = getattr(win, "settings", None)
        if action_text is None and s is not None and not tip_seen(s, "edit_refusal"):
            s.setValue(TIP_PREFIX + "edit_refusal", True)
            return orig(text, "Why?", lambda: explain_edit_refusal(win), ms=max(ms, 8000))
        return orig(text, action_text, callback, ms=ms, **kw)

    toast._leaf_tip = True
    tool.toast = toast


def reset(win) -> None:
    """Help › Show tips again: forget which tips were shown and bring the tour back on the welcome screen."""
    s = getattr(win, "settings", None)
    if s is None:
        return
    s.remove(TOUR_DONE)
    s.beginGroup(TIP_PREFIX.rstrip("/"))
    s.remove("")
    s.endGroup()
    from .window import _windows

    for w in list(_windows) or [win]:
        try:
            show_tour(w, force=True)
        except RuntimeError:
            pass
    if getattr(win.canvas, "doc", None) is not None:
        win.toast("Tips will show again. The welcome tour is on the start screen.", ms=4000)


# ------------------------------------------------------------------ empty states
class EmptyHint(QWidget):
    """A short centered hint (+ optional action) over an item view while it has nothing to show.

    Lives on the view's viewport; mouse events outside the button pass through, so drops still reach the view."""

    def __init__(self, view: QAbstractItemView, text: str, action_text: str | None = None, callback=None, when=None,
                 empty=None):
        super().__init__(view.viewport())
        self.view = view
        self.when = when  # optional callable: extra condition to show (e.g. a document is open)
        self.empty = empty  # optional callable replacing "the model has no rows"
        self.setObjectName("leafEmptyHint")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, action_text is None)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        Tour._secondary(self.label)
        lay.addWidget(self.label)
        self.button = None
        if action_text:
            self.button = QPushButton(action_text)
            self.button.setAutoDefault(False)
            self.button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.button.clicked.connect(lambda: callback() if callback else None)
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(self.button)
            row.addStretch()
            lay.addLayout(row)
        m = view.model()
        for sig in (m.rowsInserted, m.rowsRemoved, m.modelReset, m.layoutChanged):
            sig.connect(self._later)
        view.viewport().installEventFilter(self)
        self._timer = QTimer(self, singleShot=True, interval=0, timeout=self.update_state)
        self.update_state()

    def _later(self, *_) -> None:
        self._timer.start()

    def is_empty(self) -> bool:
        if self.empty is not None:
            return bool(self.empty())
        m = self.view.model()
        return m is None or m.rowCount() == 0

    def update_state(self) -> None:
        try:
            show = self.is_empty() and (self.when is None or bool(self.when()))
        except RuntimeError:
            return
        if show:
            self._place()
            self.show()
            self.raise_()
        else:
            self.hide()

    def _place(self) -> None:
        vp = self.view.viewport().rect()
        w = max(120, min(vp.width() - 16, 300))
        self.setFixedWidth(w)
        h = min(self.heightForWidth(w) if self.hasHeightForWidth() else self.sizeHint().height(), vp.height())
        h = max(h, self.sizeHint().height())
        self.resize(w, h)
        self.move(vp.center().x() - w // 2, max(8, vp.height() // 3 - h // 2))

    def eventFilter(self, obj, e) -> bool:
        if e.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._later()
        return False

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self.label.setPalette(QPalette())
            Tour._secondary(self.label)
        super().changeEvent(e)


def attach_empty(view, text: str, action_text: str | None = None, callback=None, when=None,
                 empty=None) -> EmptyHint | None:
    """Give `view` an empty-state hint (idempotent per view). None if `view` isn't an item view."""
    if not isinstance(view, QAbstractItemView):
        return None
    old = view.viewport().findChild(EmptyHint, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if old is not None:
        return old
    hint = EmptyHint(view, text, action_text, callback, when, empty)
    view.empty_hint = hint
    return hint


class _Refresh(QObject):
    """Re-check every hint of a window when the document changes (their `when` may depend on it)."""

    def __init__(self, win, hints):
        super().__init__(win)
        self.hints = hints
        win.canvas.documentChanged.connect(self.kick)

    def kick(self, *_) -> None:
        for h in self.hints:
            try:
                h._later()
            except RuntimeError:
                pass


def watch(win, hints) -> None:
    hints = [h for h in hints if h is not None]
    if hints:
        win._empty_hints = getattr(win, "_empty_hints", []) + hints
        _Refresh(win, hints)
