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
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# A STABLE signature, because macOS keys permissions to the app's code identity.
# Ad-hoc has no certificate, so that identity is the code-directory hash, and the
# hash changes with every rebuild: each build looked like a brand new app and the
# system asked again for Documents, Desktop and Downloads. Signing with a
# certificate makes the stored requirement name the bundle id and the leaf
# instead, which survives a rebuild. Any self-signed code-signing certificate in
# the login keychain will do; a Developer ID is only needed to hand the app to
# someone else. Ad-hoc stays as the fallback so a machine without the certificate
# still builds an app that launches.
IDENTITY="${MANUSCRIPTOR_SIGN_IDENTITY:-ClaudeHUD Dev}"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$IDENTITY"; then
  codesign --force --sign "$IDENTITY" "$APP" >/dev/null
else
  echo "no code-signing identity \"$IDENTITY\": signing ad-hoc, so macOS will ask" \
       "for file permissions again after every rebuild" >&2
  codesign --force --sign - "$APP" >/dev/null
fi

# Register now, so Open With offers Manuscriptor without a logout or a trip
# through /Applications.
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$(pwd)/$APP" || true

echo "built $(pwd)/$APP"
