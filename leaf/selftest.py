"""`leaf --self-test`: exercise a packaged build end to end (open, render, edit, save, reopen) and exit 0/1.

Runs in a throwaway settings scope so it never touches the user's recent files or preferences. Used by the
release builds to prove the frozen app works (Qt plugins found, MuPDF loads, the render worker can spawn).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback


def _make_pdf(path: str) -> None:
    import pymupdf

    d = pymupdf.open()
    for i in range(3):
        p = d.new_page()
        p.insert_text((72, 100), f"Self-test page {i + 1}", fontsize=14)
    w = pymupdf.Widget()
    w.field_name, w.field_type, w.rect = "name", pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(72, 140, 300, 160)
    d[0].add_widget(w)
    d.save(path)
    d.close()


def run() -> int:
    from PySide6.QtCore import QCoreApplication, QPointF
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setOrganizationName("LeafSelfTest")
    QCoreApplication.setApplicationName("Leaf")
    app = QApplication.instance() or QApplication(sys.argv)

    def pump(secs: float, until=None) -> None:
        end = time.time() + secs
        while time.time() < end and not (until and until()):
            app.processEvents()
            time.sleep(0.005)

    log = []
    try:
        import pymupdf

        from . import commands as C
        from .items import TextItem
        from .window import MainWindow

        tmp = tempfile.mkdtemp(prefix="leaf-selftest-")
        src, out = os.path.join(tmp, "in.pdf"), os.path.join(tmp, "out.pdf")
        _make_pdf(src)
        log.append(f"MuPDF {pymupdf.mupdf_version}, Qt platform {app.platformName()}")

        from .features import NAMES

        win = MainWindow()
        missing = [n for n in NAMES if n not in getattr(win, "installed_features", [])]
        assert not missing, f"features not installed: {missing}"
        log.append(f"features: {', '.join(win.installed_features)}")
        win.show()
        cv = win.canvas
        cv.renderer.enable_worker(True)  # headless (offscreen) runs default to in-process rendering: test the worker
        win.open_path(src)
        t0 = time.perf_counter()
        pump(15, lambda: cv.order and cv.order[0].img is not None)
        assert cv.order and cv.order[0].img is not None, "first page did not render"
        worker = cv.renderer.worker is not None
        log.append(f"first page rendered in {(time.perf_counter() - t0) * 1000:.0f} ms (worker process: {'yes' if worker else 'no ' + repr(cv.renderer.worker_error)})")
        if getattr(sys, "frozen", False):  # packaged builds must be able to re-launch themselves as the worker
            assert worker, f"render worker process failed: {cv.renderer.worker_error!r}"

        item = TextItem(QPointF(72, 200), {"size": 14, "font": "Helvetica"}, "Leaf self-test text")
        item.setParentItem(cv.order[0].content)
        cv.undo.push(C.AddItems([item]))
        cv.doc.save(out, cv.overlays_for)
        d = pymupdf.open(out)
        assert d.page_count == 3, d.page_count
        assert d[0].search_for("Leaf self-test text"), "added text missing after save"
        d.close()
        log.append("edit + save + reopen ok")

        from . import signing  # pyHanko + cryptography + aiohttp/ssl bundled and working

        p12, signed = os.path.join(tmp, "id.p12"), os.path.join(tmp, "signed.pdf")
        signing.create_identity(p12, "selftest", "Leaf Self-Test", algorithm="ec")
        ident = signing.load_identity(p12, "selftest")
        signing.sign_pdf(out, signed, ident, signing.SignRequest(rect=(72, 300, 272, 350), reason="self-test"))
        with open(signed, "rb") as f:
            st = signing.verify_pdf(f.read(), [ident.cert])
        assert [s.state for s in st] == ["valid"], [s.headline() for s in st]
        log.append("digital signature: sign + verify ok")

        log.append(_check_optimize_worker(out, os.path.join(tmp, "small.pdf")))
        log.append(_check_ocr(tmp))

        cv.undo.clear()
        win.close()
        pump(0.3)
        _report("Leaf self-test OK\n  " + "\n  ".join(log))
        return 0
    except Exception:  # noqa: BLE001 - report everything
        _report("Leaf self-test FAILED\n  " + "\n  ".join(log) + "\n" + traceback.format_exc())
        return 1


def _check_optimize_worker(src: str, dst: str) -> str:
    """Reduce file size runs `optimize.run_worker` in a spawned process: prove the frozen exe can re-enter as it."""
    import multiprocessing as mp

    from . import optimize as O

    ctx = mp.get_context("spawn")
    conn, child = ctx.Pipe(duplex=False)
    t0 = time.perf_counter()
    proc = ctx.Process(target=O.run_worker, args=(child, src, dst, O.options_dict(O.preset("lossless")), None),
                       daemon=True, name="leaf-optimize")
    proc.start()
    child.close()
    msg = None
    try:
        while time.perf_counter() - t0 < 60:
            if conn.poll(0.1):
                msg = conn.recv()
                if msg[0] in ("done", "err"):
                    break
            elif not proc.is_alive():
                break
    except EOFError:
        pass
    proc.join(5)
    assert msg and msg[0] == "done", f"optimize worker: {msg!r} (exit code {proc.exitcode})"
    return f"optimize worker process ok ({(time.perf_counter() - t0) * 1000:.0f} ms)"


def _make_scan(path: str) -> None:
    """A tiny image-only ("scanned") page with one line of text."""
    import pymupdf

    t = pymupdf.open()
    p = t.new_page(width=300, height=120)
    p.insert_text((20, 70), "Leaf scanned sample", fontsize=22)
    pix = p.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
    t.close()
    d = pymupdf.open()
    d.new_page(width=300, height=120).insert_image(pymupdf.Rect(0, 0, 300, 120), pixmap=pix)
    d.save(path)
    d.close()


def _check_ocr(tmp: str) -> str:
    """Recognize a generated scanned page in the OCR worker process, with the engine Leaf picks for English
    (RapidOCR when the build bundles it, else Tesseract with the English data shipped in every build)."""
    from . import ocr, ocrdata
    from .ocr_worker import OcrProcess

    if getattr(sys, "frozen", False):
        assert ocrdata.bundled_dir(), "bundled Tesseract English data (leaf/data/tessdata) missing"
    engines = ocr.available_engines()
    if not engines:
        return "OCR: no engine in this build (skipped)"
    e = ocr.choose_engine(engines, "eng") or engines[0]
    scan = os.path.join(tmp, "scan.pdf")
    _make_scan(scan)
    t0 = time.perf_counter()
    proc = OcrProcess(e.key, e.tessdata_for("eng"), "eng", 200, False, {1: (scan, None)}, [("p0", 1, 0)])
    words, end = None, None
    try:
        while time.perf_counter() - t0 < 180 and end is None:
            for msg in proc.poll():
                if msg[0] == "page":
                    words = msg[2]
                elif msg[0] in ("error", "fatal", "done"):
                    end = msg
            time.sleep(0.05)
    finally:
        proc.close()
    text = " ".join(w[4] for w in words or ())
    assert end and end[0] == "done", f"OCR ({e.key}): {end!r}"
    assert "scanned" in text.lower(), f"OCR ({e.key}) read {text!r}"
    return f"OCR ({e.key}) worker process ok: {text!r} in {time.perf_counter() - t0:.1f} s"


def _report(text: str) -> None:
    """stdout, plus LEAF_SELFTEST_LOG (a windowed Windows build has no console to print to)."""
    if sys.stdout is not None:
        print(text)
    path = os.environ.get("LEAF_SELFTEST_LOG")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
