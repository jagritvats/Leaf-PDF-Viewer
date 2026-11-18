"""OS integration (leaf/osint.py): recent documents, title bar colour, pen input, the jump list's "New window"
task (leaf/shellint.py), and a System settings page. Taskbar progress / keep-awake are called from the long-running
tasks themselves (osint.progress)."""
from __future__ import annotations

import os

from .. import osint

_page_registered = False
_jump_list_done = False


def install(win) -> None:
    # recent documents: every path the window adds to its own recent list also goes to the OS list
    own = win._add_recent

    def add_recent(path: str) -> None:
        own(path)
        osint.add_recent_document(path)

    win._add_recent = add_recent
    win.title_bar_follower = osint.TitleBarFollower(win)
    win.pen_input = osint.PenInput(win.canvas)
    wid = id(win)
    win.destroyed.connect(lambda *_: osint.forget_window(wid))
    _register_settings_page()
    _jump_list()


def _jump_list() -> None:
    """Once per process, a moment after start: the taskbar jump list gets a "New window" task (installed and
    portable Windows builds; not the MSIX, whose package identity launches differently, nor source runs)."""
    global _jump_list_done
    from .. import shellint

    if _jump_list_done or not shellint.IS_WIN or not osint._native():
        return
    _jump_list_done = True
    exe = shellint.frozen_exe()
    if exe is None or shellint.in_msix():
        return
    from PySide6.QtCore import QTimer

    QTimer.singleShot(2000, lambda: shellint.set_jump_list(shellint.jump_list_tasks(exe)))


def _register_settings_page() -> None:
    global _page_registered
    if _page_registered:
        return
    try:
        from ..settings import register_page
    except Exception:  # noqa: BLE001 - the settings dialog isn't there (yet)
        return
    try:
        register_page("system", "System", settings_page, order=75,
                      keywords="recent documents jump list taskbar progress sleep awake pen pressure tablet stylus "
                               "eraser palm touch default app pdf open with associate")
        _page_registered = True
    except Exception:  # noqa: BLE001
        pass


TOGGLES = [
    (osint.KEY_RECENT, "Add opened and saved files to the system's recent documents",
     "Windows: Explorer's Recent files and Start; with the installed app also Leaf's taskbar jump list."),
    (osint.KEY_TASKBAR, "Show progress of long tasks on the taskbar icon",
     "Recognize text, Reduce file size, Export, Print, Replace all and Search & redact."),
    (osint.KEY_AWAKE, "Keep the computer awake during long tasks",
     "The system doesn't go to sleep while text recognition, Reduce file size or an export runs."),
    (osint.KEY_PRESSURE, "Pen pressure changes the width of freehand strokes",
     "Harder strokes are wider. Saved as an ink annotation with the varying width in its appearance; "
     "other apps that redraw it use the average width."),
]


def settings_page(win):
    """System page of the settings dialog. Each checkbox is written to the settings immediately."""
    from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

    w = QWidget()
    w.setObjectName("settings_system")
    lay = QVBoxLayout(w)
    for key, text, tip in TOGGLES:
        cb = QCheckBox(text)
        cb.setObjectName("system_" + key.split("/", 1)[1])
        cb.setToolTip(tip)
        cb.setAccessibleDescription(tip)
        cb.setChecked(osint.setting(key))
        cb.toggled.connect(lambda on, k=key: win.settings.setValue(k, bool(on)))
        lay.addWidget(cb)
    note = QLabel("Pen: the eraser end erases freehand strokes, the barrel button pans, and touches are ignored "
                  "while the pen is near the screen.")
    note.setWordWrap(True)
    note.setEnabled(False)
    lay.addWidget(note)
    _default_app_row(win, lay)
    lay.addStretch()
    return w


def _default_app_row(win, lay) -> None:
    """Default PDF app: the button runs win.a_make_default (features/share.py); a portable copy that registered
    itself can unregister here."""
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

    from .. import shellint

    act = getattr(win, "a_make_default", None)
    if act is None:
        return
    lay.addSpacing(8)
    lay.addWidget(QLabel("<b>Default PDF app</b>"))
    row = QHBoxLayout()
    b = QPushButton("Make Leaf the default PDF app…")
    b.setObjectName("system_make_default")
    b.setToolTip(act.toolTip())
    b.clicked.connect(act.trigger)
    row.addWidget(b)
    exe = shellint.frozen_exe()
    mine = shellint.registered_exe() if shellint.IS_WIN and exe else None
    if mine and os.path.normcase(mine) == os.path.normcase(exe) and not shellint.in_msix() \
            and not shellint.installed_by_installer():
        u = QPushButton("Remove from Windows' app lists…")
        u.setObjectName("system_unregister")

        def unregister():
            from .share import unregister_portable

            if unregister_portable(win):
                u.hide()

        u.clicked.connect(unregister)
        row.addWidget(u)
    row.addStretch()
    lay.addLayout(row)
