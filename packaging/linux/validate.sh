#!/usr/bin/env bash
# Validate the Linux desktop metadata offline: AppStream (appstreamcli, package `appstream`) and the desktop entry
# (desktop-file-validate, package `desktop-file-utils`, checked only when installed). Run from anywhere:
#   bash packaging/linux/validate.sh
# Flathub's linter additionally fetches the screenshot URLs; that needs the repository to be public.
set -euo pipefail
cd "$(dirname "$0")"
METAINFO=$(ls ./*.metainfo.xml)
APP_ID=$(basename "$METAINFO" .metainfo.xml)
appstreamcli validate --no-net --explain "$APP_ID.metainfo.xml"
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$APP_ID.desktop"
  echo "desktop entry OK"
else
  echo "desktop-file-validate not installed: desktop entry not checked"
fi
