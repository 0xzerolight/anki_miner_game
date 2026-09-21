#!/usr/bin/env bash
# Definition-of-Done gate (see CLAUDE.md). Runs every step, never stops on
# first failure, prints PASS/FAIL per step and a SUMMARY, exits nonzero if any
# step failed.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

# Prefer the project venv binaries; fall back to PATH.
BIN=""
[ -x ".venv/bin/python" ] && BIN=".venv/bin/"

failed=()

run() {  # name, cmd...
  local name="$1"; shift
  echo "=== ${name} ==="
  if "$@"; then
    echo "PASS ${name}"
  else
    echo "FAIL ${name}"
    failed+=("${name}")
  fi
  echo
}

run "black"   "${BIN}black" --check .
run "ruff"    "${BIN}ruff" check .
run "mypy"    "${BIN}mypy" anki_miner_game
run "pytest"  "${BIN}pytest" -m "not vad and not obs_live and not network and not windows_only"

echo "================ SUMMARY ================"
if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED:  ${failed[*]}"
  exit 1
fi
echo "all green"
