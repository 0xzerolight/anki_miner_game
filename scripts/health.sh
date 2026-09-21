#!/usr/bin/env bash
# Definition-of-Done gate (see CLAUDE.md). Runs every step, never stops on
# first failure, prints PASS/FAIL per step and a SUMMARY, exits nonzero if any
# step failed.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

# Always the project venv's tools: a PATH fallback could report green against
# the wrong environment.
BIN="./.venv/bin/"
if [ ! -x "${BIN}python" ]; then
  echo "health.sh: no ${BIN}python here; symlink the shared venv:" >&2
  echo "  ln -sfn /home/light/Projects/anki_miner_game/.venv $(pwd)/.venv" >&2
  exit 2
fi

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
# No -m here: the marker deselect lives in pyproject addopts, and a CLI -m
# would replace it.
run "pytest"  "${BIN}pytest"

echo "================ SUMMARY ================"
if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED:  ${failed[*]}"
  exit 1
fi
echo "all green"
