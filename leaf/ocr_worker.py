"""OCR in a separate process, so recognition (seconds per page, GIL-bound) never stalls the UI.

Qt-free: the spawned child imports only this module, leaf.ocr and PyMuPDF (+ the engine). Spawn-safe and
frozen-safe (leaf/__main__.py calls multiprocessing.freeze_support()).

The job is fixed when the process starts (pickled arguments):
    engine key, tessdata, language, dpi, only_if_needed,
    sources {sid: (temp file path, password)}   source bytes handed over in temp files (the GUI deletes them),
    pages [(key, sid, index), ...]
Worker -> GUI over a Pipe:
    ("start", key)                        recognizing that page now
    ("page", key, words, seconds)         words: see leaf.ocr (page.rect space of the source page)
    ("skip", key)                         only_if_needed and the page already has text
    ("error", key, message)               this page failed; the others go on
    ("fatal", message)                    the engine could not start
    ("done", peak_rss_mb)
Cancel = terminate the process (pages recognized so far have already been delivered).
"""
from __future__ import annotations

import multiprocessing as mp
import os


def _peak_rss_mb() -> float:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return pmc.PeakWorkingSetSize / 2**20
            return 0.0
        import resource
        import sys

        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 2**20 if sys.platform == "darwin" else r / 1024
    except Exception:  # noqa: BLE001 - diagnostics only
        return 0.0


def _lower_priority() -> None:
    """Recognition uses every core: run below normal priority so the UI (and the rest of the machine) stays snappy."""
    try:
        if os.name == "nt":
            import ctypes

            k32 = ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass(ctypes.c_void_p(k32.GetCurrentProcess()), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _worker_main(conn, engine_key, tessdata, language, dpi, only_if_needed, sources, pages) -> None:
    import time

    import pymupdf

    from . import ocr

    _lower_priority()
    try:
        engine = ocr.create_engine(engine_key, tessdata)
    except Exception as e:  # noqa: BLE001
        conn.send(("fatal", str(e)))
        return
    docs = {}
    try:
        for sid, (path, password) in sources.items():
            with open(path, "rb") as f:
                d = pymupdf.open(stream=f.read(), filetype="pdf")
            if d.needs_pass:
                d.authenticate(password or "")
            docs[sid] = d
        for key, sid, index in pages:
            try:
                page = docs[sid][index]
                if only_if_needed and ocr.text_chars(page) >= 20:
                    conn.send(("skip", key))
                    continue
                conn.send(("start", key))
                t = time.perf_counter()
                words = engine.recognize(page, language, dpi)
                conn.send(("page", key, words, time.perf_counter() - t))
            except ImportError as e:
                conn.send(("fatal", f"The OCR engine couldn't be loaded: {e}"))
                return
            except (EOFError, OSError, BrokenPipeError):
                raise
            except Exception as e:  # noqa: BLE001
                conn.send(("error", key, str(e) or type(e).__name__))
        conn.send(("done", _peak_rss_mb()))
    except (EOFError, OSError, BrokenPipeError, KeyboardInterrupt):
        return  # GUI went away / cancelled
    finally:
        for d in docs.values():
            d.close()


class OcrProcess:
    """GUI-side handle (non-blocking). `poll()` returns the messages that arrived."""

    def __init__(self, engine_key, tessdata, language, dpi, only_if_needed, sources, pages) -> None:
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe(duplex=False)
        self.proc = ctx.Process(target=_worker_main, daemon=True, name="leaf-ocr",
                                args=(child, engine_key, tessdata, language, int(dpi), bool(only_if_needed),
                                      sources, pages))
        self.proc.start()
        child.close()
        self.finished = False

    def poll(self, max_items: int = 16) -> list[tuple]:
        out = []
        if self.finished:
            return out
        try:
            while len(out) < max_items and self.conn.poll():
                msg = self.conn.recv()
                out.append(msg)
                if msg[0] in ("done", "fatal"):
                    self.finished = True
                    break
        except (EOFError, OSError):
            self.finished = True
            code = self.proc.exitcode if self.proc is not None else None
            out.append(("fatal", f"The OCR process stopped unexpectedly (exit code {code})."))
        if not out and not self.finished and self.proc is not None and not self.proc.is_alive():
            # died without a word (crash in a native library)
            self.finished = True
            out.append(("fatal", f"The OCR process stopped unexpectedly (exit code {self.proc.exitcode})."))
        return out

    def close(self) -> None:
        self.finished = True
        try:
            self.conn.close()
        except OSError:
            pass
        p, self.proc = self.proc, None
        if p is not None:
            if p.is_alive():
                p.terminate()
            p.join(0.5)
