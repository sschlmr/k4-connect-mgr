#!/usr/bin/env bash
# Build the macOS client package.
#
#   ./packaging/macos-client/build.sh
#
# Produces packaging/out/k4cm-client-<version>.pkg, which installs the runtime
# folder into /usr/local/libexec/k4cm and a symlink at /usr/local/bin/k4cm.
# Installing over an older version is an upgrade: scripts/preinstall clears the
# old program folder first. ~/.config/k4cm (settings, pinned cert) is kept.
# Run from the project root.
#
# If PyInstaller fails with: codesign ... "Operation not permitted",
# an antivirus scanner is holding the freshly written binary. See
# packaging/README.md ("Antivirus false positives").

set -euo pipefail

VERSION="${VERSION:-0.1.0}"
IDENTIFIER="net.mh01.k4cm.client"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$ROOT/packaging/out"
STAGE="$ROOT/build/macos-pkg-root"

cd "$ROOT"

echo "==> Building the k4cm runtime folder with PyInstaller"
python -m PyInstaller --clean --noconfirm packaging/macos-client/k4cm.spec

if [ ! -x dist/k4cm/k4cm ]; then
    echo "error: dist/k4cm/k4cm was not produced (see the PyInstaller output above)" >&2
    exit 1
fi

echo "==> Staging the package root"
rm -rf "$STAGE"
mkdir -p "$STAGE/usr/local/libexec" "$STAGE/usr/local/bin"
cp -R dist/k4cm "$STAGE/usr/local/libexec/k4cm"
chmod -R go-w "$STAGE/usr/local/libexec/k4cm"
ln -s ../libexec/k4cm/k4cm "$STAGE/usr/local/bin/k4cm"

echo "==> Verifying the staged binary runs"
"$STAGE/usr/local/libexec/k4cm/k4cm" --help >/dev/null

echo "==> Building the installer package"
mkdir -p "$OUT"
pkgbuild \
    --root "$STAGE" \
    --identifier "$IDENTIFIER" \
    --version "$VERSION" \
    --install-location / \
    --scripts "$ROOT/packaging/macos-client/scripts" \
    "$OUT/k4cm-client-$VERSION.pkg"

echo
echo "Built: $OUT/k4cm-client-$VERSION.pkg"
echo "Install with: sudo installer -pkg $OUT/k4cm-client-$VERSION.pkg -target /"
echo
echo "For distribution to other Macs, sign and notarize so Gatekeeper and"
echo "antivirus products accept it:"
echo "  codesign --force --deep --timestamp --options runtime \\"
echo "      --sign 'Developer ID Application: <you>' dist/k4cm/k4cm"
echo "  productsign --sign 'Developer ID Installer: <you>' <pkg> <signed.pkg>"
echo "  xcrun notarytool submit <signed.pkg> --keychain-profile <profile> --wait"
echo "  xcrun stapler staple <signed.pkg>"
