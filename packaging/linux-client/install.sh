#!/usr/bin/env bash
# Install the K4 Connect Manager client.
#
#   sudo ./install.sh                 install to /usr/local/bin
#   PREFIX=~/.local ./install.sh      install to ~/.local/bin (no sudo needed)

set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
BINDIR="$PREFIX/bin"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/k4cm"

if [ ! -f "$SOURCE" ]; then
    echo "error: k4cm executable not found next to this script" >&2
    exit 1
fi

if [ ! -d "$BINDIR" ]; then
    mkdir -p "$BINDIR"
fi

if [ ! -w "$BINDIR" ]; then
    echo "error: $BINDIR is not writable. Re-run with sudo, or set PREFIX=~/.local" >&2
    exit 1
fi

install -m 755 "$SOURCE" "$BINDIR/k4cm"
echo "Installed $BINDIR/k4cm"

case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo "note: $BINDIR is not on your PATH — add it to your shell profile." ;;
esac

echo
echo "Next: run 'k4cm config' to set the server address, your callsign and API key."
