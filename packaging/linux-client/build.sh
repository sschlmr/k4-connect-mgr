#!/usr/bin/env bash
# Build the Linux client package.
#
#   ./packaging/linux-client/build.sh
#
# Produces packaging/out/k4cm-client-<version>-linux-<arch>.tar.gz containing
# the k4cm executable, an install.sh and the member guide. If `fpm` is available a .deb is built
# as well. Run from the project root on the oldest glibc you need to support.

set -euo pipefail

VERSION="${VERSION:-0.2.0-rc1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$ROOT/packaging/out"
ARCH="$(uname -m)"
STAGE="$ROOT/build/linux-tarball/k4cm-client-$VERSION"

cd "$ROOT"

echo "==> Building the k4cm executable with PyInstaller"
python -m PyInstaller --clean --noconfirm packaging/linux-client/k4cm.spec

echo "==> Staging the tarball"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp dist/k4cm "$STAGE/k4cm"
chmod 755 "$STAGE/k4cm"
cp packaging/linux-client/install.sh "$STAGE/install.sh"
chmod 755 "$STAGE/install.sh"
cp README.md "$STAGE/README.md" 2>/dev/null || true
cp docs/k4cm-user-guide.md "$STAGE/k4cm-user-guide.md"

mkdir -p "$OUT"
TARBALL="$OUT/k4cm-client-$VERSION-linux-$ARCH.tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"
echo "Built: $TARBALL"

if command -v fpm >/dev/null 2>&1; then
    echo "==> Building a .deb with fpm"
    fpm -s dir -t deb \
        -n k4cm-client \
        -v "$VERSION" \
        --description "K4 Connect Manager client - request and manage remote Elecraft K4 sessions" \
        --url "https://mh01.net" \
        --license MIT \
        -p "$OUT/k4cm-client_${VERSION}_${ARCH}.deb" \
        "$STAGE/k4cm=/usr/local/bin/k4cm" \
        "$STAGE/k4cm-user-guide.md=/usr/local/share/doc/k4cm/k4cm-user-guide.md"
    echo "Built: $OUT/k4cm-client_${VERSION}_${ARCH}.deb"
else
    echo "fpm not installed - skipping the .deb (the tarball is enough)."
fi
