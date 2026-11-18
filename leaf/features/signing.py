"""Digital signatures: Document › Sign digitally…, the Signatures panel (View, F12), a banner over signed
documents, a warning before a save that would rewrite (and so invalidate) a signed file.

Signing works on the saved file: unsaved edits are saved first, then pyHanko appends the signature as an
incremental update (new file or in place) and the window reopens the signed file."""
from __future__ import annotations

import os

from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QDockWidget, QFileDialog, QMessageBox

from .. import signing as S
from ..icons import icon
from ..sigpanel import SignatureBanner, SignaturesPanel, SignDialog, TrustDialog, Verifier


def _action(win, name: str, text: str, slot, keys=(), ic: str | None = None, tip: str | None = None) -> QAction:
    a = QAction(text, win)
    a.setObjectName(f"a_{name}")
    if keys:
        a.setShortcuts([QKeySequence(k) for k in keys])
    if ic:
        a.setIcon(icon(ic))
        a.setProperty("icon_name", ic)
    a.setToolTip(tip or text.replace("&", "").rstrip("…"))
    a.triggered.connect(lambda _=False: slot())
    setattr(win, f"a_{name}", a)
    win.addAction(a)
    return a


def _insert_after(menu, after, actions) -> None:
    acts = menu.actions()
    if after in acts and acts.index(after) + 1 < len(acts):
        menu.insertActions(acts[acts.index(after) + 1], actions)
    else:
        menu.addActions(actions)


class Signing(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cv = win.canvas
        self.settings = win.settings
        self.pending = None  # (identity, request, in_place) while the user positions a new signature box
        self.statuses: list = []
        self.from_saved = False  # statuses describe the file as last saved (page numbers = current order)
        self._confirmed_rewrite = None  # the document for which "rewrite anyway" was answered
        self._baseline: dict = {}  # field -> state before a save, to notice a save that broke something

        self.verifier = Verifier(self)
        self.verifier.done.connect(self._verified)
        self._job = None  # (token, kind) kind: "open" | "saved"

        self.panel = SignaturesPanel(self.settings)
        self.panel.jump.connect(self._jump)
        self.panel.sign_requested.connect(self.sign)
        self.panel.trust_changed.connect(self.reverify)
        dock = self.dock = QDockWidget("Signatures", win)
        dock.setObjectName("signatures")
        dock.setWidget(self.panel)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        if getattr(win, "props_dock", None) is not None:
            win.tabifyDockWidget(win.props_dock, dock)
            win.props_dock.raise_()
        dock.hide()
        win.signatures_dock = dock

        self.banner = SignatureBanner()
        self.banner.show_panel.connect(self.show_panel)
        host = win.find.parentWidget().layout() if getattr(win, "find", None) is not None else None
        if host is not None:
            host.insertWidget(0, self.banner)

        self.a_sign = _action(win, "sign_digital", "Sign &digitally…", self.sign, (), "signature",
                              "Sign the document with your certificate (PAdES)")
        self.a_trust = _action(win, "trusted_certs", "&Trusted certificates…", self.manage_trust, (), None,
                               "Certificates whose signatures Leaf shows as trusted")
        toggle = self.a_panel = dock.toggleViewAction()
        toggle.setText("Si&gnatures panel")
        toggle.setObjectName("a_signatures_panel")
        toggle.setShortcut(QKeySequence("F12"))
        toggle.setIcon(icon("signature"))
        toggle.setProperty("icon_name", "signature")
        toggle.triggered.connect(lambda on: on and dock.raise_())
        win.a_signatures_panel = toggle

        menus = getattr(win, "menus", {})
        m = menus.get("document") or menus.get("file")
        if m is not None:
            m.addSeparator()
            m.addActions([self.a_sign, self.a_trust])
        if "view" in menus:
            after = getattr(win, "a_comments_panel", None) or getattr(win, "a_props", None)
            _insert_after(menus["view"], after, [toggle])

        win.sign_field_digitally = self.sign_field  # forms.py / fields.py context menus
        orig_save_to = win._save_to

        def save_to(path, pages=None):
            return self._save_to(orig_save_to, path, pages)

        win._save_to = save_to
        self.cv.documentChanged.connect(self._doc_changed)
        self._doc_changed()

    # ------------------------------------------------------------- state
    def _doc_changed(self) -> None:
        doc = self.cv.doc
        self.pending = None
        self.statuses = []
        self._baseline = {}
        self.a_sign.setEnabled(doc is not None)
        if doc is None or not S.has_signatures(doc.primary.doc):
            self.panel.set_none(doc is not None)
            self.banner.hide()
            self._job = None
            return
        self.banner.set_busy()
        self.panel.set_busy()
        self._verify(doc.primary.data, doc.primary.password, "open")

    def _verify(self, data: bytes, password, kind: str) -> None:
        trusted = S.trusted_certificates(self.settings)
        system = S.use_system_trust(self.settings)
        self._job = (self.verifier.start(data, trusted, system, password), kind)

    def _verified(self, token, result) -> None:
        if self._job is None or self._job[0] != token or self.cv.doc is None:
            return
        kind = self._job[1]
        self._job = None
        if isinstance(result, Exception):
            self.panel.set_error(str(result) or type(result).__name__)
            self.banner.hide()
            return
        before = {s.field: s.state for s in self.statuses}
        self.statuses = result
        self.from_saved = kind == "saved"
        self.panel.set_statuses(result, "as saved" if self.from_saved else "")
        self.banner.set_status(result)
        if not result:
            self.panel.set_none(True)
        if kind == "saved":
            worse = [s for s in result if s.state in ("modified", "invalid") and before.get(s.field) in
                     ("valid", "untrusted")]
            if worse:
                n = len(worse)
                self.win.toast(f"Saved, but {n} signature{'s' if n > 1 else ''} no longer valid: "
                               + worse[0].changes_text().split(":")[-1].strip(), "Details", self.show_panel, ms=8000)

    def reverify(self) -> None:
        doc = self.cv.doc
        if doc is None or not S.has_signatures(doc.primary.doc):
            return
        self.panel.set_busy()
        if self.from_saved and doc.path and os.path.exists(doc.path):
            with open(doc.path, "rb") as f:
                self._verify(f.read(), doc.output_password(), "saved")
        else:
            self._verify(doc.primary.data, doc.primary.password, "open")

    def show_panel(self) -> None:
        self.dock.show()
        self.dock.raise_()

    def manage_trust(self) -> None:
        if TrustDialog(self.settings, self.win).exec():
            self.reverify()

    def _jump(self, page: int, rect) -> None:
        doc = self.cv.doc
        if doc is None:
            return
        i = page if self.from_saved else self.cv.page_index_for(doc.primary, page)
        if i is None or not 0 <= i < len(self.cv.order):
            self.win.toast("That page is no longer in the document.")
            return
        pt = QPointF(rect[0], rect[1]) if rect else None
        self.cv.go_to_point(i, pt)

    # -------------------------------------------------------------- save
    def _save_to(self, orig, path, pages=None) -> bool:
        doc = self.cv.doc
        signed = doc is not None and S.has_signatures(doc.primary.doc)
        if signed and pages is None and self._confirmed_rewrite is not doc \
                and not doc.is_identity(list(doc.pages)):
            n = S.signature_count(doc)
            box = QMessageBox(QMessageBox.Icon.Warning, "Save",
                              f"This document has {n} digital signature{'s' if n > 1 else ''}. Your page changes "
                              "(order, rotation, inserted or removed pages) make Leaf rewrite the whole file, which "
                              f"invalidates {'them' if n > 1 else 'it'}.", parent=self.win)
            box.setInformativeText("Save a copy with Save As to keep the signed original.")
            go = box.addButton("Save anyway", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not go:
                return False
            self._confirmed_rewrite = doc
        ok = orig(path, pages)
        if ok and signed and pages is None and self.cv.doc is doc and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return ok
            self.panel.set_busy()
            self._verify(data, doc.output_password(), "saved")
        return ok

    # --------------------------------------------------------------- sign
    def sign_field(self, name: str) -> None:
        """Context menu of an empty signature field (existing or newly placed)."""
        if self.pending is not None:
            ident, req, in_place = self.pending
            self.pending = None
            self._sign(ident, req, name, in_place)
            return
        self.sign(preset=name)

    def sign(self, preset: str | None = None) -> None:
        cv, win = self.cv, self.win
        doc = cv.doc
        if doc is None:
            return
        cv._clear_focus()
        fields = []
        for f in S.signature_fields(doc.primary.doc):
            if not f.signed and f.name and cv.page_index_for(doc.primary, f.page) is not None:
                i = cv.page_index_for(doc.primary, f.page)
                fields.append((f.name, f"Field “{f.name}” (page {i + 1})"))
        try:
            from ..fields import authored_fields

            for it in authored_fields(cv):
                if it.ftype == "signature" and it.scene() is not None:
                    pg = it.parentItem().parentItem() if it.parentItem() else None
                    i = cv.order.index(pg) if pg in cv.order else None
                    fields.append((it.props["name"], f"New field “{it.props['name']}”"
                                   + (f" (page {i + 1})" if i is not None else "")))
        except Exception:  # noqa: BLE001  (forms authoring unavailable)
            pass
        name = os.path.splitext(os.path.basename(doc.path or "document.pdf"))[0]
        dlg = SignDialog(win, self.settings, fields, S.has_signatures(doc.primary.doc),
                         cv.current_page_item() is not None, preset, name)
        if not dlg.exec():
            return
        if dlg.placement == "new":
            self._place_box(dlg.identity, dlg.request, dlg.in_place)
            return
        self._sign(dlg.identity, dlg.request, None if dlg.placement == "invisible" else dlg.placement, dlg.in_place)

    def _place_box(self, ident, req, in_place) -> None:
        from .. import fields

        cv = self.cv
        item = fields.insert_field(cv, "signature", fields.default_props(cv, "signature"))
        if item is None:
            return
        self.pending = (ident, req, in_place)
        name = item.props["name"]
        self.win.toast("Drag the signature box into place (corners resize), then click Sign.", "Sign",
                       lambda: self._sign_pending(name), ms=120000)

    def _sign_pending(self, name: str) -> None:
        if self.pending is None:
            return
        ident, req, in_place = self.pending
        self.pending = None
        self._sign(ident, req, name, in_place)

    def _ensure_saved(self) -> bool:
        win, cv = self.win, self.cv
        doc = cv.doc
        needs_as = not doc.path or getattr(doc, "converted", False) or not os.path.exists(doc.path)
        if cv.undo.isClean() and not needs_as:
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "Sign digitally",
                          "Signing works on the saved file. Save your changes first?", parent=win)
        save = None if needs_as else box.addButton(QMessageBox.StandardButton.Save)
        save_as = box.addButton("Save As…", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save or save_as)
        box.exec()
        b = box.clickedButton()
        if save is not None and b is save:
            return win.save()
        if b is save_as:
            return win.save_as()
        return False

    def _sign(self, ident, req, field: str | None, in_place: bool) -> None:
        win, cv = self.win, self.cv
        doc = cv.doc
        if doc is None or not self._ensure_saved():
            return
        src = doc.path
        if in_place:
            dst = src
        else:
            base, _ = os.path.splitext(src)
            dst, _ = QFileDialog.getSaveFileName(win, "Save signed document", base + "-signed.pdf", "PDF (*.pdf)")
            if not dst:
                return
            if not dst.lower().endswith(".pdf"):
                dst += ".pdf"
        req.field = field
        req.rect = None
        req.password = doc.output_password()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            S.sign_pdf(src, dst, ident, req)
        except Exception as ex:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(win, "Sign digitally", f"Couldn't sign:\n{ex}\n\nNo file was changed.")
            return
        QApplication.restoreOverrideCursor()
        self.reopen(dst, req.password)
        win.toast(f"Signed by {ident.name}  ·  {os.path.basename(dst)}", "Signatures", self.show_panel, ms=5000)

    def reopen(self, path: str, password: str | None = None) -> None:
        """Show `path` (just signed) in this window in place of the current document."""
        from ..document import PdfDocument

        win = self.win
        path = os.path.abspath(path)
        try:
            doc = PdfDocument.open(path, password)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(win, "Sign digitally", f"Signed, but couldn't reopen {os.path.basename(path)}:\n{ex}")
            return
        win.canvas.set_document(doc)
        win._add_recent(path)
        win._update_enabled()
        win._update_title()
        win._sync_view()
        win._tool_changed(win.canvas.tool)


def install(win) -> None:
    win.signing = Signing(win)
