# Contributing to Leaf

Thanks for helping. Bug reports, fixes, translations and features are all welcome.

## Reporting a bug

Open an [issue](https://github.com/jagritvats/Leaf-PDF-Viewer/issues) with:

- Leaf version and OS (Windows 11, which Linux distro and desktop, Wayland or X11);
- what you did, what you expected, and what happened;
- the error text, if any. Run `leaf` (not `leaf-gui`) or `Leaf.exe` from a terminal to see it.

**Don't attach PDFs that contain anything private.** If a bug needs a specific file, try to reproduce it with a
dummy document first, or describe the file (producer app, fonts, forms, signatures, scanned or not).

## Development setup

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run leaf path/to/file.pdf                      # run from source, errors go to the terminal
QT_QPA_PLATFORM=offscreen uv run pytest -q        # full test suite, headless (a few minutes)
uv run pytest -q tests/test_pages.py              # one area
```

Packaged builds (PyInstaller, installers, AppImage, Flatpak, AUR) are described in
[packaging/PLATFORMS.md](packaging/PLATFORMS.md). Every packaged build can check itself with `--self-test`.

## How the code is organised

The README's [How it's built](README.md#how-its-built) section has the module map. The main ideas:

- **Sources are never modified.** Opened PDFs are kept as immutable bytes. Everything you change is recorded as an
  edit, such as the page list, form values, text edits, overlay items or redactions. Saving replays those edits onto a
  fresh copy and writes it atomically (temp file, verify, rename). New features should add edits, not mutate the open
  document.
- **Undo is mandatory.** Every user-visible change goes through an undo command, so it appears in the History panel.
- **Features are modules.** Each file in `leaf/features/` has an `install(win)` function that adds its menus, tools,
  shortcuts and command-palette entries. New modules must be imported explicitly in `leaf/features/__init__.py` and
  added to `MODULES`, because packaged builds only include what is imported statically.
- **Rendering runs in a worker process** (`leaf/render_worker.py`). Don't do slow MuPDF work on the UI thread; OCR and
  file-size reduction have their own workers.
- **Local only.** Leaf makes no network requests except ones the user explicitly asks for (such as downloading an OCR
  language). Keep it that way.

## Pull requests

- Keep each pull request to one change, and explain what it fixes or adds and how you tested it.
- Add or update tests in `tests/`. `tests/e2e_kit.py` has helpers for driving the real main window headless. The
  full suite must pass.
- Match the surrounding code style: type hints, lines up to about 120 characters, and comments that explain *why*.
- UI changes should work in light and dark themes, at 100% and 200% scaling, and by keyboard. Add a screenshot to
  the pull request.
- New dependencies need a good reason: Leaf ships as a self-contained app, and every package adds to the download.

## Translations

See [docs/translating.md](docs/translating.md).

## License

Leaf is licensed under the [GNU AGPL v3.0 or later](LICENSE). By submitting a contribution you agree that it is
licensed under the same terms.
