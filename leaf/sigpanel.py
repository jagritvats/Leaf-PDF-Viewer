"""Digital signature UI: the Sign dialog, creating a self-signed identity, the trust list, the Signatures panel,
the banner shown over signed documents, and background verification. The logic lives in signing.py."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import signing as S
from .icons import icon

STATE_COLORS = {"valid": QColor(30, 130, 60), "untrusted": QColor(190, 120, 0), "modified": QColor(200, 50, 40),
                "invalid": QColor(200, 50, 40)}
STATE_WORDS = {"valid": "Valid", "untrusted": "Signer not trusted", "modified": "Modified after signing",
               "invalid": "Invalid"}


def _pw_edit() -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    e.setMinimumWidth(240)
    return e


def _warn_label() -> QLabel:
    lab = QLabel()
    lab.setWordWrap(True)
    lab.setStyleSheet("color: palette(link-visited);")
    lab.hide()
    return lab


def _fmt_time(dt) -> str:
    if dt is None:
        return "—"
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    except Exception:  # noqa: BLE001
        return str(dt)


def state_icon(state: str) -> QIcon:
    return icon("check" if state == "valid" else "info", STATE_COLORS.get(state))


def identities_dir() -> str:
    d = os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), "identities")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------ verification
class Verifier(QObject):
    """Runs signing.verify_pdf in a thread; `done(token, statuses_or_exception)` arrives on the GUI thread."""

    done = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = 0

    def start(self, data: bytes, trusted, system: bool, password: str | None = None) -> int:
        self._token += 1
        token = self._token

        def run() -> None:
            try:
                res = S.verify_pdf(data, trusted, system, password)
            except Exception as ex:  # noqa: BLE001
                res = ex
            try:
                self.done.emit(token, res)
            except RuntimeError:  # the window closed meanwhile
                pass

        threading.Thread(target=run, name="leaf-verify", daemon=True).start()
        return token

    def current(self, token) -> bool:
        return token == self._token


# ------------------------------------------------------------------ banner
class SignatureBanner(QFrame):
    """A strip over the page view: "Signed by X — valid" with a button to the Signatures panel."""

    show_panel = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("leafSigBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 6, 5)
        lay.setSpacing(8)
        self.icon = QLabel()
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton("Signatures…")
        self.button.setFlat(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.show_panel)
        close = QToolButton()
        close.setIcon(icon("close"))
        close.setAutoRaise(True)
        close.setToolTip("Hide")
        close.clicked.connect(self.hide)
        lay.addWidget(self.icon)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.button)
        lay.addWidget(close)
        self.state = "valid"
        self.hide()

    def set_status(self, statuses: list) -> None:
        if not statuses:
            self.hide()
            return
        worst = max(statuses, key=lambda s: ["valid", "untrusted", "modified", "invalid"].index(s.state))
        self.state = worst.state
        n = len(statuses)
        text = worst.headline() if n == 1 else (
            f"{n} signatures — " + ("all valid" if worst.state == "valid" else
                                    f"{sum(1 for s in statuses if s.state != 'valid')} with problems: "
                                    + worst.headline()))
        self.label.setText(text)
        self.icon.setPixmap(state_icon(worst.state).pixmap(QSize(18, 18)))
        self._restyle()
        self.show()

    def set_busy(self) -> None:
        self.label.setText("Checking digital signatures…")
        self.icon.setPixmap(icon("signature").pixmap(QSize(18, 18)))
        self.state = "busy"
        self._restyle()
        self.show()

    def _restyle(self) -> None:
        pal = self.palette()
        base = pal.color(QPalette.ColorRole.Window)
        tint = STATE_COLORS.get(self.state, pal.color(QPalette.ColorRole.Highlight))
        a = 0.16
        bg = QColor(round(base.red() * (1 - a) + tint.red() * a), round(base.green() * (1 - a) + tint.green() * a),
                    round(base.blue() * (1 - a) + tint.blue() * a))
        ss = (f"#leafSigBanner {{ background: {bg.name()}; border-bottom: 1px solid "
              f"{pal.color(QPalette.ColorRole.Mid).name()}; }}")
        if ss != self.styleSheet():
            self._styling = True
            try:
                self.setStyleSheet(ss)
            finally:
                self._styling = False

    def changeEvent(self, e) -> None:
        from PySide6.QtCore import QEvent

        super().changeEvent(e)
        if e.type() == QEvent.Type.PaletteChange and not getattr(self, "_styling", False):
            self._restyle()


# ------------------------------------------------------------------- panel
class SignaturesPanel(QWidget):
    """Lists the document's signatures with their status; clicking one scrolls to its field."""

    jump = Signal(int, object)  # page number in the file, rect (page.rect space) or None
    sign_requested = Signal()
    trust_changed = Signal()
    reverify = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.statuses: list[S.SigStatus] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self.summary = QLabel("No document")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setIndentation(14)
        self.tree.setWordWrap(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideNone)  # wrap long signer/status lines instead of "..."
        self.tree.itemClicked.connect(self._clicked)
        self.tree.currentItemChanged.connect(lambda *_: self._sync_buttons())
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.trust_btn = QPushButton("Trust signer")
        self.trust_btn.setToolTip("Add the selected signer's certificate to Leaf's trusted certificates")
        self.trust_btn.clicked.connect(self._trust_selected)
        self.details_btn = QPushButton("Details…")
        self.details_btn.clicked.connect(self._details)
        row.addWidget(self.trust_btn)
        row.addWidget(self.details_btn)
        row.addStretch()
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.sign_btn = QPushButton(icon("signature"), "Sign digitally…")
        self.sign_btn.clicked.connect(self.sign_requested)
        manage = QPushButton("Trusted certificates…")
        manage.clicked.connect(self._manage_trust)
        row2.addWidget(self.sign_btn)
        row2.addWidget(manage)
        row2.addStretch()
        lay.addLayout(row2)
        self._sync_buttons()

    # state ---------------------------------------------------------------
    def set_busy(self) -> None:
        self.tree.clear()
        self.statuses = []
        self.summary.setText("Checking signatures…")
        self._sync_buttons()

    def set_none(self, has_doc: bool) -> None:
        self.tree.clear()
        self.statuses = []
        self.summary.setText("This document has no digital signatures." if has_doc else "No document")
        self.sign_btn.setEnabled(has_doc)
        self._sync_buttons()

    def set_error(self, text: str) -> None:
        self.tree.clear()
        self.statuses = []
        self.summary.setText(f"Couldn't check the signatures: {text}")
        self._sync_buttons()

    def set_statuses(self, statuses: list, note: str = "") -> None:
        self.statuses = list(statuses)
        self.tree.clear()
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        for i, st in enumerate(self.statuses):
            top = QTreeWidgetItem([st.signer or "Unknown signer"])
            top.setIcon(0, state_icon(st.state))
            top.setFont(0, bold)
            top.setData(0, Qt.ItemDataRole.UserRole, i)
            top.setToolTip(0, st.headline())
            rows = [STATE_WORDS[st.state] + (" (certifying signature)" if st.certify else "")]
            rows.append("Signed: " + _fmt_time(st.timestamp if st.timestamp_ok else st.signing_time)
                        + ("  (timestamp)" if st.timestamp_ok else "  (signer's clock)" if st.signing_time else ""))
            if st.reason:
                rows.append("Reason: " + st.reason)
            if st.location:
                rows.append("Location: " + st.location)
            if st.contact:
                rows.append("Contact: " + st.contact)
            if st.error:
                rows.append(st.error)
            else:
                rows.append("Integrity: " + ("intact" if st.intact and st.valid else
                                             "signed content was changed" if not st.intact else "signature is invalid"))
                rows.append("Changes: " + st.changes_text())
                rows.append("Trust: " + st.trust_text())
                if st.timestamp is not None:
                    rows.append("Timestamp: " + _fmt_time(st.timestamp) + ("" if st.timestamp_ok else " (not valid)"))
                if st.certify:
                    rows.append("Certified: " + S.CERTIFY_LEVELS.get(st.certify, str(st.certify)))
            where = f"Field “{st.field}”" + (f", page {st.page + 1}" if st.page is not None else "")
            rows.append(where + ("" if st.rect else " (invisible)"))
            for t in rows:
                ch = QTreeWidgetItem([t])
                ch.setData(0, Qt.ItemDataRole.UserRole, i)
                ch.setToolTip(0, t)
                top.addChild(ch)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        n = len(self.statuses)
        ok = sum(1 for s in self.statuses if s.state == "valid")
        text = f"{n} signature{'s' if n != 1 else ''}" + (f", {ok} valid" if n else "")
        self.summary.setText(text + (f" · {note}" if note else ""))
        if self.statuses:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._sync_buttons()

    def selected(self) -> S.SigStatus | None:
        it = self.tree.currentItem()
        if it is None:
            return None
        i = it.data(0, Qt.ItemDataRole.UserRole)
        return self.statuses[i] if isinstance(i, int) and 0 <= i < len(self.statuses) else None

    def _sync_buttons(self) -> None:
        st = self.selected()
        self.details_btn.setEnabled(st is not None and bool(st.details or st.error))
        self.trust_btn.setEnabled(st is not None and bool(st.cert_pem) and st.trust in ("untrusted",))

    # actions -------------------------------------------------------------
    def _clicked(self, item, _col=0) -> None:
        st = self.selected()
        if st is not None and st.page is not None:
            self.jump.emit(st.page, st.rect)

    def _details(self) -> None:
        st = self.selected()
        if st is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Signature details — {st.signer}")
        v = QVBoxLayout(dlg)
        t = QPlainTextEdit()
        t.setReadOnly(True)
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        t.setFont(f)
        t.setPlainText((st.error + "\n\n" if st.error else "") + st.details + "\n" + st.cert_pem)
        v.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        save = bb.addButton("Save certificate…", QDialogButtonBox.ButtonRole.ActionRole)
        save.clicked.connect(lambda: self._save_cert(st))
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(720, 560)
        dlg.exec()

    def _save_cert(self, st) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save certificate", f"{st.signer or 'signer'}.pem",
                                              "Certificate (*.pem)")
        if path:
            with open(path, "w", encoding="ascii") as f:
                f.write(st.cert_pem)

    def _trust_selected(self) -> None:
        st = self.selected()
        if st is None or not st.cert_pem:
            return
        r = QMessageBox.question(
            self, "Trust signer",
            f"Trust signatures made with this certificate?\n\n{st.subject}\nIssued by: {st.issuer}\n"
            f"SHA-256: {st.fingerprint[:32]}…\n\nOnly do this if you know the certificate really belongs to "
            f"{st.signer} (for example, compare the fingerprint with them).")
        if r != QMessageBox.StandardButton.Yes:
            return
        S.add_trusted(self.settings, S.parse_certificates(st.cert_pem.encode()))
        self.trust_changed.emit()

    def _manage_trust(self) -> None:
        if TrustDialog(self.settings, self).exec():
            self.trust_changed.emit()


# ------------------------------------------------------------ trust dialog
class TrustDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Trusted certificates")
        v = QVBoxLayout(self)
        info = QLabel("Signatures are shown as trusted when the signer's certificate is one of these, or was issued "
                      "by one of them.")
        info.setWordWrap(True)
        v.addWidget(info)
        self.list = QListWidget()
        self.list.setMinimumSize(460, 180)
        v.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton(icon("plus"), "Add…")
        add.clicked.connect(self._add)
        self.remove = QPushButton(icon("delete"), "Remove")
        self.remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(self.remove)
        row.addStretch()
        v.addLayout(row)
        self.system = QCheckBox("Also trust the system's root certificates")
        self.system.setChecked(S.use_system_trust(settings))
        v.addWidget(self.system)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for c in S.trusted_certificates(self.settings):
            it = QListWidgetItem(f"{S.cert_name(c)}   ·   expires {c.not_valid_after:%Y-%m-%d}")
            it.setToolTip(f"{c.subject.human_friendly}\nIssued by: {c.issuer.human_friendly}\n"
                          f"SHA-256: {S.cert_fingerprint(c)}")
            it.setData(Qt.ItemDataRole.UserRole, S.cert_fingerprint(c))
            self.list.addItem(it)
        self.remove.setEnabled(self.list.count() > 0)

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add trusted certificates", "",
                                                "Certificates (*.pem *.crt *.cer *.der);;All files (*)")
        bad = []
        for p in paths:
            try:
                with open(p, "rb") as f:
                    S.add_trusted(self.settings, S.parse_certificates(f.read()))
            except Exception:  # noqa: BLE001
                bad.append(os.path.basename(p))
        if bad:
            QMessageBox.warning(self, "Trusted certificates", "Not a certificate (PEM or DER): " + ", ".join(bad))
        self._fill()

    def _remove(self) -> None:
        it = self.list.currentItem()
        if it is not None:
            S.remove_trusted(self.settings, it.data(Qt.ItemDataRole.UserRole))
            self._fill()

    def _ok(self) -> None:
        self.settings.setValue("signing/system_trust", self.system.isChecked())
        self.accept()


# ------------------------------------------------------ new identity dialog
class CreateIdentityDialog(QDialog):
    """Name, email, organization, validity, key type, password -> a .p12 file. `path`/`password` after exec."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create a self-signed certificate")
        self.path = ""
        self.password = ""
        v = QVBoxLayout(self)
        note = QLabel("A self-signed certificate proves that a document hasn't changed since you signed it. Others "
                      "see your signature as valid once they trust your certificate (send them the .pem file "
                      "from the Signatures panel's Details).")
        note.setWordWrap(True)
        v.addWidget(note)
        f = QFormLayout()
        from PySide6.QtCore import QSettings

        self.name = QLineEdit(str(QSettings().value("annotations/author", "") or ""))  # Settings › General › Author
        self.email = QLineEdit()
        self.org = QLineEdit()
        self.years = QSpinBox()
        self.years.setRange(1, 20)
        self.years.setValue(3)
        self.years.setSuffix(" years")
        self.algo = QComboBox()
        self.algo.addItem("RSA 3072-bit (most compatible)", "rsa")
        self.algo.addItem("ECDSA P-256 (smaller, faster)", "ec")
        self.pw, self.pw2 = _pw_edit(), _pw_edit()
        f.addRow("Name:", self.name)
        f.addRow("Email:", self.email)
        f.addRow("Organization:", self.org)
        f.addRow("Valid for:", self.years)
        f.addRow("Key:", self.algo)
        f.addRow("Password:", self.pw)
        f.addRow("Confirm:", self.pw2)
        v.addLayout(f)
        self.error = _warn_label()
        v.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Create…")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def _ok(self) -> None:
        name = self.name.text().strip()
        if not name:
            return self._fail("Enter your name.")
        if not self.pw.text():
            return self._fail("Choose a password; it protects the private key.")
        if self.pw.text() != self.pw2.text():
            return self._fail("The passwords don't match.")
        safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in name).strip() or "identity"
        path, _ = QFileDialog.getSaveFileName(self, "Save certificate and key", os.path.join(identities_dir(),
                                              f"{safe}.p12"), "PKCS#12 identity (*.p12 *.pfx)")
        if not path:
            return
        if not path.lower().endswith((".p12", ".pfx")):
            path += ".p12"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            S.create_identity(path, self.pw.text(), name, self.email.text(), self.org.text(),
                              days=self.years.value() * 365, algorithm=self.algo.currentData())
        except Exception as ex:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            return self._fail(f"Couldn't create the certificate: {ex}")
        QApplication.restoreOverrideCursor()
        self.path, self.password = os.path.abspath(path), self.pw.text()
        self.accept()


# ------------------------------------------------------------- sign dialog
class SignDialog(QDialog):
    """Collects identity, placement, appearance and options. After exec: `identity`, `request` (a SignRequest
    without placement), `placement` ("new", "invisible" or a field name), `in_place`."""

    def __init__(self, parent, settings, fields: list[tuple[str, str]], signed: bool, can_box: bool,
                 preset_field: str | None = None, doc_name: str = ""):
        """fields: (field name, label) of empty signature fields; signed: the document already has signatures."""
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Sign digitally")
        self.identity: S.Identity | None = None
        self.request: S.SignRequest | None = None
        self.placement = "invisible"
        self.in_place = False
        v = QVBoxLayout(self)

        who = QGroupBox("Signer")
        f = QFormLayout(who)
        row = QHBoxLayout()
        self.ident = QComboBox()
        self.ident.setMinimumWidth(300)
        for p in S.recent_identities(settings):
            if os.path.exists(p):
                self.ident.addItem(os.path.basename(p), p)
                self.ident.setItemData(self.ident.count() - 1, p, Qt.ItemDataRole.ToolTipRole)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.ident, 1)
        row.addWidget(browse)
        f.addRow("Certificate:", row)
        self.pw = _pw_edit()
        f.addRow("Password:", self.pw)
        create = QPushButton("Create a self-signed certificate…")
        create.setFlat(True)
        create.setStyleSheet("text-align: left; color: palette(link);")
        create.setCursor(Qt.CursorShape.PointingHandCursor)
        create.clicked.connect(self._create)
        f.addRow("", create)
        v.addWidget(who)

        sig = QGroupBox("Signature")
        f = QFormLayout(sig)
        self.place = QComboBox()
        for name, label in fields:
            self.place.addItem(label, name)
        if can_box:
            self.place.addItem("New box — place it on the page next", "new")
        self.place.addItem("Invisible (no mark on the page)", "invisible")
        if preset_field:
            i = self.place.findData(preset_field)
            if i >= 0:
                self.place.setCurrentIndex(i)
        f.addRow("Place:", self.place)
        self.look = QComboBox()
        self.look.setIconSize(QSize(72, 24))
        self._fill_looks()
        self.look.activated.connect(self._look_chosen)
        f.addRow("Appearance:", self.look)
        self.show_text = QCheckBox("Show name, date and reason")
        self.show_text.setChecked(settings.value("signing/show_text", True) in (True, "true", "1", 1))
        f.addRow("", self.show_text)
        self.reason = QLineEdit(str(settings.value("signing/reason", "") or ""))
        self.reason.setPlaceholderText("e.g. I approve this document")
        self.location = QLineEdit(str(settings.value("signing/location", "") or ""))
        self.contact = QLineEdit(str(settings.value("signing/contact", "") or ""))
        f.addRow("Reason:", self.reason)
        f.addRow("Location:", self.location)
        f.addRow("Contact:", self.contact)
        self.certify = QComboBox()
        self.certify.addItem("No — approval signature", 0)
        for lvl, text in S.CERTIFY_LEVELS.items():
            self.certify.addItem(f"Certify — allow: {text.lower()}" if lvl > 1 else f"Certify — {text.lower()}", lvl)
        self.certify.setToolTip("A certifying signature fixes which changes keep the document valid. Only the first "
                                "signature of a document can certify it.")
        if signed:
            self.certify.setEnabled(False)
        f.addRow("Certify:", self.certify)
        self.tsa = QLineEdit(S.tsa_url(settings))
        self.tsa.setPlaceholderText("optional, e.g. http://timestamp.example.com")
        self.tsa.setToolTip("An RFC 3161 timestamp server proves when you signed (PAdES B-T). Leaf contacts it only "
                            "while signing; leave empty to sign offline (PAdES B-B).")
        f.addRow("Timestamp server:", self.tsa)
        v.addWidget(sig)

        out = QGroupBox("Save")
        ov = QVBoxLayout(out)
        self.as_copy = QRadioButton(f"Save the signed document as a new file ({doc_name}-signed.pdf)" if doc_name
                                    else "Save the signed document as a new file")
        self.as_self = QRadioButton("Sign this file in place")
        grp = QButtonGroup(self)
        grp.addButton(self.as_copy)
        grp.addButton(self.as_self)
        (self.as_self if settings.value("signing/in_place", False) in (True, "true", "1", 1) else self.as_copy).setChecked(True)
        ov.addWidget(self.as_copy)
        ov.addWidget(self.as_self)
        v.addWidget(out)

        self.error = _warn_label()
        v.addWidget(self.error)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Sign")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.place.currentIndexChanged.connect(self._sync)
        self._sync()
        (self.pw if self.ident.count() else browse).setFocus()

    # appearance choices --------------------------------------------------
    def _fill_looks(self, select: str | None = None) -> None:
        from .signature import library_dir

        self.look.clear()
        self.look.addItem("Text only", "")
        try:
            names = sorted((n for n in os.listdir(library_dir()) if n.lower().endswith(".png")), reverse=True)
        except OSError:
            names = []
        for n in names:
            p = os.path.join(library_dir(), n)
            self.look.addItem(QIcon(p), "Handwritten signature", p)
        self.look.addItem("Draw or import a new one…", "new")
        want = select if select is not None else str(self.settings.value("signing/image", "") or "")
        i = self.look.findData(want)
        self.look.setCurrentIndex(i if i >= 0 else (1 if names else 0))

    def _look_chosen(self, i: int) -> None:
        if self.look.itemData(i) != "new":
            return
        from .signature import SignatureDialog, library_dir

        before = set(os.listdir(library_dir()))
        dlg = SignatureDialog(self)
        dlg.place_new.setText("Save")
        dlg.use_btn.setText("Use selected")
        if dlg.exec() and dlg.result_image is not None:
            new = sorted(set(os.listdir(library_dir())) - before)
            path = os.path.join(library_dir(), new[-1]) if new else ""
            if not path:  # picked from the library list
                path = self._library_match(dlg.result_image)
            self._fill_looks(path)
        else:
            self._fill_looks()

    @staticmethod
    def _library_match(img) -> str:
        from PySide6.QtGui import QImage

        from .signature import library_dir

        d = library_dir()
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if n.lower().endswith(".png") and QImage(p) == img:
                return p
        return ""

    def _sync(self) -> None:
        visible = self.place.currentData() != "invisible"
        self.look.setEnabled(visible)
        self.show_text.setEnabled(visible)

    # identity ------------------------------------------------------------
    def _browse(self) -> None:
        start = os.path.dirname(self.ident.currentData() or "") or identities_dir()
        path, _ = QFileDialog.getOpenFileName(self, "Choose your certificate", start,
                                              "PKCS#12 identity (*.p12 *.pfx);;All files (*)")
        if path:
            self._select_identity(os.path.abspath(path))
            self.pw.clear()
            self.pw.setFocus()

    def _select_identity(self, path: str) -> None:
        i = self.ident.findData(path)
        if i < 0:
            self.ident.insertItem(0, os.path.basename(path), path)
            self.ident.setItemData(0, path, Qt.ItemDataRole.ToolTipRole)
            i = 0
        self.ident.setCurrentIndex(i)

    def _create(self) -> None:
        dlg = CreateIdentityDialog(self)
        if dlg.exec():
            self._select_identity(dlg.path)
            self.pw.setText(dlg.password)
            S.remember_identity(self.settings, dlg.path)

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def _ok(self) -> None:
        path = self.ident.currentData()
        if not path:
            return self._fail("Choose your certificate (.p12 or .pfx), or create a self-signed one.")
        try:
            ident = S.load_identity(path, self.pw.text())
        except S.IdentityError as ex:
            return self._fail(str(ex))
        if ident.expired:
            return self._fail(f"This certificate expired on {ident.not_after:%Y-%m-%d}.")
        tsa = self.tsa.text().strip()
        if tsa and not tsa.lower().startswith(("http://", "https://")):
            return self._fail("The timestamp server must be an http:// or https:// address.")
        image = None
        look = self.look.currentData() if self.look.isEnabled() else ""
        if look and look != "new":
            try:
                with open(look, "rb") as f:
                    image = f.read()
            except OSError:
                image = None
        s = self.settings
        S.remember_identity(s, path)
        s.setValue("signing/reason", self.reason.text())
        s.setValue("signing/location", self.location.text())
        s.setValue("signing/contact", self.contact.text())
        s.setValue("signing/show_text", self.show_text.isChecked())
        s.setValue("signing/image", look if look != "new" else "")
        s.setValue("signing/tsa_url", tsa)
        s.setValue("signing/in_place", self.as_self.isChecked())
        self.identity = ident
        self.placement = self.place.currentData()
        self.in_place = self.as_self.isChecked()
        self.request = S.SignRequest(reason=self.reason.text(), location=self.location.text(),
                                     contact=self.contact.text(), image=image,
                                     show_text=self.show_text.isChecked() or image is None,
                                     certify=self.certify.currentData() or None, tsa_url=tsa)
        self.accept()
