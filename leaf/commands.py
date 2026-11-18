"""Undo commands. Items removed from the scene are kept alive by the command (and freed with it)."""
from __future__ import annotations

import time

from PySide6.QtGui import QUndoCommand

MERGE_SECS = 1.5  # repeated drags/resizes/style tweaks of the same items within this window become one step
KIND_NAMES = {
    "text": ("text box", "text boxes"), "highlight": ("highlight", "highlights"), "ink": ("drawing", "drawings"),
    "rect": ("rectangle", "rectangles"), "ellipse": ("ellipse", "ellipses"), "line": ("line", "lines"),
    "arrow": ("arrow", "arrows"), "whiteout": ("whiteout", "whiteouts"), "image": ("image", "images"),
    "signature": ("signature", "signatures"), "redact": ("redaction", "redactions"),
}
GENERIC = {"Add", "Delete", "Move", "Resize", "Nudge", "Duplicate", "Paste", "Insert"}
PROP_NAMES = {"color": "color", "width": "line width", "opacity": "opacity", "size": "font size", "fill": "fill"}


def describe(items) -> str:
    """'rectangle', '3 arrows', '2 items'."""
    items = list(items)
    kinds = {getattr(i, "kind", "") for i in items}
    k = kinds.pop() if len(kinds) == 1 else ""
    one, many = KIND_NAMES.get(k, (k or "item", f"{k}s" if k else "items"))
    return one if len(items) == 1 else f"{len(items)} {many if k else 'items'}"


def _label(text: str | None, verb: str, items) -> str:
    """Spell out generic texts ('Add', 'Add rect', 'Move') as 'Add rectangle', 'Move 2 items'."""
    words = (text or verb).split(" ", 1)
    if (len(words) == 1 and words[0] in GENERIC) or (len(words) == 2 and words[1] in KIND_NAMES):
        return f"{words[0]} {describe(items)}"
    return text


class AddItems(QUndoCommand):
    def __init__(self, items, text: str = "Add"):
        items = list(items)
        super().__init__(_label(text, "Add", items))
        self.items = [(it, it.parentItem()) for it in items]

    def redo(self) -> None:
        for it, parent in self.items:
            if it.parentItem() is not parent:
                it.setParentItem(parent)

    def undo(self) -> None:
        for it, _ in self.items:
            it.setSelected(False)
            if it.scene() is not None:
                it.scene().removeItem(it)


class RemoveItems(AddItems):
    def __init__(self, items, text: str = "Delete"):
        items = list(items)
        super().__init__(items, _label(text, "Delete", items))

    def redo(self) -> None:
        AddItems.undo(self)

    def undo(self) -> None:
        AddItems.redo(self)


class SetGeometry(QUndoCommand):
    ID = 1002

    def __init__(self, item, old, new, text: str = "Resize"):
        super().__init__(_label(text, "Resize", [item]))
        self.item, self.old, self.new = item, old, new
        self._first = True
        self.stamp = time.monotonic()

    def id(self) -> int:
        return self.ID

    def mergeWith(self, other) -> bool:
        if not isinstance(other, SetGeometry) or other.item is not self.item or other.stamp - self.stamp > MERGE_SECS:
            return False
        self.new, self.stamp = other.new, other.stamp
        self.setObsolete(self.old == self.new)
        return True

    def redo(self) -> None:
        if self._first:  # already applied interactively
            self._first = False
            return
        self.item.set_geometry(self.new)

    def undo(self) -> None:
        self.item.set_geometry(self.old)


class MoveItems(QUndoCommand):
    ID = 1001

    def __init__(self, moves: dict, text: str = "Move", nudge: bool = False):
        """moves: item -> (old_pos, new_pos). Nudges of the same items always merge; drags merge within MERGE_SECS."""
        super().__init__(_label(text, "Move", moves))
        self.moves = dict(moves)
        self.nudge = nudge
        self.stamp = time.monotonic()

    def id(self) -> int:
        return self.ID

    def mergeWith(self, other) -> bool:
        if not isinstance(other, MoveItems) or other.nudge != self.nudge or set(self.moves) != set(other.moves):
            return False
        if not self.nudge and other.stamp - self.stamp > MERGE_SECS:
            return False
        for it, (_, new) in other.moves.items():
            self.moves[it] = (self.moves[it][0], new)
        self.stamp = other.stamp
        self.setObsolete(all(old == new for old, new in self.moves.values()))
        return True

    def redo(self) -> None:
        for it, (_, new) in self.moves.items():
            it.setPos(new)

    def undo(self) -> None:
        for it, (old, _) in self.moves.items():
            it.setPos(old)


class SetProps(QUndoCommand):
    ID = 1003

    def __init__(self, changes: dict, text: str = "Change style"):
        """changes: item -> (old_props, new_props). Consecutive changes of the same property merge (sliders, spin boxes)."""
        keys = frozenset(k for old, new in changes.values() for k in set(old) | set(new) if old.get(k) != new.get(k))
        if text == "Change style" and len(keys) == 1:
            k = next(iter(keys))
            text = f"Change {PROP_NAMES.get(k, k)}"
        super().__init__(text)
        self.changes, self.keys = changes, keys
        self.stamp = time.monotonic()

    def id(self) -> int:
        return self.ID

    def mergeWith(self, other) -> bool:
        if not isinstance(other, SetProps) or set(other.changes) != set(self.changes) or other.keys != self.keys \
                or other.stamp - self.stamp > MERGE_SECS:
            return False
        for it, (_, new) in other.changes.items():
            self.changes[it] = (self.changes[it][0], new)
        self.stamp = other.stamp
        self.setObsolete(all(old == new for old, new in self.changes.values()))
        return True

    def redo(self) -> None:
        for it, (_, new) in self.changes.items():
            it.set_props(new)

    def undo(self) -> None:
        for it, (old, _) in self.changes.items():
            it.set_props(old)


class EditText(QUndoCommand):
    def __init__(self, item, old: str, new: str):
        super().__init__("Edit text")
        self.item, self.old, self.new = item, old, new
        self._first = True

    def redo(self) -> None:
        if self._first:
            self._first = False
            return
        getattr(self.item, "set_content", self.item.setPlainText)(self.new)  # text boxes keep run formatting

    def undo(self) -> None:
        getattr(self.item, "set_content", self.item.setPlainText)(self.old)


class SetFormValues(QUndoCommand):
    def __init__(self, canvas, changes: dict, text: str = "Fill form"):
        """changes: key -> (old, new); old/new of _UNSET means "use the PDF's own value"."""
        if text == "Fill form":
            names = {k[1] if isinstance(k, tuple) and len(k) > 1 else k for k in changes}
            text = f"Fill “{next(iter(names))}”" if len(names) == 1 else f"Fill {len(names)} fields"
        super().__init__(text)
        self.canvas, self.changes = canvas, changes

    def _apply(self, idx: int) -> None:
        vals = self.canvas.doc.form_values
        for key, pair in self.changes.items():
            v = pair[idx]
            if v is UNSET:
                vals.pop(key, None)
            else:
                vals[key] = v
        self.canvas.refresh_fields(self.changes.keys())

    def redo(self) -> None:
        self._apply(1)

    def undo(self) -> None:
        self._apply(0)


class EditPageText(QUndoCommand):
    """Change the text of a line/paragraph of the page itself (leaf/textedit.py). Redo applies, undo restores;
    never merges. Text equal to the original removes the edit (the page is pixel-identical again)."""

    def __init__(self, canvas, ref, unit, new_text: str, text: str = "Edit page text"):
        super().__init__(text)
        from .textedit import original_text

        self.canvas, self.ref, self.key = canvas, ref, unit.key
        self.old = canvas.doc.text_edits.get(ref.uid, {}).get(unit.key)  # None: the original text
        orig = original_text(canvas.doc, ref, unit.key)
        self.new = None if new_text.rstrip() == orig.rstrip() else new_text
        self.warnings: list[str] = []
        self.setObsolete(self.old == self.new)

    def _set(self, value) -> None:
        doc = self.canvas.doc
        edits = doc.text_edits.setdefault(self.ref.uid, {})
        if value is None:
            edits.pop(self.key, None)
        else:
            edits[self.key] = value
        if not edits:
            del doc.text_edits[self.ref.uid]
        self.warnings = self.canvas.sync_text(self.ref)

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


class EditPageTexts(QUndoCommand):
    """Many page-text edits as one undo step (Find & replace): `changes` = [(ref, unit key, new text)]; text equal
    to the original removes that edit. Every touched page re-syncs once per redo/undo, page by page (the last
    planned page first: its engine data is still cached). A page whose edits can't be applied on the first redo is
    left exactly as it was and dropped from the command: `failed` {uid: reason}. `progress(done, total)` (optional
    attribute) is called after each page."""

    def __init__(self, canvas, changes, text: str = "Replace text"):
        super().__init__(text)
        self.canvas = canvas
        doc = canvas.doc
        self.changes: list[tuple] = []  # (ref, key, old, new); None = the original text
        for ref, key, new in changes:
            old = doc.text_edits.get(ref.uid, {}).get(key)
            if old != new:
                self.changes.append((ref, key, old, new))
        self.warnings: dict[int, list[str]] = {}
        self.failed: dict[int, str] = {}
        self.progress = None
        self._first = True
        self.setObsolete(not self.changes)

    def _pages(self) -> list[tuple]:
        by: dict[int, tuple] = {}
        for ch in self.changes:
            by.setdefault(ch[0].uid, (ch[0], []))[1].append(ch)
        return list(reversed(by.values()))

    def _set(self, which: int, changes) -> None:
        edits_of = self.canvas.doc.text_edits
        for ch in changes:
            ref, key, value = ch[0], ch[1], ch[which]
            edits = edits_of.setdefault(ref.uid, {})
            if value is None:
                edits.pop(key, None)
            else:
                edits[key] = value
            if not edits:
                del edits_of[ref.uid]

    def _apply(self, which: int) -> None:
        from .textedit import original_text

        pages = self._pages()
        keep: list[tuple] = []
        for i, (ref, mine) in enumerate(pages):
            if self._first:  # text equal to the original removes the edit (the page is pixel-identical again)
                doc = self.canvas.doc
                norm = []
                for r, key, old, new in mine:
                    if new is not None and new.rstrip() == original_text(doc, r, key).rstrip():
                        new = None
                    if old != new:
                        norm.append((r, key, old, new))
                mine = norm
            if mine:
                self._set(which, mine)
                try:
                    self.warnings[ref.uid] = self.canvas.sync_text(ref)
                except Exception as ex:  # noqa: BLE001  (textedit.Refuse or a MuPDF error)
                    if not self._first:
                        raise
                    self._set(2, mine)
                    self.canvas.sync_text(ref)
                    self.failed[ref.uid] = str(ex) or "The change couldn’t be applied."
                    mine = []
            keep[:0] = mine
            if self.progress is not None:
                self.progress(i + 1, len(pages))
        if self._first:
            self.changes = keep

    def redo(self) -> None:
        self._apply(3)
        self._first = False
        if not self.changes:
            self.setObsolete(True)

    def undo(self) -> None:
        self._apply(2)


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class SetPages(QUndoCommand):
    def __init__(self, canvas, old, new, text: str):
        """old/new: list of (PageRef, rotation)"""
        super().__init__(text)
        self.canvas, self.old, self.new = canvas, old, new

    def _apply(self, state) -> None:
        for ref, rot in state:
            ref.rotation = rot
        self.canvas.doc.pages = [ref for ref, _ in state]
        self.canvas.sync_pages()

    def redo(self) -> None:
        self._apply(self.new)

    def undo(self) -> None:
        self._apply(self.old)
