#!/usr/bin/env bash
# Build a release Manuscriptor.app and install it into /Applications, so the
# app is launchable from Spotlight and the menubar instead of a terminal.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/build.sh"
APP="$HERE/build/Manuscriptor.app"
[ -d "$APP" ] || { echo "build did not produce $APP" >&2; exit 1; }
DEST="/Applications/Manuscriptor.app"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
echo "installed -> $DEST"
open -R "$DEST"
