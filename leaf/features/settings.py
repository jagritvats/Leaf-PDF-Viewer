"""Settings feature: Edit › Settings… (Ctrl+,), the preferences that live outside any one feature (theme, memory,
nudge step, full rewrite, backups), Help › Show tips again, the first-run tour, one-time tips, empty-state hints for
the panels, and dialogs that always fit on the screen."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QDialog, QDockWidget

from .. import onboarding
from .. import settings as S


def _action(win, name: str, text: str, slot, keys=(), tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    base = tip or text.replace("&", "").rstrip("…")
    a.setProperty("base_tip", base)
    sc = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
    a.setToolTip(base + (f"  ({sc})" if sc else ""))
    a.triggered.connect(lambda _=False: slot())
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


# ------------------------------------------------------------------ dialogs fit on the screen
class _DialogFitter(QObject):
    """App-wide: a dialog never opens larger than the screen it's on (125%/150% scaling on small laptops) and never
    smaller than its layout needs (nothing clipped)."""

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Show and isinstance(obj, QDialog) and obj.isWindow():
            try:
                _fit(obj)
            except RuntimeError:
                pass
        return False


def _fit(dlg: QDialog) -> None:
    lay = dlg.layout()
    need = lay.totalMinimumSize() if lay is not None else dlg.minimumSizeHint()
    w, h = max(dlg.width(), need.width()), max(dlg.height(), need.height())
    screen = dlg.screen() or QApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        w, h = min(w, avail.width() - 24), min(h, avail.height() - 48)
        w, h = max(w, min(need.width(), avail.width())), max(h, min(need.height(), avail.height()))
    if (w, h) != (dlg.width(), dlg.height()):
        dlg.resize(w, h)


def _install_fitter() -> None:
    app = QApplication.instance()
    if app is None or getattr(app, "_leaf_fitter", None) is not None:
        return
    app._leaf_fitter = _DialogFitter(app)
    app.installEventFilter(app._leaf_fitter)


# ------------------------------------------------------------------ saving hooks
def _wrap_save(win) -> None:
    """Keep file.pdf.bak: the first save over the opened file in a session first writes the file as it was opened.
    (The opened bytes are immutable in memory, so this needs no change to the save pipeline.)"""
    inner = win._save_to

    def save_to(path: str, pages=None) -> bool:
        doc = win.canvas.doc
        if doc is not None and pages is None and S.truthy(win.settings.value(S.BACKUP), False):
            _backup(win, doc, path)
        return inner(path, pages)

    win._save_to = save_to


def _backup(win, doc, path: str) -> None:
    if getattr(doc, "_leaf_backup_done", False) or not doc.path or getattr(doc, "converted", False):
        return
    same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(doc.path))
    if not same or not os.path.exists(path):
        return
    try:
        data = doc.primary.data
        bak = path + ".bak"
        tmp = bak + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, bak)
        doc._leaf_backup_done = True
    except Exception as ex:  # noqa: BLE001 - the save itself must still happen
        win.toast(f"Couldn't keep a backup: {ex}", ms=5000)


def _doc_opened(win) -> None:
    doc = win.canvas.doc
    if doc is not None:
        doc.force_rewrite = S.truthy(win.settings.value(S.FULL_REWRITE), False)


# ------------------------------------------------------------------ empty states
def _empty_states(win) -> None:
    cv = win.canvas
    has_doc = lambda: cv.doc is not None  # noqa: E731
    hints = []

    def dock(name: str):
        d = win.findChild(QDockWidget, name)
        return d.widget() if d is not None else None

    comments = dock("comments")
    if comments is not None and hasattr(comments, "tree"):
        def note_tool() -> None:
            a = getattr(win, "a_tool_note", None)
            if a is not None:
                a.trigger()
            else:
                cv.set_tool("note")
            cv.setFocus()

        hints.append(onboarding.attach_empty(comments.tree, "No comments here yet. Add a sticky note or highlight "
                                             "some text; replies show up here too.", "Add a note", note_tool, has_doc))
    outline = dock("outline")
    if outline is not None and hasattr(outline, "tree"):
        hints.append(onboarding.attach_empty(
            outline.tree, "No bookmarks. Add one to jump back to the place you're viewing.", "Add bookmark",
            lambda: outline.add_here(edit=True), has_doc))
    att = dock("attachments")
    if att is not None and hasattr(att, "tree"):
        hints.append(onboarding.attach_empty(att.tree, "No attached files. Drop files here to attach them.",
                                             "Add files…", att.add_dialog, has_doc))
    sig = dock("signatures")
    if sig is not None and hasattr(sig, "tree"):
        def no_sigs() -> bool:
            return cv.doc is not None and not sig.summary.text().startswith("Checking")

        hints.append(onboarding.attach_empty(sig.tree, "This document has no digital signatures.", "Sign digitally…",
                                             lambda: sig.sign_requested.emit(), no_sigs))
    red = dock("redactions")
    if red is not None and hasattr(red, "list"):
        a = getattr(win, "a_search_redact", None)
        hints.append(onboarding.attach_empty(
            red.list, "Nothing marked for redaction. Use the Redact tool (X) to drag across text or draw a box.",
            "Search && redact…" if a is not None else None, (lambda: a.trigger()) if a is not None else None, has_doc))
    agents = dock("agent_suggestions")  # agentlink feature, when present
    if agents is not None and hasattr(agents, "tree"):
        has_page = "agents" in {p.key for p in S.pages()}
        hints.append(onboarding.attach_empty(
            agents.tree, "No suggestions. Edits proposed by a connected agent are listed here for you to review.",
            "Agent settings…" if has_page else None, (lambda: S.open_settings(win, "agents")) if has_page else None))
    onboarding.watch(win, hints)


# ------------------------------------------------------------------ install
def install(win) -> None:
    s = win.settings
    menus = getattr(win, "menus", {})
    _install_fitter()

    a = _action(win, "settings", "&Settings…", lambda: S.open_settings(win), ["Ctrl+,"], "Settings")
    a.setMenuRole(QAction.MenuRole.PreferencesRole)
    if "edit" in menus:
        menus["edit"].addSeparator()
        menus["edit"].addAction(a)
    tips = _action(win, "show_tips", "Show &tips again", lambda: onboarding.reset(win),
                   tip="Show the welcome tour and one-time tips again")
    if "help" in menus:
        m = menus["help"]
        about = next((x for x in m.actions() if "about" in x.text().lower()), None)
        if about is not None:
            m.insertAction(about, tips)
            m.insertSeparator(about)
        else:
            m.addAction(tips)

    # preferences that live outside any one feature
    S.apply_theme(str(s.value(S.THEME, "system")))
    S.apply_memory(s)
    S.apply_nudge(win)
    win.canvas.documentChanged.connect(lambda: _doc_opened(win))
    _doc_opened(win)
    if hasattr(win, "_save_to"):
        _wrap_save(win)

    # onboarding: the tour on the welcome screen (first launch), the "Why?" link on the first Edit text refusal
    onboarding.show_tour(win)
    tool = getattr(win, "textedit_tool", None)
    if tool is not None:
        onboarding.wrap_edit_text_toast(win, tool)
    _empty_states(win)
