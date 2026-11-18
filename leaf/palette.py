"""Command palette (Ctrl+K / Ctrl+Shift+P): fuzzy-search every live action, jump to a page, open a recent file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QLineEdit, QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout,
)

from .shortcuts import SEP, all_entries, apply_saved, clean, current, native

RECENT_KEY = "palette/recent"
MAX_ROWS = 250
_PAGE_RE = re.compile(r"^\s*(?:p(?:age)?\.?\s*)?(\d+)\s*$", re.IGNORECASE)


@dataclass
class Entry:
    title: str
    group: str = ""  # "Pages › " style prefix, drawn muted
    shortcut: str = ""
    run: Callable[[], None] | None = None
    enabled: bool = True
    key: str = ""  # stable id for "recently used"
    tip: str = ""
    hay: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.hay = f"{self.group}{self.title}".lower()
        self.low = self.title.lower()


def _score(q: str, text: str, start: int = 0) -> tuple[float, int] | None:
    """Fuzzy score (higher is better) of `q` in `text` from `start`, and where the match ends; None if no match.
    Both lowercase. Substrings beat scattered letters; word starts beat mid-word hits."""
    best = None
    i = text.find(q, start)
    while i >= 0:
        boundary = i == 0 or not text[i - 1].isalnum()
        sc = 100.0 + (30 if boundary else 0) - (i - start) * 0.5
        if best is None or sc > best[0]:
            best = (sc, i + len(q))
        if boundary:
            break
        i = text.find(q, i + 1)
    if best is not None:
        return best[0] - len(text) * 0.1, best[1]
    score, pos, prev = 0.0, start, -2
    for ch in q:
        j = text.find(ch, pos)
        if j < 0:
            return None
        if j == prev + 1:
            score += 6
        elif j == 0 or not text[j - 1].isalnum():
            score += 9
        else:
            score -= min(j - pos, 8)
        prev, pos = j, j + 1
    return score - len(text) * 0.1, pos


def match(query: str, entries: list[Entry]) -> list[Entry]:
    """Entries matching every word of `query` (in order in the title preferred), best first; disabled ones last."""
    words = query.lower().split()
    if not words:
        return list(entries)
    scored = []
    for n, e in enumerate(entries):
        total, pos = 0.0, 0
        for w in words:
            t = _score(w, e.low, pos)
            if t is None and pos:
                t = _score(w, e.low)
                t = t and (t[0] - 20, t[1])
            p = _score(w, e.hay)
            if p is not None and (t is None or p[0] - 15 > t[0]):  # menu-path hits count less than title hits
                total += p[0] - 15
            elif t is not None:
                total += t[0]
                pos = t[1]
            else:
                break
        else:
            scored.append((not e.enabled, -total, n, e))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored]


def page_entry(win, query: str) -> Entry | None:
    m = _PAGE_RE.match(query)
    cv = win.canvas
    if not m or cv.doc is None:
        return None
    n, total = int(m.group(1)), len(cv.order)
    if 1 <= n <= total:
        return Entry(f"Go to page {n}", shortcut=f"of {total}", run=lambda: (cv.go_to(n - 1), cv.setFocus()), key="")
    return Entry(f"Page {n} doesn't exist", shortcut=f"1–{total}", enabled=False)


def action_entries(win) -> list[Entry]:
    out = []
    for path, a in all_entries(win):
        if not a.isVisible():
            continue
        seqs = current(a)
        title = clean(a.text())
        if a.isCheckable():
            title += "  ✓" if a.isChecked() else ""
        hint = a.text().split("\t", 1)[1] if "\t" in a.text() else ""  # shortcut that works in one widget only
        out.append(Entry(title, SEP.join(path) + SEP, native(seqs[0]) if seqs else hint, a.trigger, a.isEnabled(),
                         a.objectName() or title, a.property("base_tip") or ""))
    return out


def recent_entries(win) -> list[Entry]:
    out = []
    for p in (win.settings.value("recent") or [])[:12]:
        if isinstance(p, str) and os.path.exists(p):
            out.append(Entry(f"Open recent: {os.path.basename(p)}", run=lambda p=p: win.open_path(p), key=f"recent:{p}", tip=p))
    return out


class _Delegate(QStyledItemDelegate):
    def paint(self, p, opt, idx) -> None:
        e: Entry = idx.data(Qt.ItemDataRole.UserRole)
        self.initStyleOption(opt, idx)
        style = opt.widget.style() if opt.widget else QApplication.style()
        opt.text = ""
        pal = opt.palette
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        # The selected row gets the palette's Highlight background with HighlightedText on it, painted here: styles
        # draw a selected item differently (Windows 11: a faint tint meant for normal Text; an unfocused list: the
        # Inactive highlight), so the style's background and the text colour chosen here wouldn't match (white on a
        # light row in light mode). The list never takes focus (the line edit keeps it), so use the Active group.
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, p, opt.widget)
        if selected:
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight))
            p.drawRoundedRect(QRectF(opt.rect).adjusted(2, 1, -2, -1), 4, 4)
            p.restore()
        if e is None:
            return
        role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        fg = pal.color(QPalette.ColorGroup.Disabled if not e.enabled else QPalette.ColorGroup.Active, role)
        if selected and not e.enabled:
            fg = pal.color(QPalette.ColorGroup.Active, role)
            fg.setAlphaF(0.6)
        muted = pal.color(QPalette.ColorGroup.Active, role)
        muted.setAlphaF(0.78 if selected else 0.7)  # a11y: group and shortcut stay AA (4.5:1) on the popup
        r = opt.rect.adjusted(12, 0, -12, 0)
        fm = QFontMetrics(opt.font)
        p.save()
        if e.shortcut:
            p.setPen(muted)
            sw = fm.horizontalAdvance(e.shortcut)
            p.drawText(QRect(r.right() - sw, r.top(), sw, r.height()), Qt.AlignmentFlag.AlignVCenter, e.shortcut)
            r.setRight(r.right() - sw - 16)
        if e.group:
            p.setPen(muted)
            g = fm.elidedText(e.group, Qt.TextElideMode.ElideRight, r.width() // 2)
            gw = fm.horizontalAdvance(g)
            p.drawText(QRect(r.left(), r.top(), gw, r.height()), Qt.AlignmentFlag.AlignVCenter, g)
            r.setLeft(r.left() + gw)
        p.setPen(fg)
        p.drawText(r, Qt.AlignmentFlag.AlignVCenter, fm.elidedText(e.title, Qt.TextElideMode.ElideRight, r.width()))
        p.restore()

    def sizeHint(self, opt, idx) -> QSize:
        return QSize(100, QFontMetrics(opt.font).height() + 14)


class CommandPalette(QFrame):
    def __init__(self, win):
        super().__init__(win, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.win = win
        self.setObjectName("leafPalette")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.edit = QLineEdit(placeholderText="Type a command, a page number, or a file name")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumHeight(QFontMetrics(self.edit.font()).height() + 16)
        self.list = QListWidget()
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        lay.addWidget(self.edit)
        lay.addWidget(self.list)
        self.entries: list[Entry] = []
        self.shown: list[Entry] = []
        self.edit.textChanged.connect(self.refilter)
        self.edit.installEventFilter(self)
        self.list.itemClicked.connect(lambda it: self.run(it.data(Qt.ItemDataRole.UserRole)))

    # ----------------------------------------------------------------- data
    def collect(self) -> None:
        apply_saved(self.win, only_new=True)  # names, defaults and overrides for actions added since install
        acts = action_entries(self.win)
        rec = recent_entries(self.win)
        order = {k: i for i, k in enumerate(self.recent_keys())}
        acts.sort(key=lambda e: order.get(e.key, len(order)))  # recently used first, then menu order
        self.entries = acts + rec

    def recent_keys(self) -> list[str]:
        v = self.win.settings.value(RECENT_KEY) or []
        return [v] if isinstance(v, str) else [str(x) for x in v]

    def _remember(self, key: str) -> None:
        if not key:
            return
        keys = [key] + [k for k in self.recent_keys() if k != key]
        self.win.settings.setValue(RECENT_KEY, keys[:8])

    def refilter(self, text: str = "") -> None:
        text = self.edit.text()
        shown = match(text, self.entries)
        pe = page_entry(self.win, text)
        if pe is not None:
            shown.insert(0, pe)
        self.shown = shown[:MAX_ROWS]
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        first = None
        for e in self.shown:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, e)
            if e.tip:
                it.setToolTip(e.tip)
            if not e.enabled:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            elif first is None:
                first = it
            self.list.addItem(it)
        if first is not None:
            self.list.setCurrentItem(first)
        self.list.setUpdatesEnabled(True)
        self._fit()

    # ------------------------------------------------------------------- ui
    def _restyle(self) -> None:
        """Style layer: a popup surface, the query as a large borderless field over a hairline, the selected row
        in the soft accent tint (its text stays the normal text color)."""
        from . import style

        if not style.active():
            return
        from .style.tokens import rgba

        t = style.current()
        self.setStyleSheet(
            f"#leafPalette {{ background: {rgba(t.popup)}; border: 1px solid {rgba(t.stroke2)}; border-radius: {t.r_l}px; }}"
            f"#leafPalette QLineEdit {{ background: transparent; border: none; border-bottom: 1px solid {rgba(t.stroke)};"
            f" border-radius: 0; padding: 8px 10px 10px 10px; font-size: {t.pt * 1.15:.1f}pt; }}"
            f"#leafPalette QLineEdit:focus {{ border: none; border-bottom: 1px solid {rgba(t.stroke)}; padding-bottom: 10px; }}"
            "#leafPalette QListWidget { background: transparent; }")
        self.layout().setContentsMargins(6, 4, 6, 6)
        pal = self.list.palette()
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight, t.accent_soft)
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText, t.text)
        self.list.setPalette(pal)

    def popup(self) -> None:
        self._restyle()  # the theme may have changed since the last time
        self.collect()
        self.edit.clear()
        self.refilter()
        self._place()
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()

    def _place(self) -> None:
        cv = self.win.canvas
        host = cv if cv.isVisible() else self.win
        w = max(320, min(620, host.width() - 32))
        top_left = host.mapToGlobal(QPoint((host.width() - w) // 2, min(56, host.height() // 8)))
        self.setFixedWidth(w)
        self.move(top_left)
        self._fit()

    def _fit(self) -> None:
        rows = min(max(len(self.shown), 1), 10)
        h = self.list.sizeHintForRow(0) if self.list.count() else 30
        self.list.setFixedHeight(rows * h + 4)
        self.adjustSize()

    def current_entry(self) -> Entry | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def accept_current(self) -> None:
        self.run(self.current_entry())

    def run(self, e: Entry | None) -> None:
        if e is None or not e.enabled or e.run is None:
            return
        self._remember(e.key)
        self.hide()
        QTimer.singleShot(0, e.run)  # after the popup has closed and focus is back in the window

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.edit and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.accept_current()
                return True
            if k == Qt.Key.Key_Escape:
                self.hide()
                return True
            if k in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                if self.list.count():
                    QApplication.sendEvent(self.list, ev)
                return True
            if k in (Qt.Key.Key_Home, Qt.Key.Key_End) and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                QApplication.sendEvent(self.list, ev)
                return True
        return super().eventFilter(obj, ev)

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        if self.win.isVisible() and QApplication.activePopupWidget() is None:
            self.win.activateWindow()


def show_palette(win) -> CommandPalette:
    pal = win.findChild(CommandPalette)
    if pal is None:
        pal = CommandPalette(win)
    pal.popup()
    return pal
