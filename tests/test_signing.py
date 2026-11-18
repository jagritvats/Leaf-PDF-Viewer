"""Digital signatures: identities, signing (invisible / new box / existing field), verification, what later
saves do to a signature, certification levels, trust list, the Signatures panel."""
from __future__ import annotations

import datetime
import os

import pymupdf
import pytest
from PySide6.QtCore import QRectF, QSettings

from leaf import signing as S
from leaf.document import PdfDocument, rect_key
from leaf.items import ShapeItem
from tests.test_editor import add, app, canvas_for, make_pdf, pump  # noqa: F401


@pytest.fixture(scope="module")
def ident(tmp_path_factory):
    d = tmp_path_factory.mktemp("id")
    path = str(d / "jane.p12")
    S.create_identity(path, "secret", "Jane Doe", "jane@example.com", "ACME", days=30, algorithm="ec")
    return S.load_identity(path, "secret")


@pytest.fixture
def pdf(tmp_path):
    path = str(tmp_path / "in.pdf")
    make_pdf(path)  # 3 pages (page 2 rotated 90), text/checkbox/combo fields on page 1
    d = pymupdf.open(path)
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = S.SIG, "Approver", pymupdf.Rect(300, 600, 500, 660)
    d[0].add_widget(w)
    d.saveIncr()
    d.close()
    return path


def verify(path, *trusted):
    with open(path, "rb") as f:
        return S.verify_pdf(f.read(), list(trusted))


def fill(path, out, values: dict) -> bool:
    doc = PdfDocument.open(path)
    ref = doc.pages[0]
    for w in doc.page(ref).widgets():
        if w.field_name in values:
            doc.form_values[(ref.uid, w.field_name, rect_key(w.rect))] = values[w.field_name]
    return doc.save(out, lambda r: [])


def test_identity_create_and_load(ident, tmp_path):
    assert ident.name == "Jane Doe" and ident.self_signed and not ident.expired
    assert "jane@example.com" in ident.subject
    with pytest.raises(S.IdentityError):
        S.load_identity(ident.path, "wrong")
    rsa = str(tmp_path / "rsa.p12")
    S.create_identity(rsa, "pw", "Bob", algorithm="rsa")
    assert S.load_identity(rsa, "pw").cert.public_key.bit_size == 3072
    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    S.remember_identity(s, rsa)
    S.remember_identity(s, ident.path)
    S.remember_identity(s, rsa)
    assert S.recent_identities(s) == [os.path.abspath(rsa), ident.path]
    s.sync()
    assert "secret" not in open(str(tmp_path / "s.ini"), encoding="utf-8").read()


def test_sign_invisible(ident, pdf, tmp_path):
    out = str(tmp_path / "s.pdf")
    name = S.sign_pdf(pdf, out, ident, S.SignRequest(reason="Approve", location="Pune", contact="jane@example.com"))
    assert open(out, "rb").read().startswith(open(pdf, "rb").read())  # incremental
    [st] = verify(out, ident.cert)
    assert st.field == name and st.signer == "Jane Doe" and st.intact and st.valid
    assert st.state == "valid" and st.coverage == "whole" and st.reason == "Approve" and st.location == "Pune"
    assert st.rect is None and st.page == 0 and st.certify is None
    assert S.has_signatures(pymupdf.open(out)) and not S.has_signatures(pymupdf.open(pdf))
    assert S.signature_count(PdfDocument.open(out)) == 1


def test_sign_visible_new_box_on_rotated_page(ident, pdf, tmp_path, app):
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtCore import QBuffer, QIODevice

    img = QImage(300, 100, QImage.Format.Format_ARGB32)
    img.fill(QColor("navy"))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    out = str(tmp_path / "s.pdf")
    box = (100, 100, 300, 160)  # page 2 (rotated 90) display space
    S.sign_pdf(pdf, out, ident, S.SignRequest(page=1, rect=box, reason="Seen", image=bytes(buf.data())))
    [st] = verify(out, ident.cert)
    assert st.state == "valid" and st.page == 1
    assert pymupdf.Rect(st.rect) == pymupdf.Rect(box)
    d = pymupdf.open(out)
    d.bake()  # appearance streams into the page content, for text extraction
    page = d[1]
    words = {w[4]: pymupdf.Rect(w[:4]) * page.rotation_matrix for w in page.get_text("words")}  # display space
    jane, doe = words["Jane"], words["Doe"]
    assert pymupdf.Rect(box).contains(jane) and pymupdf.Rect(box).contains(doe)
    assert jane.x1 <= doe.x0 and abs(jane.y0 - doe.y0) < 1  # reads left to right, upright on the rotated page


def test_sign_existing_field_certified(ident, pdf, tmp_path):
    out = str(tmp_path / "s.pdf")
    assert S.sign_pdf(pdf, out, ident, S.SignRequest(field="Approver", reason="OK", certify=2)) == "Approver"
    [st] = verify(out, ident.cert)
    assert st.field == "Approver" and st.certify == 2 and st.state == "valid"
    assert pymupdf.Rect(st.rect) == pymupdf.Rect(300, 600, 500, 660)
    fields = {f.name: f for f in S.signature_fields(pymupdf.open(out))}
    assert fields["Approver"].signed
    with pytest.raises(S.SigningError):  # already signed
        S.sign_pdf(out, str(tmp_path / "t.pdf"), ident, S.SignRequest(field="Approver"))
    with pytest.raises(S.SigningError):  # only the first signature certifies
        S.sign_pdf(out, str(tmp_path / "t.pdf"), ident, S.SignRequest(certify=1))
    # certification level 2: form filling is allowed
    filled = str(tmp_path / "f.pdf")
    assert fill(out, filled, {"name": "Bob", "agree": True, "color": "Green"}) is True
    [st] = verify(filled, ident.cert)
    assert st.intact and st.coverage == "revision" and st.changes == "form" and st.changes_ok and st.state == "valid"


def test_certified_no_changes_rejects_form_filling(ident, pdf, tmp_path):
    out, filled = str(tmp_path / "s.pdf"), str(tmp_path / "f.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest(certify=1))
    fill(out, filled, {"name": "Bob"})
    [st] = verify(filled, ident.cert)
    assert st.intact and st.certify == 1 and st.changes == "form" and st.changes_ok is False
    assert st.state == "modified"


def test_form_fill_keeps_approval_signature_valid(ident, pdf, tmp_path):
    out, filled = str(tmp_path / "s.pdf"), str(tmp_path / "f.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest(field="Approver"))
    assert fill(out, filled, {"name": "Bob", "agree": True}) is True  # incremental
    data = open(filled, "rb").read()
    assert data.startswith(open(out, "rb").read())
    [st] = verify(filled, ident.cert)
    assert st.state == "valid" and st.changes == "form" and st.changes_ok
    # a second signature on top: the first one stays valid
    two = str(tmp_path / "two.pdf")
    S.sign_pdf(filled, two, ident, S.SignRequest(reason="Second"))
    sts = verify(two, ident.cert)
    assert [s.state for s in sts] == ["valid", "valid"]
    assert sts[0].changes == "form" and sts[1].coverage == "whole"


def test_comments_after_signing(ident, pdf, tmp_path, app):
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest())
    c = canvas_for(out)
    add(c, 0, ShapeItem("rect", QRectF(100, 400, 120, 60), dict(c.defaults["rect"])))
    saved = str(tmp_path / "a.pdf")
    assert c.doc.save(saved, c.overlays_for) is True
    [st] = verify(saved, ident.cert)
    assert st.changes == "annotations" and st.changes_ok and st.state == "valid"
    # certified for form filling only: a comment is not allowed
    out2, saved2 = str(tmp_path / "s2.pdf"), str(tmp_path / "a2.pdf")
    S.sign_pdf(pdf, out2, ident, S.SignRequest(certify=2))
    c = canvas_for(out2)
    add(c, 0, ShapeItem("rect", QRectF(100, 400, 120, 60), dict(c.defaults["rect"])))
    c.doc.save(saved2, c.overlays_for)
    [st] = verify(saved2, ident.cert)
    assert st.changes == "annotations" and st.changes_ok is False and st.state == "modified"


def test_page_content_change_and_rewrite_break_signature(ident, pdf, tmp_path, app):
    from leaf.items import TextItem
    from PySide6.QtCore import QPointF

    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest(field="Approver"))
    c = canvas_for(out)
    t = TextItem(QPointF(100, 500), dict(c.defaults["text"]))
    t.setPlainText("added later")
    add(c, 0, t)  # text boxes are flattened into the page content
    inc = str(tmp_path / "t.pdf")
    assert c.doc.save(inc, c.overlays_for) is True
    [st] = verify(inc, ident.cert)
    assert st.intact and st.changes == "other" and st.state == "modified"
    # a full rewrite (page reorder) breaks the signature outright
    doc = PdfDocument.open(out)
    doc.pages = [doc.pages[1], doc.pages[0], doc.pages[2]]
    re = str(tmp_path / "re.pdf")
    assert doc.save(re, lambda r: []) is False
    [st] = verify(re, ident.cert)
    assert st.state == "invalid" and not st.intact


def test_trust_list(ident, pdf, tmp_path):
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest())
    [st] = verify(out)
    assert st.intact and st.valid and not st.trusted and st.trust == "untrusted" and st.state == "untrusted"
    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    pem = str(tmp_path / "jane.pem")
    S.export_certificate(ident, pem)
    certs = S.parse_certificates(open(pem, "rb").read())
    assert S.add_trusted(s, certs) == 1 and S.add_trusted(s, certs) == 0
    der = str(tmp_path / "jane.cer")
    open(der, "wb").write(ident.cert.dump())
    assert S.parse_certificates(open(der, "rb").read())[0].dump() == ident.cert.dump()
    [st] = verify(out, *S.trusted_certificates(s))
    assert st.trusted and st.state == "valid"
    S.remove_trusted(s, S.cert_fingerprint(ident.cert))
    assert S.trusted_certificates(s) == []


def test_expired_certificate(pdf, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Old Key")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(now - datetime.timedelta(days=20))
            .not_valid_after(now - datetime.timedelta(days=10)).sign(key, hashes.SHA256()))
    p12 = str(tmp_path / "old.p12")
    open(p12, "wb").write(pkcs12.serialize_key_and_certificates(b"old", key, cert, None,
                                                                serialization.BestAvailableEncryption(b"pw")))
    old = S.load_identity(p12, "pw")
    assert old.expired
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, old, S.SignRequest())
    [st] = verify(out, old.cert)
    assert st.intact and not st.trusted and st.trust == "expired" and st.state == "untrusted"


def test_timestamped_signature(ident, pdf, tmp_path):
    from asn1crypto import keys, x509 as ax509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from pyhanko.sign.timestamps import DummyTimeStamper

    key = rsa.generate_private_key(65537, 2048)  # pyHanko's test TSA is RSA-only
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=10))
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
            .sign(key, hashes.SHA256()))
    tsa_cert = ax509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    tsa_key = keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest(timestamper=DummyTimeStamper(tsa_cert, tsa_key)))
    [st] = verify(out, ident.cert, tsa_cert)
    assert st.timestamp is not None and st.timestamp_ok and st.state == "valid"


def test_saved_form_field_then_signed(ident, tmp_path, app):
    """A signature box placed in Leaf (forms authoring field) is saved as an empty field, then signed."""
    from leaf import fields

    path = str(tmp_path / "in.pdf")
    make_pdf(path, form=False)
    c = canvas_for(path)
    props = fields.default_props(c, "signature")
    item = fields.insert_field(c, "signature", props, pg=c.order[0])
    assert item is not None
    saved = str(tmp_path / "saved.pdf")
    c.doc.save(saved, c.overlays_for)
    [f] = S.signature_fields(pymupdf.open(saved))
    assert not f.signed and f.name == props["name"]
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(saved, out, ident, S.SignRequest(field=f.name))
    [st] = verify(out, ident.cert)
    assert st.state == "valid" and st.field == f.name and pymupdf.Rect(st.rect) == f.rect


def test_encrypted_document(ident, tmp_path):
    path = str(tmp_path / "enc.pdf")
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "secret")
    d.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    d.close()
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(path, out, ident, S.SignRequest(password="u", rect=(72, 100, 272, 150)))
    [st] = S.verify_pdf(open(out, "rb").read(), [ident.cert], password="u")
    assert st.state == "valid"


# ------------------------------------------------------------------ UI
@pytest.fixture
def win(app):  # noqa: F811
    from PySide6.QtCore import QCoreApplication

    from leaf.window import MainWindow

    old = QCoreApplication.organizationName(), QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("LeafTest")
    QCoreApplication.setApplicationName("LeafSigningTest")
    QSettings().clear()
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    yield w
    if w.canvas.doc is not None:
        w.canvas.undo.setClean()
    w.close()
    QSettings().clear()
    QCoreApplication.setOrganizationName(old[0])
    QCoreApplication.setApplicationName(old[1])


def wait_verified(app, w, secs=10.0):  # noqa: F811
    import time

    end = time.time() + secs
    while time.time() < end and w.signing._job is not None:
        app.processEvents()
        time.sleep(0.01)
    pump(app, 0.05)
    assert w.signing._job is None, "verification did not finish"


def test_panel_and_banner(app, win, ident, pdf, tmp_path):  # noqa: F811
    out = str(tmp_path / "s.pdf")
    S.sign_pdf(pdf, out, ident, S.SignRequest(field="Approver", reason="Approve"))
    win.open_path(out)
    wait_verified(app, win)
    sg = win.signing
    assert [s.signer for s in sg.statuses] == ["Jane Doe"]
    assert sg.panel.tree.topLevelItemCount() == 1
    assert sg.banner.isVisible() and "Jane Doe" in sg.banner.label.text() and sg.banner.state == "untrusted"
    assert sg.panel.trust_btn.isEnabled()
    # trusting the signer turns it valid
    S.add_trusted(win.settings, [ident.cert])
    sg.reverify()
    wait_verified(app, win)
    assert sg.statuses[0].state == "valid" and sg.banner.state == "valid"
    # clicking the entry scrolls to the field (page 1); go elsewhere first
    win.canvas.go_to(2)
    pump(app, 0.05)
    sg.panel._clicked(sg.panel.tree.topLevelItem(0))
    pump(app, 0.05)
    cv = win.canvas
    field_pt = cv.order[0].content.mapToScene(QRectF(300, 600, 200, 60).center())
    assert cv.viewport().rect().contains(cv.mapFromScene(field_pt))
    # form filling + save keeps it valid (re-verified as saved)
    doc = win.canvas.doc
    ref = doc.pages[0]
    w = next(x for x in doc.page(ref).widgets() if x.field_name == "name")
    win.canvas.set_form_values({(ref.uid, "name", rect_key(w.rect)): "Bob"})
    assert win.save()
    wait_verified(app, win)
    assert sg.from_saved and sg.statuses[0].state == "valid" and sg.statuses[0].changes == "form"
    assert "Sign digitally…" in [a.text().replace("&", "") for a in win.menus["document"].actions()]
    assert win.a_signatures_panel.shortcut().toString() == "F12"


def test_unsigned_document_has_no_banner(app, win, pdf):  # noqa: F811
    win.open_path(pdf)
    pump(app, 0.1)
    assert not win.signing.banner.isVisible() and win.signing._job is None
    assert "no digital signatures" in win.signing.panel.summary.text()


def test_sign_from_window_reopens_signed_file(app, win, ident, pdf, tmp_path):  # noqa: F811
    path = str(tmp_path / "doc.pdf")
    import shutil

    shutil.copy(pdf, path)
    win.open_path(path)
    pump(app, 0.1)
    sg = win.signing
    sg._sign(ident, S.SignRequest(reason="In place"), "Approver", True)  # clean document: no save prompt
    wait_verified(app, win)
    assert win.canvas.doc.path == os.path.abspath(path) and win.canvas.undo.isClean()
    assert [s.field for s in sg.statuses] == ["Approver"] and sg.statuses[0].intact
    assert S.has_signatures(win.canvas.doc)
    # a new box placed on the page is saved as a field, then signed (prompt answered with Save)
    from PySide6.QtWidgets import QMessageBox

    sg._place_box(ident, S.SignRequest(reason="Box"), True)
    assert sg.pending is not None and not win.canvas.undo.isClean()
    orig = QMessageBox.exec

    def answer_save(box):
        box.clickedButton = lambda: next(b for b in box.buttons()
                                         if box.standardButton(b) == QMessageBox.StandardButton.Save)
        return 0

    QMessageBox.exec = answer_save
    try:
        name = next(it.props["name"] for it in __import__("leaf.fields", fromlist=["x"]).authored_fields(win.canvas))
        sg._sign_pending(name)
    finally:
        QMessageBox.exec = orig
    wait_verified(app, win)
    assert sorted(s.field for s in sg.statuses) == sorted(["Approver", name])
    first = next(s for s in sg.statuses if s.field == "Approver")
    assert first.intact and first.changes in ("form", "none")  # the saved empty field + second signature
    assert first.changes_ok


def test_dialogs_smoke(app, win, ident, pdf):  # noqa: F811
    from leaf.sigpanel import SignDialog, TrustDialog

    S.remember_identity(win.settings, ident.path)
    S.add_trusted(win.settings, [ident.cert])
    dlg = SignDialog(win, win.settings, [("Approver", "Field “Approver” (page 1)")], False, True, "Approver", "in")
    assert dlg.place.currentData() == "Approver" and dlg.ident.currentData() == ident.path
    dlg.pw.setText("nope")
    dlg._ok()
    assert dlg.identity is None and dlg.error.text()
    dlg.pw.setText("secret")
    dlg.reason.setText("Because")
    dlg.certify.setCurrentIndex(dlg.certify.findData(2))
    dlg._ok()
    assert dlg.identity is not None and dlg.placement == "Approver" and dlg.request.certify == 2
    assert dlg.request.reason == "Because" and win.settings.value("signing/reason") == "Because"
    t = TrustDialog(win.settings, win)
    assert t.list.count() == 1
    t.list.setCurrentRow(0)
    t._remove()
    assert t.list.count() == 0 and S.trusted_certificates(win.settings) == []
