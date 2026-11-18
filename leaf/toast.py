"""Small non-blocking notifications that fade in near the bottom of the central view."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QWidget


class Toast(QFrame):
    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("leafToast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 9, 16, 9)
        lay.setSpacing(14)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton()
        self.button.setFlat(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.clicked.connect(self._clicked)
        lay.addWidget(self.label)
        lay.addWidget(self.button)
        self._callback = None
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(0.0)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._anim_done)
        self._timer = QTimer(self, singleShot=True, timeout=self.dismiss)
        host.installEventFilter(self)
        bar = getattr(host.window(), "minibar", None)
        if bar is not None:
            bar.installEventFilter(self)  # re-place when the mini toolbar appears or moves
        self._restyle()
        self.hide()

    def _restyle(self) -> None:
        pal = QApplication.palette()
        dark = pal.color(QPalette.ColorRole.Window).lightness() < 128
        # inverted surface, like Windows/GNOME snackbars: readable over any page
        bg, fg = ("#f3f3f3", "#1a1a1a") if dark else ("#2b2b2b", "#f5f5f5")
        acc = pal.color(QPalette.ColorRole.Accent)
        if dark:  # light surface: a deeper accent
            link = (acc.darker(140) if acc.lightness() > 110 else acc).name()
        else:
            link = (acc.lighter(165) if acc.lightness() < 140 else acc).name()
        from .a11y import high_contrast, readable

        border = "rgba(128,128,128,60)"
        if high_contrast():  # the theme's own colors, a solid edge
            bg = pal.color(QPalette.ColorRole.ToolTipBase).name()
            fg = pal.color(QPalette.ColorRole.ToolTipText).name()
            link = pal.color(QPalette.ColorRole.Link).name()
            border = fg
        link = readable(link, bg).name()  # a11y: WCAG AA (4.5:1) for the action button on any accent color
        ss = (f"#leafToast {{ background: {bg}; border-radius: 8px; border: 1px solid {border}; }}"
              f"#leafToast QLabel {{ color: {fg}; background: transparent; }}"
              f"#leafToast QPushButton {{ color: {link}; font-weight: 600; border: none; background: transparent;"
              f" padding: 2px 6px; }}"
              f"#leafToast QPushButton:hover {{ text-decoration: underline; }}")
        if ss != self.styleSheet():  # setting a style sheet fires PaletteChange again
            self.setStyleSheet(ss)

    def show_text(self, text: str, action_text: str | None, callback, ms: int) -> None:
        self.label.setText(text)
        self._callback = callback
        self.button.setText(action_text or "")
        self.button.setVisible(bool(action_text))
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._fade(1.0, 160)
        self._timer.start(max(800, ms))

    def dismiss(self) -> None:
        self._timer.stop()
        if self.isVisible():
            self._fade(0.0, 220)

    def _fade(self, to: float, ms: int) -> None:
        self._anim.stop()
        self._anim.setDuration(ms)
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(to)
        self._anim.start()

    def _anim_done(self) -> None:
        if self._fx.opacity() < 0.01:
            self.hide()

    def _clicked(self) -> None:
        cb, self._callback = self._callback, None
        self.dismiss()
        if cb:
            cb()

    def _place(self) -> None:
        host = self.parentWidget()
        x, y = (host.width() - self.width()) // 2, host.height() - self.height() - 28
        # stay clear of the floating mini toolbar (it often sits near the bottom with the selection)
        win = host.window()
        bar = getattr(win, "minibar", None)
        if bar is not None and bar.isVisible():
            r = bar.geometry().translated(bar.parentWidget().mapTo(host, bar.parentWidget().rect().topLeft()))
            if r.intersects(self.rect().translated(x, y)):
                y = max(8, r.top() - self.height() - 10)
        self.move(x, y)

    def enterEvent(self, e) -> None:
        self._timer.stop()  # keep it while the pointer is on it
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._timer.start(1200)
        super().leaveEvent(e)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        if e.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Move, QEvent.Type.Hide) and self.isVisible():
            self._place()
        return False

    def changeEvent(self, e) -> None:
        if e.type() == QEvent.Type.PaletteChange:
            self._restyle()
        super().changeEvent(e)


def show_toast(win: QWidget, text: str, action_text: str | None = None, callback=None, ms: int = 2500) -> Toast:
    """Show `text` bottom-center over the window's central area; replaces a toast that is still showing."""
    host = win.centralWidget() if hasattr(win, "centralWidget") and win.centralWidget() else win
    t = host.findChild(Toast, options=Qt.FindChildOption.FindDirectChildrenOnly)
    if t is None:
        t = Toast(host)
    t.show_text(text, action_text, callback, ms)
    return t
