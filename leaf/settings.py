"""Settings dialog (Edit › Settings…, Ctrl+,): a searchable list of pages on the left, the page on the right.

Contract for feature modules::

    from leaf import settings
    settings.register_page("mykey", "My area", lambda win: MyPage(win), order=60)

The factory gets the MainWindow and returns a QWidget. Each page reads and writes `win.settings` itself, using the
keys its module already uses. Every built-in page applies its changes immediately (there is no OK/Cancel: the
dialog only has Close), so a registered page should do the same, or say on the page when a change takes effect.
Pages are built lazily, the first time they are shown or searched."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QSettings, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

KEY_ROLE = Qt.ItemDataRole.UserRole

# settings keys introduced by this module (everything else reuses the key its module already had)
THEME = "ui/theme"  # "system" | "light" | "dark"
RECENT_MAX = "ui/recent_max"
RESTORE_POSITION = "view/restore_position"
MEMORY_MB = "view/memory_mb"
NUDGE = "edit/nudge"
NUDGE_BIG = "edit/nudge_big"
CONFIRM_DELETE = "pages/confirm_delete"
RECOVERY_MINUTES = "recovery/interval"  # 0 = off
FULL_REWRITE = "save/full_rewrite"
BACKUP = "save/backup"

RECENT_DEFAULT = 12
MEMORY_DEFAULT = 384


@dataclass
class Page:
    key: str
    title: str
    factory: Callable
    order: int = 50
    keywords: str = ""


_PAGES: dict[str, Page] = {}


def register_page(key: str, title: str, factory: Callable, order: int = 50, keywords: str = "") -> None:
    """Add (or replace) a settings page. `factory(win) -> QWidget`; `keywords` help the search box find it."""
    _PAGES[key] = Page(key, title, factory, order, keywords)


def unregister_page(key: str) -> None:
    _PAGES.pop(key, None)


def pages() -> list[Page]:
    return sorted(_PAGES.values(), key=lambda p: (p.order, p.title.lower()))


# ------------------------------------------------------------------ values
def truthy(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


def int_value(settings, key: str, default: int) -> int:
    try:
        return int(float(settings.value(key, default)))
    except (TypeError, ValueError):
        return default


def float_value(settings, key: str, default: float) -> float:
    try:
        return float(settings.value(key, default))
    except (TypeError, ValueError):
        return default


def recent_max(settings) -> int:
    return max(0, min(50, int_value(settings, RECENT_MAX, RECENT_DEFAULT)))


def apply_theme(value: str) -> None:
    """system / light / dark for the whole app (Qt 6.8+ color scheme; ignored where the platform can't)."""
    hints = QGuiApplication.styleHints()
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(str(value), Qt.ColorScheme.Unknown)
    try:
        hints.setColorScheme(scheme)
    except AttributeError:  # older Qt
        pass


def apply_memory(settings) -> None:
    from . import canvas

    mb = max(64, min(16384, int_value(settings, MEMORY_MB, MEMORY_DEFAULT)))
    canvas.IMAGE_BUDGET = mb * 2**20


def apply_nudge(win) -> None:
    cv = win.canvas
    cv.nudge_step = max(0.1, float_value(win.settings, NUDGE, 1.0))
    cv.nudge_big = max(0.1, float_value(win.settings, NUDGE_BIG, 10.0))


# ------------------------------------------------------------------ building blocks
def _secondary(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    # a role, not a copied color: the theme can switch while this dialog is open (Appearance › Theme), and a copy
    # kept the old theme's gray (light gray on the light theme)
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


def note(text: str) -> QLabel:
    """A short explanation under a setting, in the secondary text color."""
    lab = QLabel(text)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    return _secondary(lab)


class PageWidget(QWidget):
    """A settings page: a vertical list of groups. `group(title)` returns a QFormLayout to add rows to."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(14)

    def group(self, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.lay.addWidget(box)
        return form

    def finish(self) -> "PageWidget":
        self.lay.addStretch(1)
        return self


def _check(text: str, on: bool, slot, tip: str = "") -> QCheckBox:
    cb = QCheckBox(text)
    cb.setChecked(on)
    if tip:
        cb.setToolTip(tip)
    cb.toggled.connect(slot)
    return cb


def _set_checked(action, on: bool) -> None:
    if action is not None and action.isChecked() != on:
        action.blockSignals(True)
        action.setChecked(on)
        action.blockSignals(False)


# ------------------------------------------------------------------ built-in pages
def general_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("You")
    name = QLineEdit()
    mgr = getattr(win.canvas, "annots", None)
    name.setText(str(getattr(mgr, "author", "") or s.value("annotations/author", "") or ""))
    name.setPlaceholderText("Your name")
    name.setAccessibleName("Author name")

    def set_name() -> None:
        n = name.text().strip()
        if not n:
            return
        if mgr is not None and hasattr(mgr, "set_author"):
            mgr.set_author(n)
        s.setValue("annotations/author", n)

    name.editingFinished.connect(set_name)
    page.author = name
    f.addRow("Author name:", name)
    f.addRow(note("Shown on the notes and comments you add, and suggested for new signing certificates."))

    f = page.group("Units")
    units = QComboBox()
    for key, label in (("mm", "Millimeters (mm)"), ("in", "Inches (in)"), ("pt", "Points (pt)")):
        units.addItem(label, key)
    cur = str(s.value("crop/unit", "mm"))
    units.setCurrentIndex(max(0, units.findData(cur)))
    units.currentIndexChanged.connect(lambda _: s.setValue("crop/unit", units.currentData()))
    page.units = units
    f.addRow("Measurements:", units)
    f.addRow(note("Used for page margins when cropping."))

    f = page.group("Opening files")
    restore = _check("Reopen each file where you left off", truthy(s.value(RESTORE_POSITION), True),
                     lambda on: s.setValue(RESTORE_POSITION, on), "Page, zoom and fit mode are remembered per file")
    page.restore = restore
    f.addRow(restore)
    rec = QSpinBox()
    rec.setRange(0, 50)
    rec.setValue(recent_max(s))
    rec.setSuffix(" files")
    rec.setAccessibleName("Number of recent files")

    def set_recent(n: int) -> None:
        s.setValue(RECENT_MAX, n)
        from .welcome import recent_paths

        s.setValue("recent", recent_paths(s)[:n])

    rec.valueChanged.connect(set_recent)
    page.recent = rec
    clear = QPushButton("Clear recent files")
    clear.setAutoDefault(False)

    def do_clear() -> None:
        welcome = getattr(win, "welcome", None)
        if welcome is not None:
            welcome.clear_recent()
        else:
            s.setValue("recent", [])
        clear.setText("Cleared")
        clear.setEnabled(False)

    clear.clicked.connect(do_clear)
    row = QHBoxLayout()
    row.addWidget(rec)
    row.addWidget(clear)
    row.addStretch()
    f.addRow("Remember:", row)
    return page.finish()


THEMES = (("system", "Same as the system"), ("light", "Light"), ("dark", "Dark"))


def appearance_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("Theme")
    theme = QComboBox()
    for key, label in THEMES:
        theme.addItem(label, key)
    theme.setCurrentIndex(max(0, theme.findData(str(s.value(THEME, "system")))))

    def set_theme(_=0) -> None:
        v = theme.currentData()
        s.setValue(THEME, v)
        apply_theme(v)

    theme.currentIndexChanged.connect(set_theme)
    page.theme = theme
    f.addRow("Theme:", theme)

    viewing = getattr(win, "viewing", None)
    night_act = getattr(win, "a_night_mode", None)

    def set_night(on: bool) -> None:
        if viewing is not None:
            viewing.set_night(on)
        else:
            s.setValue("view/night", on)
        _set_checked(night_act, on)

    night = _check("Night mode: show pages inverted (dark pages)", truthy(s.value("view/night"), False), set_night,
                   "Also in View › Night mode (Ctrl+Shift+N)")
    page.night = night
    f.addRow(night)

    f = page.group("Pages panel and toolbar")
    from .sidebar import SIZES

    lo, hi, default = SIZES[False]
    thumbs = QSpinBox()
    thumbs.setRange(lo, hi)
    thumbs.setSingleStep(8)
    thumbs.setSuffix(" px")
    sb = getattr(win, "sidebar", None)
    thumbs.setValue(sb.thumb_w if sb is not None else int_value(s, "pages/thumb_list", default))
    thumbs.setAccessibleName("Thumbnail width")

    def set_thumbs(w: int) -> None:
        if sb is not None:
            sb.set_thumb_width(w)
        else:
            s.setValue("pages/thumb_list", w)

    thumbs.valueChanged.connect(set_thumbs)
    page.thumbs = thumbs
    f.addRow("Thumbnail width:", thumbs)
    f.addRow(note("Ctrl+wheel over the Pages panel changes it too."))
    labels = _check("Show text labels under toolbar buttons", truthy(s.value("ui/toolbar_labels"), False),
                    lambda on: win._set_toolbar_labels(on) if hasattr(win, "_set_toolbar_labels")
                    else s.setValue("ui/toolbar_labels", on))
    page.labels = labels
    f.addRow(labels)
    return page.finish()


def editing_page(win) -> QWidget:
    s = win.settings
    cv = win.canvas
    page = PageWidget()
    f = page.group("New text boxes")
    from .props import FontBox

    text = cv.defaults.setdefault("text", {})
    font = FontBox()
    font.set_key(str(text.get("font") or "Helvetica"))
    font.setAccessibleName("Default font")

    def set_font(key: str) -> None:
        cv.defaults["text"]["font"] = key
        _save_defaults(win)

    font.picked.connect(set_font)
    page.font = font
    f.addRow("Font:", font)
    size = QDoubleSpinBox()
    size.setRange(4, 144)
    size.setDecimals(1)
    size.setSuffix(" pt")
    size.setValue(float(text.get("size") or 12))
    size.setAccessibleName("Default font size")

    def set_size(v: float) -> None:
        cv.defaults["text"]["size"] = int(v) if float(v).is_integer() else v
        _save_defaults(win)

    size.valueChanged.connect(set_size)
    page.size = size
    f.addRow("Size:", size)
    f.addRow(note("The Style panel changes these too: whatever you pick there with nothing selected becomes the "
                  "default."))

    f = page.group("Moving with the arrow keys")
    step = QDoubleSpinBox()
    step.setRange(0.1, 72)
    step.setDecimals(1)
    step.setSuffix(" pt")
    step.setValue(float_value(s, NUDGE, 1.0))
    big = QDoubleSpinBox()
    big.setRange(0.1, 288)
    big.setDecimals(1)
    big.setSuffix(" pt")
    big.setValue(float_value(s, NUDGE_BIG, 10.0))
    for w, key in ((step, NUDGE), (big, NUDGE_BIG)):
        w.valueChanged.connect(lambda v, k=key: (s.setValue(k, v), apply_nudge(win)))
    page.step, page.big = step, big
    f.addRow("Arrow keys move by:", step)
    f.addRow("With Shift:", big)

    f = page.group("Pages")
    confirm = _check("Ask before deleting pages", truthy(s.value(CONFIRM_DELETE), False),
                     lambda on: s.setValue(CONFIRM_DELETE, on), "Deleting can always be undone (Ctrl+Z)")
    page.confirm = confirm
    f.addRow(confirm)
    return page.finish()


def _save_defaults(win) -> None:
    if hasattr(win, "_save_defaults"):
        win._save_defaults()
    else:
        for kind, d in win.canvas.defaults.items():
            win.settings.setValue(f"defaults/{kind}", d)


RECOVERY_CHOICES = ((0, "Off"), (1, "Every minute"), (2, "Every 2 minutes"), (5, "Every 5 minutes"),
                    (10, "Every 10 minutes"))


def saving_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("Crash recovery")
    rec = QComboBox()
    for m, label in RECOVERY_CHOICES:
        rec.addItem(label, m)
    rec.setCurrentIndex(max(0, rec.findData(int_value(s, RECOVERY_MINUTES, 1))))

    def set_recovery(_=0) -> None:
        s.setValue(RECOVERY_MINUTES, rec.currentData())
        from .window import _windows

        for w in list(_windows) or [win]:
            r = getattr(w, "recovery", None)
            if r is not None and hasattr(r, "set_interval"):
                r.set_interval(rec.currentData())

    rec.currentIndexChanged.connect(set_recovery)
    page.recovery = rec
    f.addRow("Save a recovery copy:", rec)
    f.addRow(note("While a document has unsaved changes, a copy is kept in Leaf's data folder (also shortly after "
                  "you stop editing). After a crash Leaf offers to reopen it. Deleted when you save or close."))

    f = page.group("Saving")
    full = _check("Always rewrite the whole file", truthy(s.value(FULL_REWRITE), False),
                  lambda on: (s.setValue(FULL_REWRITE, on), _apply_rewrite(win)))
    page.full = full
    f.addRow(full)
    f.addRow(note("Off: when only annotations or form values changed, Leaf appends the changes and keeps the original "
                  "bytes, so existing digital signatures stay valid. On: every save writes a fresh, compact file "
                  "(this invalidates existing signatures)."))
    bak = _check("Keep a backup of the original (file.pdf.bak)", truthy(s.value(BACKUP), False),
                 lambda on: s.setValue(BACKUP, on))
    page.backup = bak
    f.addRow(bak)
    f.addRow(note("The first time you save over a file in a session, the file as you opened it is kept next to it."))
    return page.finish()


def _apply_rewrite(win) -> None:
    from .window import _windows

    on = truthy(win.settings.value(FULL_REWRITE), False)
    for w in list(_windows) or [win]:
        doc = getattr(w.canvas, "doc", None)
        if doc is not None:
            doc.force_rewrite = on


def performance_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("Rendering")
    viewing = getattr(win, "viewing", None)
    act = getattr(win, "a_background_render", None)
    on = act.isChecked() if act is not None else truthy(s.value("view/worker"), True)

    def set_worker(v: bool) -> None:
        if viewing is None:
            s.setValue("view/worker", v)
            return
        _set_checked(act, v)
        viewing.set_worker(v)  # unchecks the menu action when the worker can't start
        if act is not None and act.isChecked() != v:
            worker.blockSignals(True)
            worker.setChecked(act.isChecked())
            worker.blockSignals(False)

    worker = _check("Render pages in a background process", on, set_worker)
    page.worker = worker
    f.addRow(worker)
    f.addRow(note("Keeps the window responsive on heavy pages. Turn it off if rendering looks wrong or your "
                  "security software blocks it."))
    mem = QSpinBox()
    mem.setRange(64, 16384)
    mem.setSingleStep(64)
    mem.setSuffix(" MB")
    mem.setValue(int_value(s, MEMORY_MB, MEMORY_DEFAULT))
    mem.setAccessibleName("Memory for page images")
    mem.valueChanged.connect(lambda v: (s.setValue(MEMORY_MB, v), apply_memory(s)))
    page.memory = mem
    f.addRow("Memory for page images:", mem)
    f.addRow(note("Rendered pages are kept up to this size; pages out of view are dropped first."))
    return page.finish()


def ocr_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("Text recognition (OCR)")
    try:
        from . import ocr as O

        engines = O.available_engines()
        choices = O.language_choices(engines)
    except Exception:  # noqa: BLE001 - the page still shows what it can
        engines, choices = [], []
    lang = QComboBox()
    lang.setEditable(True)
    lang.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    lang.setAccessibleName("OCR language")
    engine = QComboBox()
    engine.setAccessibleName("OCR engine")

    def fill() -> None:
        nonlocal engines, choices
        try:
            from . import ocr as O

            engines = O.available_engines()
            choices = O.language_choices(engines)
        except Exception:  # noqa: BLE001
            engines, choices = [], []
        for w in (lang, engine):
            w.blockSignals(True)
            w.clear()
        for code, label in choices:
            lang.addItem(f"{label} ({code})", code)
        cur = str(s.value("ocr/language", "eng") or "eng")
        i = lang.findData(cur)
        if i >= 0:
            lang.setCurrentIndex(i)
        else:
            lang.setEditText(cur)
        engine.addItem("Automatic (best for the language)", "auto")
        for e in engines:
            engine.addItem(e.label, e.key)
        engine.setCurrentIndex(max(0, engine.findData(str(s.value("ocr/engine", "auto") or "auto"))))
        for w in (lang, engine):
            w.blockSignals(False)

    def store(*_) -> None:
        i = lang.currentIndex()
        picked = i >= 0 and lang.currentText() == lang.itemText(i)
        code = lang.itemData(i) if picked else lang.currentText().strip()
        if code:
            s.setValue("ocr/language", code)
        s.setValue("ocr/engine", engine.currentData() or "auto")

    fill()
    lang.currentIndexChanged.connect(store)
    lang.lineEdit().editingFinished.connect(store)
    engine.currentIndexChanged.connect(store)
    page.language, page.engine = lang, engine
    f.addRow("Language:", lang)
    f.addRow("Engine:", engine)
    if not engines:
        f.addRow(note("No OCR engine is available in this build."))
    manage = QPushButton("Add or remove languages…")
    manage.setAutoDefault(False)

    def open_languages() -> None:
        from .features.ocr import LanguageDialog

        dlg = LanguageDialog(page.window())
        dlg.exec()
        fill()

    manage.clicked.connect(open_languages)
    row = QHBoxLayout()
    row.addWidget(manage)
    row.addStretch()
    f.addRow(row)
    f.addRow(note("Used as the starting choice in Document › Recognize text. Recognition runs on this computer."))
    return page.finish()


def signing_page(win) -> QWidget:
    s = win.settings
    page = PageWidget()
    f = page.group("Trust")
    count = QLabel()

    def refresh() -> None:
        try:
            from . import signing as S

            n = len(S.trusted_certificates(s))
        except Exception:  # noqa: BLE001
            n = 0
        count.setText("No certificates trusted yet." if not n else f"{n} trusted certificate{'s' if n != 1 else ''}.")

    refresh()
    manage = QPushButton("Trusted certificates…")
    manage.setAutoDefault(False)

    def open_trust() -> None:
        sig = getattr(win, "signing", None)
        if sig is not None and hasattr(sig, "manage_trust"):
            sig.manage_trust()
        else:
            from .sigpanel import TrustDialog

            TrustDialog(s, page.window()).exec()
        refresh()
        system.blockSignals(True)
        system.setChecked(truthy(s.value("signing/system_trust"), True))
        system.blockSignals(False)

    manage.clicked.connect(open_trust)
    row = QHBoxLayout()
    row.addWidget(count, 1)
    row.addWidget(manage)
    f.addRow(row)

    def set_system(on: bool) -> None:
        s.setValue("signing/system_trust", on)
        sig = getattr(win, "signing", None)
        if sig is not None and hasattr(sig, "reverify"):
            try:
                sig.reverify()
            except Exception:  # noqa: BLE001 - re-checking is a courtesy
                pass

    system = _check("Also trust the certificates your system trusts", truthy(s.value("signing/system_trust"), True),
                    set_system)
    page.system = system
    f.addRow(system)

    f = page.group("Timestamps")
    tsa = QLineEdit(str(s.value("signing/tsa_url", "") or ""))
    tsa.setPlaceholderText("https://… (empty: sign offline)")
    tsa.setAccessibleName("Timestamp server address")
    tsa.editingFinished.connect(lambda: s.setValue("signing/tsa_url", tsa.text().strip()))
    page.tsa = tsa
    f.addRow("Timestamp server:", tsa)
    f.addRow(note("With a server, signatures get a trusted time (PAdES B-T); Leaf contacts it only while signing."))
    return page.finish()


def privacy_page(win) -> QWidget:
    page = PageWidget()
    f = page.group("Your documents")
    f.addRow(note("Leaf works offline. Documents are opened, edited and saved on this computer and are never "
                  "uploaded. Leaf only goes online when you ask it to: downloading OCR language files, or contacting "
                  "the timestamp server you set while signing."))
    f = page.group("Recovery copies")
    from . import recovery

    folder = recovery.recovery_dir() or ""
    path = QLabel(folder or "Recovery is not available.")
    path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    path.setWordWrap(True)
    f.addRow("Folder:", path)
    status = note("")

    def count() -> int:
        try:
            return len([p for p in os.listdir(folder) if p.endswith(".pdf")]) if folder and os.path.isdir(folder) else 0
        except OSError:
            return 0

    def refresh() -> None:
        n = count()
        status.setText("No recovery copies." if not n else f"{n} recovery cop{'ies' if n != 1 else 'y'} "
                       "(copies of open windows with unsaved changes are kept).")
        clear.setEnabled(n > 0)

    open_btn = QPushButton("Open folder")
    open_btn.setAutoDefault(False)
    open_btn.setEnabled(bool(folder))
    open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
    clear = QPushButton("Delete recovery copies")
    clear.setAutoDefault(False)

    def do_clear() -> None:
        for info in recovery.orphans():  # only copies no running Leaf owns
            recovery.drop(info)
        refresh()

    clear.clicked.connect(do_clear)
    row = QHBoxLayout()
    row.addWidget(open_btn)
    row.addWidget(clear)
    row.addStretch()
    f.addRow(row)
    f.addRow(status)
    page.clear_recovery = clear
    refresh()
    return page.finish()


def keyboard_page(win) -> QWidget:
    page = PageWidget()
    f = page.group("Keyboard shortcuts")
    f.addRow(note("Every command can have its own shortcut. Single keys (V, T, I, …) pick a tool while the page has "
                  "focus."))
    edit = QPushButton("Customize shortcuts…")
    edit.setAutoDefault(False)

    def customize() -> None:
        from . import shortcuts

        shortcuts.open_preferences(win)

    edit.clicked.connect(customize)
    sheet = QPushButton("Show cheat sheet")
    sheet.setAutoDefault(False)

    def cheat() -> None:
        from . import shortcuts

        shortcuts.show_cheat_sheet(win)

    sheet.clicked.connect(cheat)
    row = QHBoxLayout()
    row.addWidget(edit)
    row.addWidget(sheet)
    row.addStretch()
    f.addRow(row)
    f = page.group("Tips")
    tips = QPushButton("Show the welcome tour and tips again")
    tips.setAutoDefault(False)

    def again() -> None:
        from . import onboarding

        onboarding.reset(win)
        tips.setEnabled(False)

    tips.clicked.connect(again)
    row = QHBoxLayout()
    row.addWidget(tips)
    row.addStretch()
    f.addRow(row)
    return page.finish()


register_page("general", "General", general_page, 10, "author name units mm inch points recent files reopen position")
register_page("appearance", "Appearance", appearance_page, 20, "theme dark light night mode thumbnails toolbar labels")
register_page("editing", "Editing", editing_page, 30, "font text box size nudge arrow keys snap delete pages confirm")
register_page("saving", "Saving", saving_page, 40, "crash recovery autosave incremental rewrite backup bak")
register_page("performance", "Performance", performance_page, 50, "render worker background process memory budget")
register_page("ocr", "Text recognition", ocr_page, 60, "ocr language engine tesseract rapidocr download")
register_page("signing", "Signing", signing_page, 70, "digital signatures trusted certificates timestamp server tsa")
register_page("privacy", "Privacy", privacy_page, 80, "offline recovery folder clear data network agent")
register_page("keyboard", "Keyboard", keyboard_page, 90, "shortcuts keys cheat sheet tips tour")


# ------------------------------------------------------------------ dialog
class SettingsDialog(QDialog):
    """Searchable settings. Changes apply immediately; Close (or Esc) just closes."""

    def __init__(self, win, start: str | None = None):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Settings")
        self.setObjectName("leafSettings")
        self.setMinimumSize(QSize(560, 380))
        self.resize(780, 560)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Search settings")
        self.list = QListWidget()
        self.list.setAccessibleName("Settings pages")
        self.list.setMaximumWidth(220)
        self.list.setMinimumWidth(150)
        self.stack = QStackedWidget()
        self.title = QLabel()
        tf = QFont(self.title.font())
        tf.setPointSizeF(tf.pointSizeF() * 1.35)
        tf.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(tf)
        self.empty = note("No settings match your search.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self.search)
        left.addWidget(self.list, 1)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.title)
        right.addWidget(self.stack, 1)
        right.addWidget(self.empty, 1)
        body = QHBoxLayout()
        body.setSpacing(14)
        body.addLayout(left)
        body.addLayout(right, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        hint = note("Changes apply right away.")
        foot = QHBoxLayout()
        foot.addWidget(hint, 1)
        foot.addWidget(bb)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addLayout(foot)

        self.entries: dict[str, Page] = {}
        self.widgets: dict[str, QWidget] = {}
        self._holders: dict[str, QScrollArea] = {}
        for p in pages():
            self.entries[p.key] = p
            it = QListWidgetItem(p.title)
            it.setData(KEY_ROLE, p.key)
            self.list.addItem(it)
            holder = QScrollArea()
            holder.setWidgetResizable(True)
            holder.setFrameShape(QFrame.Shape.NoFrame)
            self._holders[p.key] = holder
            self.stack.addWidget(holder)
        self.list.currentItemChanged.connect(self._current_changed)
        self.search.textChanged.connect(self.filter)
        self.show_page(start if start in self.entries else (self.list.item(0).data(KEY_ROLE) if self.list.count() else ""))
        self.search.setFocus()

    # ---------------------------------------------------------------- pages
    def page_widget(self, key: str) -> QWidget | None:
        """The page's widget, built on first use (None if the key is unknown or its factory failed)."""
        if key in self.widgets:
            return self.widgets[key]
        p = self.entries.get(key)
        if p is None:
            return None
        try:
            w = p.factory(self.win)
        except Exception as ex:  # noqa: BLE001 - one broken page must not take the dialog down
            w = note(f"This page couldn't be shown: {ex}")
        if w is None:
            w = QWidget()
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(2, 0, 10, 0)
        il.addWidget(w)
        self._holders[key].setWidget(inner)
        self.widgets[key] = w
        return w

    def show_page(self, key: str) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(KEY_ROLE) == key:
                if it.isHidden():
                    self.search.clear()
                self.list.setCurrentItem(it)
                return

    def current_key(self) -> str:
        it = self.list.currentItem()
        return it.data(KEY_ROLE) if it is not None else ""

    def _current_changed(self, it, _prev=None) -> None:
        if it is None:
            return
        key = it.data(KEY_ROLE)
        self.page_widget(key)
        self.stack.setCurrentWidget(self._holders[key])
        self.title.setText(self.entries[key].title)

    # --------------------------------------------------------------- search
    def _haystack(self, key: str) -> str:
        p = self.entries[key]
        w = self.page_widget(key)
        parts = [p.title, p.keywords]
        if w is not None:
            for c in [w, *w.findChildren(QWidget)]:
                if isinstance(c, (QLabel, QAbstractButton)):
                    parts.append(c.text())
                elif isinstance(c, QGroupBox):
                    parts.append(c.title())
                elif isinstance(c, QComboBox):
                    parts += [c.itemText(i) for i in range(min(c.count(), 40))]
        return " ".join(parts).replace("&", "").lower()

    def filter(self, text: str) -> list[str]:
        """Show only the pages whose title, keywords or controls match every word. Returns the matching keys."""
        words = text.strip().lower().split()
        shown = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            key = it.data(KEY_ROLE)
            ok = not words or all(w in self._haystack(key) for w in words)
            it.setHidden(not ok)
            if ok:
                shown.append(key)
        cur = self.list.currentItem()
        if shown and (cur is None or cur.isHidden()):
            self.show_page(shown[0])
        none = not shown
        self.empty.setVisible(none)
        self.stack.setVisible(not none)
        self.title.setVisible(not none)
        return shown


def open_settings(win, page: str | None = None) -> SettingsDialog:
    """Show the window's Settings dialog (one per window, non-modal), optionally on `page`."""
    dlg = getattr(win, "_settings_dialog", None)
    try:
        if dlg is not None:
            dlg.isVisible()  # raises if the C++ side is gone
    except RuntimeError:
        dlg = None
    if dlg is None:
        dlg = SettingsDialog(win, page)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.destroyed.connect(lambda *_: setattr(win, "_settings_dialog", None))
        win._settings_dialog = dlg
    elif page:
        dlg.show_page(page)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


def ask_delete_pages(win, n: int) -> bool:
    """Pages › Delete: True to go ahead (always, unless the user asked to confirm)."""
    s = getattr(win, "settings", None) or QSettings()
    if not truthy(s.value(CONFIRM_DELETE), False):
        return True
    r = QMessageBox.question(win, "Delete pages", f"Delete {n} page{'s' if n != 1 else ''}? You can undo this.",
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                             QMessageBox.StandardButton.Yes)
    return r == QMessageBox.StandardButton.Yes
