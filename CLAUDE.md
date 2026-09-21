# CLAUDE.md — Anki Miner Game

Standalone desktop app: records a video-game session through OBS and writes a
same-stem `.mkv` + `.srt` (+ `.session.json`) pair, so Anki Miner can mine a
game session the way it mines an anime episode. Own repository; **Anki Miner
itself is never modified.** Python 3.12, PyQt6, PyInstaller. Licence
GPL-3.0-only.

Working preferences, planning and general implementation rules live in the
global `~/.claude/CLAUDE.md` (concise responses, worktree-per-change, atomic
conventional commits, proportional fixes, no new settings inside a fix,
Definition-of-Done gate before claiming done). This file adds the
project-specific rules below; where the two conflict, this file wins for this
repository.

Design: [`docs/specs/2026-09-20-anki-miner-game-design.md`](docs/specs/2026-09-20-anki-miner-game-design.md)
(canonical from repo creation on; amendments land here, not in the Anki Miner
copy). Master plan: [`docs/plans/2026-09-21-master-plan.md`](docs/plans/2026-09-21-master-plan.md).
Progress: [`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md).

## Repository layout

```
anki_miner_game/                  repo root
  CLAUDE.md  LICENSE  README.md  pyproject.toml  uv.lock
  anki_miner_game/                spec 4.1 packages + paths.py, store.py, runtime/
  tests/                          unit per package, fakes/, contract/, integration/, fixtures/
  tools/                          m0/, sync_probe/, obs_transcript_recorder.py, handoff_probe.py
  scripts/                        health.sh, diff_vendored_matcher.py, bundle_smoke.sh, release_dryrun.sh
  packaging/  .github/workflows/  docs/
  .worktrees/  .orchestration/    gitignored
```

## Dependency rule (spec 4.1)

```
anki_miner_game/
  models/        frozen dataclasses, no I/O
  text/          sources/ (websocket, clipboard, ocr) and pipeline.py
  obs/           discovery, client, provision, recorder
  session/       clock, cues, srt_writer, naming, manifest, journal, session (the actor)
  lifecycle/     auto.py
  vad/           trimmer.py, assign.py, worker/vad_worker.py
  addons/        bootstrap.py, vad_addon.py, ocr_addon.py
  feed/          ws_server.py, http_server.py, page.html
  gui/           main_window, tray, wizard, game_profile_dialog, settings_dialog, hotkey_win, cli_verbs
  interfaces/    Protocols: TextSource, RecordClock, ObsGateway, Presenter
  app.py, launch.py
```

- `models` imports nothing from the app.
- `session`, `text`, `obs`, `vad`, `feed` and `lifecycle` never import `gui`.
- `gui` reaches the rest only through `interfaces`.
- `session/cues.py` and `vad/assign.py` are pure functions: no clock, file or
  network access.
- Composition lives in `app.py`; there is no DI container.

## Worktrees

Every change, however small, runs in its own worktree:
`/home/light/Projects/anki_miner_game/.worktrees/<slug>` on branch
`feat/<slug>`, created from `main`:

```
git -C /home/light/Projects/anki_miner_game worktree add \
  /home/light/Projects/anki_miner_game/.worktrees/<slug> -b feat/<slug> main
ln -sfn /home/light/Projects/anki_miner_game/.venv <worktree>/.venv
```

Implementers commit on their own branch only. The orchestrator alone merges
to `main`, pushes and removes worktrees (this overrides the global rule to
merge and remove once green). Use absolute paths in every command; `cd` into
the worktree inside each shell call rather than relying on a persisted
working directory.

## Gate command

```
cd /home/light/Projects/anki_miner_game/.worktrees/<slug>
PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash scripts/health.sh
```

`scripts/health.sh` runs black --check, ruff check, mypy `anki_miner_game`,
then plain `pytest`, all from `./.venv/bin/` (it exits 2 when `.venv` is
missing: symlink the shared one). The marker deselect (`not vad and not
obs_live and not network and not windows_only`) lives only in
`pyproject.toml` `addopts`; a command-line `-m` replaces it, so never pass
one to the gate. It never stops at the first failure, prints `PASS <step>` /
`FAIL <step>` per step and a `SUMMARY` block, and exits nonzero if any step
failed. This is the Definition-of-Done gate; never claim a task complete or
merge on a red or unrun gate. Send output to `<worktree>/gate.log` (never pipe
through `tail`) and keep the file as evidence. Set
`PYTEST_XDIST_AUTO_NUM_WORKERS=4` inside worktrees.

## Interpreter and uv rules

- Interpreter: `/home/light/Projects/anki_miner_game/.venv/bin/python`. Never
  bare `python3`, never `uv run`, never `pip install -e`.
- The `uv` on `PATH` is a plugin shim that rejects `uv pip`; the real binary is
  `/home/light/.local/bin/uv`.
- Installs happen only in the main checkout, only into `.venv` (dependencies
  only, via `uv sync --no-install-project`) or `.venv-vad` (T10, from its
  pinned requirements). The project package itself is never installed;
  `pytest` resolves it through `pythonpath = ["."]` and mypy runs from the
  repo root. Any number of worktrees can gate at once against one shared
  `.venv` because nothing writes into it per-task.

## Parallel-safe test rules

- Bind port `0` in every test that opens a real socket — never a default
  port such as 4455 (OBS), 6677/9001/2333 (hookers), 6678/6679 (the feed).
- Derive any `QLocalServer` name from the isolated home path, not a fixed
  string, so parallel worktrees gating at once cannot collide.
- `pytest.importorskip` for `numpy`, `onnxruntime` and `av` in any test that
  needs them — they live only in `.venv-vad`, never this project's `.venv`.
- `pyproject.toml` carries a mypy override for `numpy`, `onnxruntime` and `av`
  and for `anki_miner_game/vad/worker/` (T00 added it); keep it.
- Tests reach no network beyond loopback (`tests/_network_tripwire.py`); mark
  a genuinely networked test `network`, and a real-OBS test `obs_live`.
- Every top-level `QWidget` a Qt test constructs goes through
  `qtbot.addWidget`; the suite runs offscreen (`QT_QPA_PLATFORM=offscreen`)
  with an isolated `ANKI_MINER_GAME_HOME` per test (autouse in
  `tests/conftest.py`).

## Commit rules

Atomic conventional commits (`feat`/`fix`/`test`/`docs`/`chore`/`refactor`/
`ci`), subject ≤ 72 chars, body only when the "why" isn't obvious (terse
bullets). Stage by file name, never `git add -A` or `git add .`. No
`Co-Authored-By:` trailers or AI-attribution lines. Commit only on your own
branch; never merge into `main`, never push, never remove or prune worktrees,
never delete branches — that is the orchestrator's job.

## Contract-change rule

`models/` and `interfaces/` are fixed by contract once written (T01). A task
that needs a change there does not make it: it writes a
`CONTRACT-CHANGE-REQUEST` (the exact proposed change) into its own status file
under `.orchestration/status/<slug>.json` and returns without touching those
packages. The orchestrator rules on it; a single dedicated agent edits
`models/`/`interfaces/`, and dependent tasks rebase onto the result.
Accepted additions beyond master plan section 4 are listed in
[`docs/contracts.md`](docs/contracts.md); read it with section 4.

## Release notes

Governed by `~/.claude/rules/writing-release-notes.md`. Project pins:

- Notes folder: `docs/release_notes/vX.Y.Z.md`.
- Canonical version source: `anki_miner_game/__init__.py:__version__`.
- Spelling: British.
- Navigation vocabulary: the app's own labels (e.g. `Settings -> Filtering`,
  `Game profile -> ...`), `->` not an arrow character, hyphen not an em dash.
