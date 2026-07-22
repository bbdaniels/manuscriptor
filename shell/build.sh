#!/usr/bin/env bash
# Build shell/build/Manuscriptor.app.
#
# Swift Package Manager produces a plain executable; the bundle is assembled
# here, because the only thing an .app adds over the command is the Info.plist
# that declares .tex to LaunchServices.
set -euo pipefail

cd "$(dirname "$0")"

APP="build/Manuscriptor.app"

swift build -c release
BIN="$(swift build -c release --show-bin-path)/Manuscriptor"
[ -x "$BIN" ] || { echo "no product at $BIN" >&2; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Manuscriptor"
cp Resources/Info.plist "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# Ad-hoc signature. Enough to launch and to be trusted by LaunchServices on the
# machine that built it; a Developer ID is only needed to hand the app to
# someone else.
codesign --force --sign - "$APP" >/dev/null

# Register now, so Open With offers Manuscriptor without a logout or a trip
# through /Applications.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$(pwd)/$APP" || true

echo "built $(pwd)/$APP"
