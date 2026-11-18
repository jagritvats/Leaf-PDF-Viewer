"""OCR out of the box: the shipped English Tesseract data (source tree and frozen lookup), recognition with it
(in-process and in the worker process), engine choice per language, and the language manager (downloads through
a fake opener: no network in tests)."""
from __future__ import annotations

import io
import os
import struct
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PySide6.QtCore import QSettings, Qt

from leaf import ocr as O
from leaf import ocrdata as D
from tests.test_editor import app, canvas_for, make_pdf, pump  # noqa: F401
from tests.test_ocr import make_scanned


def _texts(words):
    return {w[4].strip(".,").lower() for w in words}


def fake_traineddata(size=D.MIN_SIZE + 1000) -> bytes:
    """A minimal container that passes the header check (3 entries, two present)."""
    head = struct.pack("<i3q", 3, -1, 28, 500)
    return head + b"\0" * (size - len(head))


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, length: int | None = -1):
        super().__init__(data)
        n = len(data) if length == -1 else length
        self.headers = {"Content-Length": str(n)} if n is not None else {}


def opener_for(data: bytes, length: int | None = -1, urls: list | None = None):
    def op(url):
        if urls is not None:
            urls.append(url)
        return FakeResponse(data, length)
    return op


# ------------------------------------------------------------ shipped data
def test_bundled_eng_in_source_tree():
    d = D.bundled_dir()
    assert d and os.path.isfile(os.path.join(d, "eng.traineddata"))
    assert D.looks_like_traineddata(os.path.join(d, "eng.traineddata"))
    assert os.path.getsize(os.path.join(d, "eng.traineddata")) < 6 * 2**20  # tessdata_fast, not _best


def test_bundled_lookup_frozen(tmp_path, monkeypatch):
    meipass = tmp_path / "_internal"
    td = meipass / "leaf" / "data" / "tessdata"
    td.mkdir(parents=True)
    (td / "eng.traineddata").write_bytes(fake_traineddata())
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    assert os.path.normcase(D.bundled_dir()) == os.path.normcase(str(td))
    (td / "eng.traineddata").unlink()  # nothing under _MEIPASS: the package folder is still tried
    assert D.bundled_dir() and "_internal" not in D.bundled_dir()


def test_spec_bundles_tessdata():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "packaging", "leaf.spec"), encoding="utf-8") as f:
        spec = f.read()
    assert '"eng.traineddata"' in spec and "_tessdata" in spec and "datas=_ocr_datas + _tessdata" in spec


def test_tesseract_recognizes_with_shipped_eng(tmp_path):
    path = str(tmp_path / "scan.pdf")
    make_scanned(path)
    eng = O.create_engine("tesseract", D.bundled_dir())
    with open(path, "rb") as f:
        data = f.read()
    res = {i: (w, s) for i, w, s in O.recognize_document(data, [0, 1], eng, "eng", dpi=200)}
    print(f"\nTesseract (eng, tessdata_fast): {res[0][1]:.2f} s + {res[1][1]:.2f} s per page at 200 dpi")
    assert {"quick", "brown", "lazy", "invoice", "12345", "payable"} <= _texts(res[0][0])
    assert {"rotated", "scan", "sample"} <= _texts(res[1][0])  # /Rotate 90 page, words in display space
    w = next(w for w in res[0][0] if w[4].lower() == "quick")  # baseline y=100, 16 pt
    assert 80 < w[1] < 95 and 98 < w[3] < 112 and 100 < w[0] < 200
    r = next(w for w in res[1][0] if w[4].lower() == "rotated")
    assert r[0] < 200 and r[1] < 150  # upright as displayed: top left


def test_tesseract_in_worker_process(tmp_path):
    from leaf.ocr_worker import OcrProcess

    path = str(tmp_path / "small.pdf")
    make_scanned(path, small=True)
    t0 = time.perf_counter()
    proc = OcrProcess("tesseract", D.bundled_dir(), "eng", 200, False, {1: (path, None)}, [("p", 1, 0)])
    words, end = None, None
    try:
        while time.perf_counter() - t0 < 90 and end is None:
            for msg in proc.poll():
                if msg[0] == "page":
                    words = msg[2]
                elif msg[0] in ("done", "fatal", "error"):
                    end = msg
            time.sleep(0.05)
    finally:
        proc.close()
    assert end and end[0] == "done", end
    assert {"leaf", "scanned", "sample"} <= _texts(words)


# ------------------------------------------------------------ engine choice
def _engines(tmp_path, rapid=True, split=False):
    a, b = tmp_path / "user", tmp_path / "shipped"
    a.mkdir(exist_ok=True)
    b.mkdir(exist_ok=True)
    (b / "eng.traineddata").write_bytes(b"x")
    (a / "hin.traineddata").write_bytes(b"x")
    if not split:
        (a / "eng.traineddata").write_bytes(b"x")
    folders = [str(a), str(b)]
    out = []
    if rapid:
        out.append(O.EngineInfo("rapidocr", "RapidOCR", [(c, c) for c in sorted(O.RAPID_LANGS)], "eng"))
    langs = sorted({c for d in folders for c in O.tesseract_languages(d)})
    out.append(O.EngineInfo("tesseract", "Tesseract", [(c, c) for c in langs], "eng", tessdata=folders[0],
                            free_language=True, folders=folders))
    return out, str(a), str(b)


def test_engine_choice_rules(tmp_path):
    es, user, shipped = _engines(tmp_path)
    assert O.choose_engine(es, "eng").key == "rapidocr"  # bundled + supported: RapidOCR
    assert O.choose_engine(es, "hin").key == "tesseract"  # RapidOCR doesn't read Devanagari
    assert O.choose_engine(es, "eng+hin").key == "tesseract"
    assert O.choose_engine(es, "eng", "tesseract").key == "tesseract"  # remembered choice wins when it can
    assert O.choose_engine(es, "hin", "rapidocr").key == "tesseract"  # ... and falls back when it can't
    assert O.choose_engine(es, "kor") is None  # not installed anywhere
    assert O.choose_engine(es, "auto").key == "rapidocr"
    t = es[1]
    assert t.tessdata_for("eng") == user and t.tessdata_for("hin+eng") == user
    (tmp_path / "b").mkdir()
    no_rapid = _engines(tmp_path / "b", rapid=False)[0]
    assert O.choose_engine(no_rapid, "eng").key == "tesseract"
    assert [c for c, _ in O.language_choices(es)][0] == "eng" and "hin" in dict(O.language_choices(es))


def test_combination_needs_one_folder(tmp_path):
    es, user, shipped = _engines(tmp_path, rapid=False, split=True)
    t = es[0]
    assert t.tessdata_for("eng") == shipped and t.tessdata_for("hin") == user
    assert t.tessdata_for("eng+hin") is None and O.choose_engine(es, "eng+hin") is None


def test_available_engines_use_shipped_and_user_data(tmp_path, monkeypatch):
    user = tmp_path / "tessdata"
    monkeypatch.setattr(D, "user_dir", lambda: str(user))
    monkeypatch.setattr(O, "find_tessdata", lambda: None)
    monkeypatch.setattr(O, "rapidocr_available", lambda: False)
    es = O.available_engines()
    assert [e.key for e in es] == ["tesseract"] and es[0].default_language == "eng"
    assert es[0].folders == [D.bundled_dir()]
    D.download("hin", str(user), opener=opener_for(fake_traineddata()))
    t = O.available_engines()[0]
    assert t.folders[0] == str(user) and {"eng", "hin"} <= dict(t.languages).keys()
    assert O.choose_engine([t], "eng+hin").tessdata_for("eng+hin") == str(user)  # English was copied along
    monkeypatch.setattr(O, "rapidocr_available", lambda: True)
    es = O.available_engines()
    assert O.choose_engine(es, "hin").key == "tesseract" and O.choose_engine(es, "deu").key == "rapidocr"


# ------------------------------------------------------------ downloads
def test_download_ok_atomic_and_progress(tmp_path):
    dest = tmp_path / "td"
    seen, urls = [], []
    data = fake_traineddata(300_000)
    path = D.download("hin", str(dest), progress=lambda d, t: seen.append((d, t)), opener=opener_for(data, urls=urls))
    assert urls == ["https://github.com/tesseract-ocr/tessdata_fast/raw/main/hin.traineddata"]
    with open(path, "rb") as f:
        assert f.read() == data
    assert seen[-1] == (len(data), len(data)) and len(seen) > 1
    assert sorted(os.listdir(dest)) == ["eng.traineddata", "hin.traineddata"]  # no .part left


@pytest.mark.parametrize("data,length,msg", [
    (b"<html>rate limited</html>" * 4000, -1, "isn't Tesseract language data"),
    (fake_traineddata(), 10, "Unexpected size"),
    (fake_traineddata(), D.MAX_SIZE + 1, "Unexpected size"),
    (fake_traineddata(D.MIN_SIZE + 5000), D.MIN_SIZE + 9000, "incomplete"),
    (b"\x03\0\0\0" + b"\0" * 100, None, "incomplete"),
])
def test_download_rejects_bad_data(tmp_path, data, length, msg):
    dest = tmp_path / "td"
    with pytest.raises(D.DownloadError, match=msg):
        D.download("tam", str(dest), opener=opener_for(data, length))
    assert not os.path.exists(dest / "tam.traineddata") and not os.path.exists(dest / "tam.traineddata.part")


def test_download_errors_and_cancel(tmp_path):
    dest = str(tmp_path / "td")
    for bad in ("../evil", "eng/../x", "xyz", ""):
        with pytest.raises(D.DownloadError):
            D.download(bad, dest, opener=opener_for(fake_traineddata()))

    def boom(url):
        raise OSError("network is unreachable")
    with pytest.raises(D.DownloadError, match="unreachable"):
        D.download("ara", dest, opener=boom)
    calls = []
    with pytest.raises(D.Cancelled):
        D.download("ara", dest, opener=opener_for(fake_traineddata(400_000)),
                   cancelled=lambda: calls.append(1) or len(calls) > 2)
    assert not any(n.startswith("ara") for n in os.listdir(dest))


def test_remove_language(tmp_path):
    dest = str(tmp_path / "td")
    for c in ("hin", "rus"):
        D.download(c, dest, opener=opener_for(fake_traineddata()))
    assert D.languages_in(dest) == ["eng", "hin", "rus"]
    D.remove("hin", dest)
    assert D.languages_in(dest) == ["eng", "rus"]
    D.remove("rus", dest)
    assert D.languages_in(dest) == []  # the English copy goes with the last language


def test_language_list():
    codes = [c for c, _n, _s in D.LANGUAGES]
    assert len(codes) == len(set(codes)) > 100
    for c in ("eng", "hin", "ara", "rus", "chi_sim", "jpn", "kor", "ben", "tam", "heb", "tha", "ell", "mar"):
        assert c in D.NAMES
    assert D.label("hin") == "Hindi (hin)" and D.SCRIPTS["hin"] == "Devanagari"


# ------------------------------------------------------------ dialogs
def _fake_win(app, tmp_path):  # noqa: F811
    path = str(tmp_path / "t.pdf")
    make_pdf(path, form=False)
    from PySide6.QtWidgets import QWidget

    win = QWidget()  # the dialogs' parent
    win.canvas = canvas_for(path)
    win.settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    return win


def test_language_dialog_download_and_remove(app, tmp_path, monkeypatch):  # noqa: F811
    from leaf.features.ocr import LanguageDialog

    monkeypatch.setattr(O, "find_tessdata", lambda: None)
    monkeypatch.setattr(LanguageDialog, "opener", staticmethod(opener_for(fake_traineddata(200_000))))
    folder = str(tmp_path / "td")
    dlg = LanguageDialog(None, folder)
    eng, hin = dlg.item("eng"), dlg.item("hin")
    assert "built in" in eng.text() and not eng.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not dlg.get.isEnabled()
    dlg.filter.setText("devanagari")
    assert not hin.isHidden() and dlg.item("deu").isHidden()
    hin.setCheckState(Qt.CheckState.Checked)
    dlg.item("mar").setCheckState(Qt.CheckState.Checked)
    assert dlg.get.isEnabled() and dlg.get.text() == "Download (2)"
    dlg.download_checked()
    t = time.perf_counter()
    while dlg.job is not None and time.perf_counter() - t < 10:
        pump(app, 0.05)
    assert dlg.job is None and dlg.changed and sorted(dlg.added) == ["hin", "mar"]
    assert D.languages_in(folder) == ["eng", "hin", "mar"]
    assert "installed" in dlg.item("hin").text() and "Added" in dlg.status.text()
    dlg.list.setCurrentItem(dlg.item("hin"))
    assert dlg.remove.isEnabled()
    dlg.remove_current()
    assert D.languages_in(folder) == ["eng", "mar"]
    dlg.list.setCurrentItem(dlg.item("eng"))
    assert not dlg.remove.isEnabled()  # the shipped English can't be removed
    dlg.close()


def test_language_dialog_cancel_on_close(app, tmp_path, monkeypatch):  # noqa: F811
    from leaf.features.ocr import LanguageDialog

    class Slow(FakeResponse):
        def read(self, n=-1):
            time.sleep(0.02)
            return super().read(min(n, 4096))

    monkeypatch.setattr(LanguageDialog, "opener", staticmethod(lambda url: Slow(fake_traineddata(3_000_000))))
    folder = str(tmp_path / "td")
    dlg = LanguageDialog(None, folder)
    dlg.item("rus").setCheckState(Qt.CheckState.Checked)
    dlg.download_checked()
    pump(app, 0.2)
    assert dlg.job is not None and dlg.stop.isVisibleTo(dlg)
    dlg.reject()  # closing stops the download
    assert dlg.job is None
    pump(app, 0.2)
    assert not os.path.exists(folder) or not any(n.startswith("rus") for n in os.listdir(folder))


def test_ocr_dialog_engine_follows_language(app, tmp_path, monkeypatch):  # noqa: F811
    from leaf.features import ocr as F

    user = tmp_path / "td"
    monkeypatch.setattr(D, "user_dir", lambda: str(user))
    monkeypatch.setattr(O, "find_tessdata", lambda: None)
    monkeypatch.setattr(O, "rapidocr_available", lambda: True)
    win = _fake_win(app, tmp_path)
    dlg = F.OcrDialog(win, O.available_engines())
    assert dlg.language() == "eng" and dlg.info().key == "rapidocr" and dlg.ok.isEnabled()
    dlg.lang.setEditText("hin")
    assert dlg.info() is None and not dlg.ok.isEnabled() and "Hindi (hin)" in dlg.notes.text()
    D.download("hin", str(user), opener=opener_for(fake_traineddata()))  # as the language manager would
    dlg.engines = O.available_engines()
    dlg._fill("hin", "auto")
    assert dlg.info().key == "tesseract" and dlg.ok.isEnabled()
    dlg.lang.setEditText("eng+hin")
    assert dlg.info().key == "tesseract"
    dlg.engine.setCurrentIndex(dlg.engine.findData("tesseract"))
    dlg.lang.setCurrentIndex(dlg.lang.findData("deu"))
    assert dlg.info().key == "rapidocr"  # Tesseract has no German here: falls back
    dlg.lang.setCurrentIndex(dlg.lang.findData("eng"))
    assert dlg.info().key == "tesseract"  # remembered engine choice
    dlg._accept()
    spec = dlg.result_spec
    assert spec["engine"].key == "tesseract" and spec["language"] == "eng"
    assert os.path.normcase(spec["tessdata"]) == os.path.normcase(str(user))
    assert win.settings.value("ocr/engine") == "tesseract" and win.settings.value("ocr/language") == "eng"
    again = F.OcrDialog(win, O.available_engines())  # the choice is remembered
    assert again.preferred() == "tesseract" and again.language() == "eng" and again.info().key == "tesseract"


def test_tesseract_words_split_at_spans():
    """MuPDF's OCR device writes no spaces between Devanagari words (one span per word): split at spans."""
    def ch(c, x):
        return {"c": c, "bbox": (x, 10, x + 5, 20)}
    raw = {"blocks": [{"lines": [{"spans": [
        {"chars": [ch("न", 0), ch("म", 5), {"c": "्", "bbox": (10, 10, 10, 20)}]},
        {"chars": [ch("द", 20), ch("ु", 25)]},
        {"chars": [ch("a", 40), ch(" ", 45), ch("b", 50)]},
    ]}]}]}

    class Page:
        rotation_matrix = pymupdf.Matrix(1, 1)

        def get_textpage_ocr(self, **k):
            return None

        def get_text(self, kind, **k):
            assert kind == "rawdict"
            return raw

    words = O.TesseractEngine(D.bundled_dir()).recognize(Page(), "hin")
    assert [w[4] for w in words] == ["नम्", "दु", "a", "b"]
    assert [w[6] for w in words] == [0, 1, 2, 3] and {w[5] for w in words} == {0}
    assert words[0][:4] == (0, 10, 10, 20)


def test_download_redirects_stay_on_https():
    import urllib.request

    h = D._https_only_handler()
    req = urllib.request.Request(D.url_for("deu"))
    nxt = h.redirect_request(req, None, 302, "Found", {}, "https://raw.githubusercontent.com/x/deu.traineddata")
    assert nxt is not None and nxt.full_url.startswith("https://raw.githubusercontent.com/")
    for bad in ("http://raw.githubusercontent.com/x/deu.traineddata", "ftp://example.com/deu.traineddata"):
        with pytest.raises(D.DownloadError, match="non-HTTPS"):
            h.redirect_request(req, None, 302, "Found", {}, bad)
