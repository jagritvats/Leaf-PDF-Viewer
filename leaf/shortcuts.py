"""Keyboard shortcuts: stable action names, user overrides (QSettings "shortcuts/<objectName>"),
the preferences dialog and the generated cheat sheet. Everything is discovered from the live actions,
so actions added by feature modules show up without being registered anywhere."""
from __future__ import annotations

import re

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHeaderView, QKeySequenceEdit, QLabel, QLineEdit, QMessageBox,
    QPushButton, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

PREFIX = "shortcuts/"
TOOL_PREFIX = "a_tool_"
SEP = " › "
FMT = QKeySequence.SequenceFormat.PortableText
NATIVE = QKeySequence.SequenceFormat.NativeText
GESTURES = [  # not actions; listed in the cheat sheet only
    ("Space + drag", "Pan"), ("Middle-drag", "Pan"), ("Ctrl + wheel", "Zoom at cursor"),
    ("Arrows / Shift+Arrows", "Nudge selection 1 pt / 10 pt"), ("Del", "Delete selection"),
    ("Esc", "Deselect, then back to the Select tool"), ("Tab / Shift+Tab", "Next / previous form field"),
    ("?", "This cheat sheet (when the page has focus)"),
]


def clean(text: str) -> str:
    """Menu text without mnemonics, ellipsis and a tab-separated shortcut hint."""
    return text.split("\t", 1)[0].replace("&&", "\0").replace("&", "").replace("\0", "&").replace("…", "").replace("...", "").strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", clean(text).lower()).strip("_")


def native(seq: QKeySequence) -> str:
    return seq.toString(NATIVE)


# ----------------------------------------------------------------- discovery
def menu_entries(win) -> list[tuple[list[str], QAction]]:
    """(menu path, action) for every leaf action reachable from the menu bar, in menu order."""
    out: list[tuple[list[str], QAction]] = []
    skip = getattr(win, "recent_menu", None)

    def walk(menu, path):
        if menu is skip:
            return
        for a in menu.actions():
            if a.isSeparator() or not a.text():
                continue
            if a.menu() is not None:
                walk(a.menu(), path + [clean(a.text())])
            else:
                out.append((path, a))

    for top in win.menuBar().actions():
        if top.menu() is not None:
            walk(top.menu(), [clean(top.text())])
    return out


def all_entries(win) -> list[tuple[list[str], QAction]]:
    """Menu actions plus actions that only live on the window (toolbar/hidden), deduplicated."""
    ensure_names(win)
    out = menu_entries(win)
    seen = {a for _, a in out}
    for a in win.findChildren(QAction, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if a not in seen and a.text() and not a.isSeparator() and a.menu() is None and a.objectName():
            seen.add(a)
            out.append((["Other"], a))
    return out


def is_tool(a: QAction) -> bool:
    return a.property("tool") is not None


def ensure_names(win) -> None:
    """Give every action a stable objectName and remember its default shortcut (idempotent, cheap)."""
    for name, v in list(vars(win).items()):
        if isinstance(v, QAction) and not v.objectName():
            v.setObjectName(name)
    keys = getattr(win.canvas, "tool_keys", {})
    for tool, a in getattr(win, "tool_actions", {}).items():
        if not a.objectName():
            a.setObjectName(TOOL_PREFIX + tool)
        if a.property("tool") is None:
            a.setProperty("tool", tool)
            key = next((k for k, t in keys.items() if t == tool), None)
            a.setProperty("tool_key", QKeySequence(key).toString(FMT) if key is not None else "")
            a.setProperty("default_tool_key", a.property("tool_key"))
    for path, a in menu_entries(win):
        if not a.objectName():
            a.setObjectName("a_" + _slug("_".join(path[:1] + [a.text()])))
    for a in win.findChildren(QAction):
        if a.property("default_shortcuts") is None and a.objectName():
            a.setProperty("default_shortcuts", QKeySequence.listToString(a.shortcuts(), FMT))
            a.setProperty("base_tip", _base_tip(a))


def _base_tip(a: QAction) -> str:
    """Tooltip without a trailing "(shortcut)", so it can be re-added when the shortcut changes."""
    tip = a.toolTip()
    m = re.match(r"^(.*?)\s*\(([^()]*)\)$", tip, re.DOTALL)
    keys = {native(s) for s in current(a)} | {s.toString(FMT) for s in current(a)}
    return m.group(1) if m and m.group(2) in keys else tip


def current(a: QAction) -> list[QKeySequence]:
    """Effective shortcuts; a tool's single key comes first."""
    seqs = list(a.shortcuts())
    if is_tool(a) and a.property("tool_key"):
        seqs.insert(0, QKeySequence(a.property("tool_key")))
    return _unique(seqs)


def _unique(seqs: list[QKeySequence]) -> list[QKeySequence]:
    """Without empties and repeats (e.g. Redo's StandardKey already is Ctrl+Y on Windows)."""
    seen: set[str] = set()
    out = []
    for s in seqs:
        t = s.toString(FMT)
        if t and t not in seen:
            seen.add(t)
            out.append(s)
    return out


def defaults(a: QAction) -> list[QKeySequence]:
    seqs = QKeySequence.listFromString(a.property("default_shortcuts") or "", FMT)
    if is_tool(a) and a.property("default_tool_key"):
        seqs.insert(0, QKeySequence(a.property("default_tool_key")))
    return _unique(seqs)


def _is_plain_key(seq: QKeySequence) -> bool:
    return seq.count() == 1 and seq[0].keyboardModifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)


def _set(a: QAction, seqs: list[QKeySequence]) -> None:
    seqs = _unique(seqs)
    if is_tool(a):  # a plain key is handled by the canvas (only while the page has focus); anything else is a real shortcut
        plain = next((s for s in seqs if _is_plain_key(s)), None)
        a.setProperty("tool_key", plain.toString(FMT) if plain else "")
        seqs = [s for s in seqs if s is not plain]
    a.setShortcuts(seqs)
    base = a.property("base_tip") or clean(a.text())
    first = current(a)
    a.setToolTip(f"{base}  ({native(first[0])})" if first else base)


def sync_tool_keys(win) -> None:
    keys = {}
    for a in getattr(win, "tool_actions", {}).values():
        seq = QKeySequence(a.property("tool_key") or "")
        if not seq.isEmpty():
            keys[seq[0].key()] = a.property("tool")
    win.canvas.tool_keys = keys


def apply_saved(win, only_new: bool = False) -> None:
    """Apply persisted overrides to this window's actions and refresh tooltips.
    only_new: just the actions not seen before (cheap; used when the palette or a dialog opens)."""
    ensure_names(win)
    s = win.settings
    for _, a in all_entries(win):
        if only_new and a.property("shortcuts_applied"):
            continue
        a.setProperty("shortcuts_applied", True)
        key = PREFIX + a.objectName()
        if s.contains(key):
            _set(a, QKeySequence.listFromString(str(s.value(key) or ""), FMT))
        else:
            _set(a, defaults(a))
    sync_tool_keys(win)


def save(win, a: QAction, seqs: list[QKeySequence]) -> None:
    """Persist (or clear, when equal to the defaults) one action's shortcuts."""
    key = PREFIX + a.objectName()
    seqs = [s for s in seqs if not s.isEmpty()]
    if [x.toString(FMT) for x in seqs] == [x.toString(FMT) for x in defaults(a)]:
        win.settings.remove(key)
    else:
        win.settings.setValue(key, QKeySequence.listToString(seqs, FMT))


def apply_everywhere() -> None:
    for w in QApplication.topLevelWidgets():
        if hasattr(w, "canvas") and hasattr(w, "settings") and hasattr(w, "menus"):
            apply_saved(w)


def duplicates(win) -> dict[str, list[QAction]]:
    """Key sequences bound to more than one action (Qt fires neither of them)."""
    by: dict[str, list[QAction]] = {}
    for _, a in all_entries(win):
        for s in {s.toString(FMT) for s in current(a)}:
            by.setdefault(s, []).append(a)
    return {k: v for k, v in by.items() if len(v) > 1}


# --------------------------------------------------------------- preferences
class ShortcutsDialog(QDialog):
    """One row per action with a QKeySequenceEdit; edits are pending until OK."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(640, 640)
        self.search = QLineEdit(placeholderText="Search actions or keys", clearButtonEnabled=True)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Action", "Shortcut", ""])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(1, 190)
        h.resizeSection(2, 34)
        h.setStretchLastSection(False)
        note = QLabel("Single keys without Ctrl/Alt pick a tool only while the page has focus.")
        note.setEnabled(False)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        reset_all = QPushButton("Reset all")
        bb.addButton(reset_all, QDialogButtonBox.ButtonRole.ResetRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        reset_all.clicked.connect(self.reset_all)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.tree)
        lay.addWidget(note)
        lay.addWidget(bb)

        self.pending: dict[QAction, list[QKeySequence]] = {}
        self.edits: dict[QAction, QKeySequenceEdit] = {}
        self.rows: dict[QAction, QTreeWidgetItem] = {}
        self.resets: dict[QAction, QToolButton] = {}
        self.index: list[tuple[QTreeWidgetItem, QTreeWidgetItem, QAction]] = []
        self.titles: dict[QAction, str] = {}
        groups: dict[str, QTreeWidgetItem] = {}
        bold = self.font()
        bold.setBold(True)
        # uniform rows take the first row's height (a group title): make room for the editor and the item padding
        # around it, or the style's taller line edits are squeezed into a text line and the shortcuts can't be read
        row_h = QSize(0, max(QKeySequenceEdit().sizeHint().height() + 8, self.tree.fontMetrics().height() + 8))
        for path, a in all_entries(win):
            g = groups.get(path[0])
            if g is None:
                g = groups[path[0]] = QTreeWidgetItem(self.tree, [path[0]])
                g.setFirstColumnSpanned(True)
                g.setFont(0, bold)
                g.setFlags(Qt.ItemFlag.ItemIsEnabled)
                g.setSizeHint(0, row_h)
            title = SEP.join(path[1:] + [clean(a.text())])
            row = QTreeWidgetItem(g, [title])
            row.setSizeHint(0, row_h)
            row.setToolTip(0, a.property("base_tip") or title)
            self.titles[a] = title
            self.rows[a] = row
            self.index.append((g, row, a))
            self.pending[a] = current(a)
            ed = QKeySequenceEdit()
            ed.setMaximumSequenceLength(1)
            ed.setClearButtonEnabled(True)
            ed.setKeySequence(self.pending[a][0] if self.pending[a] else QKeySequence())
            ed.editingFinished.connect(lambda a=a: self._edited(a))
            ed.keySequenceChanged.connect(lambda seq, a=a: seq.isEmpty() and self._edited(a))  # clear button
            self.edits[a] = ed
            self.tree.setItemWidget(row, 1, ed)
            rb = QToolButton(text="↺", autoRaise=True, toolTip="Reset to default")
            rb.clicked.connect(lambda _=False, a=a: self.reset(a))
            self.tree.setItemWidget(row, 2, rb)
            self.resets[a] = rb
            self._mark(a)
        self.tree.expandAll()
        self.search.textChanged.connect(self.filter)
        self.search.setFocus()

    # ---------------------------------------------------------------- logic
    def conflicts(self, a: QAction, seq: QKeySequence) -> list[QAction]:
        if seq.isEmpty():
            return []
        return [b for b, seqs in self.pending.items() if b is not a and any(s.matches(seq) == QKeySequence.SequenceMatch.ExactMatch for s in seqs)]

    def ask_reassign(self, seq: QKeySequence, a: QAction, others: list[QAction]) -> bool:
        names = ", ".join(f"“{self.titles[b]}”" for b in others)
        r = QMessageBox.question(self, "Shortcut in use", f"{native(seq)} is already used by {names}.\n\n"
                                 f"Assign it to “{self.titles[a]}” instead?")
        return r == QMessageBox.StandardButton.Yes

    def assign(self, a: QAction, seq: QKeySequence) -> bool:
        """Make `seq` the primary shortcut of `a` (keeping its alternates); False if the user declined a conflict."""
        others = self.conflicts(a, seq)
        if others and not self.ask_reassign(seq, a, others):
            self._show(a)
            return False
        for b in others:
            self.pending[b] = [s for s in self.pending[b] if s.matches(seq) != QKeySequence.SequenceMatch.ExactMatch]
            self._show(b)
        rest = self.pending[a][1:]
        self.pending[a] = ([seq] if not seq.isEmpty() else []) + [s for s in rest if s != seq]
        self._show(a)
        return True

    def _edited(self, a: QAction) -> None:
        seq = self.edits[a].keySequence()
        old = self.pending[a][0] if self.pending[a] else QKeySequence()
        if seq != old:
            self.assign(a, seq)

    def reset(self, a: QAction) -> None:
        d = defaults(a)
        for seq in d:
            others = self.conflicts(a, seq)
            if others and not self.ask_reassign(seq, a, others):
                return
            for b in others:
                self.pending[b] = [s for s in self.pending[b] if s != seq]
                self._show(b)
        self.pending[a] = d
        self._show(a)

    def reset_all(self) -> None:
        for a in self.pending:
            self.pending[a] = defaults(a)
            self._show(a)

    def accept(self) -> None:
        for a, seqs in self.pending.items():
            save(self.win, a, seqs)
        apply_everywhere()
        super().accept()

    # ------------------------------------------------------------------- ui
    def _show(self, a: QAction) -> None:
        ed = self.edits[a]
        ed.blockSignals(True)
        ed.setKeySequence(self.pending[a][0] if self.pending[a] else QKeySequence())
        ed.blockSignals(False)
        self._mark(a)

    def _mark(self, a: QAction) -> None:
        row = self.rows[a]
        seqs = self.pending[a]
        changed = [s.toString(FMT) for s in seqs] != [s.toString(FMT) for s in defaults(a)]
        f = row.font(0)
        f.setItalic(changed)
        row.setFont(0, f)
        extra = ", ".join(native(s) for s in seqs[1:])
        row.setToolTip(1, f"Also: {extra}" if extra else "")
        self.resets[a].setEnabled(changed)

    def filter(self, text: str) -> None:
        words = text.strip().lower().split()
        shown: set[int] = set()
        for g, row, a in self.index:
            hay = f"{g.text(0)} {row.text(0)} {' '.join(native(s) for s in self.pending[a])}".lower()
            hide = not all(w in hay for w in words)
            row.setHidden(hide)
            if not hide:
                shown.add(id(g))
        for g in {id(g): g for g, _, _ in self.index}.values():
            g.setHidden(id(g) not in shown)


def open_preferences(win) -> None:
    apply_saved(win, only_new=True)  # names + defaults for actions added since install
    ShortcutsDialog(win).exec()


# --------------------------------------------------------------- cheat sheet
class CheatSheet(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(520, 600)
        self.search = QLineEdit(placeholderText="Search", clearButtonEnabled=True)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)  # platform button order
        customize = btns.addButton("Customize…", QDialogButtonBox.ButtonRole.ActionRole)
        customize.setAutoDefault(False)
        customize.clicked.connect(lambda: (self.close(), open_preferences(win)))
        btns.rejected.connect(self.close)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.tree)
        lay.addWidget(btns)
        self.search.textChanged.connect(self.filter)

    def rebuild(self) -> None:
        self.tree.clear()
        bold = self.font()
        bold.setBold(True)
        groups: dict[str, list[tuple[str, str]]] = {}
        for path, a in all_entries(self.win):
            keys = " / ".join(native(s) for s in current(a)) or (a.text().split("\t", 1)[1] if "\t" in a.text() else "")
            if keys:
                groups.setdefault(path[0], []).append((keys, SEP.join(path[1:] + [clean(a.text())])))
        groups["Mouse and keys"] = GESTURES
        for name, rows in groups.items():
            g = QTreeWidgetItem(self.tree, [name])
            g.setFont(0, bold)
            g.setFirstColumnSpanned(True)
            for keys, title in rows:
                it = QTreeWidgetItem(g, [keys, title])
                it.setFont(0, bold)
        self.tree.expandAll()
        self.filter(self.search.text())

    def filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            shown = 0
            for j in range(g.childCount()):
                row = g.child(j)
                hay = f"{g.text(0)} {row.text(0)} {row.text(1)}".lower()
                hide = bool(q) and not all(w in hay for w in q.split())
                row.setHidden(hide)
                shown += not hide
            g.setHidden(shown == 0)


def show_cheat_sheet(win) -> None:
    apply_saved(win, only_new=True)
    sheet = win.findChild(CheatSheet)
    if sheet is None:
        sheet = CheatSheet(win)
    sheet.rebuild()
    sheet.search.clear()
    sheet.show()
    sheet.raise_()
    sheet.activateWindow()
    sheet.search.setFocus()

