#!/usr/bin/env bash
# Build dist/AnkiMinerGame-<version>-Linux-x86_64.AppImage from the PyInstaller bundle
# dist/AnkiMinerGame/. Ported from Anki Miner packaging/appimage/build-appimage.sh at commit 9959edc9.
#
# Usage: packaging/appimage/build-appimage.sh <version>
# APPDIR_ONLY=1 stages dist/AnkiMinerGame.AppDir and stops before downloading and running appimagetool.
set -euo pipefail

VERSION="${1:?Usage: build-appimage.sh <version>}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUNDLE="$REPO_ROOT/dist/AnkiMinerGame"
APPDIR="$REPO_ROOT/dist/AnkiMinerGame.AppDir"
OUT="$REPO_ROOT/dist/AnkiMinerGame-${VERSION}-Linux-x86_64.AppImage"

# A pinned release and its SHA256 (the GitHub release asset digest), never the mutable
# "continuous" build. Bump both together.
APPIMAGETOOL_VERSION="1.9.1"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
APPIMAGETOOL="$REPO_ROOT/dist/appimagetool"

if [ ! -x "$BUNDLE/AnkiMinerGame" ]; then
    echo "build-appimage.sh: no bundle at $BUNDLE; run pyinstaller first" >&2
    exit 1
fi

echo "Building AppImage for Anki Miner Game v${VERSION}..."

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps"
for px in 48 64 128 256; do
    mkdir -p "$APPDIR/usr/share/icons/hicolor/${px}x${px}/apps"
done

cp -a "$BUNDLE/." "$APPDIR/usr/bin/"

# The root .desktop is a relative symlink, not a second copy to drift.
cp "$REPO_ROOT/packaging/appimage/anki-miner-game.desktop" "$APPDIR/usr/share/applications/"
ln -sf usr/share/applications/anki-miner-game.desktop "$APPDIR/anki-miner-game.desktop"

# The root icon and .DirIcon are a 256x256 PNG: file managers read .DirIcon for the thumbnail and
# most show nothing for an SVG there.
cp "$REPO_ROOT/packaging/icons/anki-miner-game.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/anki-miner-game.svg"
for px in 48 64 128 256; do
    cp "$REPO_ROOT/packaging/icons/anki-miner-game-${px}.png" \
        "$APPDIR/usr/share/icons/hicolor/${px}x${px}/apps/anki-miner-game.png"
done
cp "$REPO_ROOT/packaging/icons/anki-miner-game-256.png" "$APPDIR/anki-miner-game.png"
ln -sf anki-miner-game.png "$APPDIR/.DirIcon"

# Rendered into dist/ because packaging/nfpm.yaml reads the same file for the .deb.
METAINFO="$REPO_ROOT/dist/anki-miner-game.metainfo.xml"
"$REPO_ROOT/packaging/render_metainfo.sh" "$VERSION" "$METAINFO"
cp "$METAINFO" "$APPDIR/usr/share/metainfo/io.github._0xzerolight.AnkiMinerGame.metainfo.xml"

# AppRun is the launcher shim, not a symlink to the binary (see packaging/linux-launcher.sh).
install -m 0755 "$REPO_ROOT/packaging/linux-launcher.sh" "$APPDIR/usr/bin/anki-miner-game-launcher"
rm -f "$APPDIR/AppRun"
ln -sf usr/bin/anki-miner-game-launcher "$APPDIR/AppRun"

if [ "${APPDIR_ONLY:-}" = "1" ]; then
    echo "AppDir staged: dist/$(basename "$APPDIR")"
    exit 0
fi

# Fail closed: a checksum mismatch stops the build rather than packaging with an unverified tool.
if [ ! -f "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool ${APPIMAGETOOL_VERSION}..."
    curl -fsSL -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"
fi
echo "${APPIMAGETOOL_SHA256}  ${APPIMAGETOOL}" | sha256sum -c -
chmod +x "$APPIMAGETOOL"

# --appimage-extract-and-run: no FUSE needed on CI runners.
ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT"

echo "AppImage created: dist/$(basename "$OUT")"
