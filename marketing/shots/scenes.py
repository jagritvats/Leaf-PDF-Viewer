"""Store screenshots of the real Leaf app, scripted through its actions, tools and panels.

    python marketing/shots/scenes.py --theme dark --size 2560x1600 [--only 1,3] [--out DIR]
    python marketing/shots/shoot_all.py          (every scene, both themes, both sizes; one app at a time)

Runs on the real platform plugin (not offscreen) with its own settings organization ("LeafShots"), so the user's
Leaf settings, recent files, signatures and trusted certificates are untouched. Each size is a fixed logical window
(1600x1000 or 1600x900) rendered at a fixed device pixel ratio (1.6 / 1.2), captured with QWidget.grab(): window
contents only, no desktop. Dialogs and popups that sit on top of the window (Search & Redact, the command palette)
are drawn onto the window capture at the position they have on screen.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
import time

if os.environ.get("TZ") != "UTC0":  # neutral time zone in signature dates and comment times (not the machine's)
    import subprocess

    os.environ["TZ"] = "UTC0"
    sys.exit(subprocess.call([sys.executable, os.path.abspath(__file__)] + sys.argv[1:]))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
WORK = os.path.join(HERE, "work")
SIZES = {"2560x1600": (1.6, 1600, 1000), "1920x1080": (1.2, 1600, 900)}

ap = argparse.ArgumentParser()
ap.add_argument("--theme", choices=["dark", "light"], default="dark")
ap.add_argument("--size", choices=list(SIZES), default="2560x1600")
ap.add_argument("--only", default="")
ap.add_argument("--out", default=os.path.join(ROOT, "marketing", "out", "screens"))
ap.add_argument("--onscreen", action="store_true", help="keep the window on screen (default: just off screen)")
ARGS = ap.parse_args()
DPR, LW, LH = SIZES[ARGS.size]

# real Windows platform, fixed scale: set before Qt starts
os.environ.pop("QT_QPA_PLATFORM", None)
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCALE_FACTOR"] = str(DPR)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QMouseEvent, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox, QWidget,
)

QCoreApplication.setOrganizationName("LeafShots")
QCoreApplication.setApplicationName("Leaf")
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication(sys.argv)
app.setApplicationDisplayName("Leaf")
app.styleHints().setColorScheme(Qt.ColorScheme.Dark if ARGS.theme == "dark" else Qt.ColorScheme.Light)

from leaf import recovery  # noqa: E402
from leaf.icons import app_icon  # noqa: E402

recovery.OVERRIDE_DIR = os.path.join(WORK, "recovery")
app.setWindowIcon(app_icon())

L = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier
LOG: list[str] = []
FAILED: list[int] = []


def log(*a) -> None:
    s = " ".join(str(x) for x in a)
    LOG.append(s)
    print(s, flush=True)


# ------------------------------------------------------------------ event helpers
def pump(secs: float = 0.2) -> None:
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def wait_until(cond, secs: float = 20.0, msg: str = "condition") -> bool:
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except RuntimeError:
            pass
        time.sleep(0.01)
    log(f"  !! timed out waiting for {msg}")
    return False


def send_mouse(target, kind, pos, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton, mods=NOMOD):
    lp = QPointF(pos)
    ev = QMouseEvent(kind, lp, lp, QPointF(target.mapToGlobal(pos)), button, buttons, mods)
    QApplication.sendEvent(target, ev)


def move(target, pos, held=False):
    send_mouse(target, QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton, L if held else Qt.MouseButton.NoButton)


def click(target, pos, mods=NOMOD):
    move(target, pos)
    send_mouse(target, QEvent.Type.MouseButtonPress, pos, L, L, mods)
    send_mouse(target, QEvent.Type.MouseButtonRelease, pos, L, Qt.MouseButton.NoButton, mods)
    pump(0.05)


def drag(target, a: QPoint, b: QPoint, steps: int = 10, mods=NOMOD):
    move(target, a)
    send_mouse(target, QEvent.Type.MouseButtonPress, a, L, L, mods)
    for k in range(1, steps + 1):
        p = QPoint(round(a.x() + (b.x() - a.x()) * k / steps), round(a.y() + (b.y() - a.y()) * k / steps))
        move(target, p, held=True)
        pump(0.01)
    send_mouse(target, QEvent.Type.MouseButtonRelease, b, L, Qt.MouseButton.NoButton, mods)
    pump(0.05)


def key(widget, k, mods=NOMOD):
    QTest.keyClick(widget, k, mods)
    if mods != NOMOD:
        QTest.keyRelease(widget, Qt.Key.Key_Control, NOMOD)  # QTest leaves the modifier state set
    pump(0.03)


def shortcut(win, seq: str):
    combo = QKeySequence(seq)[0]
    target = QApplication.focusWidget()
    if target is None or target.window() is not win:
        target = win
    key(target, combo.key(), combo.keyboardModifiers())
    pump(0.05)


def type_text(widget, text: str):
    QTest.keyClicks(widget, text)
    pump(0.03)


# ------------------------------------------------------------------ modal dialogs never block
class Popups:
    """Modal dialogs, message boxes and file dialogs run a queued handler instead of a nested event loop."""

    def __init__(self):
        self.queue: list[tuple[str, object]] = []
        me = self

        def dlg_exec(dlg, *a, **k):
            return me._run("dialog", dlg, f"dialog {type(dlg).__name__} {dlg.windowTitle()!r}", QDialog.DialogCode.Rejected)

        for cls in (QDialog, QMessageBox, QFileDialog, QInputDialog):
            cls.exec = dlg_exec
        for name in ("question", "information", "warning", "critical"):
            setattr(QMessageBox, name, staticmethod(
                lambda *a, _n=name, **k: me._run("msg", (_n, a, k), f"QMessageBox.{_n} {a[1:3]}",
                                                 QMessageBox.StandardButton.Yes if _n == "question" else QMessageBox.StandardButton.Ok)))
        for name in ("getSaveFileName", "getOpenFileName", "getOpenFileNames", "getExistingDirectory"):
            setattr(QFileDialog, name, staticmethod(
                lambda *a, _n=name, **k: me._run("file", (_n, a, k), f"QFileDialog.{_n}", ("", ""))))

    def on(self, kind: str, handler) -> None:
        self.queue.append((kind, handler))

    def _run(self, kind, obj, desc, default):
        i = next((i for i, (k, _) in enumerate(self.queue) if k == kind), -1)
        if i >= 0:
            _, h = self.queue.pop(i)
            log(f"  popup handled: {desc}")
            return h(obj)
        log(f"  popup (default answer): {desc}")
        if isinstance(obj, QDialog):
            obj.reject()
        return default


POP = Popups()


# ------------------------------------------------------------------ window + capture
def new_window():
    from leaf import window as W

    win = W.MainWindow()
    win.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)  # lets the window be exactly LWxLH logical
    win.resize(LW, LH)
    scr = app.primaryScreen().geometry()
    x = 0 if ARGS.onscreen else scr.right() + 40  # off screen: the real mouse can't hover it
    screens = app.screens()
    if not ARGS.onscreen and len(screens) > 1:
        # monitors with different scales: park the window just outside the highest-DPI screen's free edge, so Qt
        # and Windows agree on the screen (else text is measured for one scale and drawn for another)
        target = max(screens, key=lambda sc: sc.devicePixelRatio())
        g = target.geometry()
        if g.left() <= min(sc.geometry().left() for sc in screens):
            x = g.left() - LW - 200
        elif g.right() >= max(sc.geometry().right() for sc in screens):
            x = g.right() + 40
        win.setScreen(target)
    win.move(x, 0)
    win.show()
    win.raise_()
    win.activateWindow()
    pump(0.3)
    if win.size().width() != LW or win.size().height() != LH:
        log(f"  !! window is {win.size().width()}x{win.size().height()}, wanted {LW}x{LH}")
    return win


def close_window(win) -> None:
    try:
        tool = getattr(win, "textedit_tool", None)
        if tool is not None and tool.editing():
            tool.cancel() if hasattr(tool, "cancel") else None
        if win.canvas.doc is not None:
            win.canvas.undo.setClean()
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                w.hide()
        win.close()
    except RuntimeError:
        pass
    pump(0.3)


def open_doc(win, name: str, copy_as: str | None = None) -> str:
    src = os.path.join(WORK, "docs", name)
    run = os.path.join(WORK, "run")
    os.makedirs(run, exist_ok=True)
    dst = os.path.join(run, copy_as or name)
    shutil.copyfile(src, dst)
    win.open_path(dst)
    wait_until(lambda: win.canvas.doc is not None and win.canvas.order, msg="document open")
    pump(0.4)
    win.canvas.setFocus()
    return dst


def page_point(win, page: int, x: float, y: float) -> QPoint:
    cv = win.canvas
    pg = cv.order[page]
    return cv.mapFromScene(pg.content.mapToScene(QPointF(x, y)))


def words(win, page: int):
    ref = win.canvas.order[page].ref
    return ref.src.words(ref.index, ref.uid)


def phrase_rect(win, page: int, phrase: str):
    """(x0, y0, x1, y1) of the first occurrence of `phrase` (consecutive words) on display page `page`."""
    ws = [w for w in words(win, page)]
    want = phrase.split()
    for i in range(len(ws) - len(want) + 1):
        if all(ws[i + k][4].strip(".,;:()") == want[k].strip(".,;:()") for k in range(len(want))):
            grp = ws[i:i + len(want)]
            return (min(w[0] for w in grp), min(w[1] for w in grp), max(w[2] for w in grp), max(w[3] for w in grp))
    raise RuntimeError(f"phrase {phrase!r} not on page {page}")


def visible_pages(win):
    cv = win.canvas
    vr = cv.mapToScene(cv.viewport().rect()).boundingRect()
    out = []
    for pg in cv.order:
        r = pg.mapRectToScene(pg.page_rect()) & vr
        if r.width() * r.height() * cv.transform().m11() ** 2 > 16:  # more than a sliver on screen
            out.append(pg)
    return out


def pages_ready(win) -> bool:
    for pg in visible_pages(win):
        if pg.img is None or pg._wanted() is not None:
            return False
    return True


def thumbs_ready(sb) -> bool:
    if not sb.isVisible():
        return True
    vp = sb.viewport().rect()
    for i in range(sb.count()):
        it = sb.item(i)
        if sb.visualItemRect(it).intersects(vp):
            uid = it.data(Qt.ItemDataRole.UserRole + 1) if False else sb.uids()[i]
            if uid not in sb._thumbs:
                return False
    return True


def settle(win, extra: float = 0.4) -> None:
    """Wait until every visible page and thumbnail is rendered at its final resolution. The window sits off screen
    (not exposed), so paints are driven by rendering it into a pixmap, like the final capture does."""
    pump(0.3)
    sbs = [win.sidebar] + ([win.organizer] if getattr(win, "organizer", None) is not None else [])

    def ready() -> bool:
        return (not win.canvas.isVisible() or pages_ready(win)) and all(thumbs_ready(s) for s in sbs)

    for _ in range(3):
        end = time.time() + 30
        while time.time() < end:
            win.grab()
            pump(0.15)
            if ready():
                break
        else:
            log("  !! rendering not finished:", [(pg.ref.index, pg.img is not None, pg._wanted(), pg.job)
                                                 for pg in visible_pages(win)], "thumbs:", [thumbs_ready(s) for s in sbs])
        pump(0.2)
    pump(extra)


def dismiss_toasts(win) -> None:
    from leaf.toast import Toast

    for t in win.findChildren(Toast):
        if t.isVisible():
            t.hide()


def drop_shadow(p: QPainter, r: QRectF, radius: float = 8.0) -> None:
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(14, 0, -1):
        c = QColor(0, 0, 0, int(5 + (14 - i) * 1.6))
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(-i, -i + 6, i, i + 6), radius + i, radius + i)
        p.setBrush(c)
        p.drawPath(path)
    p.restore()


def capture(win, overlays=()) -> QImage:
    """The window's contents, with top-level dialogs/popups that sit over it painted where they are on screen."""
    pm = win.grab()
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    origin = win.mapToGlobal(QPoint(0, 0))
    for w in overlays:
        g = w.grab()
        at = w.mapToGlobal(QPoint(0, 0)) - origin
        r = QRectF(at.x(), at.y(), w.width(), w.height())
        drop_shadow(p, r, 8)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        p.drawPixmap(r.topLeft(), g)
        p.restore()
        p.setPen(QPen(QColor(0, 0, 0, 70) if ARGS.theme == "light" else QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
    p.end()
    return pm.toImage()


def save_shot(img: QImage, num: int, name: str) -> str:
    d = os.path.join(ARGS.out, ARGS.theme)
    os.makedirs(d, exist_ok=True)
    w, h = img.width(), img.height()
    path = os.path.join(d, f"{num:02d}-{name}-{w}x{h}.png")
    want = ARGS.size
    if f"{w}x{h}" != want:
        log(f"  !! captured {w}x{h}, wanted {want}")
    img.save(path, "PNG")
    log(f"  saved {path}")
    return path


def hide_docks(win, keep=()) -> None:
    from PySide6.QtWidgets import QDockWidget

    for d in win.findChildren(QDockWidget):
        if d not in keep:
            d.hide()


def fresh_settings() -> None:
    s = QSettings()
    s.clear()
    s.setValue("textedit/paragraph", False)
    s.setValue("ui/theme", ARGS.theme)  # Leaf's own Appearance setting (else it follows the OS scheme)
    s.setValue("onboarding/tour_done", True)  # a returning user: no first-run tour card
    s.sync()
    from leaf.signature import library_dir

    for fn in os.listdir(library_dir()):
        try:
            os.remove(os.path.join(library_dir(), fn))
        except OSError:
            pass


# ================================================================== scenes
SCENES = {}


def scene(num: int, name: str):
    def deco(fn):
        SCENES[num] = (name, fn)
        return fn
    return deco


AUTHOR = "Jordan Blake"


@scene(1, "hero-annotate")
def s_hero(win):
    from leaf.items import TextItem

    cv, vp = win.canvas, win.canvas.viewport()
    cv.annots.set_author(AUTHOR)
    open_doc(win, "canopy-report.pdf")
    win.side_dock.show()
    win.side_dock.raise_()  # the Pages tab in front of Outline
    fit(win, "width")
    pump(0.3)
    cv.go_to(0)
    pump(0.2)
    # two highlights with the Highlight tool (H + drag)
    key(cv, Qt.Key.Key_H)
    for phrase in ("18.4 percent, up 2.1 points since the 2021 baseline",):  # yellow
        r = phrase_rect(win, 0, phrase)
        my = (r[1] + r[3]) / 2
        drag(vp, page_point(win, 0, r[0] + 1, my), page_point(win, 0, r[2] - 1, my))
    before = set(map(id, cv.order[0].overlays()))
    r = phrase_rect(win, 0, "Riverside and Linden Hill now exceed 24 percent cover")
    my = (r[1] + r[3]) / 2
    cv.defaults["highlight"]["color"] = "#8ee08a"
    drag(vp, page_point(win, 0, r[0] + 1, my), page_point(win, 0, r[2] - 1, my))
    cv.defaults["highlight"]["color"] = "#ffd83d"
    hls = [i for i in cv.order[0].overlays() if type(i).__name__ == "HighlightItem"]
    hls = [i for i in hls if not type(i).__name__.startswith("Adopted")]
    # a sticky note (N + click), the comment typed into the note editor
    cv.annots.prompt = lambda item, title, author, when, text, can_delete: (
        "Nice result. Can we show the 2021-2026 change as a small chart on this page?", AUTHOR)
    key(cv, Qt.Key.Key_N)
    click(vp, page_point(win, 0, 548, 470))
    # a formatted text box (T + click, type, format)
    key(cv, Qt.Key.Key_T)
    click(vp, page_point(win, 0, 392, 40))
    ed = cv.scene().focusItem()
    if isinstance(ed, TextItem):
        type_text(cv, "DRAFT 2 · for board review")
        shortcut(win, "Ctrl+A")
        shortcut(win, "Ctrl+B")
        key(cv, Qt.Key.Key_Escape)
        pump(0.1)
    boxes = [i for i in cv.order[0].overlays() if isinstance(i, TextItem)]
    if boxes:
        box = boxes[0]
        cv.scene().clearSelection()
        box.setSelected(True)
        pump(0.1)
        # style it from the Style panel's model: colour, fill, border, size
        set_props(win, box, {"color": "#b3261e", "bg": "#fff4f2", "border": "#b3261e", "size": 11, "pad": 5.0})
    key(cv, Qt.Key.Key_Escape)
    key(cv, Qt.Key.Key_V)
    # comment on the first highlight + the Comments panel
    log("  new highlights:", [type(i).__name__ for i in hls])
    if hls:
        cv.annots.set_comment(hls[0], "Lead with this number in the press summary.", AUTHOR)
    win.a_comments_panel.trigger()
    pump(0.3)
    win.resizeDocks([win.comments_dock], [450], Qt.Orientation.Horizontal)
    panel = win.comments_dock.widget()
    wait_until(lambda: not cv.annots.busy() and panel.tree.topLevelItemCount() >= 3, secs=10, msg="comments listed")
    pump(0.3)
    # open the threads, select the note with two replies
    panel.tree.expandAll()
    target = None
    for i in range(panel.tree.topLevelItemCount()):
        pn = panel.tree.topLevelItem(i)
        for k in range(pn.childCount()):
            if pn.child(k).childCount() >= 2 and target is None:
                target = pn.child(k)
    if target is not None:
        panel.tree.setCurrentItem(target)
    cv.scene().clearSelection()
    cv.setFocus()
    fit(win, "width")
    pump(0.2)
    cv.verticalScrollBar().setValue(0)
    dismiss_toasts(win)
    settle(win, 0.6)
    return capture(win)


def field_named(win, name: str):
    for f in win.canvas._all_fields():
        if f.name == name:
            return f
    raise RuntimeError(f"no field {name!r}")


def field_point(win, f) -> QPoint:
    cv = win.canvas
    return cv.mapFromScene(f.mapToScene(f.r.center()))


def draw_signature(dlg, style: str) -> None:
    """Sign in the signature dialog's Draw tab with the mouse."""
    from sig import signature_strokes

    dlg.tabs.setCurrentIndex(0)
    pad = dlg.pad
    pad.resize(max(pad.width(), 560), max(pad.height(), 200))
    for stroke in signature_strokes(style):
        pts = [QPoint(round(x), round(y)) for x, y in stroke]
        send_mouse(pad, QEvent.Type.MouseButtonPress, pts[0], L, L)
        for q in pts[1:]:
            move(pad, q, held=True)
        send_mouse(pad, QEvent.Type.MouseButtonRelease, pts[-1], L, Qt.MouseButton.NoButton)
    pump(0.05)


def signature_png(style: str, path: str) -> str:
    """A handwritten signature image made with Leaf's own signature pad (for the digital-signature appearance)."""
    from leaf.signature import SignatureDialog

    dlg = SignatureDialog(None)
    draw_signature(dlg, style)
    img = dlg.pad.image()
    img.save(path)
    dlg.deleteLater()
    return path


@scene(2, "forms")
def s_forms(win):
    cv, vp = win.canvas, win.canvas.viewport()
    open_doc(win, "rental-application.pdf")
    dismiss_toasts(win)
    hide_docks(win)
    win.a_view_spread.trigger()
    win.a_view_cover_alone.setChecked(False)
    pump(0.3)
    fit(win, "page")
    settle(win, 0.2)
    texts = {
        "applicant_name": "Avery Lindqvist", "applicant_dob": "14 March 1994",
        "applicant_email": "avery.lindqvist@mail.example", "applicant_phone": "(555) 013-7720",
        "current_address": "18 Thistle Court, Port Alder", "current_landlord": "Bayview Lettings",
        "years_at_address": "3", "move_in": "1 February 2027", "occupants": "2",
        "employer": "Tidewater Design Co.", "position": "Senior UX Researcher", "income": "6,450",
        "employed_since": "June 2021", "emergency_name": "Maja Lindqvist", "emergency_relationship": "Sister",
        "signature_date": "18 November 2026",
    }
    for name, value in texts.items():
        f = field_named(win, name)
        click(vp, field_point(win, f))
        type_text(QApplication.focusWidget() or cv, value)
        key(QApplication.focusWidget() or cv, Qt.Key.Key_Tab)
    # multi-line notes
    f = field_named(win, "notes")
    click(vp, field_point(win, f))
    type_text(QApplication.focusWidget() or cv, "I work from home three days a week, so a quiet unit away from the lift "
                                                "would be ideal. One indoor cat.")
    # dropdowns: the value picked from the field's menu
    for name, value in (("unit_type", "Two bedroom with balcony"), ("lease_term", "12 months"), ("floor", "Upper floor")):
        field_named(win, name).commit(value)
    # checkboxes by mouse
    for name in ("pet_cat", "bike_storage", "updates", "agree_terms", "agree_credit"):
        click(vp, field_point(win, field_named(win, name)))
    # sign the signature field: click it, draw in the dialog, Save & place
    sigf = field_named(win, "applicant_signature")

    def sign(dlg):
        draw_signature(dlg, "avery")
        dlg.place_new.click()
        return dlg.result()

    POP.on("dialog", sign)
    click(vp, field_point(win, sigf))
    pump(0.3)
    # click on empty page area: no field focused, no selection
    cv.scene().clearSelection()
    click(vp, page_point(win, 1, 300, 800))
    cv.scene().clearSelection()
    # Forms > Highlight fields off: the filled form reads like paper (the signature field would otherwise still
    # show its "Sign here" placeholder under the placed signature)
    if win.a_field_highlight.isChecked():
        win.a_field_highlight.trigger()
    fit(win, "page")
    dismiss_toasts(win)
    settle(win, 0.5)
    return capture(win)


@scene(3, "edit-text")
def s_edit_text(win):
    from PySide6.QtGui import QTextCursor

    cv, vp = win.canvas, win.canvas.viewport()
    open_doc(win, "canopy-report.pdf")
    win.side_dock.show()
    win.side_dock.raise_()
    tool = win.textedit_tool
    P = 4  # Discussion page
    r = phrase_rect(win, P, "The citywide trend is encouraging")
    cv.go_to(P)
    pump(0.2)
    win.props_dock.hide()
    fit(win, "width")
    cv.go_to(P)
    pump(0.3)
    key(cv, Qt.Key.Key_I)
    w = phrase_rect(win, P, "encouraging")
    at = page_point(win, P, w[0] + 6, (w[1] + w[3]) / 2)
    move(vp, at)
    wait_until(lambda: tool.hover is not None and tool.hover.isVisible(), secs=5, msg="hover outline")
    click(vp, at)
    if not wait_until(tool.editing, secs=5, msg="inline editor"):
        from PySide6.QtWidgets import QLabel
        from leaf.toast import Toast

        log("  toasts:", [lb.text() for t in win.findChildren(Toast) for lb in t.findChildren(QLabel)],
            "tool:", cv.tool, "hover:", tool.hover is not None and tool.hover.isVisible())
        raise RuntimeError("the click didn't open the inline editor")
    ed = tool.editor
    txt = ed.plain_text()
    i = txt.find("encouraging")
    if i >= 0:
        c = ed.textCursor()
        c.setPosition(i)
        c.setPosition(i + len("encouraging"), QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(c)
        type_text(cv, "promising")
        c = ed.textCursor()  # the new word stays selected, so it reads as just typed
        c.setPosition(i)
        c.setPosition(i + len("promising"), QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(c)
    pump(0.3)
    # the hover outline on another line
    h = phrase_rect(win, P, "vulnerable. Structural soil cells")
    move(vp, page_point(win, P, h[0] + 60, (h[1] + h[3]) / 2))
    pump(0.4)
    dismiss_toasts(win)
    settle(win, 0.4)
    return capture(win)


@scene(4, "organize-pages")
def s_organize(win):
    open_doc(win, "canopy-report.pdf")
    run = os.path.join(WORK, "run")
    extra = []
    for name in ("services-agreement.pdf", "invoice.pdf"):
        shutil.copyfile(os.path.join(WORK, "docs", name), os.path.join(run, name))
        extra.append(os.path.join(run, name))
    win.insert_files(extra, len(win.canvas.doc.pages))
    pump(0.5)
    dismiss_toasts(win)
    shortcut(win, "Ctrl+Shift+O")
    pump(0.3)
    org = win._organizer
    org.view.set_thumb_width(340 if LH >= 1000 else 300)
    pump(0.3)
    uids = org.view.uids()
    org.view.set_current(2)
    org.view.select_uids([uids[2], uids[3], uids[6]])
    org.view.clearFocus()  # no keyboard focus rectangle on the current thumbnail
    pump(0.2)
    settle(win, 0.5)
    return capture(win)


@scene(5, "redact")
def s_redact(win):
    cv = win.canvas
    open_doc(win, "invoice.pdf")
    hide_docks(win)
    win.a_redactions_panel.trigger()
    pump(0.2)
    fit(win, "width")
    pump(0.2)
    win.a_search_redact.trigger()
    dlg = win._search_redact_dlg
    pump(0.2)

    def search(preset: str):
        dlg.preset.setCurrentIndex(dlg.preset.findText(preset))
        dlg.b_search.click()
        wait_until(lambda: dlg._job is None and dlg.tree.topLevelItemCount() > 0, secs=20, msg=f"{preset} hits")
        pump(0.2)

    search("Email addresses")
    dlg.b_mark.click()
    pump(0.3)
    win.a_search_redact.trigger()
    pump(0.2)
    search("Phone numbers")
    # review the hits: the invoice number isn't a phone number
    for k in range(dlg.tree.topLevelItemCount()):
        it = dlg.tree.topLevelItem(k)
        if "(" not in it.text(1):
            it.setCheckState(0, Qt.CheckState.Unchecked)
    dlg.tree.setCurrentItem(dlg.tree.topLevelItem(1))
    for c in range(3):
        dlg.tree.resizeColumnToContents(c)
    dlg.tree.setFocus()
    cv.verticalScrollBar().setValue(0)
    dismiss_toasts(win)
    # the dialog over the lower right of the page view
    dlg.resize(700, 470) if LH >= 1000 else dlg.resize(660, 452)
    gv = cv.mapToGlobal(QPoint(cv.width() - dlg.width() - 28, cv.height() - dlg.height() - 28))
    dlg.move(gv)
    pump(0.3)
    settle(win, 0.4)
    return capture(win, [dlg])


@scene(6, "digital-signature")
def s_digital(win):
    import pymupdf

    from leaf import signing as S

    run = os.path.join(WORK, "run")
    os.makedirs(run, exist_ok=True)
    src = os.path.join(run, "services-agreement.pdf")
    shutil.copyfile(os.path.join(WORK, "docs", "services-agreement.pdf"), src)
    signed1 = os.path.join(run, "services-agreement-signed-1.pdf")
    signed = os.path.join(run, "services-agreement-signed.pdf")
    people = [("Rowan Achebe", "Maple & Fern Studio", "rowan", "rowan@mapleandfern.example"),
              ("Elena Marsh", "Harbor Lane Apartments", "elena", "e.marsh@harborlane.example")]
    certs = []
    cur = src
    for k, (name, org, style, email) in enumerate(people):
        p12 = os.path.join(run, f"{style}.p12")
        S.create_identity(p12, "demo-pass", name, email=email, organization=org)
        ident = S.load_identity(p12, "demo-pass")
        certs.append(ident.cert)
        png = signature_png(style, os.path.join(run, f"{style}-sig.png"))
        with open(png, "rb") as f:
            img = f.read()
        # signature boxes above the two signature lines (see make_docs.make_agreement)
        d = pymupdf.open(cur)
        line = d[0].search_for(name)[-1]
        d.close()
        box = (line.x0 - 2, line.y0 - 86, line.x0 + 214, line.y0 - 12)
        req = S.SignRequest(page=0, rect=box, reason="Agreed and accepted" if k else "Issued for signature",
                            location="Port Alder", image=img)
        out = signed1 if k == 0 else signed
        S.sign_pdf(cur, out, ident, req)
        cur = out
    S.add_trusted(QSettings(), certs)
    win.open_path(signed)
    wait_until(lambda: win.canvas.doc is not None, msg="signed doc open")
    pump(0.3)
    hide_docks(win)
    panel = win.signing.panel
    win.a_signatures_panel.trigger()
    wait_until(lambda: panel.statuses and all(s.state == "valid" for s in panel.statuses), secs=60,
               msg="valid signatures")
    log("  signatures:", [(s.signer, s.state) for s in panel.statuses])
    fit(win, "width")
    pump(0.3)
    cv = win.canvas
    cv.verticalScrollBar().setValue(0)
    pump(0.1)
    cv.ensure_rect_visible(QRectF(60, 400, 470, 300), cv.order[0].content)  # clauses down to the signatures
    dismiss_toasts(win)
    settle(win, 0.6)
    return capture(win)


@scene(7, "ocr")
def s_ocr(win):
    from leaf import ocr as O

    cv, vp = win.canvas, win.canvas.viewport()
    open_doc(win, "scanned-notice.pdf")
    hide_docks(win)
    win.side_dock.show()
    win.side_dock.raise_()
    pump(0.8)
    dismiss_toasts(win)

    def ocr(dlg):
        k = dlg.engine.findText("Tesseract", Qt.MatchFlag.MatchContains)
        if k >= 0:
            dlg.engine.setCurrentIndex(k)
        log("  OCR engine:", dlg.engine.currentText(), "language:", dlg.language())
        dlg.ok.click()
        return dlg.result()

    POP.on("dialog", ocr)
    win.a_ocr.trigger()
    st = win._ocr
    wait_until(lambda: not st.running, secs=180, msg="OCR finished")
    pump(0.3)
    log("  recognized pages:", len(cv.doc.ocr or {}))
    # the recognized text is searchable right away: Ctrl+F finds words on the scan
    win.find.open_bar()
    win.find.edit.setText("water")
    wait_until(lambda: len(win.find.hits) >= 3, secs=10, msg="find hits")
    pump(0.3)
    # the whole letter: page y 50-650 pt fills the view height
    cv.zoom_to((cv.viewport().height() - 60) / 600)
    pump(0.3)
    # ... and selectable: drag across the first paragraph
    entry = next(iter(cv.doc.ocr.values()))
    ws = getattr(entry, "words", entry)
    a = O.search_words(ws, "hot")
    b = O.search_words(ws, "affected.")
    if a and b:
        ra, rb = a[0], b[-1]
        cv.verticalScrollBar().setValue(0)
        pump(0.1)
        cv.ensure_rect_visible(QRectF(60, 50, 470, 600), cv.order[0].content)
        pump(0.2)
        cv.setFocus()
        drag(vp, page_point(win, 0, ra.x0 + 1, (ra.y0 + ra.y1) / 2), page_point(win, 0, rb.x1 - 1, (rb.y0 + rb.y1) / 2))
        log("  selected:", cv.selected_text()[:80])
    dismiss_toasts(win)
    settle(win, 0.5)
    return capture(win)


@scene(8, "command-palette")
def s_palette(win):
    from leaf.palette import show_palette

    cv = win.canvas
    open_doc(win, "canopy-report.pdf")
    win.side_dock.show()
    win.side_dock.raise_()
    win.props_dock.hide()
    fit(win, "width")
    cv.go_to(2)
    pump(0.3)
    settle(win, 0.2)
    pal = show_palette(win)
    pump(0.2)
    type_text(pal.edit, "page")
    pump(0.3)
    if not pal.isVisible():
        log("  palette popup closed by the platform; re-showing it")
        pal.show()
        pump(0.1)
    settle(win, 0.3)
    return capture(win, [pal])


@scene(9, "watermark")
def s_watermark(win):
    cv = win.canvas
    open_doc(win, "canopy-report.pdf")
    hide_docks(win)
    win.a_view_spread.trigger()
    win.a_view_cover_alone.setChecked(False)
    cv.go_to(0)
    fit(win, "page")

    def hf(d):
        d.slot_edits[4].setText("Page {page} of {pages}")
        d.slot_edits[5].setText("")
        d.changed()
        pump(0.2)
        d.bb.button(d.bb.StandardButton.Ok).click()
        return d.result()

    POP.on("dialog", hf)
    win.a_header_footer.trigger()
    pump(0.5)
    dismiss_toasts(win)
    shown = {}

    def wm(d):
        d.text.setText("DRAFT")
        d.changed()
        pump(0.3)
        d.adjustSize()
        g = cv.mapToGlobal(QPoint(cv.width() - d.width() - 24, (cv.height() - d.height()) // 2))
        d.move(g)
        d.show()
        settle(win, 0.6)
        shown["img"] = capture(win, [d])
        d.hide()
        return QDialog.DialogCode.Rejected

    POP.on("dialog", wm)
    win.a_watermark.trigger()
    return shown.get("img")


@scene(10, "night-mode")
def s_night(win):
    cv = win.canvas
    open_doc(win, "canopy-report.pdf")
    hide_docks(win)
    win.a_view_spread.trigger()
    win.a_view_cover_alone.setChecked(False)
    pump(0.2)
    cv.go_to(4)
    fit(win, "page")
    pump(0.2)
    win.a_night_mode.trigger()
    pump(0.3)
    dismiss_toasts(win)
    settle(win, 0.5)
    return capture(win)


@scene(11, "start-screen")
def s_start(win):
    """The start screen of a returning user: recent files as page covers (copies in the work folder, dated like a
    normal week, so no real path or file shows)."""
    run = os.path.join(WORK, "run")
    os.makedirs(run, exist_ok=True)
    rec = []
    now = time.time()
    for name, age in (("canopy-report.pdf", 240), ("services-agreement.pdf", 3 * 3600), ("rental-application.pdf", 26 * 3600),
                      ("invoice.pdf", 3 * 86400), ("scanned-notice.pdf", 9 * 86400)):
        src = os.path.join(WORK, "docs", name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(run, name)
        shutil.copyfile(src, dst)
        os.utime(dst, (now - age, now - age))
        rec.append(dst)
    win.settings.setValue("recent", rec)
    win.show_view("welcome")
    win.welcome.refresh()
    wait_until(lambda: not win.welcome._queue, msg="covers")
    for _ in range(8):
        win.grab()
        pump(0.15)
    return capture(win)


@scene(12, "focus-mode")
def s_focus_mode(win):
    """View › Focus mode: just the page, a floating toolbar on top and the page/zoom bar below."""
    cv = win.canvas
    open_doc(win, "canopy-report.pdf")
    dismiss_toasts(win)
    win.a_focus_mode.trigger()
    pump(0.3)
    fit(win, "width")
    cv.go_to(1)
    pump(0.3)
    settle(win, 0.5)
    return capture(win)


def fit(win, mode: str = "width") -> None:
    """Fit width/page after the panels have settled (layout changes arrive a few event-loop turns later)."""
    cv = win.canvas
    for _ in range(2):
        pump(0.25)
        (win.a_fit_w if mode == "width" else win.a_fit_p).trigger()
    pump(0.2)
    log(f"  fit {mode}: zoom {cv.zoom:.2f}, viewport {cv.viewport().width()}x{cv.viewport().height()}, "
        f"hscroll {cv.horizontalScrollBar().value()}/{cv.horizontalScrollBar().maximum()}")


def set_props(win, item, values: dict) -> None:
    """Apply style values the way the Style panel does (one undoable change per call)."""
    props = win.props
    if hasattr(props, "changed"):
        props.changed.emit(values)
    else:
        win._props_changed(values)
    pump(0.1)


def run(nums: list[int]) -> None:
    for n in nums:
        fresh_settings()  # every scene starts from default view settings (layout, panels, night mode, ...)
        name, fn = SCENES[n]
        log(f"scene {n:02d} {name} [{ARGS.theme} {ARGS.size}]")
        win = new_window()
        try:
            img = fn(win)
            if img is not None:
                save_shot(img, n, name)
            else:
                FAILED.append(n)
        except Exception as ex:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            log(f"  !! scene {n} failed: {ex}")
            FAILED.append(n)
        finally:
            close_window(win)


if __name__ == "__main__":
    nums = [int(x) for x in ARGS.only.split(",") if x.strip()] if ARGS.only else sorted(SCENES)
    run(nums)
    from leaf import pageops

    pageops._clip.clear()
    QGuiApplication.clipboard().clear()
    import gc

    gc.collect()
    if FAILED:
        print("failed scenes:", FAILED, flush=True)
    os._exit(1 if FAILED else 0)
