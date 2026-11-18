"""OS integration (leaf/osint.py): recent documents, title bar colour, pen input, and a System settings page.
Taskbar progress / keep-awake are called from the long-running tasks themselves (osint.progress)."""
from __future__ import annotations

from .. import osint

_page_registered = False


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
                               "eraser palm touch")
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
    lay.addStretch()
    return w
