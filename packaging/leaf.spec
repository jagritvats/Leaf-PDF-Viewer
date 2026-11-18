# PyInstaller spec for Windows, macOS and Linux. From the repo root:
#   python packaging/make_icons.py
#   pyinstaller --noconfirm packaging/leaf.spec
# Output: dist/Leaf/ (Windows, Linux) or dist/Leaf.app (macOS). Windows: dist/Leaf/Leaf.exe (windowed) and
# dist/Leaf/leaf-cli.exe (console, for the command line).
import os
import sys
import tomllib

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICONS = os.path.join(ROOT, "build", "icons")
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]
WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
# OCR add-on (RapidOCR + ONNX Runtime + OpenCV, ~150 MB): bundled only with LEAF_BUNDLE_OCR=1 (and the `ocr` extra
# installed). Without it, packaged builds recognize text with MuPDF's built-in Tesseract and the bundled tessdata.
BUNDLE_OCR = os.environ.get("LEAF_BUNDLE_OCR") == "1"
_ocr_datas, _ocr_hidden = [], []
if BUNDLE_OCR:
    from PyInstaller.utils.hooks import collect_data_files, collect_submodules

    _ocr_datas = collect_data_files("rapidocr")  # models (.onnx), config/model yaml files
    _ocr_hidden = collect_submodules("rapidocr")
# Tesseract English language data (tessdata_fast, Apache-2.0, ~4 MB): every build recognizes text out of the box
# with MuPDF's built-in Tesseract. leaf/ocrdata.py finds it at sys._MEIPASS/leaf/data/tessdata.
_tessdata = [(os.path.join(ROOT, "leaf", "data", "tessdata", "eng.traineddata"), os.path.join("leaf", "data", "tessdata"))]
# ssl/http stay in: pyHanko (digital signatures) imports aiohttp at import time, and the optional timestamp
# server is reached over HTTP(S). (rapidocr also imports requests; its models are local.)
_net = []
# Modules that may only be imported lazily (command line, agent link / MCP server): listed when present so
# PyInstaller bundles them even if nothing imports them at module level.
_lazy = [m for m in ("leaf.cli", "leaf.editset", "leaf.ipc", "leaf.mcp_server", "leaf.agentlink", "leaf.osint",
                     "leaf.settings", "leaf.compare", "leaf.i18n", "leaf.onboarding")
         if os.path.exists(os.path.join(ROOT, *m.split(".")) + ".py")]
# docs/ (CLI / agent documentation), bundled as sys._MEIPASS/docs when the folder exists at build time
_docs = [(os.path.join(ROOT, "docs"), "docs")] if os.path.isdir(os.path.join(ROOT, "docs")) else []
# Only the RapidOCR engine needs these: numpy (leaf/ocr.py imports it lazily for RapidOCR, ~27 MB) and Pillow
# (rapidocr; pyHanko imports it only for bitmap stamp backgrounds, Leaf's signature appearances are PDF pages).
_OCR_ONLY = ["rapidocr", "onnxruntime", "cv2", "numpy", "PIL", "omegaconf", "pyclipper", "colorlog"]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    datas=_ocr_datas + _tessdata + _docs,
    hiddenimports=_ocr_hidden + _lazy,
    excludes=["tkinter", "unittest", "pydoc_data", "test", "pytest", "PyInstaller",
              "pymupdf.layout", "lxml.objectify", "lxml.html", "lxml.isoschematron"] + _net
             + ([] if BUNDLE_OCR else _OCR_ONLY),
    noarchive=False,
)

# Leaf never loads these (verified with --self-test after trimming); ~90 MB of the default bundle
_DROP = (
    "pymupdf/layout/", "pymupdf/mupdf-devel/",          # layout-analysis add-on, C headers/import libs
    "opengl32sw",                                     # software OpenGL fallback (we draw with raster)
    # QtNetwork stays: single instance + the agent link use QLocalServer/QLocalSocket (no TLS/network plugins needed)
    "plugins/tls/", "plugins/networkinformation/",
    "lxml/objectify", "lxml/html/", "lxml/isoschematron/",  # pyHanko only uses lxml.etree (XMP metadata)
    "opencv_videoio_ffmpeg",                          # OCR variant: OpenCV's video I/O plugin (41 MB), never used
)  # libssl/libcrypto/_ssl are kept for pyHanko (see _net)
if sys.platform.startswith("linux"):
    # Qt translations live under Qt/ here. The GTK3 platform theme plugin drags in the build machine's GTK stack
    # (~27 MB; mixing it with the user's GTK/theme is a classic crash source): without it Qt uses the
    # xdg-desktop-portal theme (native file dialogs via the portal) or its own. These libraries are needed by
    # nothing else in the bundle (checked with ldd over every bundled module and plugin).
    _DROP += (
        "platformthemes/libqgtk3",
        "libgtk-3.so", "libgdk-3.so", "libgdk_pixbuf-2.0.so", "libatk-1.0.so", "libatk-bridge-2.0.so",
        "libatspi.so", "libcairo.so", "libcairo-gobject.so", "libpango-1.0.so", "libpangocairo-1.0.so",
        "libpangoft2-1.0.so", "libepoxy.so", "libthai.so", "libdatrie.so", "libfribidi.so", "libgraphite2.so",
        "libharfbuzz.so", "libpixman-1.so", "libXcomposite.so", "libXcursor.so", "libXdamage.so", "libXinerama.so",
        "libXrandr.so", "libXi.so", "libXext.so", "libXfixes.so", "libXrender.so", "libgio-2.0.so",
        "libgmodule-2.0.so", "libgobject-2.0.so", "libmount.so", "libblkid.so", "libselinux.so", "libjpeg.so.8",
    )


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    if "/translations/" in dest:  # Qt's own dialogs in the user's language: keep qtbase_*.qm only (~3 MB)
        return dest.rsplit("/", 1)[-1].startswith("qtbase_")
    return not any(p in dest for p in _DROP)


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
pyz = PYZ(a.pure)

icon = None
if WIN and os.path.exists(os.path.join(ICONS, "leaf.ico")):
    icon = os.path.join(ICONS, "leaf.ico")
elif MAC and os.path.exists(os.path.join(ICONS, "leaf.icns")):
    icon = os.path.join(ICONS, "leaf.icns")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Leaf" if not sys.platform.startswith("linux") else "leaf",
    console=False,
    icon=icon,
    upx=False,
    argv_emulation=False,
)
# Windows: Leaf.exe is a windowed (GUI-subsystem) exe, so a terminal gets no output from it. leaf-cli.exe is the
# same program as a console exe in the same folder (shares _internal/), for `leaf-cli text x.pdf` and friends.
# Linux and macOS executables are console-capable already.
_exes = [exe]
if WIN:
    _exes.append(EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="leaf-cli",
        console=True,
        icon=icon,
        upx=False,
        argv_emulation=False,
    ))
coll = COLLECT(*_exes, a.binaries, a.datas, name="Leaf", upx=False)

if MAC:
    app = BUNDLE(
        coll,
        name="Leaf.app",
        icon=icon,
        bundle_identifier="app.leaf.pdfeditor",
        version=VERSION,
        info_plist={
            "CFBundleName": "Leaf",
            "CFBundleDisplayName": "Leaf",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow dark mode
            "LSMinimumSystemVersion": "14.0",  # PySide6 6.12 (and onnxruntime) wheels need macOS 14
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "PDF document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            }],
        },
    )
