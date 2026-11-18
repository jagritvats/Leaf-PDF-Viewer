# Translating Leaf

Leaf is English-only today. The groundwork for translations is in place (`leaf/i18n.py`); this page is the
workflow for marking strings, producing translation files and testing layouts.

## What is loaded at startup

`MainWindow` calls `i18n.install()` once per process. For the user's UI languages (`QLocale.system()`) it loads:

1. **Qt's own strings** — `qtbase_<lang>.qm` from Qt's translations folder (standard dialog buttons, file dialogs,
   text-field context menus, …). PySide6 ships these in `PySide6/translations`.
2. **Leaf's strings** — `leaf/i18n/leaf_<lang>.qm`, when present.

English is the source language, so nothing is loaded for `en_*` locales.

> Packaged builds: `packaging/leaf.spec` currently excludes `PySide6/translations/` (and `PySide6/Qt/translations/`
> on Linux), so Qt's own strings stay English there. Bundle the `qtbase_*.qm` files (≈ 2 MB for all languages) and
> `leaf/i18n/*.qm` once real translations exist; `i18n.qt_translations_dirs()` also looks in
> `sys._MEIPASS/PySide6/translations` and `sys._MEIPASS/translations`.

## Marking strings

```python
from .i18n import tr

menu = mb.addMenu(tr("&File"))                    # context "" (what pyside6-lupdate records for a bare tr())
label = tr("Open", "verb, toolbar button")        # second argument: disambiguation comment for translators
title = self.tr("Keyboard shortcuts")             # inside a QObject: context = the class name
```

- Format after translating, with placeholders the translator can move: `tr("Page {n} of {total}").format(n=..., total=...)`.
  Don't build sentences from pieces.
- Plurals: `tr("%n page(s)", None, count)` and provide the plural forms in Linguist.
- Keep `&` mnemonics in the text; translators choose their own letter.
- **Actions need a stable `objectName`** (shortcut overrides are stored by it). Feature modules already set
  `a_<name>`; for a plain `menu.addAction(tr("..."))` set one explicitly, as `window.py` does for the Help menu —
  otherwise `shortcuts.ensure_names` derives it from the (translated) text.

The pilot: the main menu titles and the Help menu in `leaf/window.py`. The rest of the UI is not marked yet.

## Producing translations

From the project root (the tools come with PySide6):

```sh
# 1. extract strings into a .ts file per language (re-run after code changes; keeps existing translations)
uv run pyside6-lupdate leaf/*.py leaf/features/*.py -ts leaf/i18n/leaf_de.ts

# 2. translate in Qt Linguist (or any .ts editor)
uv run pyside6-linguist leaf/i18n/leaf_de.ts

# 3. compile to the binary .qm Leaf loads
uv run pyside6-lrelease leaf/i18n/leaf_de.ts -qm leaf/i18n/leaf_de.qm
```

Commit the `.ts` sources; the `.qm` files are build output (generate them before packaging).

To try a language without changing the system setting, start Leaf with `LANGUAGE=de` (Linux) or temporarily call
`i18n.install(QLocale("de_DE"), force=True)`.

## Pseudo-locale: finding clipped layouts

```sh
LEAF_PSEUDO=1 uv run leaf file.pdf   # strings that go through tr() (and Qt's own) become "[Ŝàvé ~~~]"
LEAF_PSEUDO=2 uv run leaf file.pdf   # also rewrites every label, button, menu and window title as it appears
```

Pseudo strings are accented and about 40 % longer (German and Finnish UIs typically run 30–40 % longer than
English), wrapped in brackets so truncation is obvious. Placeholders (`%1`, `{name}`), rich-text tags, entities,
`&` mnemonics and `\tShortcut` suffixes are kept intact. Mode 1 also shows which strings are *not* marked yet: they
stay plain English.

Things to check in pseudo mode: the status bar (page box, zoom), the style panel's form labels, the find/replace
bar, dialog button rows, the compare view's top bar and the welcome screen. Prefer layouts that grow (no
`setFixedWidth` on widgets holding text; `QSizePolicy.Preferred`/`Expanding`, word-wrapped labels).
