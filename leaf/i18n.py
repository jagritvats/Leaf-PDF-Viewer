"""Translations: Qt's own strings for the system language, Leaf's .qm files, `tr()`, and a pseudo-locale.

- `install()` (idempotent; MainWindow calls it first thing) loads Qt's base translations (qtbase_<lang>.qm: the
  standard dialog buttons, file dialogs, context menus of text fields, ...) for the user's UI languages from Qt's
  translations folder, then Leaf's own translations from `leaf/i18n/leaf_<lang>.qm` when there are any.
- `tr(text, disambiguation=None, n=-1)` translates in the empty context, which is exactly what `pyside6-lupdate`
  records for a bare `tr("...")` call (the second argument becomes the disambiguation comment). Use it for strings
  outside QObject methods; `self.tr()` works too (context = class name). See docs/translating.md.
- Pseudo-locale: `LEAF_PSEUDO=1` turns every translated string into an accented, ~40 % longer version in brackets
  ("[Ŝàvé ~~~]"), so untranslated strings stand out and clipped layouts show up without a real translation.
  `LEAF_PSEUDO=2` also rewrites the text of labels, buttons, check boxes, group boxes, menus and window titles as
  they appear, to preview the whole UI before its strings are marked for translation.
"""
from __future__ import annotations

import os
import re
import sys

from PySide6.QtCore import QCoreApplication, QEvent, QLibraryInfo, QLocale, QObject, QTranslator
from PySide6.QtWidgets import QAbstractButton, QApplication, QGroupBox, QLabel, QMenu, QWidget

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")
_ACCENTS = str.maketrans(
    "abcdeghiklmnorstuwyzABCDEGHIKLMNORSTUWYZ",
    "àƀçđéĝĥîķĺɱñöŕšţûŵýžÀßÇĐÉĜĤÎĶĹМÑÖŔŠŢÛŴÝŽ",
)
_KEEP = re.compile(r"(%\d+|%[sdn]|\{[^{}]*\}|<[^<>]*>|&[a-zA-Z]+;|\t.*$)")  # placeholders, tags, entities, "\tShortcut"
_state: dict = {"installed": False, "translators": [], "loaded": [], "filter": None}


def pseudo_mode() -> int:
    v = os.environ.get("LEAF_PSEUDO", "")
    return int(v) if v.isdigit() else 0


def pseudo(text: str) -> str:
    """Accent every letter, pad by ~40 % and bracket the string; keeps placeholders, tags, entities, shortcut
    suffixes and `&` mnemonics working. Idempotent."""
    head = text.split("	", 1)[0]
    if not text or head.startswith("[") and head.endswith("]") and "~" in head:
        return text
    parts = _KEEP.split(text)
    out = "".join(p if i % 2 else p.translate(_ACCENTS) for i, p in enumerate(parts))
    letters = sum(c.isalpha() for c in text)
    if not letters:
        return text
    suffix = ""
    m = re.search(r"\t.*$", out)
    if m:
        suffix, out = m.group(0), out[: m.start()]
    return f"[{out} {'~' * max(2, round(letters * 0.4))}]{suffix}"


class PseudoTranslator(QTranslator):
    """Answers every lookup with the pseudo-localized source text."""

    def translate(self, context, source, disambiguation=None, n=-1):  # noqa: D401, ARG002
        return pseudo(source) if source else source

    def isEmpty(self) -> bool:
        return False


def qt_translations_dirs() -> list[str]:
    """Where Qt's own .qm files may be: Qt's configured path, the PySide6 package, a PyInstaller bundle."""
    dirs = [QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)]
    try:
        import PySide6

        dirs.append(os.path.join(os.path.dirname(PySide6.__file__), "translations"))
    except Exception:  # noqa: BLE001
        pass
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs += [os.path.join(base, "PySide6", "translations"), os.path.join(base, "translations")]
    seen, out = set(), []
    for d in dirs:
        if d and os.path.isdir(d) and os.path.normcase(d) not in seen:
            seen.add(os.path.normcase(d))
            out.append(d)
    return out


def _load(app, locale: QLocale, name: str, dirs: list[str]) -> str | None:
    for d in dirs:
        t = QTranslator(app)
        if t.load(locale, name, "_", d):
            app.installTranslator(t)
            _state["translators"].append(t)
            _state["loaded"].append(t.filePath() or f"{d}/{name}")
            return t.filePath()
    return None


def install(locale: QLocale | None = None, app_dir: str | None = None, force: bool = False) -> list[str]:
    """Install the translators for `locale` (default: the user's UI languages); returns the .qm files loaded."""
    app = QCoreApplication.instance()
    if app is None or (_state["installed"] and not force):
        return list(_state["loaded"])
    if force:
        uninstall()
    _state["installed"] = True
    loc = locale or QLocale.system()
    # English is the source language: no lookup needed (and some Qt builds ship a near-empty qtbase_en)
    if not loc.name().startswith("en") or locale is not None:
        _load(app, loc, "qtbase", qt_translations_dirs())
        _load(app, loc, "leaf", [app_dir or APP_DIR])
    mode = pseudo_mode()
    if mode:
        t = PseudoTranslator(app)
        app.installTranslator(t)  # installed last = asked first
        _state["translators"].append(t)
        _state["loaded"].append("<pseudo>")
        if mode >= 2 and isinstance(app, QApplication) and _state["filter"] is None:
            _state["filter"] = _PseudoWidgets(app)
            app.installEventFilter(_state["filter"])
    return list(_state["loaded"])


def uninstall() -> None:
    app = QCoreApplication.instance()
    for t in _state["translators"]:
        if app is not None:
            app.removeTranslator(t)
    if _state["filter"] is not None and app is not None:
        app.removeEventFilter(_state["filter"])
    _state.update(installed=False, translators=[], loaded=[], filter=None)


def tr(text: str, disambiguation: str | None = None, n: int = -1) -> str:
    """Translate `text` (context "", like `pyside6-lupdate` records a bare tr() call)."""
    if QCoreApplication.instance() is None:
        return text
    return QCoreApplication.translate("", text, disambiguation, n)


class _PseudoWidgets(QObject):
    """LEAF_PSEUDO=2: pseudo-localize widget texts as they are polished (strings not yet marked with tr())."""

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.Polish and isinstance(obj, QWidget) and not obj.property("leafPseudo"):
            obj.setProperty("leafPseudo", True)
            pseudo_widget(obj)
        return False


def pseudo_widget(w: QWidget) -> None:
    if isinstance(w, (QLabel, QAbstractButton, QGroupBox)):
        t = w.title() if isinstance(w, QGroupBox) else w.text()
        if t:
            new = pseudo(t)
            (w.setTitle if isinstance(w, QGroupBox) else w.setText)(new)
    if isinstance(w, QMenu):
        w.setTitle(pseudo(w.title()))
        for a in w.actions():
            if a.text() and not a.property("leafPseudo"):
                a.setProperty("leafPseudo", True)
                a.setText(pseudo(a.text()))
    if w.isWindow() and w.windowTitle():
        w.setWindowTitle(pseudo(w.windowTitle()))
