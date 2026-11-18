"""Tesseract language data: the English file shipped with Leaf, and the languages the user downloads.

Qt-free except `user_dir()` (QStandardPaths, imported when called). Leaf ships `eng.traineddata` from
tessdata_fast (Apache-2.0, https://github.com/tesseract-ocr/tessdata_fast) in leaf/data/tessdata, so text
recognition works out of the box with MuPDF's built-in Tesseract. More languages are downloaded only when the
user asks for them (Add languages… in the OCR dialog) into AppLocalDataLocation/tessdata. Only the language
file is fetched; no document content is ever sent anywhere.

Downloads: HTTPS with urllib (Qt's network module is not part of the packaged app), size sanity checks, a
check of the traineddata container header, and an atomic write (`<code>.traineddata.part` + os.replace).
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import sys

BASE_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/"
SOURCE = "tessdata_fast (github.com/tesseract-ocr/tessdata_fast, Apache-2.0)"
MIN_SIZE = 50_000          # the smallest tessdata_fast language file is a few hundred KB
MAX_SIZE = 64 * 2**20      # the largest is well under this
CHUNK = 64 * 1024
TIMEOUT = 30

# tessdata_fast languages: (code, name, script). osd/equ (not languages) are left out.
LANGUAGES: list[tuple[str, str, str]] = [
    ("afr", "Afrikaans", "Latin"), ("amh", "Amharic", "Ethiopic"), ("ara", "Arabic", "Arabic"),
    ("asm", "Assamese", "Bengali"), ("aze", "Azerbaijani", "Latin"), ("aze_cyrl", "Azerbaijani (Cyrillic)", "Cyrillic"),
    ("bel", "Belarusian", "Cyrillic"), ("ben", "Bengali", "Bengali"), ("bod", "Tibetan", "Tibetan"),
    ("bos", "Bosnian", "Latin"), ("bre", "Breton", "Latin"), ("bul", "Bulgarian", "Cyrillic"),
    ("cat", "Catalan", "Latin"), ("ceb", "Cebuano", "Latin"), ("ces", "Czech", "Latin"),
    ("chi_sim", "Chinese (Simplified)", "Han"), ("chi_sim_vert", "Chinese (Simplified, vertical)", "Han"),
    ("chi_tra", "Chinese (Traditional)", "Han"), ("chi_tra_vert", "Chinese (Traditional, vertical)", "Han"),
    ("chr", "Cherokee", "Cherokee"), ("cos", "Corsican", "Latin"), ("cym", "Welsh", "Latin"),
    ("dan", "Danish", "Latin"), ("deu", "German", "Latin"), ("div", "Dhivehi", "Thaana"),
    ("dzo", "Dzongkha", "Tibetan"), ("ell", "Greek", "Greek"), ("eng", "English", "Latin"),
    ("enm", "English (Middle)", "Latin"), ("epo", "Esperanto", "Latin"), ("est", "Estonian", "Latin"),
    ("eus", "Basque", "Latin"), ("fao", "Faroese", "Latin"), ("fas", "Persian", "Arabic"),
    ("fil", "Filipino", "Latin"), ("fin", "Finnish", "Latin"), ("fra", "French", "Latin"),
    ("frk", "German (Fraktur)", "Latin"), ("frm", "French (Middle)", "Latin"), ("fry", "Frisian", "Latin"),
    ("gla", "Scottish Gaelic", "Latin"), ("gle", "Irish", "Latin"), ("glg", "Galician", "Latin"),
    ("grc", "Greek (Ancient)", "Greek"), ("guj", "Gujarati", "Gujarati"), ("hat", "Haitian Creole", "Latin"),
    ("heb", "Hebrew", "Hebrew"), ("hin", "Hindi", "Devanagari"), ("hrv", "Croatian", "Latin"),
    ("hun", "Hungarian", "Latin"), ("hye", "Armenian", "Armenian"), ("iku", "Inuktitut", "Canadian Syllabics"),
    ("ind", "Indonesian", "Latin"), ("isl", "Icelandic", "Latin"), ("ita", "Italian", "Latin"),
    ("ita_old", "Italian (Old)", "Latin"), ("jav", "Javanese", "Latin"), ("jpn", "Japanese", "Japanese"),
    ("jpn_vert", "Japanese (vertical)", "Japanese"), ("kan", "Kannada", "Kannada"), ("kat", "Georgian", "Georgian"),
    ("kat_old", "Georgian (Old)", "Georgian"), ("kaz", "Kazakh", "Cyrillic"), ("khm", "Khmer", "Khmer"),
    ("kir", "Kyrgyz", "Cyrillic"), ("kmr", "Kurdish (Kurmanji)", "Latin"), ("kor", "Korean", "Hangul"),
    ("kor_vert", "Korean (vertical)", "Hangul"), ("lao", "Lao", "Lao"), ("lat", "Latin", "Latin"),
    ("lav", "Latvian", "Latin"), ("lit", "Lithuanian", "Latin"), ("ltz", "Luxembourgish", "Latin"),
    ("mal", "Malayalam", "Malayalam"), ("mar", "Marathi", "Devanagari"), ("mkd", "Macedonian", "Cyrillic"),
    ("mlt", "Maltese", "Latin"), ("mon", "Mongolian", "Cyrillic"), ("mri", "Maori", "Latin"),
    ("msa", "Malay", "Latin"), ("mya", "Burmese", "Myanmar"), ("nep", "Nepali", "Devanagari"),
    ("nld", "Dutch", "Latin"), ("nor", "Norwegian", "Latin"), ("oci", "Occitan", "Latin"),
    ("ori", "Odia", "Odia"), ("pan", "Punjabi", "Gurmukhi"), ("pol", "Polish", "Latin"),
    ("por", "Portuguese", "Latin"), ("pus", "Pashto", "Arabic"), ("que", "Quechua", "Latin"),
    ("ron", "Romanian", "Latin"), ("rus", "Russian", "Cyrillic"), ("san", "Sanskrit", "Devanagari"),
    ("sin", "Sinhala", "Sinhala"), ("slk", "Slovak", "Latin"), ("slv", "Slovenian", "Latin"),
    ("snd", "Sindhi", "Arabic"), ("spa", "Spanish", "Latin"), ("spa_old", "Spanish (Old)", "Latin"),
    ("sqi", "Albanian", "Latin"), ("srp", "Serbian (Cyrillic)", "Cyrillic"), ("srp_latn", "Serbian (Latin)", "Latin"),
    ("sun", "Sundanese", "Latin"), ("swa", "Swahili", "Latin"), ("swe", "Swedish", "Latin"),
    ("syr", "Syriac", "Syriac"), ("tam", "Tamil", "Tamil"), ("tat", "Tatar", "Cyrillic"),
    ("tel", "Telugu", "Telugu"), ("tgk", "Tajik", "Cyrillic"), ("tha", "Thai", "Thai"),
    ("tir", "Tigrinya", "Ethiopic"), ("ton", "Tongan", "Latin"), ("tur", "Turkish", "Latin"),
    ("uig", "Uyghur", "Arabic"), ("ukr", "Ukrainian", "Cyrillic"), ("urd", "Urdu", "Arabic"),
    ("uzb", "Uzbek", "Latin"), ("uzb_cyrl", "Uzbek (Cyrillic)", "Cyrillic"), ("vie", "Vietnamese", "Latin"),
    ("yid", "Yiddish", "Hebrew"), ("yor", "Yoruba", "Latin"),
]
NAMES = {c: n for c, n, _s in LANGUAGES}
SCRIPTS = {c: s for c, _n, s in LANGUAGES}
_CODE = re.compile(r"^[a-z]{3}(_[a-z]{3,4})?$")


def label(code: str) -> str:
    return f"{NAMES[code]} ({code})" if code in NAMES else code


# ------------------------------------------------------------------ folders
def bundled_dir() -> str | None:
    """leaf/data/tessdata: next to the package in a source tree, under sys._MEIPASS in a frozen build."""
    cands = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "leaf", "data", "tessdata"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tessdata"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "eng.traineddata")):
            return os.path.abspath(c)
    return None


def user_dir() -> str:
    """Where downloaded languages go (not created here)."""
    from PySide6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return os.path.join(base or os.path.expanduser("~/.leaf"), "tessdata")


def languages_in(folder: str | None) -> list[str]:
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        code, ext = os.path.splitext(name)
        if ext == ".traineddata" and code not in ("osd", "equ") and os.path.isfile(os.path.join(folder, name)):
            out.append(code)
    return sorted(out)


# ------------------------------------------------------------------ download
class DownloadError(Exception):
    pass


class Cancelled(DownloadError):
    pass


def url_for(code: str) -> str:
    return BASE_URL + f"{code}.traineddata"


def check_code(code: str) -> str:
    if code not in NAMES or not _CODE.match(code):
        raise DownloadError(f"Unknown language {code!r}")
    return code


def looks_like_traineddata(path: str) -> bool:
    """Tesseract's container: int32 entry count, then that many int64 offsets (-1 = absent, else in the file)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4 + 8 * 64)
        if len(head) < 12:
            return False
        (n,) = struct.unpack_from("<i", head, 0)
        if not 1 <= n <= 64 or len(head) < 4 + 8 * n:
            return False
        offs = struct.unpack_from(f"<{n}q", head, 4)
        present = [o for o in offs if o != -1]
        return bool(present) and all(0 < o < size for o in present)
    except (OSError, struct.error):
        return False


def _ssl_context():
    import ssl

    ctx = ssl.create_default_context()
    try:  # frozen Linux/macOS builds may not find the system CA store: add certifi's when it is there
        import certifi

        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


def _https_only_handler():
    """urllib follows redirects to http:// (and ftp://) too; GitHub's raw links redirect, so each hop is checked."""
    import urllib.request

    class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not str(newurl).lower().startswith("https://"):
                raise DownloadError(f"The download was redirected to a non-HTTPS address ({newurl})")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return HttpsOnlyRedirects()


def _open(url: str):
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Leaf-PDF-editor"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()), _https_only_handler())
    return opener.open(req, timeout=TIMEOUT)


def download(code: str, dest: str, progress=None, cancelled=None, opener=None) -> str:
    """Fetch `<code>.traineddata` into `dest` and return its path. `progress(done, total or 0)` is called while
    reading, `cancelled()` is polled between chunks (raises Cancelled). `opener(url)` returns a file-like
    response (tests pass a fake). Nothing is left behind on failure. Also makes sure `dest` holds English,
    so combinations like eng+hin find all their files in one folder."""
    check_code(code)
    url = url_for(code)
    if not url.startswith("https://"):
        raise DownloadError("Only HTTPS downloads are allowed")
    os.makedirs(dest, exist_ok=True)
    final = os.path.join(dest, f"{code}.traineddata")
    part = final + ".part"
    try:
        with (opener or _open)(url) as r:
            total = 0
            try:
                total = int((r.headers.get("Content-Length") if getattr(r, "headers", None) else 0) or 0)
            except (TypeError, ValueError):
                total = 0
            if total and not MIN_SIZE <= total <= MAX_SIZE:
                raise DownloadError(f"Unexpected size for {label(code)}: {total} bytes")
            done = 0
            with open(part, "wb") as f:
                while True:
                    if cancelled is not None and cancelled():
                        raise Cancelled("Cancelled")
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_SIZE:
                        raise DownloadError(f"{label(code)}: the download is larger than expected")
                    f.write(chunk)
                    if progress is not None:
                        progress(done, total)
        if done < MIN_SIZE or (total and done != total):
            raise DownloadError(f"{label(code)}: incomplete download ({done} bytes)")
        if not looks_like_traineddata(part):
            raise DownloadError(f"{label(code)}: the downloaded file isn't Tesseract language data")
        os.replace(part, final)
    except DownloadError:
        _remove_quietly(part)
        raise
    except Exception as e:  # noqa: BLE001 - network/disk errors -> one readable message
        _remove_quietly(part)
        raise DownloadError(f"Couldn't download {label(code)}: {e}") from e
    ensure_english(dest)
    return final


def ensure_english(dest: str) -> None:
    """Copy the shipped English file into `dest` (so languages there combine with English)."""
    src = bundled_dir()
    target = os.path.join(dest, "eng.traineddata")
    if src and not os.path.exists(target):
        try:
            shutil.copyfile(os.path.join(src, "eng.traineddata"), target + ".part")
            os.replace(target + ".part", target)
        except OSError:
            _remove_quietly(target + ".part")


def remove(code: str, dest: str) -> None:
    check_code(code)
    os.remove(os.path.join(dest, f"{code}.traineddata"))
    if languages_in(dest) == ["eng"]:  # only the English copy is left: drop it too (the shipped one remains)
        _remove_quietly(os.path.join(dest, "eng.traineddata"))


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
