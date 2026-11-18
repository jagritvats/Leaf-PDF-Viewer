# Leaf on each platform

Status as of 2026-11-18 (Leaf 0.1.0: phase 2 complete; PySide6-Essentials 6.12, PyMuPDF 2.1 / MuPDF 1.28.6, pyHanko).

| Platform | Status | Output | Next step |
|---|---|---|---|
| Windows x64 | **Verified** (built + frozen self-test on this machine) | `dist/Leaf/` + `Leaf-<v>-windows-x64.zip`; OCR variant `dist/ocr/Leaf/` + `Leaf-<v>-windows-x64-ocr.zip` | Installer: install Inno Setup (`winget install JRSoftware.InnoSetup`) and run `iscc`, or let CI build it. Code signing certificate for SmartScreen |
| Linux x86_64 | **Verified** in WSL (Ubuntu 24.04): built, self-test OK offscreen, on the WSLg Wayland display and from the AppImage | `leaf-<v>-linux-x86_64.tar.gz` (90 MB), `Leaf-<v>-x86_64.AppImage` (84 MB) (+ `-ocr` via CI) | Test on a real desktop (X11 and Wayland, GNOME + KDE); CI builds on Ubuntu 22.04 for older glibc. OCR variant not built locally (CI does) |
| macOS arm64 | **Prepared** (CI only: needs a Mac) | `Leaf-<v>-macos-arm64.dmg` (ad-hoc signed) (+ `-ocr`) | Push to GitHub (or run the workflow manually) and check the macOS job's self-test. Developer ID + notarization for a warning-free install |
| Microsoft Store (MSIX) | **Prepared**: unsigned MSIX packed with makeappx on this machine (69 MB) from a test build; not installed (needs a signing cert) | `Leaf-<v>-windows-x64.msix` (CI, unsigned, placeholder identity) | Partner Center account + reserved name, then rebuild with its identity. See [Stores and distributions](#stores-and-distributions) |
| Flathub (Flatpak) | **Prepared**: manifest written, YAML checked; not built (no flatpak-builder here) | `packaging/flatpak/<app-id>.yml` | App id, license, release tarball sha256, then the Flathub submission PR |
| AUR (Arch) | **Prepared**: PKGBUILD's `package()` dry-run in WSL against the tarball layout; no makepkg/namcap here | `packaging/aur/PKGBUILD` (`leaf-pdf-bin`) | AUR account, checksum, license, `makepkg --printsrcinfo` |
| AppStream / desktop entry | **Validated** with `appstreamcli validate --no-net` 1.0.2 (WSL): passes (one pedantic note: uppercase in the id) | `packaging/linux/<app-id>.metainfo.xml`, `.desktop` | Real app id; screenshots must be pushed to the public repo |
| Android | **Assessed, not started** | — | See [Android](#android) below: recommended route is a PWA on mupdf.js |
| iOS | Not assessed in depth | — | The PWA route below covers it too |

## What every packaged build checks

`Leaf --self-test` (CI runs it on every build; on Windows the windowed exe writes to `LEAF_SELFTEST_LOG`):

1. MuPDF loads, Qt platform plugin loads, every feature module installs into the window
2. open a generated PDF, first page rendered **in the render worker process** (frozen exe re-entering through
   `multiprocessing.freeze_support()`)
3. add text, save, reopen, text found
4. digital signature: create a self-signed EC identity (.p12), visible PAdES signature, verify (pyHanko,
   cryptography, lxml, aiohttp/ssl all bundled)
5. Reduce file size in its **worker process**
6. OCR in its **worker process** on a generated scanned page, with the engine Leaf picks for English (RapidOCR in
   the OCR variant; Tesseract with the bundled `eng.traineddata` in the standard build, so it runs in every build;
   a frozen build without the bundled data fails)

## Windows

Build (from the repo root, in the project venv):

```
python packaging/make_icons.py
pyinstaller --noconfirm --log-level WARN packaging/leaf.spec                       # standard -> dist/Leaf
LEAF_BUNDLE_OCR=1 pyinstaller --noconfirm --log-level WARN --distpath dist/ocr --workpath build/ocr packaging/leaf.spec
LEAF_SELFTEST_LOG=selftest.log dist/Leaf/Leaf.exe --self-test                      # exit code 0 = OK
```

Measured on a Windows 11 laptop (NVMe):

| | Standard | OCR variant |
|---|---|---|
| Folder size | 126 MB | 335 MB |
| Zip | 57 MB | 150 MB |
| PyInstaller build | ~1 min | ~1 min |
| Start to main window, first launch after build | 0.66 s | 0.68 s |
| Start to main window, warm | 0.51–0.56 s | 0.51–0.56 s |
| Self-test (whole run) | 3.1–4.2 s | 4.8–6.2 s (OCR of the sample page 1.4–1.9 s incl. engine start) |

(A true cold start after a reboot is slower; the first-launch number had the files in the OS cache from the build.)
The higher self-test numbers were taken while other test suites were running on the machine. The final builds
include the in-progress phase 3 code (`textedit_ui` feature) as it was on 2026-11-18; rebuild after phase 3 lands.

Size history: 100 MB before phase 2 → 170 MB with pyHanko/OCR code untrimmed → 126 MB after trimming.
Trims in `packaging/leaf.spec` (each one verified with the self-test):

- standard build excludes the OCR stack: rapidocr, onnxruntime, cv2, **numpy** (only `leaf/ocr.py`'s RapidOCR path
  imports it, ~47 MB with numpy.libs) and **Pillow** (rapidocr; pyHanko imports it only for bitmap stamp
  backgrounds, Leaf's signature appearances are PDF pages, ~11 MB)
- lxml: only `lxml.etree` is used (pyHanko XMP / diff analysis); objectify, html, isoschematron dropped
- OCR variant: OpenCV's ffmpeg video plugin (41 MB) dropped
- earlier trims: MuPDF layout add-on and devel files, Qt software OpenGL, Qt translations, QtNetwork and its plugins

What stays on purpose: OpenSSL + `_ssl` + aiohttp (pyHanko imports aiohttp; the optional timestamp server is
HTTP(S)), certifi (CA bundle fallback), tzdata (pyHanko's tzlocal on Windows), cryptography (~10 MB).

Command line: the Windows bundle has two executables sharing `_internal/`: `Leaf.exe` (windowed, no console
output) and `leaf-cli.exe` (the same program as a console exe, ~9 MB extra), so `leaf-cli text x.pdf` prints to the
terminal. Its self-test passed on this machine. Linux and macOS executables are console-capable already. A `docs/`
folder, when it exists at build time, is bundled as `_internal/docs`.

Installer: `packaging/leaf.iss` (Inno Setup 6.3+), per-user install without admin rights, Start menu entry, optional
desktop icon, "Open with" registration for .pdf (does not take over the default PDF app), uninstaller. Inno Setup is
not installed on this machine, so the installer was not built locally; CI installs it with Chocolatey.

```
iscc /DVersion=0.1.0 packaging\leaf.iss                                             -> dist\Leaf-0.1.0-windows-x64-setup.exe
iscc /DVersion=0.1.0 /DSourceDir=..\dist\ocr\Leaf /DSuffix=-ocr packaging\leaf.iss  -> dist\Leaf-0.1.0-windows-x64-ocr-setup.exe
```

Signing: unsigned builds show a SmartScreen warning ("More info › Run anyway"). An OV/EV code-signing certificate
(or Azure Trusted Signing) and `signtool sign /fd sha256 /tr <timestamp-url> /td sha256` on `Leaf.exe` and the
setup exe remove it (EV/Trusted Signing immediately, OV after reputation builds up).

## Linux

`bash packaging/build_linux.sh` (Linux or WSL, uv installed; `LEAF_BUNDLE_OCR=1` for the OCR variant,
`APPIMAGETOOL=/path/to/appimagetool` for the AppImage). It syncs a separate `.venv-linux`, builds, runs the
self-test headless (`QT_QPA_PLATFORM=offscreen`), then writes the tarball and the AppImage. Tarball layout (the
Flatpak manifest and the AUR PKGBUILD install from it): `leaf/` (the bundle), `<app-id>.desktop`,
`<app-id>.metainfo.xml`, `icons/hicolor/{64,128,256,512}x…/apps/<app-id>.png`. The AppImage carries the desktop
entry, icon and `usr/share/metainfo/<app-id>.appdata.xml` (appimagetool runs with `-n`: its own AppStream check
fetches the screenshot URLs, which only exist once the repository is public). Runtime libraries Qt needs: `libopengl0 libegl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1
libdbus-1-3 libxcb-cursor0`. Build on the oldest distro you support: PySide6 6.12 wheels need glibc 2.34+
(Ubuntu 22.04, Debian 12, RHEL 9 or newer); CI builds on Ubuntu 22.04.

Verified in WSL on 2026-11-18 (Ubuntu 24.04, glibc 2.39; this build therefore needs glibc 2.39+, the CI build on
22.04 needs 2.35+):

| | Standard |
|---|---|
| Folder size | 219 MB (was 245 MB before the Linux trims below) |
| Tarball / AppImage | 90 MB / 84 MB |
| PyInstaller build | ~1 min (plus ~15 s first `uv sync`) |
| Self-test | 1.6 s on the WSLg Wayland display; 1.9 s from the AppImage (`APPIMAGE_EXTRACT_AND_RUN=1`) |

Linux-only trims (spec): Qt translations, and the GTK3 platform-theme plugin together with the GTK stack it pulls
from the build machine (gtk/gdk/cairo/pango/atk/X extensions…, ~27 MB; checked with `ldd` that nothing else in the
bundle needs them). Without it Qt uses the xdg-desktop-portal theme (portal file dialogs) or its own style; the
self-test ran with `XDG_CURRENT_DESKTOP=GNOME` on Wayland.

Size: PySide6 is ~91 MB here (ICU data alone 31 MB) and PyMuPDF ~53 MB; both are needed.

The xcb (X11) plugin is bundled but needs the system's `libxcb-cursor0`, `libxcb-icccm4`, `libxcb-keysyms1`,
`libxcb-shape0`, `libxcb-render-util0`, `libxcb-xkb1`, `libxkbcommon-x11-0` (present on normal desktops; missing
in plain WSL, so only Wayland was tested there). Leaf needs `libOpenGL.so.0` (`libopengl0`) even though it draws
with the raster engine: Qt links it.

How it was run (the WSL distro has uv in `~/.local/bin`, libOpenGL unpacked in `~/build/gl`, appimagetool in
`~/build`). Use `wsl.exe -e` so Windows' `PATH` (with spaces and parentheses) is not expanded by a shell first:

```
wsl.exe -d Ubuntu-24.04 -e bash -lc 'rsync -a --delete --exclude .venv --exclude .venv-linux --exclude dist --exclude build --exclude .pytest_cache --exclude __pycache__ /mnt/c/path/to/leaf/ ~/build/leaf/ && cd ~/build/leaf && export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" LD_LIBRARY_PATH="$HOME/build/gl/usr/lib/x86_64-linux-gnu" APPIMAGETOOL="$HOME/build/appimagetool" && bash packaging/build_linux.sh'
```

Outputs land in `~/build/leaf/dist/` inside WSL (copied to `dist/` here).

## macOS

Only on a Mac (or the GitHub Actions `macos-14` runner): the workflow builds `Leaf.app` with the spec's `BUNDLE`
section, signs it ad hoc (`codesign --force --deep -s -`, then `codesign --verify --deep --strict`), runs the
self-test from `Leaf.app/Contents/MacOS/Leaf`, and wraps it in a UDZO disk image with an Applications link.

Reviewed for phase 2:

- `LSMinimumSystemVersion` raised from 12.0 to **14.0**: the PySide6 6.12 and onnxruntime wheels are built for
  macOS 14, so the app cannot run on older systems anyway.
- CA certificates: python-build-standalone's OpenSSL looks for certificates at build-time paths. Leaf's own trust
  list reads `/etc/ssl/cert.pem` or certifi; for HTTPS timestamp servers `packaging/launcher.py` now points
  `SSL_CERT_FILE` at the bundled certifi file when the default location doesn't exist (frozen macOS/Linux only,
  never overrides a user setting).
- Finder "Open With" / Dock drops arrive as Qt `FileOpen` events (handled in `leaf/__main__.py`), so
  `argv_emulation` stays off.
- The worker processes (render, optimize, OCR) re-launch `Contents/MacOS/Leaf`; `freeze_support()` turns them
  into workers. The self-test exercises all three.
- Build is arm64 only (PyMuPDF has no universal2 wheel). An Intel build would need a separate x86_64 runner
  (`macos-13` is being retired by GitHub).

Signing and notarization for distribution outside the App Store: Apple Developer ID ($99/year), sign with the
hardened runtime (`codesign --options runtime --timestamp -s "Developer ID Application: …"`, every nested dylib/so
first or `--deep`), `xcrun notarytool submit Leaf.dmg --wait`, then `xcrun stapler staple`. Until then users must
right-click › Open (or allow it in System Settings › Privacy & Security) the first time.

## CI (`.github/workflows/build.yml`)

- Windows / macOS-14 / Ubuntu-22.04 matrix; uv with its cache keyed on `uv.lock`.
- Installs all extras so the OCR tests run (they `importorskip` rapidocr/onnxruntime and are skipped when absent);
  the spec excludes the OCR stack from the standard build, so one environment builds both variants.
- Standard build on every push and PR; the OCR variant on `v*` tags and on manual runs (the "Also build the OCR
  variant" input of *Run workflow*). Each variant is self-tested and uploaded as its own artifact
  (`leaf-<platform>` and `leaf-<platform>-ocr`). A tag drafts a GitHub release with all files.
- `metadata` job (ubuntu-latest, ~1 min): `packaging/linux/validate.sh` (appstreamcli + desktop-file-validate),
  parses the Flatpak manifest and workflows (PyYAML), `bash -n` on the PKGBUILD, XML-parses the MSIX manifest.
  A tag's release waits for it.
- Windows also packs an unsigned MSIX (`dist/*.msix`, placeholder identity) when `makeappx.exe` is on the runner
  (the hosted Windows images have the Windows SDK), and runs `leaf-cli.exe --self-test`.
- Not run yet (there is no git repository / GitHub remote). First push = first real test of the macOS and Linux jobs.

## Stores and distributions

Files: `packaging/linux/` (AppStream metainfo, desktop entry, `validate.sh`), `packaging/flatpak/` (manifest,
`build_local.sh`), `packaging/aur/` (PKGBUILD, .SRCINFO), `packaging/msix/` (AppxManifest template,
`make_assets.py`, `build_msix.ps1`), `packaging/set_app_id.py`.

**App id.** Linux metadata uses the reverse-DNS id `io.github.jagritvats.Leaf` (a placeholder; Flathub requires
`io.github.<github-user>.<App>` for apps hosted on GitHub, `-` in the user name becomes `_`). The single source is
the name of `packaging/linux/<app-id>.metainfo.xml`; build scripts and tests read it from there.
`python packaging/set_app_id.py <github-owner>` renames the files and rewrites the id, the developer id and every
`OWNER/leaf` URL in `packaging/` and `.github/` (`--dry-run` first to see the list).

**Window matching (cross-area, `leaf/__main__.py`).** `app.setDesktopFileName("leaf")` makes the Wayland app_id
`leaf`, while the desktop entry is now `<app-id>.desktop`. `StartupWMClass=leaf` lets GNOME and KDE match it, but
the clean fix is `setDesktopFileName("<app-id>")` (needed for Flatpak, where the portal identifies the app by id).
On Windows, the explicit AppUserModelID `Leaf.PdfEditor` is right for the zip/installer builds but must be skipped
in the MSIX (Windows assigns `<PackageFamilyName>!Leaf`; check `GetCurrentPackageFullName` returns 0), otherwise
the taskbar groups Leaf's windows apart from its Start entry. The desktop action "New Window" runs
`leaf --new-window` (today the flag is ignored and a second instance opens, which is a new window anyway).

| Channel | Package | Status | You need to |
|---|---|---|---|
| Flathub | `packaging/flatpak/<app-id>.yml`: org.freedesktop.Platform 25.08 + the CI tarball (Qt bundled) | manifest written + YAML-checked; not built (no flatpak-builder here) | 1. set the app id; 2. choose the license (`<project_license>` in the metainfo is a TODO; Flathub requires it); 3. tag a release so the tarball URL exists, put its sha256 in the manifest; 4. push `marketing/out/screens/light/*-1920x1080.png` to `main` (the metainfo's screenshot URLs point at raw.githubusercontent.com); 5. test with `bash packaging/flatpak/build_local.sh` on a Linux desktop; 6. fork github.com/flathub/flathub, branch `new-pr`, add the manifest, open the PR and answer the review (expect questions about `--filesystem=home` and `--share=network`, both justified in the manifest, and possibly a request for a source build: the route is described at the top of the manifest) |
| AUR | `leaf-pdf-bin` (binary tarball → `/opt/leaf-pdf-bin`, `/usr/bin/leaf` launcher, desktop entry, metainfo, icons) | `package()` dry-run OK in WSL; makepkg/namcap not available here | 1. AUR account with an SSH key; 2. maintainer line + license in the PKGBUILD; 3. on Arch: `updpkgsums && makepkg -si && namcap PKGBUILD *.pkg.tar.zst && makepkg --printsrcinfo > .SRCINFO`; 4. `git clone ssh://aur@aur.archlinux.org/leaf-pdf-bin.git`, add PKGBUILD + .SRCINFO, push. Bump `pkgver` and checksums for every release |
| Microsoft Store | MSIX of `dist/Leaf` (Leaf.exe + leaf-cli.exe with a `leaf-cli` execution alias, .pdf "Open with", runFullTrust) | packed with makeappx 10.0.26100 on this machine (unsigned, 69 MB); CI packs it too | 1. Partner Center developer account (individual or company); 2. reserve the name "Leaf" (or another: "Leaf" may be taken); 3. copy *Product identity* (Package/Identity/Name, Publisher `CN=…`, Publisher display name) and build: `pwsh packaging\msix\build_msix.ps1 -IdentityName … -Publisher "CN=…" -PublisherDisplayName …`; 4. upload the unsigned .msix (the Store signs it); 5. justify `runFullTrust` (Win32 desktop app) in the submission; 6. listing assets below; 7. privacy policy URL (the Store asks because of the network use: OCR language downloads, timestamp server) |
| Sideloading the MSIX | same | — | a code-signing certificate whose subject equals `-Publisher`, then `-CertPath cert.pfx -CertPassword …` (signtool). For a local test: `New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Leaf Dev" -CertStoreLocation Cert:\CurrentUser\My`, export it as .pfx, trust it in *Local Machine › Trusted People* (admin), then `Add-AppxPackage` |

Microsoft Store listing assets (uploaded in Partner Center, not inside the package): screenshots
`marketing/out/screens/{light,dark}/*-1920x1080.png` (and 2560x1600), box art / 1:1 app tile
`marketing/out/promo/tile-light-2160x2160.png` (and 1080x1080), 16:9 hero `marketing/out/promo/hero-light-1920x1080.png`,
feature images `feature-0N-*-1600x900.png` as additional art. Package assets (tiles, app list icons with
`targetsize`/`altform-unplated` variants, store logo; scale 100 and 200, indexed with makepri) are drawn by
`packaging/msix/make_assets.py` from the app icon.

MSIX notes: writes to `%APPDATA%`/`%LOCALAPPDATA%` (settings, recovery copies, signature library, downloaded OCR
languages) are redirected per package and removed on uninstall. The MSIX version is the pyproject version with a
fourth part `0` (Store rule).

## Android

Checked on 2026-11-18. This machine has the Android SDK (`%LOCALAPPDATA%\Android\Sdk`: platforms 34–36.1,
build-tools up to 37, NDK 27.1 and 28.2, emulator, system images) and Android Studio with its bundled JDK, so an
Android toolchain is present. What is missing is everything Python-side:

- **PyMuPDF has no Android wheels** (PyPI: Windows, macOS arm64, manylinux x86_64/aarch64 only), and no maintained
  python-for-android recipe; it would have to be cross-compiled (MuPDF C library + its SWIG/C++ bindings) with the NDK.
- **PySide6 for Android** exists only outside PyPI (download.qt.io has `pyside6-6.12.0-…-android_aarch64` wheels).
  `pyside6-android-deploy` is not part of PySide6-Essentials, runs on Linux hosts only, and drives
  python-for-android, which has recipes for cryptography/lxml but not for PyMuPDF.
- cryptography, lxml, numpy, onnxruntime: no Android wheels on PyPI either (onnxruntime has its own Android AAR).
- MuPDF itself is available for Android (Artifex's Java/Android library, AGPL or commercial) and for the web:
  `mupdf` on npm is mupdf.js, MuPDF **1.28.6** (the same version Leaf uses) compiled to WebAssembly, AGPL.

Routes for an MVP (view, annotate, fill forms, place an image signature, save; edits as a model replayed at save,
as on desktop):

| Route | Reuses | Effort to MVP | Risk | Notes |
|---|---|---|---|---|
| (a) PySide6 for Android + PyMuPDF cross-compiled | Python code (document model, save pipeline) | 6–10 weeks | High | Custom p4a recipe for PyMuPDF; pyHanko's native deps too. The Qt Widgets UI (menus, docks, shortcuts, hover) is desktop-shaped and needs a touch UI anyway. APK ~100+ MB, slow start. Only bindings Qt itself barely supports on Android |
| (b) Native Android app (Kotlin) on MuPDF's Android library (or PDFium + another writer) | design only | 5–8 weeks | Medium | Best native feel and performance. MuPDF Java bindings cover rendering, annotations, widgets, incremental save, signing hooks. Separate codebase to maintain. PDFium is BSD but its editing/saving support is thin |
| (c) Kivy or BeeWare (Python) | some Python | 8+ weeks | High | Same PyMuPDF cross-compile problem as (a) plus a full UI rewrite; no advantage over (a) |
| (d) **Web app / PWA on mupdf.js** (WASM), optionally wrapped as a Trusted Web Activity for the Play Store | design + MuPDF semantics (same engine version) | 3–5 weeks | Low–medium | One codebase for Android, iOS, ChromeOS and any desktop browser; offline via service worker; files open/save through the file picker or share sheet. mupdf.js exposes pages, rendering, annotations, form widgets and incremental save. Digital (PAdES) signatures are a later step (WebCrypto + a JS CMS library, or keep them desktop-only at first) |

Recommendation: **(d)**. It reaches Android (and iOS) soonest, keeps the same MuPDF engine and the same
"edits are a model, applied on save" design as the desktop app, and the toolchain already on this machine
(Android SDK + JDK) is enough to wrap it as a TWA with Bubblewrap later. Route (b) is the fallback if a store-quality
native app becomes the goal. No Android prototype was built: none of the routes is cheap enough to be worth a
throwaway spike before deciding.

## License (AGPL) and distribution

PyMuPDF and MuPDF (also mupdf.js and MuPDF for Android) are AGPL-3.0, or commercial from Artifex. Every binary Leaf
ships (zip, installer, AppImage, dmg, a future APK or web app) links MuPDF, so distributing it requires either:

- releasing Leaf's complete corresponding source under AGPL-3.0-compatible terms and offering it to everyone who
  gets a binary (for a web app also to everyone who uses it over the network), e.g. a link to the tagged source in
  the About box and release notes; or
- an Artifex commercial license for MuPDF, which allows a closed-source distribution.

The other bundled components are permissive or weak copyleft (Qt/PySide6 LGPL-3.0: dynamically linked, users can
swap the Qt DLLs, ship the license texts; pyHanko MIT; cryptography Apache/BSD; RapidOCR/onnxruntime/OpenCV
Apache/MIT; numpy BSD). Ship the third-party license texts with each build before a public release.

NOTICE: `leaf/data/tessdata/eng.traineddata` (bundled in every build, ~4 MB) is Tesseract's English language data
from tessdata_fast (https://github.com/tesseract-ocr/tessdata_fast), Copyright Google / the Tesseract OCR
contributors, Apache License 2.0; unmodified. Languages a user adds in Leaf (OCR › Add languages…) are downloaded
from the same repository at that time and are not redistributed by Leaf.
