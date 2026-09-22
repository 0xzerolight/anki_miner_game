#!/usr/bin/env bash
# Render packaging/appstream/anki-miner-game.metainfo.xml.in for one release. The AppImage and the
# .deb carry the same file; appimagetool rejects one that fails validation, so this validates.
# Ported from Anki Miner packaging/render_metainfo.sh at commit 9959edc9.
#
# Usage: packaging/render_metainfo.sh <version> <out-path>
set -euo pipefail

VERSION="${1:?Usage: render_metainfo.sh <version> <out-path>}"
OUT="${2:?Usage: render_metainfo.sh <version> <out-path>}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_ROOT/packaging/appstream/anki-miner-game.metainfo.xml.in"

# SOURCE_DATE_EPOCH when set (reproducible builds), today otherwise.
DATE="$(date -u -d "@${SOURCE_DATE_EPOCH:-$(date +%s)}" +%Y-%m-%d)"

mkdir -p "$(dirname "$OUT")"
sed -e "s|@VERSION@|${VERSION}|g" -e "s|@DATE@|${DATE}|g" "$TEMPLATE" > "$OUT"

appstreamcli validate --no-net "$OUT"

echo "Rendered metainfo: $OUT (version ${VERSION}, date ${DATE})"
