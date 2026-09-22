#!/usr/bin/env bash
# Bundle smoke (spec 19): launch a PyInstaller one-folder build offscreen in a throwaway home and
# check it. Every check runs; a PASS/FAIL line each, then a SUMMARY; nonzero exit on any FAIL.
#
# Usage: scripts/bundle_smoke.sh <dist_dir>      e.g. dist/AnkiMinerGame
#
# The app runs with ANKI_MINER_GAME_SMOKE=1 (anki_miner_game/runtime/bundle_smoke.py): once started
# it checks from inside the bundle that onnxruntime, numpy, PyAV and owocr cannot be imported, that
# the text feed it started serves page.html, and that one real HTTPS GET to github.com (the add-on
# bootstrap's transport, so the network is needed) verifies a certificate; it logs
# BUNDLED_SMOKE_PASS or BUNDLED_SMOKE_FAIL lines and quits. This script then checks:
#   exit            the app exited 0
#   config.json     the app wrote its settings file
#   log             the app wrote its log
#   self-check      the log has BUNDLED_SMOKE_PASS and no BUNDLED_SMOKE_FAIL
#   absent modules  no add-on package or library anywhere in the bundle's files
set -uo pipefail

DIST="${1:?usage: bundle_smoke.sh <dist_dir> (e.g. dist/AnkiMinerGame)}"
if [ ! -d "$DIST" ]; then
  echo "bundle_smoke.sh: no bundle directory $DIST" >&2
  exit 2
fi
APP=""
for candidate in "$DIST/AnkiMinerGame" "$DIST/AnkiMinerGame.exe"; do
  if [ -f "$candidate" ]; then
    APP="$candidate"
    break
  fi
done
if [ -z "$APP" ]; then
  echo "bundle_smoke.sh: no AnkiMinerGame executable in $DIST" >&2
  exit 2
fi

TIMEOUT_S=180  # the app's own worst case is the HTTPS GET's 30 s socket timeout plus a quick quit

ROOT="$(mktemp -d)"
trap 'rm -rf -- "$ROOT"' EXIT

# Every home the app or Qt could reach lies inside ROOT: the app's own, and ~ with the Windows and
# XDG folders, which hold OBS's config root and the default ~/Videos/Anki Miner Game output folder.
export ANKI_MINER_GAME_HOME="$ROOT/app-home"
export HOME="$ROOT/home"
export USERPROFILE="$HOME"
export APPDATA="$HOME/AppData/Roaming"
export LOCALAPPDATA="$HOME/AppData/Local"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache"
mkdir -p "$HOME"
export QT_QPA_PLATFORM=offscreen
export ANKI_MINER_GAME_SMOKE=1

LOG="$ANKI_MINER_GAME_HOME/anki_miner_game.log"
failed=()

check() {  # name, command...
  local name="$1"
  shift
  if "$@"; then
    echo "PASS ${name}"
  else
    echo "FAIL ${name}"
    failed+=("${name}")
  fi
}

self_check_passed() {
  [ -f "$LOG" ] && grep -q "BUNDLED_SMOKE_PASS" "$LOG" && ! grep -q "BUNDLED_SMOKE_FAIL" "$LOG"
}

no_add_on_files() {
  local found
  found="$(find "$DIST" \( -name numpy -o -name numpy.libs -o -name 'onnxruntime*' -o -name 'libonnxruntime*' \
    -o -name av -o -name av.libs -o -name owocr \) -print)"
  if [ -n "$found" ]; then
    echo "add-on files in the bundle:"
    echo "$found"
    return 1
  fi
}

echo "=== launch: $APP ==="
timeout "$TIMEOUT_S" "$APP"
code=$?
echo "exit code ${code}"
echo

check "exit" test "$code" -eq 0
check "config.json" test -s "$ANKI_MINER_GAME_HOME/config.json"
check "log" test -s "$LOG"
check "self-check" self_check_passed
check "absent modules" no_add_on_files

if [ ${#failed[@]} -gt 0 ] && [ -f "$LOG" ]; then
  echo
  echo "=== $LOG ==="
  cat "$LOG"
fi

echo
echo "================ SUMMARY ================"
if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED:  ${failed[*]}"
  exit 1
fi
echo "all green"
