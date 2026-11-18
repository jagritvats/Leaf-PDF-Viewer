"""Output: Print… / Print preview, document Properties… (metadata, fonts, security) and Protect with password…"""
from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence

from .. import docprops, printing
from ..icons import icon


def _action(win, name: str, text: str, slot, keys=(), ic: str | None = None, tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    if ic:
        a.setIcon(icon(ic))
        a.setProperty("icon_name", ic)
    a.setProperty("base_tip", tip or text.replace("&", "").rstrip("…"))
    a.triggered.connect(slot)
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def install(win) -> None:
    cv = win.canvas
    acts = [
        _action(win, "print", "&Print…", lambda: printing.print_document(win), [QKeySequence.StandardKey.Print],
                "print", "Print the document"),
        _action(win, "print_preview", "Print pre&view…", lambda: printing.preview(win)),
        _action(win, "doc_props", "P&roperties…", lambda: docprops.show_properties(win), ["Ctrl+Alt+P"], "info",
                "Document properties: title, author, fonts, security"),
        _action(win, "protect", "Protect with pass&word…", lambda: docprops.protect(win), (), "lock",
                "Require a password to open, or restrict printing and editing"),
    ]

    m = getattr(win, "menus", {}).get("file")
    if m is not None:
        # after the save group, before "Close": ... Save flattened copy | Print, Preview | Properties, Protect | Close
        items = m.actions()
        close = getattr(win, "a_close", None)
        before = None
        if close in items:
            k = items.index(close)
            before = items[k - 1] if k and items[k - 1].isSeparator() else close
        group = [acts[0], acts[1], None, acts[2], acts[3]]
        if before is None:
            m.addSeparator()
        else:
            m.insertSeparator(before)
        for a in group:
            if a is None:
                sep = QAction(win)
                sep.setSeparator(True)
                a = sep
            if before is None:
                m.addAction(a)
            else:
                m.insertAction(before, a)

    # title bar: "(protected)" while the output will be encrypted
    base_title = win._update_title

    def update_title() -> None:
        base_title()
        doc = cv.doc
        if doc is not None and docprops.is_protected(doc):
            t = win.windowTitle()
            sep = " — "
            if sep in t:
                name, rest = t.split(sep, 1)
                win.setWindowTitle(f"{name} (protected){sep}{rest}")

    win._update_title = update_title
    win._output_changed = update_title
    cv.undo.indexChanged.connect(lambda _: update_title())

    def update_enabled() -> None:
        has = cv.doc is not None
        for a in acts:
            a.setEnabled(has)

    cv.documentChanged.connect(update_enabled)
    cv.documentChanged.connect(update_title)
    update_enabled()
