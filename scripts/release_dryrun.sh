#!/usr/bin/env bash
# Release dry run (spec 19). Shaped on Anki Miner's scripts/release_dryrun.sh.
#
# Dispatches .github/workflows/release.yml on the current branch: the real build matrix, bundle smokes,
# installers and the Windows installer smoke, but no tag and no GitHub Release (the ci-gate and release
# jobs run on a v* tag push only). A green build alone is not enough, so after it this script proves:
#   - the ci-gate and release jobs exist and were skipped;
#   - every selected build leg succeeded, and its log shows the bundle smoke's self-check passing
#     (and, on Windows, the installer smoke's pass line), so no smoke was skipped;
#   - origin's tags and the repository's GitHub Releases are the same as before the dispatch.
# Prints RELEASE DRY-RUN GREEN and exits 0 only then. Exits 1 on a red run or a failed proof, 2 on a
# usage error or an unmet precondition (then nothing was dispatched).
#
# Usage: scripts/release_dryrun.sh [all|linux|windows]      (default: all)
#
# Needs gh (authenticated with actions read and write, contents read), git and jq. Preconditions:
#   - release.yml with its workflow_dispatch trigger is on the default branch: GitHub only dispatches
#     workflows the default branch has;
#   - the current branch is pushed to origin at HEAD: the run builds origin's branch, and the result
#     should be about the commit checked out here.
# One dispatch per branch at a time: the workflow's concurrency group queues a second one.
set -euo pipefail

cd "$(dirname "$0")/.."

WORKFLOW="release.yml"
PLATFORMS="${1:-all}"
case "$PLATFORMS" in
  all) LEGS=(linux windows) ;;
  linux | windows) LEGS=("$PLATFORMS") ;;
  *)
    echo "ERROR: unknown platforms '$PLATFORMS' (use: all, linux or windows)" >&2
    exit 2
    ;;
esac

for tool in gh git jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: $tool is not on PATH" >&2
    exit 2
  fi
done

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git ls-remote --heads origin "refs/heads/$BRANCH" | cut -f1)"
if [ -z "$REMOTE_SHA" ]; then
  echo "ERROR: branch '$BRANCH' is not on origin. Push it first: git push -u origin $BRANCH" >&2
  exit 2
fi
if [ "$REMOTE_SHA" != "$HEAD_SHA" ]; then
  echo "ERROR: origin has '$BRANCH' at $REMOTE_SHA but HEAD is $HEAD_SHA. Push it first." >&2
  exit 2
fi

tags() { git ls-remote --tags --refs origin | sort; }
releases() { gh release list --limit 1000 --json tagName --jq '.[].tagName' | sort; }

# The negatives are proved against these snapshots.
TAGS_BEFORE="$(tags)"
RELEASES_BEFORE="$(releases)"

# Our run is the first dispatch run on this branch with an id above the newest one before dispatching.
CUTOFF="$(gh run list --workflow "$WORKFLOW" --event workflow_dispatch --branch "$BRANCH" --limit 30 \
  --json databaseId --jq '[.[].databaseId] | max // 0')"

echo "==> Release dry run: $WORKFLOW on $BRANCH ($HEAD_SHA), platforms=$PLATFORMS"
if ! gh workflow run "$WORKFLOW" --ref "$BRANCH" -f platforms="$PLATFORMS"; then
  echo "ERROR: the dispatch failed. Is $WORKFLOW on the default branch, and may gh write actions?" >&2
  exit 2
fi

# Any status counts: a run that fails in setup can finish before the first poll.
RUN_ID=""
for _ in $(seq 1 40); do
  RUN_ID="$(gh run list --workflow "$WORKFLOW" --event workflow_dispatch --branch "$BRANCH" --limit 30 \
    --json databaseId --jq "[.[].databaseId | select(. > $CUTOFF)] | min // empty")"
  [ -n "$RUN_ID" ] && break
  sleep 3
done
if [ -z "$RUN_ID" ]; then
  echo "ERROR: the dispatched run never appeared. Look: gh run list --workflow $WORKFLOW --branch $BRANCH" >&2
  exit 1
fi
echo "==> Watching run $RUN_ID: $(gh run view "$RUN_ID" --json url --jq .url)"

failed() {
  echo "ERROR: $*" >&2
  echo "RELEASE DRY-RUN FAILED (run $RUN_ID)"
  exit 1
}

if ! gh run watch "$RUN_ID" --exit-status --interval 30; then
  gh run view "$RUN_ID" --log-failed || true
  failed "the run is red"
fi

echo "==> The run is green. Proving it was a dry run that built and smoked every leg..."
JOBS="$(gh run view "$RUN_ID" --json jobs)"

conclusion() {  # job name -> its conclusion, or "absent" unless exactly one job has that name
  jq -r --arg name "$1" \
    '[.jobs[] | select(.name == $name)] | if length == 1 then .[0].conclusion else "absent" end' <<<"$JOBS"
}

for job in ci-gate release; do
  result="$(conclusion "$job")"
  [ "$result" = "skipped" ] || failed "job '$job' must exist and be skipped on a dispatch; it is: $result"
  echo "    $job: skipped"
done

for leg in "${LEGS[@]}"; do
  result="$(conclusion "build $leg")"
  [ "$result" = "success" ] || failed "leg 'build $leg' must have succeeded; it is: $result"
done

LOG_DIR="$(mktemp -d)"
trap 'rm -rf -- "$LOG_DIR"' EXIT

# A leg's own log, not the whole run's: GitHub archives each job's log when that job ends, while the
# run log can lag behind a slower leg. Retried while the archive catches up.
leg_log_has() {  # leg, line
  local id log
  id="$(jq -r --arg name "build $1" '.jobs[] | select(.name == $name) | .databaseId' <<<"$JOBS")"
  log="$LOG_DIR/$id.log"
  for _ in $(seq 1 30); do
    if gh run view "$RUN_ID" --job "$id" --log >"$log" 2>/dev/null && grep -qF "$2" "$log"; then
      return 0
    fi
    sleep 6
  done
  return 1
}

for leg in "${LEGS[@]}"; do
  # scripts/bundle_smoke.sh prints this line only when the app's in-bundle self-check passed.
  leg_log_has "$leg" "PASS self-check" || failed "the $leg leg's log has no 'PASS self-check': its bundle smoke did not run"
  echo "    build $leg: bundle smoke ran and passed"
  if [ "$leg" = "windows" ]; then
    leg_log_has "$leg" "INSTALLER_SMOKE_PASS" || failed "the windows leg's log has no INSTALLER_SMOKE_PASS"
    echo "    build $leg: installer smoke ran and passed"
  fi
done

TAGS_AFTER="$(tags)"
if [ "$TAGS_AFTER" != "$TAGS_BEFORE" ]; then
  diff <(echo "$TAGS_BEFORE") <(echo "$TAGS_AFTER") >&2 || true
  failed "origin's tags changed during the dry run"
fi
echo "    no tag created"

RELEASES_AFTER="$(releases)"
if [ "$RELEASES_AFTER" != "$RELEASES_BEFORE" ]; then
  diff <(echo "$RELEASES_BEFORE") <(echo "$RELEASES_AFTER") >&2 || true
  failed "the repository's GitHub Releases changed during the dry run"
fi
echo "    no GitHub Release created"

echo "RELEASE DRY-RUN GREEN (run $RUN_ID, platforms=$PLATFORMS)"
