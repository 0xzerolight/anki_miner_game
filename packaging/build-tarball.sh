#!/usr/bin/env bash
# Pack the Linux PyInstaller bundle as dist/AnkiMinerGame-<version>-Linux-x86_64.tar.gz: one
# AnkiMinerGame/ folder holding the bundle, the launcher shim as `anki-miner-game` (start the app
# with it, see packaging/linux-launcher.sh) and the licence.
#
# Usage: packaging/build-tarball.sh <version>      (after pyinstaller built dist/AnkiMinerGame/)
set -euo pipefail

VERSION="${1:?Usage: build-tarball.sh <version>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE="$REPO_ROOT/dist/AnkiMinerGame"
OUT="$REPO_ROOT/dist/AnkiMinerGame-${VERSION}-Linux-x86_64.tar.gz"

if [ ! -x "$BUNDLE/AnkiMinerGame" ]; then
    echo "build-tarball.sh: no bundle at $BUNDLE; run pyinstaller first" >&2
    exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT

cp -a "$BUNDLE" "$STAGE/AnkiMinerGame"
install -m 0755 "$REPO_ROOT/packaging/linux-launcher.sh" "$STAGE/AnkiMinerGame/anki-miner-game"
install -m 0644 "$REPO_ROOT/LICENSE" "$STAGE/AnkiMinerGame/LICENSE"

# Root-owned, name-sorted members; mtimes from SOURCE_DATE_EPOCH when set (reproducible builds).
TAR_OPTS=(--sort=name --owner=0 --group=0 --numeric-owner)
if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
    TAR_OPTS+=(--mtime="@${SOURCE_DATE_EPOCH}" --clamp-mtime)
fi
rm -f "$OUT"
tar "${TAR_OPTS[@]}" -czf "$OUT.part" -C "$STAGE" AnkiMinerGame
mv "$OUT.part" "$OUT"

echo "Tarball created: dist/$(basename "$OUT")"
