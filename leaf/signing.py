"""Digital signatures (PAdES, via pyHanko): identities, signing, verification and the trust list.

No widgets here (dialogs and the Signatures panel: sigpanel.py; wiring: features/signing.py), so the
"this will invalidate signatures" warnings elsewhere can use `has_signatures` / `signature_fields` cheaply.

Signing never re-serialises the document: pyHanko appends the signature to the saved file as an incremental
update, and everything after it (Leaf's own saves of a signed file) is incremental too (document.py).
pyHanko is imported lazily: it pulls in aiohttp/asyncio and costs ~0.5 s, which startup shouldn't pay.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import os
import sys
import tempfile
from dataclasses import dataclass, field

import pymupdf

SIG = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
CERTIFY_LEVELS = {1: "No changes allowed", 2: "Form filling and signing", 3: "Form filling, signing and comments"}


# ------------------------------------------------------------------ fields
@dataclass
class SigField:
    name: str
    page: int  # page number in the file
    rect: pymupdf.Rect  # in that page's page.rect (display) space
    signed: bool
    xref: int


def _pdf(doc) -> pymupdf.Document:
    """A pymupdf.Document from a PdfDocument (its primary source) or a pymupdf.Document."""
    prim = getattr(doc, "primary", None)
    return prim.doc if prim is not None else doc


def signature_fields(doc) -> list[SigField]:
    """Every signature field widget, signed or not (`doc`: PdfDocument or pymupdf.Document)."""
    pdf = _pdf(doc)
    if not pdf.is_form_pdf:
        return []
    out = []
    for page in pdf:
        if not page.first_widget:
            continue
        m = page.rotation_matrix
        for w in page.widgets(types=[SIG]):
            signed = pdf.xref_get_key(w.xref, "V")[0] in ("xref", "dict")
            if not signed:  # the value may sit on the parent field
                t, p = pdf.xref_get_key(w.xref, "Parent")
                if t == "xref":
                    signed = pdf.xref_get_key(int(p.split()[0]), "V")[0] in ("xref", "dict")
            out.append(SigField(w.field_name or "", page.number, (w.rect * m).normalize(), signed, w.xref))
    return out


def _signed_fields(pdf: pymupdf.Document):
    """Xrefs of signed signature fields, from the AcroForm field tree (no page loading: fast on big files)."""
    import re

    if not pdf.is_form_pdf:
        return
    t, v = pdf.xref_get_key(pdf.pdf_catalog(), "AcroForm/Fields")
    if t == "xref":
        v = pdf.xref_object(int(v.split()[0]), compressed=True)
        t = "array"
    if t != "array":
        return
    stack = [(int(x), None) for x in re.findall(r"(\d+)\s+\d+\s+R", v)]
    seen = set()
    while stack:
        x, ft = stack.pop()
        if x in seen or len(seen) > 100000:
            continue
        seen.add(x)
        try:
            tt, fv = pdf.xref_get_key(x, "FT")
        except Exception:  # noqa: BLE001  (not a dictionary)
            continue
        ft = fv if tt == "name" else ft
        if ft == "/Sig" and pdf.xref_get_key(x, "V")[0] in ("xref", "dict"):
            yield x
            continue
        tk, kids = pdf.xref_get_key(x, "Kids")
        if tk == "xref":
            kids = pdf.xref_object(int(kids.split()[0]), compressed=True)
            tk = "array"
        if tk == "array":
            stack.extend((int(k), ft) for k in re.findall(r"(\d+)\s+\d+\s+R", kids))


def has_signatures(doc) -> bool:
    """True if the document (PdfDocument or pymupdf.Document) holds at least one digital signature."""
    return next(_signed_fields(_pdf(doc)), None) is not None


def signature_count(doc) -> int:
    return sum(1 for _ in _signed_fields(_pdf(doc)))


def invalidation_warning(doc) -> str:
    """One sentence for "this rewrites the file" confirmations; empty when the document isn't signed."""
    n = signature_count(doc)
    if not n:
        return ""
    return (f"This document has {n} digital signature{'s' if n > 1 else ''}. Rewriting the file invalidates "
            f"{'them' if n > 1 else 'it'}.")


# -------------------------------------------------------------- identities
class IdentityError(Exception):
    pass


@dataclass
class Identity:
    path: str
    signer: object  # pyhanko.sign.signers.SimpleSigner

    @property
    def cert(self):
        return self.signer.signing_cert

    @property
    def name(self) -> str:
        return cert_name(self.cert)

    @property
    def subject(self) -> str:
        return self.cert.subject.human_friendly

    @property
    def not_after(self) -> _dt.datetime:
        return self.cert.not_valid_after

    @property
    def expired(self) -> bool:
        return self.not_after < _dt.datetime.now(_dt.timezone.utc)

    @property
    def self_signed(self) -> bool:
        return self.cert.self_signed in ("yes", "maybe")


def cert_name(cert) -> str:
    """Common name (or the next best thing) of an asn1crypto certificate."""
    n = cert.subject.native
    return str(n.get("common_name") or n.get("organization_name") or n.get("email_address") or cert.subject.human_friendly)


def load_identity(path: str, password: str) -> Identity:
    """A signing identity from a PKCS#12 (.p12/.pfx) file. Raises IdentityError with a readable message."""
    from pyhanko.sign import signers

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as ex:
        raise IdentityError(f"Couldn't read {os.path.basename(path)}: {ex.strerror or ex}") from None
    try:
        signer = signers.SimpleSigner.load_pkcs12_data(data, other_certs=None, passphrase=(password or "").encode())
    except Exception:  # noqa: BLE001  (pyHanko logs and re-raises various errors)
        signer = None
    if signer is None:
        raise IdentityError("Wrong password, or not a PKCS#12 file with a private key.")
    return Identity(os.path.abspath(path), signer)


def create_identity(path: str, password: str, name: str, email: str = "", organization: str = "",
                    days: int = 3 * 365, algorithm: str = "rsa") -> str:
    """Write a new self-signed signing certificate + key as a password-protected PKCS#12 file. Returns `path`.

    algorithm: "rsa" (RSA-3072) or "ec" (ECDSA P-256)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    if not name.strip():
        raise IdentityError("A name is required.")
    if not password:
        raise IdentityError("Choose a password to protect the private key.")
    key = ec.generate_private_key(ec.SECP256R1()) if algorithm == "ec" else rsa.generate_private_key(65537, 3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name.strip())]
    if organization.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization.strip()))
    if email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email.strip()))
    subject = x509.Name(attrs)
    now = _dt.datetime.now(_dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(now + _dt.timedelta(days=max(1, days)))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                      crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION,
                                               x509.ObjectIdentifier("1.3.6.1.4.1.311.10.3.12")]),  # MS doc signing
                        critical=False)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if email.strip():
        b = b.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email.strip())]), critical=False)
    cert = b.sign(key, hashes.SHA256())
    data = pkcs12.serialize_key_and_certificates(name.strip().encode(), key, cert, None,
                                                 serialization.BestAvailableEncryption(password.encode()))
    _atomic_write(path, data)
    return path


def export_certificate(identity: Identity, path: str) -> None:
    """The public certificate as PEM (to send to people who should trust your signatures)."""
    _atomic_write(path, cert_pem(identity.cert).encode())


# settings: recently used identity files (paths only; passwords are never stored)
def recent_identities(settings) -> list[str]:
    v = settings.value("signing/identities", [])
    if isinstance(v, str):
        v = [v]
    return [p for p in (v or []) if isinstance(p, str) and p]


def remember_identity(settings, path: str) -> None:
    path = os.path.abspath(path)
    rec = [p for p in recent_identities(settings) if os.path.normcase(p) != os.path.normcase(path)]
    settings.setValue("signing/identities", [path] + rec[:7])


def forget_identity(settings, path: str) -> None:
    settings.setValue("signing/identities",
                      [p for p in recent_identities(settings) if os.path.normcase(p) != os.path.normcase(path)])


# --------------------------------------------------------------- trust list
def cert_pem(cert) -> str:
    b64 = base64.encodebytes(cert.dump()).decode().replace("\n", "")
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) + \
        "\n-----END CERTIFICATE-----\n"


def cert_fingerprint(cert) -> str:
    return hashlib.sha256(cert.dump()).hexdigest()


def parse_certificates(data: bytes) -> list:
    """asn1crypto certificates from PEM (one or many) or DER bytes."""
    from asn1crypto import pem, x509

    out = []
    if pem.detect(data):
        for kind, _, der in pem.unarmor(data, multiple=True):
            if kind == "CERTIFICATE":
                out.append(x509.Certificate.load(der))
    else:
        out.append(x509.Certificate.load(data))
        out[-1].native  # noqa: B018  (parse now so garbage fails here, not during validation)
    return out


def trusted_certificates(settings) -> list:
    """Certificates the user added to Leaf's trust list."""
    from asn1crypto import x509

    out = []
    for s in _str_list(settings.value("signing/trusted", [])):
        try:
            c = x509.Certificate.load(base64.b64decode(s))
            c.native  # noqa: B018
            out.append(c)
        except Exception:  # noqa: BLE001  (skip a damaged entry)
            continue
    return out


def add_trusted(settings, certs) -> int:
    """Add certificates to the trust list; returns how many were new."""
    cur = _str_list(settings.value("signing/trusted", []))
    have = {hashlib.sha256(base64.b64decode(s)).hexdigest() for s in cur}
    n = 0
    for c in certs:
        if cert_fingerprint(c) not in have:
            cur.append(base64.b64encode(c.dump()).decode())
            have.add(cert_fingerprint(c))
            n += 1
    settings.setValue("signing/trusted", cur)
    return n


def remove_trusted(settings, fingerprint: str) -> None:
    cur = _str_list(settings.value("signing/trusted", []))
    settings.setValue("signing/trusted",
                      [s for s in cur if hashlib.sha256(base64.b64decode(s)).hexdigest() != fingerprint])


def use_system_trust(settings) -> bool:
    return settings.value("signing/system_trust", True) in (True, "true", "1", 1)


def tsa_url(settings) -> str:
    return str(settings.value("signing/tsa_url", "") or "").strip()


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v else []
    return [s for s in (v or []) if isinstance(s, str) and s]


_system_roots: list | None = None


def system_roots() -> list:
    """Root certificates of the operating system's store (cached). Windows: the ROOT store via the standard
    library; elsewhere the system CA bundle (OpenSSL's default paths), else certifi's bundle if present.
    These are TLS roots; a document signer chaining up to one of them is shown as trusted."""
    global _system_roots
    if _system_roots is not None:
        return _system_roots
    from asn1crypto import x509

    ders: list[bytes] = []
    try:
        if sys.platform == "win32":
            import ssl

            for der, enc, trust in ssl.enum_certificates("ROOT"):
                if enc == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.4" in trust or "1.3.6.1.5.5.7.3.3" in trust
                                          or "2.5.29.37.0" in trust or "1.3.6.1.5.5.7.3.1" in trust):
                    ders.append(der)
        else:
            files = []
            try:
                import ssl

                p = ssl.get_default_verify_paths()
                files += [p.cafile, p.openssl_cafile]
            except Exception:  # noqa: BLE001
                pass
            files += ["/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/cert.pem",
                      "/etc/ssl/ca-bundle.pem"]
            try:
                import certifi

                files.append(certifi.where())
            except Exception:  # noqa: BLE001
                pass
            for fn in files:
                if fn and os.path.isfile(fn):
                    with open(fn, "rb") as f:
                        from asn1crypto import pem

                        ders = [der for kind, _, der in pem.unarmor(f.read(), multiple=True) if kind == "CERTIFICATE"]
                    break
    except Exception:  # noqa: BLE001  (no system store: Leaf's own list still works)
        ders = []
    out = []
    for der in ders:
        try:
            out.append(x509.Certificate.load(der))
        except Exception:  # noqa: BLE001
            continue
    _system_roots = out
    return out


# ------------------------------------------------------------------ signing
@dataclass
class SignRequest:
    field: str | None = None  # an existing empty signature field to fill; None = a new field
    page: int = 0  # new field: page number in the file
    rect: tuple | None = None  # new field: (x0, y0, x1, y1) in that page's page.rect space; None = invisible
    reason: str = ""
    location: str = ""
    contact: str = ""
    image: bytes | None = None  # PNG of a handwritten signature for the appearance
    show_text: bool = True  # name/date/reason text in the appearance
    certify: int | None = None  # DocMDP level 1-3 (certifying signature), None = approval signature
    tsa_url: str = ""  # RFC 3161 timestamp server -> PAdES B-T; empty -> B-B
    password: str | None = None  # opens an encrypted file
    timestamper: object = None  # tests: a pyHanko TimeStamper used instead of tsa_url


class SigningError(Exception):
    pass


def unique_field_name(pdf: pymupdf.Document, base: str = "Signature") -> str:
    names = set()
    if pdf.is_form_pdf:
        for page in pdf:
            for w in page.widgets():
                names.add(w.field_name)
    i = 1
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"


def sign_pdf(src: str, dst: str, identity: Identity, req: SignRequest) -> str:
    """Sign the PDF file `src` into `dst` (may be the same path) as an incremental update. Atomic: written to a
    temp file next to `dst`, checked, then renamed. Returns the signature field name."""
    from pyhanko.pdf_utils import layout
    from pyhanko.pdf_utils.content import ImportedPdfPage
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko.stamp import StaticStampStyle

    _quiet()
    with open(src, "rb") as f:
        data = f.read()
    pdf = pymupdf.open(stream=data, filetype="pdf")
    try:
        if pdf.needs_pass and not pdf.authenticate(req.password or ""):
            raise SigningError("The document is password protected.")
        existing = {f.name: f for f in signature_fields(pdf)}
        if req.field and req.field in existing:
            sf = existing[req.field]
            if sf.signed:
                raise SigningError(f"The field “{req.field}” is already signed.")
            name, pno, disp = sf.name, sf.page, sf.rect
            box = None
        else:
            name = req.field or unique_field_name(pdf)
            pno = max(0, min(req.page, pdf.page_count - 1))
            disp = pymupdf.Rect(req.rect) if req.rect else None
            box = None
            if disp is not None:
                page = pdf[pno]
                unrot = (disp * page.derotation_matrix).normalize()
                pr = (unrot * ~page.transformation_matrix).normalize()  # MuPDF (y down) -> PDF user space (y up)
                box = (round(pr.x0, 2), round(pr.y0, 2), round(pr.x1, 2), round(pr.y1, 2))
        rotation = pdf[pno].rotation
        if req.certify and has_signatures(pdf):
            raise SigningError("Only the first signature of a document can certify it.")
    finally:
        pdf.close()

    tmpdir = tempfile.mkdtemp(prefix="leaf-sign-")
    try:
        style = None
        if disp is not None and not disp.is_empty:
            ap = os.path.join(tmpdir, "appearance.pdf")
            make_appearance(ap, disp.width, disp.height, rotation, identity.name, req)
            style = StaticStampStyle(
                background=ImportedPdfPage(ap), border_width=0, background_opacity=1.0,
                background_layout=layout.SimpleBoxLayoutRule(
                    x_align=layout.AxisAlignment.ALIGN_MID, y_align=layout.AxisAlignment.ALIGN_MID,
                    margins=layout.Margins.uniform(0), inner_content_scaling=layout.InnerScaling.STRETCH_FILL))
        meta = signers.PdfSignatureMetadata(
            field_name=name, md_algorithm="sha256", subfilter=fields.SigSeedSubFilter.PADES,
            reason=req.reason.strip() or None, location=req.location.strip() or None,
            contact_info=req.contact.strip() or None,
            certify=bool(req.certify), docmdp_permissions=fields.MDPPerm(req.certify or 2),
        )
        timestamper = req.timestamper
        if timestamper is None and req.tsa_url.strip():
            from pyhanko.sign.timestamps import HTTPTimeStamper

            timestamper = HTTPTimeStamper(req.tsa_url.strip(), timeout=15)
        spec = None
        if not (req.field and req.field in existing):
            spec = fields.SigFieldSpec(sig_field_name=name, on_page=pno, box=box)
        writer = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
        if writer.prev.encrypted:
            writer.encrypt((req.password or "").encode())
            _direct_encrypt_dict(writer)
        out = io.BytesIO()
        try:
            signers.PdfSigner(meta, identity.signer, timestamper=timestamper, stamp_style=style,
                              new_field_spec=spec).sign_pdf(writer, output=out)
        except Exception as ex:  # noqa: BLE001
            raise SigningError(_explain(ex)) from ex
        signed = out.getvalue()
    finally:
        for fn in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, fn))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
    if not signed.startswith(data):
        raise SigningError("The signature wasn't appended incrementally.")  # never happens with pyHanko
    _atomic_write(dst, signed, check=True, password=req.password)
    return name


def _direct_encrypt_dict(writer) -> None:
    """MuPDF writes /Encrypt as a direct dictionary in the trailer; pyHanko's writer expects an indirect one
    (it compares object numbers to leave the Encrypt dictionary itself unencrypted). A direct dictionary is
    never written as an object, so any number that matches no object will do."""
    from pyhanko.pdf_utils import generic

    enc = getattr(writer, "_encrypt", None)
    if isinstance(enc, generic.DictionaryObject) and not hasattr(enc, "idnum"):
        class DirectEncrypt(generic.DictionaryObject):
            idnum = -1

        writer._encrypt = DirectEncrypt(enc)


def _quiet() -> None:
    """pyHanko logs every failed check (an untrusted signer, say) with a traceback; Leaf reports results itself."""
    import logging

    for name in ("pyhanko", "pyhanko_certvalidator"):
        lg = logging.getLogger(name)
        if not lg.handlers:
            lg.addHandler(logging.NullHandler())
            lg.propagate = False


def _explain(ex: Exception) -> str:
    s = str(ex) or type(ex).__name__
    if "timestamp" in s.lower() or "aiohttp" in type(ex).__module__ or "Timestamp" in type(ex).__name__:
        return f"The timestamp server didn't answer: {s}"
    return s


def _atomic_write(path: str, data: bytes, check: bool = False, password: str | None = None) -> None:
    path = os.path.abspath(path)
    fd, tmp = tempfile.mkstemp(prefix=".leaf-", suffix=os.path.splitext(path)[1] or ".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if check:
            d = pymupdf.open(tmp)
            try:
                if d.needs_pass:
                    d.authenticate(password or "")
                if d.page_count < 1:
                    raise SigningError("Verification failed: the signed file has no pages.")
            finally:
                d.close()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def make_appearance(path: str, w: float, h: float, rotation: int, signer: str, req: SignRequest) -> None:
    """One-page PDF used as the visible signature: handwritten image (if any) + name/date/reason text.

    `w`, `h`: the box as displayed. On a page with /Rotate the content is turned so it reads upright."""
    inner = pymupdf.open()
    pg = inner.new_page(width=w, height=h)
    lines = []
    if req.show_text or not req.image:
        lines.append(f"<b>Digitally signed by {_esc(signer)}</b>")
        lines.append("Date: " + _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z"))
        if req.reason.strip():
            lines.append("Reason: " + _esc(req.reason.strip()))
        if req.location.strip():
            lines.append("Location: " + _esc(req.location.strip()))
    pad = min(4.0, w * 0.04, h * 0.08)
    area = pymupdf.Rect(pad, pad, w - pad, h - pad)
    img_r = txt_r = area
    if req.image and lines:
        if w >= h * 1.6:  # wide box: image left, text right
            img_r = pymupdf.Rect(area.x0, area.y0, area.x0 + area.width * 0.45, area.y1)
            txt_r = pymupdf.Rect(img_r.x1 + pad, area.y0, area.x1, area.y1)
        else:  # tall box: image on top
            img_r = pymupdf.Rect(area.x0, area.y0, area.x1, area.y0 + area.height * 0.55)
            txt_r = pymupdf.Rect(area.x0, img_r.y1 + pad / 2, area.x1, area.y1)
    if req.image:
        try:
            pg.insert_image(img_r, stream=req.image, keep_proportion=True)
        except Exception:  # noqa: BLE001  (unreadable image: text only)
            txt_r = area
    if lines:
        html = "<p style='margin:0;font-family:sans-serif;font-size:9pt;line-height:1.15;color:#1a1a1a'>" + \
               "<br>".join(lines) + "</p>"
        try:
            pg.insert_htmlbox(txt_r, html, scale_low=0)
        except Exception:  # noqa: BLE001
            pg.insert_textbox(txt_r, "\n".join(_strip_tags(s) for s in lines), fontsize=7)
    out = pymupdf.open()
    rot = rotation % 360
    if rot in (90, 270):
        page = out.new_page(width=h, height=w)
    else:
        page = out.new_page(width=w, height=h)
    # the field's appearance is drawn in unrotated page space; turn it back so it reads upright on screen
    page.show_pdf_page(page.rect, inner, 0, rotate=rot)
    out.save(path, garbage=3, deflate=True)
    out.close()
    inner.close()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_tags(s: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", s).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# -------------------------------------------------------------- verification
@dataclass
class SigStatus:
    field: str
    page: int | None = None
    rect: tuple | None = None  # page.rect space of `page`
    signer: str = ""
    subject: str = ""
    issuer: str = ""
    fingerprint: str = ""
    cert_pem: str = ""
    signing_time: _dt.datetime | None = None
    reason: str = ""
    location: str = ""
    contact: str = ""
    intact: bool = False  # the signed bytes are unchanged
    valid: bool = False  # ... and the cryptographic signature checks out
    trusted: bool = False
    trust: str = "unknown"  # trusted | untrusted | expired | revoked | unknown
    trust_note: str = ""
    coverage: str = ""  # whole | revision | partial
    changes: str = ""  # none | form | annotations | other  (after signing)
    changes_ok: bool | None = None  # allowed by the signature's policy (certification level)
    certify: int | None = None  # DocMDP level of a certifying signature
    timestamp: _dt.datetime | None = None
    timestamp_ok: bool | None = None
    revision: int = 0
    details: str = ""
    error: str = ""

    @property
    def state(self) -> str:
        """valid | untrusted | modified | invalid"""
        if self.error or not (self.intact and self.valid):
            return "invalid"
        if self.coverage != "whole" and not self.changes_ok:
            return "modified"
        return "valid" if self.trusted else "untrusted"

    def headline(self) -> str:
        who = self.signer or "unknown signer"
        return {
            "valid": f"Signed by {who} — valid",
            "untrusted": f"Signed by {who} — signature intact, signer not trusted",
            "modified": f"Signed by {who} — modified after signing",
            "invalid": f"Signed by {who} — invalid" + ("" if self.error or self.intact else ": changed after signing"),
        }[self.state]

    def trust_text(self) -> str:
        return {
            "trusted": "Trusted",
            "untrusted": "Unknown issuer (not in the trust list)",
            "expired": "Certificate expired or not yet valid",
            "revoked": "Certificate revoked",
            "unknown": "Couldn't be checked",
        }.get(self.trust, self.trust) + (f" — {self.trust_note}" if self.trust_note else "")

    def changes_text(self) -> str:
        if self.coverage == "whole":
            return "None: the signature covers the whole document"
        if self.coverage == "partial":
            return "The signature doesn't cover the document it is in"
        what = {"none": "no content changes (signature/timestamp data only)", "form": "form filling or signing",
                "annotations": "comments/annotations", "other": "changes not permitted for a signed document"}
        s = "Later revisions: " + what.get(self.changes, self.changes or "not analysed")
        if self.changes_ok is True:
            s += " — allowed"
        elif self.changes_ok is False:
            s += " — not allowed" + (f" by the certification ({CERTIFY_LEVELS.get(self.certify, '')})"
                                     if self.certify and self.changes != "other" else "")
        return s


def verify_pdf(data: bytes, trusted=(), system: bool = False, password: str | None = None) -> list[SigStatus]:
    """Validate every signature in a PDF (bytes). `trusted`: extra trust anchors (asn1crypto certificates);
    `system`: also trust the OS root store. Never goes online (no OCSP/CRL fetching)."""
    import asyncio

    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import async_validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    _quiet()
    reader = PdfFileReader(io.BytesIO(data), strict=False)
    if reader.encrypted:
        reader.decrypt((password or "").encode())
    sigs = list(reader.embedded_signatures)
    if not sigs:
        return []
    roots = list(trusted) + (system_roots() if system else [])
    places = _field_places(data, password)
    policy = leaf_diff_policy()
    out = []

    async def run_all():
        for s in sigs:
            out.append(await _verify_one(s, roots, places, policy, ValidationContext, async_validate_pdf_signature))

    asyncio.run(run_all())
    return out


async def _verify_one(s, roots, places, policy, ValidationContext, validate) -> SigStatus:
    from pyhanko.sign.diff_analysis import ModificationLevel
    from pyhanko.sign.validation.status import SignatureCoverageLevel

    st = SigStatus(field=s.field_name or "")
    st.page, st.rect = places.get(st.field, (None, None))
    try:
        sd = s.sig_object
        st.reason = str(_get(sd, "/Reason", "") or "")
        st.location = str(_get(sd, "/Location", "") or "")
        st.contact = str(_get(sd, "/ContactInfo", "") or "")
        st.revision = s.signed_revision
        cert = s.signer_cert
        st.signer = cert_name(cert)
        st.subject = cert.subject.human_friendly
        st.issuer = cert.issuer.human_friendly
        st.fingerprint = cert_fingerprint(cert)
        st.cert_pem = cert_pem(cert)
        st.signing_time = s.self_reported_timestamp
        lvl = s.docmdp_level  # a certifying signature's DocMDP (or a field lock's /P)
        st.certify = int(lvl.value) if lvl is not None and _is_certification(s) else None
    except Exception as ex:  # noqa: BLE001
        st.error = f"Couldn't read the signature: {ex}"
        return st
    try:
        vc = ValidationContext(trust_roots=roots, allow_fetching=False, revocation_mode="soft-fail")
        r = await validate(s, signer_validation_context=vc, diff_policy=policy)
    except Exception as ex:  # noqa: BLE001
        st.error = f"Validation failed: {ex}"
        return st
    st.intact, st.valid, st.trusted = bool(r.intact), bool(r.valid), bool(r.trusted)
    st.coverage = {SignatureCoverageLevel.ENTIRE_FILE: "whole", SignatureCoverageLevel.ENTIRE_REVISION: "revision"}.get(
        r.coverage, "partial")
    ml = r.modification_level
    if ml is not None and st.coverage != "whole":
        st.changes = {ModificationLevel.NONE: "none", ModificationLevel.LTA_UPDATES: "none",
                      ModificationLevel.FORM_FILLING: "form", ModificationLevel.ANNOTATIONS: "annotations"}.get(ml, "other")
    st.changes_ok = r.docmdp_ok
    if st.coverage == "whole":
        st.changes, st.changes_ok = "none", True
    tsv = r.timestamp_validity
    if tsv is not None:
        st.timestamp = tsv.timestamp
        st.timestamp_ok = bool(tsv.intact and tsv.valid)
    cert = s.signer_cert
    when = st.timestamp if st.timestamp_ok else _dt.datetime.now(_dt.timezone.utc)
    in_validity = cert.not_valid_before <= when <= cert.not_valid_after
    if r.revoked:
        st.trust = "revoked"
    elif st.trusted and not in_validity:  # a trust anchor's own validity isn't part of path validation
        st.trusted, st.trust = False, "expired"
    elif st.trusted:
        st.trust = "trusted"
        st.trust_note = "revocation not checked (offline)"
    else:
        ind = str(getattr(r, "trust_problem_indic", "") or "")
        if "OUT_OF_BOUNDS" in ind or "EXPIRED" in ind or not in_validity:
            st.trust = "expired"
        elif "REVOK" in ind:
            st.trust = "revoked" if "FAILURE" in ind else "unknown"
        else:
            st.trust = "untrusted"
            if cert.self_signed in ("yes", "maybe"):
                st.trust_note = "self-signed"
    try:
        st.details = r.pretty_print_details()
    except Exception:  # noqa: BLE001
        st.details = r.summary()
    return st


def _is_certification(s) -> bool:
    try:
        return any(_get(ref.get_object(), "/TransformMethod") == "/DocMDP"
                   for ref in _get(s.sig_object, "/Reference", []))
    except Exception:  # noqa: BLE001
        return False


def _get(d, key, default=None):
    """d[key] dereferenced (pyHanko's DictionaryObject.get returns indirect references as they are)."""
    try:
        return d[key] if key in d else default
    except Exception:  # noqa: BLE001
        return default


def _field_places(data: bytes, password: str | None) -> dict[str, tuple]:
    """Field name -> (page number, rect in page.rect space) for signature widgets."""
    out = {}
    try:
        d = pymupdf.open(stream=data, filetype="pdf")
        if d.needs_pass:
            d.authenticate(password or "")
        for f in signature_fields(d):
            out.setdefault(f.name, (f.page, None if f.rect.is_empty else tuple(f.rect)))
        d.close()
    except Exception:  # noqa: BLE001
        pass
    return out


# ------------------------------------------------- difference analysis policy
_POLICY = None


def leaf_diff_policy():
    """pyHanko's default policy plus two rules for what Leaf (MuPDF) writes after a signature:

    - form values in fields whose widget is a separate /Kids entry (pyHanko only follows /AP changes on merged
      field/widget dictionaries; MuPDF always creates kids), and
    - comments: annotations added, changed or removed on a page whose dictionary otherwise stays the same."""
    global _POLICY
    if _POLICY is None:
        from pyhanko.sign.diff_analysis import DEFAULT_DIFF_POLICY, StandardDiffPolicy

        d = DEFAULT_DIFF_POLICY
        _POLICY = StandardDiffPolicy(
            global_rules=[*d.global_rules, _widget_rule(), _annot_rule()], form_rule=d.form_rule,
            reject_object_freeing=d.reject_object_freeing, ignore_orphaned_objects=d.ignore_orphaned_objects,
            ignore_identical_objects=getattr(d, "ignore_identical_objects", True))
    return _POLICY


def _plain(d, skip: set) -> dict:
    """A dictionary's entries for comparison, minus `skip` keys and null values (MuPDF writes `/K null` for
    removed keys)."""
    from pyhanko.pdf_utils import generic

    return {k: v for k, v in d.items() if k not in skip and not isinstance(v, generic.NullObject)}


def _same(a, b, skip: set) -> bool:
    try:
        return _plain(a, skip) == _plain(b, skip)
    except Exception:  # noqa: BLE001
        return False


def _widget_rule():
    from pyhanko.pdf_utils import generic
    from pyhanko.sign.diff_analysis import ModificationLevel, QualifiedWhitelistRule, ReferenceUpdate
    from pyhanko.sign.diff_analysis.rules.form_field_rules import _allow_appearance_update

    class KidWidgetAppearanceRule(QualifiedWhitelistRule):
        def apply_qualified(self, old, new):
            changed = new.explicit_refs_in_revision()
            if not changed:
                return
            try:
                fields = new.root["/AcroForm"]["/Fields"]
            except (KeyError, TypeError):
                return
            stack = [(f, None) for f in fields]
            seen = set()
            while stack:
                ref, ft = stack.pop()
                if not isinstance(ref, generic.IndirectObject) or ref.reference in seen:
                    continue
                seen.add(ref.reference)
                obj = ref.get_object()
                if not isinstance(obj, generic.DictionaryObject):
                    continue
                ft = _get(obj, "/FT", ft)
                if "/T" in obj:
                    for k in _get(obj, "/Kids", []):
                        stack.append((k, ft))
                    continue
                # a widget kid of a (non-signature) terminal field
                if ft == "/Sig" or ref.reference not in changed:
                    continue
                try:
                    prev = old(ref.reference)
                except Exception:  # noqa: BLE001  (new object: not ours to explain)
                    continue
                if not isinstance(prev, generic.DictionaryObject) or not _same(prev, obj, {"/AP", "/AS"}):
                    continue
                try:
                    deps = list(_allow_appearance_update(prev, obj, old, new))
                except Exception:  # noqa: BLE001  (SuspiciousModification: leave it unexplained)
                    continue
                yield ModificationLevel.FORM_FILLING, ReferenceUpdate(ref.reference)
                for r in deps:
                    yield ModificationLevel.FORM_FILLING, ReferenceUpdate(r)

    return KidWidgetAppearanceRule()


def _annot_rule():
    from pyhanko.pdf_utils import generic
    from pyhanko.sign.diff_analysis import ModificationLevel, QualifiedWhitelistRule, ReferenceUpdate

    links = {"/P", "/Popup", "/IRT", "/Parent"}  # don't follow these into the page or other annotations

    def deps(new, obj, since):
        out = set()
        for k, v in obj.items():
            if k not in links:
                out |= new.collect_dependencies(v, since_revision=since)
        return out

    def is_widget(o) -> bool:
        return isinstance(o, generic.DictionaryObject) and _get(o, "/Subtype") == "/Widget"

    class AnnotationRule(QualifiedWhitelistRule):
        def apply_qualified(self, old, new):
            changed = new.explicit_refs_in_revision()
            if not changed:
                return
            since = old.revision + 1
            level = ModificationLevel.ANNOTATIONS
            stack = [new.root.raw_get("/Pages")]
            seen = set()
            while stack:
                pref = stack.pop()
                if not isinstance(pref, generic.IndirectObject) or pref.reference in seen:
                    continue
                seen.add(pref.reference)
                page = pref.get_object()
                if _get(page, "/Type") == "/Pages":
                    stack.extend(_get(page, "/Kids", []))
                    continue
                try:
                    prev = old(pref.reference)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(prev, generic.DictionaryObject):
                    continue
                new_annots_raw = page.raw_get("/Annots") if "/Annots" in page else None
                arr_changed = isinstance(new_annots_raw, generic.IndirectObject) and \
                    new_annots_raw.reference in changed
                if pref.reference not in changed and not arr_changed:
                    # the page and its /Annots array are as signed; annotations edited in place?
                    for a in _get(page, "/Annots", []):
                        if isinstance(a, generic.IndirectObject) and a.reference in changed:
                            yield from self._edited(old, new, a, since, level)
                    continue
                if pref.reference in changed and not _same(prev, page, {"/Annots"}):
                    continue  # page content or geometry changed: not a comment
                old_refs = [a.reference for a in _get(prev, "/Annots", []) if isinstance(a, generic.IndirectObject)]
                new_refs = [a for a in _get(page, "/Annots", []) if isinstance(a, generic.IndirectObject)]
                new_set = {a.reference for a in new_refs}
                if any(r not in new_set and is_widget(old(r)) for r in old_refs):
                    continue  # a form field's widget disappeared
                ok = []
                for a in new_refs:
                    if a.reference in old_refs:
                        if a.reference in changed:
                            ok.extend(self._edited(old, new, a, since, level))
                        continue
                    obj = a.get_object()
                    if is_widget(obj) or not isinstance(obj, generic.DictionaryObject):
                        break  # new fields go through the form rules
                    ok.append((level, ReferenceUpdate(a.reference)))
                    ok.extend((level, ReferenceUpdate(r)) for r in deps(new, obj, since))
                else:
                    if pref.reference in changed:
                        yield level, ReferenceUpdate(pref.reference)
                    if arr_changed:
                        yield level, ReferenceUpdate(new_annots_raw.reference)
                    yield from ok

        @staticmethod
        def _edited(old, new, a, since, level):
            try:
                prev = old(a.reference)
            except Exception:  # noqa: BLE001
                return
            obj = a.get_object()
            if is_widget(prev) or is_widget(obj) or not isinstance(obj, generic.DictionaryObject):
                return
            yield level, ReferenceUpdate(a.reference)
            for r in deps(new, obj, since):
                yield level, ReferenceUpdate(r)

    return AnnotationRule()
