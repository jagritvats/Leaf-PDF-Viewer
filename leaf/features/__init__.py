"""Feature modules. Each exposes `install(win: MainWindow) -> None` and wires itself in
(actions into `win.menus[...]`, docks, signal connections) without editing window.py.

Imported statically (not by name) so packagers like PyInstaller see and bundle every module."""

from . import agentlink, annotations, compare, decorate, editset, focus, forms, history, images, ocr, optimize, osint, output, pages, redact, replace, settings, share, signing, structure, textfmt, viewing
from . import textedit_ui

# history last: it applies saved shortcut overrides to every action the others created
MODULES = [viewing, forms, pages, textfmt, annotations, output, decorate, redact, signing, ocr, optimize, textedit_ui, images, structure, replace, editset, agentlink, osint, settings, compare, focus, share, history]
NAMES = [m.__name__.rsplit(".", 1)[1] for m in MODULES]


def install_all(win) -> None:
    win.installed_features = []
    for mod in MODULES:
        mod.install(win)
        win.installed_features.append(mod.__name__.rsplit(".", 1)[1])
