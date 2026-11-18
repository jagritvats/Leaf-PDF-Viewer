#!/usr/bin/env bash
# Build and install the Flatpak locally from dist/leaf-<version>-linux-x86_64.tar.gz (made by
# packaging/build_linux.sh) instead of the release download. Needs flatpak and flatpak-builder:
#   flatpak install --user flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
#   bash packaging/flatpak/build_local.sh && flatpak run <app-id>
set -euo pipefail
cd "$(dirname "$0")/../.."
VERSION=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
MANIFEST=$(ls packaging/flatpak/*.yml)
APP_ID=$(basename "$MANIFEST" .yml)
TARBALL=$PWD/dist/leaf-$VERSION-linux-x86_64.tar.gz
[ -f "$TARBALL" ] || { echo "missing $TARBALL: run packaging/build_linux.sh first" >&2; exit 1; }
mkdir -p build/flatpak
# swap the release URL + sha256 for the local tarball (path sources need no checksum)
python3 - "$MANIFEST" "$TARBALL" > "build/flatpak/$APP_ID.yml" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
text = re.sub(r"url: https://\S+\.tar\.gz\n(?:\s*#[^\n]*\n)?\s+sha256: \S+\n", f"path: {sys.argv[2]}\n", text, count=1)
print(text)
PY
flatpak-builder --user --install --force-clean build/flatpak/build "build/flatpak/$APP_ID.yml"
echo "installed $APP_ID: flatpak run $APP_ID"
