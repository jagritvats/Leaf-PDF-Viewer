"""Document properties: metadata (Info + XMP), fonts, and password protection (AES-256).

Like every other edit these are document-level settings (`PdfDocument.metadata_edits`, `PdfDocument.security`)
applied to the output at save time; the sources are never touched."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pymupdf
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QUndoCommand
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

META_KEYS = ("title", "author", "subject", "keywords")

# (key, label, PDF permission bits)
PERMS = [
    ("print", "Printing", pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ),
    ("copy", "Copying text and images", pymupdf.PDF_PERM_COPY),
    ("edit", "Editing pages and content", pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ASSEMBLE),
    ("annotate", "Adding comments and annotations", pymupdf.PDF_PERM_ANNOTATE),
    ("forms", "Filling in forms", pymupdf.PDF_PERM_FORM),
]
PERM_ALL = sum(b for _, _, b in PERMS) | pymupdf.PDF_PERM_ACCESSIBILITY


# ------------------------------------------------------------------ security
@dataclass(frozen=True)
class Security:
    """Protection to write at save. `Security()` (no passwords) writes the file unprotected."""
    user_pw: str = ""  # needed to open the file
    owner_pw: str = ""  # needed to change permissions/protection
    perms: int = PERM_ALL

    @property
    def encrypted(self) -> bool:
        return bool(self.user_pw or self.owner_pw)

    def allows(self, key: str) -> bool:
        bits = dict((k, b) for k, _, b in PERMS)[key]
        return (self.perms & bits) == bits

    def save_kwargs(self) -> dict:
        if not self.encrypted:
            return {"encryption": pymupdf.PDF_ENCRYPT_NONE}
        return {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": self.user_pw,
                "owner_pw": self.owner_pw or self.user_pw, "permissions": self.perms | pymupdf.PDF_PERM_ACCESSIBILITY}


@dataclass
class SecurityInfo:
    encrypted: bool
    method: str  # e.g. "Standard V5 R6 256-bit AES", "" if none
    open_password: bool
    perms: int
    pending: bool  # changed in Leaf, applied at the next save


def security_info(doc) -> SecurityInfo:
    """What the next save writes, for display."""
    sec = doc.security
    if sec is not None:
        return SecurityInfo(sec.encrypted, "AES-256" if sec.encrypted else "", bool(sec.user_pw),
                            sec.perms if sec.encrypted else PERM_ALL, True)
    src = doc.primary
    method = (src.doc.metadata or {}).get("encryption") or ""
    if not method:
        return SecurityInfo(False, "", False, PERM_ALL, False)
    t, v = src.doc.xref_get_key(-1, "Encrypt/P")
    try:
        perms = int(v) if t == "int" else PERM_ALL
    except ValueError:
        perms = PERM_ALL
    return SecurityInfo(True, method, bool(src.password), perms, False)


def is_protected(doc) -> bool:
    return doc is not None and security_info(doc).encrypted


def owner_access(src, password: str | None = None) -> bool:
    """True if `password` (default: the one the source was opened with) grants owner rights, or the file has none."""
    d = pymupdf.open(stream=src.data, filetype="pdf")
    try:
        if not d.needs_pass and not (d.metadata or {}).get("encryption"):
            return True
        return bool(d.authenticate(src.password if password is None else password) & 4)
    finally:
        d.close()


def signature_count(pdf: pymupdf.Document) -> int:
    """Signed signature fields (a field whose /V holds a signature dictionary)."""
    if not pdf.is_form_pdf:
        return 0
    n = 0
    for page in pdf:
        for w in page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE]):
            if pdf.xref_get_key(w.xref, "V")[0] in ("xref", "dict"):
                n += 1
    return n


# ------------------------------------------------------------------ metadata
_DATE = re.compile(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-]\d{2}'?\d{0,2}'?)?")


def parse_pdf_date(s: str | None) -> datetime | None:
    """'D:20231015123045+02'00'' -> aware datetime (naive if the string has no zone); None if unparsable."""
    m = _DATE.match((s or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(sec or 0))
    except ValueError:
        return None
    if tz:
        if tz == "Z":
            return dt.replace(tzinfo=timezone.utc)
        digits = re.sub(r"\D", "", tz)
        off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        return dt.replace(tzinfo=timezone(off if tz[0] == "+" else -off))
    return dt


def format_date(s: str | None) -> str:
    dt = parse_pdf_date(s)
    if dt is None:
        return s or "—"
    text = dt.strftime("%Y-%m-%d %H:%M")
    if dt.tzinfo is not None:
        off = dt.utcoffset() or timedelta(0)
        mins = int(off.total_seconds() // 60)
        text += f" (UTC{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d})"
    return text


def effective_metadata(doc) -> dict:
    meta = dict(doc.primary.doc.metadata or {})
    meta.update(doc.metadata_edits)
    return meta


def apply_metadata(out: pymupdf.Document, edits: dict) -> None:
    """Write Info edits into `out` (stamps ModDate) and keep an existing XMP packet in step."""
    meta = {k: v for k, v in (out.metadata or {}).items() if k not in ("format", "encryption")}
    meta.update({k: v for k, v in edits.items() if k in META_KEYS})
    meta["modDate"] = pymupdf.get_pdf_now()
    out.set_metadata(meta)
    try:
        xml = out.get_xml_metadata()
    except Exception:  # noqa: BLE001
        xml = ""
    if xml:
        new = update_xmp(xml, {k: v for k, v in edits.items() if k in META_KEYS}, meta["modDate"])
        if new is not None and new != xml:
            out.set_xml_metadata(new)


NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "pdf": "http://ns.adobe.com/pdf/1.3/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
# Info key -> (namespace, local name, container): Alt = language alternatives, Seq = ordered list, None = text
_XMP_FIELDS = {"title": ("dc", "title", "Alt"), "author": ("dc", "creator", "Seq"),
               "subject": ("dc", "description", "Alt"), "keywords": ("pdf", "Keywords", None)}


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def _iso(pdf_date: str) -> str | None:
    dt = parse_pdf_date(pdf_date)
    return dt.isoformat() if dt else None


def _element_span(xml: str, tag: str) -> tuple[int, int] | None:
    """(start, end) of the first <tag ...>...</tag> in `xml`: what re.search(rf"<{tag}\\b.*?</{tag}>", xml, re.S)
    finds, in linear time. (That pattern rescans to the end of the text from every "<tag" when the end tag is
    missing: a few hundred KB of them in a file's metadata stalled saving for minutes.)"""
    m = re.search(rf"<{re.escape(tag)}\b", xml)
    if not m:
        return None
    end = xml.find(f"</{tag}>", m.start())
    return (m.start(), end + len(tag) + 3) if end >= 0 else None


def update_xmp(xml: str, edits: dict, mod_date: str | None = None) -> str | None:
    """`xml` with dc:title / dc:creator / dc:description / pdf:Keywords (and xmp:ModifyDate/MetadataDate if present)
    set from `edits`. The xpacket wrapper and other properties are left as they are. None if it can't be parsed."""
    span = _element_span(xml, "x:xmpmeta") or _element_span(xml, "rdf:RDF")
    if not span:
        return None
    frag = xml[span[0]:span[1]]
    try:
        import io

        for _, (prefix, uri) in ET.iterparse(io.StringIO(frag), events=("start-ns",)):
            if prefix and not re.fullmatch(r"ns\d+", prefix):
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
        root = ET.fromstring(frag)
    except ET.ParseError:
        return None
    rdf = root if root.tag == _q("rdf", "RDF") else root.find(_q("rdf", "RDF"))
    if rdf is None:
        return None
    descs = rdf.findall(_q("rdf", "Description"))
    if not descs:
        descs = [ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})]
    for key, value in edits.items():
        if key not in _XMP_FIELDS:
            continue
        prefix, name, kind = _XMP_FIELDS[key]
        tag = _q(prefix, name)
        for d in descs:  # simple properties may be written as attributes
            if tag in d.attrib:
                d.set(tag, value)
        el = next((e for d in descs for e in d.findall(tag)), None)
        if el is None:
            if not value or any(tag in d.attrib for d in descs):
                continue
            el = ET.SubElement(descs[0], tag)
        if kind is None:
            for c in list(el):
                el.remove(c)
            el.text = value
            continue
        el.text = None
        for c in list(el):
            el.remove(c)
        box = ET.SubElement(el, _q("rdf", kind))
        if value:
            li = ET.SubElement(box, _q("rdf", "li"))
            if kind == "Alt":
                li.set(_XML_LANG, "x-default")
            li.text = value
    iso = _iso(mod_date) if mod_date else None
    if iso:
        for name in ("ModifyDate", "MetadataDate"):
            tag = _q("xmp", name)
            for d in descs:
                if tag in d.attrib:
                    d.set(tag, iso)
                for e in d.findall(tag):
                    e.text = iso
    return xml[:span[0]] + ET.tostring(root, encoding="unicode") + xml[span[1]:]


# ------------------------------------------------------------------ commands
class SetMetadata(QUndoCommand):
    def __init__(self, doc, new: dict, on_change=None):
        super().__init__("Edit document properties")
        self.doc, self.old, self.new, self.on_change = doc, dict(doc.metadata_edits), dict(new), on_change

    def _set(self, edits: dict) -> None:
        self.doc.metadata_edits = dict(edits)
        if self.on_change:
            self.on_change()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


class SetSecurity(QUndoCommand):
    def __init__(self, doc, new: Security | None, on_change=None):
        text = "Remove password protection" if new is not None and not new.encrypted else "Set password protection"
        super().__init__(text)
        self.doc, self.old, self.new, self.on_change = doc, doc.security, new, on_change

    def _set(self, sec) -> None:
        self.doc.security = sec
        if self.on_change:
            self.on_change()

    def redo(self) -> None:
        self._set(self.new)

    def undo(self) -> None:
        self._set(self.old)


def metadata_changes(doc, values: dict) -> dict | None:
    """The new `metadata_edits` for edited field `values`, or None if nothing changed."""
    base = dict(doc.primary.doc.metadata or {})
    cur = effective_metadata(doc)
    if all((values.get(k, "") or "") == (cur.get(k) or "") for k in META_KEYS):
        return None
    return {k: values[k] for k in META_KEYS if (values.get(k) or "") != (base.get(k) or "")}


# ------------------------------------------------------------------ dialogs
def _warn_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: palette(link-visited);")
    return lab


def _signature_warning(doc) -> str:
    n = signature_count(doc.primary.doc)
    if not n:
        return ""
    return (f"This document has {n} digital signature{'s' if n > 1 else ''}. Changing its protection rewrites the "
            "whole file, which invalidates existing signatures.")


class ProtectDialog(QDialog):
    """Open password and/or permission password (AES-256). `result_security()` after exec."""

    def __init__(self, parent, doc):
        super().__init__(parent)
        self.setWindowTitle("Protect with password")
        self.doc = doc
        info = security_info(doc)
        cur = doc.security if doc.security is not None and doc.security.encrypted else None
        lay = QVBoxLayout(self)

        self.open_box = QGroupBox("Require a password to open the document")
        self.open_box.setCheckable(True)
        self.open_box.setChecked(info.open_password if info.encrypted else True)
        f = QFormLayout(self.open_box)
        self.user_pw, self.user_pw2 = self._pw(), self._pw()
        f.addRow("Password:", self.user_pw)
        f.addRow("Confirm:", self.user_pw2)
        lay.addWidget(self.open_box)

        self.perm_box = QGroupBox("Restrict printing and editing")
        self.perm_box.setCheckable(True)
        self.perm_box.setChecked(info.encrypted and (info.perms & PERM_ALL) != PERM_ALL)
        v = QVBoxLayout(self.perm_box)
        f2 = QFormLayout()
        self.owner_pw, self.owner_pw2 = self._pw(), self._pw()
        f2.addRow("Permissions password:", self.owner_pw)
        f2.addRow("Confirm:", self.owner_pw2)
        v.addLayout(f2)
        v.addWidget(QLabel("Allow:"))
        self.perm_checks: dict[str, QCheckBox] = {}
        for key, label, bits in PERMS:
            cb = QCheckBox(label)
            cb.setChecked(not info.encrypted or (info.perms & bits) == bits)
            v.addWidget(cb)
            self.perm_checks[key] = cb
        lay.addWidget(self.perm_box)
        if cur is not None:
            for w, t in ((self.user_pw, cur.user_pw), (self.user_pw2, cur.user_pw),
                         (self.owner_pw, cur.owner_pw), (self.owner_pw2, cur.owner_pw)):
                w.setText(t)

        note = QLabel("Encryption: AES-256. Applied when you save; you can undo it until then.")
        note.setWordWrap(True)
        lay.addWidget(note)
        warn = _signature_warning(doc)
        if warn:
            lay.addWidget(_warn_label(warn))
        self.error = _warn_label("")
        self.error.hide()
        lay.addWidget(self.error)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Protect")
        self.remove_btn = None
        if info.encrypted:
            self.remove_btn = bb.addButton("Remove protection", QDialogButtonBox.ButtonRole.DestructiveRole)
            self.remove_btn.clicked.connect(self._remove)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._result: Security | None = None

    @staticmethod
    def _pw() -> QLineEdit:
        e = QLineEdit()
        e.setEchoMode(QLineEdit.EchoMode.Password)
        e.setMinimumWidth(220)
        return e

    def result_security(self) -> Security | None:
        return self._result

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def _remove(self) -> None:
        self._result = Security()
        self.accept()

    def _ok(self) -> None:
        user = self.user_pw.text() if self.open_box.isChecked() else ""
        owner = self.owner_pw.text() if self.perm_box.isChecked() else ""
        if self.open_box.isChecked():
            if not user:
                return self._fail("Enter a password to open the document.")
            if user != self.user_pw2.text():
                return self._fail("The open passwords don't match.")
        perms = PERM_ALL
        if self.perm_box.isChecked():
            if not owner:
                return self._fail("Enter a permissions password (it is needed to lift the restrictions later).")
            if owner != self.owner_pw2.text():
                return self._fail("The permissions passwords don't match.")
            if owner == user:
                return self._fail("The permissions password must differ from the open password.")
            perms = pymupdf.PDF_PERM_ACCESSIBILITY
            for key, _, bits in PERMS:
                if self.perm_checks[key].isChecked():
                    perms |= bits
        if not user and not owner:
            return self._fail("Choose an open password, restrictions, or both.")
        self._result = Security(user, owner, perms)
        self.accept()


def ask_owner_access(parent, doc) -> bool:
    """Changing an existing protection needs owner rights: ask for the permissions password if the file was
    opened with the open password only."""
    if doc.security is not None or owner_access(doc.primary):
        return True
    while True:
        pw, ok = QInputDialog.getText(parent, "Permissions password",
                                      "This document's protection can only be changed with its permissions "
                                      "(owner) password:", QLineEdit.EchoMode.Password)
        if not ok:
            return False
        if owner_access(doc.primary, pw):
            return True
        QMessageBox.warning(parent, "Leaf", "That password doesn't grant permission to change the protection.")


def protect(win) -> bool:
    """File › Protect with password…: pushes an undoable SetSecurity. True if something changed."""
    cv = win.canvas
    if cv.doc is None or not ask_owner_access(win, cv.doc):
        return False
    dlg = ProtectDialog(win, cv.doc)
    if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_security() is None:
        return False
    sec = dlg.result_security()
    if sec == cv.doc.security:
        return False
    cv.undo.push(SetSecurity(cv.doc, sec, getattr(win, "_output_changed", None)))
    if hasattr(win, "toast"):
        win.toast("Protection removed - applies when you save" if not sec.encrypted
                  else "Password protection set - applies when you save")
    return True


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n:,} bytes"
    v = float(n)
    for unit in ("KB", "MB", "GB"):
        v /= 1024
        if v < 1024 or unit == "GB":
            return f"{v:.1f} {unit} ({n:,} bytes)"
    return f"{n:,} bytes"


def _ro(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lab.setWordWrap(True)
    return lab


class PropertiesDialog(QDialog):
    """Description (editable title/author/subject/keywords), Fonts (collected lazily), Security."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        cv = win.canvas
        self.doc = doc = cv.doc
        self.setWindowTitle(f"Properties - {os.path.basename(doc.path or 'Untitled')}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)

        # description
        meta = effective_metadata(doc)
        desc = QWidget()
        f = QFormLayout(desc)
        self.fields: dict[str, QLineEdit] = {}
        for key in META_KEYS:
            e = QLineEdit(meta.get(key) or "")
            e.setClearButtonEnabled(True)
            self.fields[key] = e
            f.addRow(f"{key.capitalize()}:", e)
        f.addRow(QLabel(""))
        f.addRow("Creator:", _ro(meta.get("creator") or "—"))
        f.addRow("Producer:", _ro(meta.get("producer") or "—"))
        f.addRow("Created:", _ro(format_date(meta.get("creationDate"))))
        f.addRow("Modified:", _ro(format_date(meta.get("modDate"))))
        f.addRow("PDF version:", _ro((meta.get("format") or "").replace("PDF ", "") or "—"))
        f.addRow("Pages:", _ro(str(len(doc.pages))))
        i = cv.current_index()
        if 0 <= i < len(doc.pages):
            from .pageops import size_text

            w, h = doc.display_size(doc.pages[i])
            f.addRow(f"Page size (page {i + 1}):", _ro(size_text(w, h)))
        path = doc.path or ""
        size = os.path.getsize(path) if path and os.path.isfile(path) else None
        f.addRow("File size:", _ro(_size_text(size) if size is not None else "Not saved yet"))
        f.addRow("Location:", _ro(path or "—"))
        self.tabs.addTab(desc, "Description")

        # fonts (filled when the tab is first shown)
        self.fonts = QTreeWidget()
        self.fonts.setHeaderLabels(["Font", "Type", "Encoding", "Embedded"])
        self.fonts.setRootIsDecorated(False)
        self.fonts.setSortingEnabled(True)
        self.fonts.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.fonts.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.font_status = QLabel()
        fw = QWidget()
        fl = QVBoxLayout(fw)
        fl.addWidget(self.fonts)
        fl.addWidget(self.font_status)
        self.tabs.addTab(fw, "Fonts")
        self._font_todo: list | None = None
        self._font_seen: set = set()
        self._font_timer = QTimer(self)
        self._font_timer.setInterval(0)
        self._font_timer.timeout.connect(self._font_chunk)

        # security
        self._pending_sec = doc.security
        self._sec_changed = False
        sw = QWidget()
        sl = QVBoxLayout(sw)
        self.sec_state = QLabel()
        self.sec_state.setWordWrap(True)
        sl.addWidget(self.sec_state)
        self.perm_view = QGroupBox("Permissions")
        pv = QVBoxLayout(self.perm_view)
        self.perm_labels: dict[str, QCheckBox] = {}
        for key, label, _ in PERMS:
            cb = QCheckBox(label)
            cb.setEnabled(False)
            pv.addWidget(cb)
            self.perm_labels[key] = cb
        sl.addWidget(self.perm_view)
        row = QHBoxLayout()
        self.change_btn = QPushButton("Change…")
        self.change_btn.clicked.connect(self._change_security)
        self.remove_btn = QPushButton("Remove protection")
        self.remove_btn.clicked.connect(self._remove_security)
        row.addWidget(self.change_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        sl.addLayout(row)
        warn = _signature_warning(doc)
        self.sig_warn = _warn_label(warn)
        self.sig_warn.setVisible(bool(warn))
        sl.addWidget(self.sig_warn)
        sl.addStretch(1)
        self.tabs.addTab(sw, "Security")
        self._show_security()

        self.tabs.currentChanged.connect(self._tab_changed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # fonts
    def _tab_changed(self, i: int) -> None:
        if self.tabs.tabText(i) == "Fonts" and self._font_todo is None:
            self.start_fonts()

    def start_fonts(self) -> None:
        seen, todo = set(), []
        for r in self.doc.pages:
            if (r.src.id, r.index) not in seen:
                seen.add((r.src.id, r.index))
                todo.append(r)
        self._font_todo = todo
        self._font_total = len(todo)
        self._font_timer.start()

    def _font_chunk(self) -> None:
        self.fonts.setSortingEnabled(False)
        for _ in range(8):
            if not self._font_todo:
                break
            r = self._font_todo.pop(0)
            try:
                fonts = r.src.doc.get_page_fonts(r.index)
            except Exception:  # noqa: BLE001
                fonts = []
            for xref, ext, ftype, base, _name, enc, *_ in fonts:
                key = (r.src.id, xref) if xref else (r.src.id, base)
                if key in self._font_seen:
                    continue
                self._font_seen.add(key)
                subset = bool(re.match(r"^[A-Z]{6}\+", base or ""))
                name = (base or _name or "(unnamed)")[7 if subset else 0:]
                embedded = ext not in ("n/a", "")
                state = ("Embedded subset" if subset else "Embedded") if embedded else "No"
                QTreeWidgetItem(self.fonts, [name, ftype or "", enc or "", state])
        self.fonts.setSortingEnabled(True)
        done = self._font_total - len(self._font_todo)
        n = self.fonts.topLevelItemCount()
        if self._font_todo:
            self.font_status.setText(f"Scanning pages… {done} of {self._font_total}")
        else:
            self._font_timer.stop()
            self.font_status.setText(f"{n} font{'s' if n != 1 else ''} on {self._font_total} page"
                                     f"{'s' if self._font_total != 1 else ''}")

    def fonts_done(self) -> bool:
        return self._font_todo is not None and not self._font_todo

    # security
    def _info(self) -> SecurityInfo:
        saved, self.doc.security = self.doc.security, self._pending_sec
        try:
            return security_info(self.doc)
        finally:
            self.doc.security = saved

    def _show_security(self) -> None:
        info = self._info()
        if not info.encrypted:
            text = "No protection. Anyone can open, print and edit this document."
        else:
            parts = ["A password is required to open this document." if info.open_password
                     else "The document opens without a password."]
            if (info.perms & PERM_ALL) != PERM_ALL:
                parts.append("Printing and editing are restricted by a permissions password.")
            parts.append(f"Encryption: {info.method}.")
            text = " ".join(parts)
        if info.pending:
            text += "\n\nChanged in Leaf: applied when you save."
        self.sec_state.setText(text)
        for key, _, bits in PERMS:
            self.perm_labels[key].setChecked(not info.encrypted or (info.perms & bits) == bits)
        self.perm_view.setVisible(info.encrypted)
        self.change_btn.setText("Change…" if info.encrypted else "Protect with password…")
        self.remove_btn.setVisible(info.encrypted)

    def _change_security(self) -> None:
        if not ask_owner_access(self, self.doc):
            return
        saved, self.doc.security = self.doc.security, self._pending_sec
        try:
            dlg = ProtectDialog(self, self.doc)
            ok = dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_security() is not None
        finally:
            self.doc.security = saved
        if ok:
            self._pending_sec = dlg.result_security()
            self._sec_changed = True
            self._show_security()

    def _remove_security(self) -> None:
        if not ask_owner_access(self, self.doc):
            return
        self._pending_sec = Security()
        self._sec_changed = True
        self._show_security()

    # result
    def values(self) -> dict:
        return {k: e.text().strip() for k, e in self.fields.items()}

    def commit(self) -> bool:
        """Push the edits as undoable commands. True if anything changed."""
        undo = self.win.canvas.undo
        cb = getattr(self.win, "_output_changed", None)
        meta = metadata_changes(self.doc, self.values())
        sec = self._sec_changed and self._pending_sec != self.doc.security
        if meta is None and not sec:
            return False
        if meta is not None and sec:
            undo.beginMacro("Edit document properties")
        if meta is not None:
            undo.push(SetMetadata(self.doc, meta, cb))
        if sec:
            undo.push(SetSecurity(self.doc, self._pending_sec, cb))
        if meta is not None and sec:
            undo.endMacro()
        return True


def show_properties(win) -> None:
    if win.canvas.doc is None:
        return
    dlg = PropertiesDialog(win)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        dlg.commit()
