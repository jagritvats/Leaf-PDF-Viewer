#!/usr/bin/env bash
# Build Leaf for Linux: dist/Leaf/ (PyInstaller), then dist/leaf-<version>-linux-x86_64.tar.gz and an AppImage.
# Run from the repo root on Linux (or WSL) with uv installed. Build on the oldest distro you want to support:
# the binaries need that glibc or newer (PySide6 6.12 wheels need glibc 2.34+).
#   LEAF_BUNDLE_OCR=1  also bundle RapidOCR (the `ocr` extra): built into dist/ocr/Leaf, files named *-ocr*
#   APPIMAGETOOL=path  appimagetool to use (else `appimagetool` on PATH; no AppImage without it)
# The Qt runtime needs libOpenGL.so.0, libEGL, libxkbcommon, libfontconfig, libdbus-1 and libxcb-cursor
# (Debian/Ubuntu: libopengl0 libegl1 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3 libxcb-cursor0).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
export UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-.venv-linux}
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
# app id (reverse DNS, set with packaging/set_app_id.py) = the name of the AppStream file in packaging/linux
METAINFO=$(ls packaging/linux/*.metainfo.xml)
APP_ID=$(basename "$METAINFO" .metainfo.xml)
DESKTOP=packaging/linux/$APP_ID.desktop

if [ "${LEAF_BUNDLE_OCR:-}" = "1" ]; then
  EXTRA=(--extra ocr)
  DIST=dist/ocr WORK=build/ocr SUFFIX=-ocr
else
  EXTRA=()
  DIST=dist WORK=build SUFFIX=
fi

# --inexact: keep extras that are already installed (CI installs all of them for the tests)
uv sync --group build --frozen --inexact "${EXTRA[@]}"
uv run --no-sync python packaging/make_icons.py
uv run --no-sync pyinstaller --noconfirm --log-level WARN --distpath "$DIST" --workpath "$WORK/pyinstaller" packaging/leaf.spec
LEAF_SELFTEST_LOG="$PWD/$DIST/selftest.log" "./$DIST/Leaf/leaf" --self-test

# tarball: app folder + desktop entry + AppStream metadata + icons. The Flatpak manifest and the AUR PKGBUILD
# install from this layout: leaf/, <app-id>.desktop, <app-id>.metainfo.xml, icons/hicolor/<n>x<n>/apps/<app-id>.png
STAGE=dist/leaf-$VERSION-linux-x86_64$SUFFIX
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -r "$DIST/Leaf" "$STAGE/leaf"
cp "$DESKTOP" "$METAINFO" LICENSE "$STAGE/"
for px in 64 128 256 512; do
  install -Dm644 "build/icons/leaf-$px.png" "$STAGE/icons/hicolor/${px}x${px}/apps/$APP_ID.png"
done
tar -C dist -czf "$STAGE.tar.gz" "$(basename "$STAGE")"
rm -rf "$STAGE"
echo "built $STAGE.tar.gz"

# AppImage (optional: needs appimagetool on PATH or APPIMAGETOOL set)
TOOL=${APPIMAGETOOL:-$(command -v appimagetool || true)}
if [ -n "$TOOL" ]; then
  APPDIR=$WORK/Leaf.AppDir
  OUT=dist/Leaf-$VERSION$SUFFIX-x86_64.AppImage
  rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/bin"
  cp -r "$DIST/Leaf/." "$APPDIR/usr/bin/"
  cp "$DESKTOP" "$APPDIR/"
  cp build/icons/leaf-256.png "$APPDIR/$APP_ID.png"
  install -Dm644 "$METAINFO" "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml"
  printf '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/usr/bin/leaf" "$@"\n' > "$APPDIR/AppRun"
  chmod +x "$APPDIR/AppRun"
  # --appimage-extract-and-run: works without FUSE (containers, WSL, CI). -n: skip appimagetool's own AppStream
  # check (online: the screenshot URLs exist only once the repository is public); packaging/linux/validate.sh and
  # CI validate the metadata offline instead.
  ARCH=x86_64 "$TOOL" --appimage-extract-and-run -n "$APPDIR" "$OUT" \
    || ARCH=x86_64 "$TOOL" -n "$APPDIR" "$OUT"
  echo "built $OUT"
fi
