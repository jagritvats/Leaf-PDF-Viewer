"""Text box formatting: Bold / Italic / Underline / Strikethrough (Ctrl+B / I / U, Ctrl+Shift+X) and alignment
actions, the style panel showing the format at the text cursor while editing, and the font fallback notice."""
from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence

from .. import fonts, shortcuts
from ..items import TextItem, editing_text_item
from ..richtext import ALIGNS

TOGGLES = [("bold", "&Bold", "Ctrl+B"), ("italic", "&Italic", "Ctrl+I"), ("underline", "&Underline", "Ctrl+U"),
           ("strike", "&Strikethrough", "Ctrl+Shift+X")]
ALIGN_TEXT = {"left": "Align &left", "center": "&Center", "right": "Align &right", "justify": "&Justify"}


def _action(win, name: str, text: str, slot, keys=(), tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    a.setProperty("base_tip", tip or shortcuts.clean(text))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def current_text_props(win) -> dict | None:
    """What a toggle acts on: the text at the cursor while editing, the selected text boxes, or the text tool's
    defaults. None when no text is involved."""
    cv = win.canvas
    ed = editing_text_item(cv.scene())
    if ed is not None:
        return ed.cursor_props()
    boxes = [i for i in cv.selected_overlays() if isinstance(i, TextItem)]
    if boxes:
        props = dict(boxes[0].props)
        for k in ("bold", "italic", "underline", "strike"):
            props[k] = all(b.props.get(k) for b in boxes)
        return props
    if cv.tool == "text" and not cv.selected_overlays():
        return dict(cv.defaults["text"])
    return None


def apply(win, delta: dict) -> None:
    fonts.warn_unembeddable(win, delta)
    win.canvas.apply_props(delta)
    win._refresh_props()
    mb = getattr(win, "minibar", None)
    if mb is not None:
        mb.refresh()


def toggle(win, key: str) -> None:
    props = current_text_props(win)
    if props is None:
        return
    apply(win, {key: not props.get(key)})


def set_align(win, align: str) -> None:
    if current_text_props(win) is not None:
        apply(win, {"align": align})


def install(win) -> None:
    cv = win.canvas
    acts = [_action(win, f"text_{key}", text, lambda _=False, k=key: toggle(win, k), [keys]) for key, text, keys in TOGGLES]
    aligns = [_action(win, f"text_align_{a}", ALIGN_TEXT[a], lambda _=False, a=a: set_align(win, a),
                      tip=f"Text: {shortcuts.clean(ALIGN_TEXT[a]).lower()}") for a in ALIGNS]
    menus = getattr(win, "menus", {})
    if "edit" in menus:
        m = menus["edit"].addMenu("Text &format")
        m.setObjectName("text_format_menu")
        m.addActions(acts)
        m.addSeparator()
        m.addActions(aligns)

    # the style panel shows the box style; while editing, show the format at the text cursor instead
    def show_cursor_format() -> None:
        ed = editing_text_item(cv.scene())
        if ed is not None:
            win.props.set_context("text", ed.cursor_props(), "Applies to the selected text")

    cv.selectionInfo.connect(show_cursor_format)
    win.props.changed.connect(lambda d: fonts.warn_unembeddable(win, d))
