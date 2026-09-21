# Anki Miner Game: orchestrated implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (the wave
> Workflows below mechanise it: fresh implementer per task, spec review, then quality review).
> Each implementer uses superpowers:test-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** Build the standalone app in the design spec (an OBS session recorder that writes a
same-stem `.mkv` + `.srt` pair from text-hooker lines) in a new private repo, milestones M0-M5,
with the main session doing orchestration only.

**Architecture:** Contracts first (models + Protocols, one agent), then one Workflow per wave. Each
wave runs its tasks as an in-script dependency graph: independent tasks run in parallel, a dependent
task starts on a branch stacked on its dependencies' finished branches. Every task: (plan -> judge
for the few high-risk ones) -> implement -> spec review -> quality review -> fix, in its own git
worktree of the new repo, Opus 5 on xhigh for anything complex (Workflow is the only reliable way to
set effort). Each wave ends with an integration branch, cross-task review, gate, then a
fast-forward of `main` and a push. Real-OBS spikes (M0) run inside wave 1 beside the pure core;
OBS-dependent code waits for their results.

**Tech Stack:** Python 3.12, PyQt6, obsws-python, websockets; dev: pytest, pytest-qt,
pytest-asyncio, pytest-xdist, hypothesis, pysubs2, black, ruff, mypy; PyInstaller, Inno Setup,
AppImage, nfpm. Add-ons in their own envs: onnxruntime, numpy, PyAV (VAD); owocr (OCR).

**Spec:** `/home/light/Projects/anki_miner/docs/anki-miner-game/specs/2026-09-20-anki-miner-game-design.md`.
T00 copies it to `docs/specs/2026-09-20-anki-miner-game-design.md` in the new repo; that copy is
canonical from then on and M0 amends it there.

## Context

The design was approved and judged twice on 2026-09-20 (22 findings, all adopted but one). No code
exists. The user asked for: a new private repo, subagents for all work, Opus 5 on xhigh for anything
complex, the main session orchestrating only, maximum parallel dispatch, and subagents allowed to
spawn their own subagents. Memory `anki-miner-game-design`: do not re-judge the design; judge the
plans. This draft was judged once by an adversarial Plan agent (2 BLOCKER, 12 MAJOR, 4 MINOR);
section 10 records the disposition.

Environment facts found while planning (2026-09-21):
- No OBS on this host. The Flathub user remote is filtered to Bottles only
  (`~/.config/flatpak/flathub-bottles-only.filter`); `sudo` needs an interactive password.
- Win11 QA VM is gone (memory `win11-boxes-vm-cli`). No Windows runtime environment.
- Host: KDE Plasma on Wayland, RTX 5070 Ti (NVENC) + AMD iGPU, 32 cores, 60 GB RAM, 1.3 TB free;
  `kwin_wayland` (supports `--virtual --xwayland`), Xwayland, ffmpeg/ffprobe, nvidia-smi present;
  xdotool, ydotool, `/dev/video*` absent.
- The `uv` on PATH is a plugin shim that rejects `uv pip`; the real binary is `/home/light/.local/bin/uv`.
  Bare `python3` is also shimmed.
- `gh` is logged in as `0xzerolight` with `repo` + `workflow` scopes; `anki_miner_game` does not exist.
- The spec's title sanitiser has a counterexample, reproduced against Anki Miner's real extractor:
  `S1 1080p E2 - 03` -> season 1, episode 2 (`_strip_technical_tokens` removes `1080p`, then the
  S/E pattern matches). Also `S1 x264 E2 - 03`, `S1 v2 E3 - 04`. T04 owns the fix.

## Global Constraints (verbatim from the spec; every task inherits these)

- Standalone app, own repository. **No Anki Miner changes, ever.** Contract = files on disk.
- Windows first-class, Linux best-effort, **macOS unsupported**.
- Python 3.12, PyQt6, PyInstaller. Licence **GPL-3.0-only**.
- Frozen runtime deps: `PyQt6`, `obsws-python`, `websockets`. "Nothing else in the frozen app."
- `models` imports nothing from the app; `session`, `text`, `obs`, `vad`, `feed`, `lifecycle` never
  import `gui`; `gui` reaches the rest only through `interfaces`. `session/cues.py` and
  `vad/assign.py` are pure. Composition lives in `app.py`; no DI container.
- All models are frozen dataclasses; JSON on disk carries `"schema": 1`.
- `SKIP_MS = 300`, `MIN_CUE_MS = 500` and every VAD threshold are module constants, not settings.
  `START_SHIFT_MS = {"hook": 0, "ocr": -1000}`.
- A line's arrival time is `time.monotonic()` read inside the source at frame receipt; nothing
  downstream re-stamps it.
- Cue invariant: `0 <= start < end <= next.start`.
- SRT: UTF-8 without BOM, `\n`, index from 1, `HH:MM:SS,mmm` from integer ms, one text line per cue,
  written to a temporary name then `os.replace`.
- Feed binds `127.0.0.1`. The OBS password is never logged and never stored unless typed as an override.
- The app never reads or writes `~/.config/owocr_config.ini`.
- The bundle contains no onnxruntime, numpy, PyAV or owocr (spec `excludes` + smoke assertion).
- Recoverable failures are banners, never modal dialogs. No `pynput`.
- Tests: pytest-qt offscreen, `qtbot.addWidget` on every top-level widget, isolated
  `ANKI_MINER_GAME_HOME` per test, no network except loopback.
- Ported code keeps a header naming the source file and commit (GSM `479747fe`, faster-whisper,
  owocr 1.26.8, Anki Miner commit).

---

## 1. Decisions

Owner decisions (answered 2026-09-21):

| # | Decision | Answer |
|---|---|---|
| D1 | OBS on this host | Flathub OBS. An agent adds `com.obsproject.Studio` to the user's Flathub filter and runs `flatpak install --user -y flathub com.obsproject.Studio` |
| D2 | Windows runtime checks (M1 Windows exit, Windows sync probe, rename-lock timing, hotkey under a fullscreen/elevated game, app-audio string) | **Deferred to pre-release manual QA (H5).** No VM rebuild. S1 settles what source can settle; hosted CI runs Windows unit tests throughout. Accepted risk: a Windows clock spread > 100 ms surfaces late. Windows-derived constants are marked provisional until H5 |
| D3 | Hosted CI on the private repo | **Full spec matrix**: Python 3.12 + 3.13 on Windows + Linux on every push to `main` (one push per merged wave); release dry-run whenever packaging changes |
| D4 | Where unattended execution stops | **Only at human gates**: H3 (VAD recordings), H4 (OCR sessions), H5 (release QA incl. every Windows check) |

Orchestrator rulings (recorded in the ledger; not owner calls):
- Repo `/home/light/Projects/anki_miner_game`, GitHub `0xzerolight/anki_miner_game`, private.
- Docs live in the new repo (`docs/specs/`, `docs/plans/`, `docs/m0/`, `docs/IMPLEMENTATION_STATUS.md`);
  the Anki Miner copy of the status file becomes a pointer.
- `CLAUDE.md` is committed at the new repo root (private repo; worktrees carry it).
- The venv holds dependencies only (`uv sync --no-install-project`); the package is never installed.
  pytest uses `pythonpath = ["."]`; mypy runs from cwd. Any number of worktrees can gate at once
  against one shared `.venv`.
- The Flatpak OBS config root (`~/.var/app/com.obsproject.Studio/config/obs-studio`) is new and used
  by nothing else, so spikes and E1 use it directly (no isolated root). Spikes snapshot and restore it
  between scenarios. This also exercises the real Flatpak discovery path.
- Spikes run OBS and probe windows as X11 clients inside a nested `kwin_wayland --virtual --xwayland`
  display (`xcomposite_input` capture). Portal-based capture (PipeWire source, owocr on Wayland)
  opens on the user's real desktop, so it is not automated: PipeWire capture goes to H5, owocr's
  interactive picker to H4.
- Empty lines (pipeline step 5) count under `no_letters`, since the manifest has no `empty` counter.
- VAD Re-run / Restore live in a recent-session row's context menu; **Open folder** stays the one
  visible action (reconciles spec 16 with 13.3).
- Judging per task only where risk is highest (T01, T06, T12, T15, T16): one judge per round, two-round
  cap. Tasks whose formulas and test tables are already in the spec go straight to implement + review.

## 2. Repository layout and conventions (T00 writes these into `CLAUDE.md`)

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

- Worktree per task: `/home/light/Projects/anki_miner_game/.worktrees/<slug>` on `feat/<slug>`,
  `.venv` symlinked in. All paths in prompts and docs are absolute.
- Gate: `bash /home/light/Projects/anki_miner_game/.worktrees/<slug>/scripts/health.sh`, run from the
  worktree: black --check, ruff, mypy `anki_miner_game`, pytest
  `-m "not vad and not obs_live and not network and not windows_only"`. Never stops at the first
  failure; prints `PASS`/`FAIL` per step and a `SUMMARY`; exit non-zero on any hard failure. Output to
  `<WT>/gate.log`, never piped to `tail`.
- `PYTEST_XDIST_AUTO_NUM_WORKERS=4` inside worktrees.
- Interpreter: `/home/light/Projects/anki_miner_game/.venv/bin/python`. Never bare `python3`, never
  `uv run`, never `pip install -e`. Installs use `/home/light/.local/bin/uv` and happen only in the
  main checkout, only into `.venv` (T00) or `.venv-vad` (T10, from its pinned requirements).
- Parallel-safe tests: bind port 0 (never a default port such as 4455, 6677, 9001, 2333, 6678,
  6679); derive the `QLocalServer` name from the home path; `pytest.importorskip` for numpy,
  onnxruntime and av; mypy override in `pyproject.toml` for those modules and for
  `anki_miner_game/vad/worker/` (T00 adds it).
- Commits: atomic conventional, staged by file name, no AI attribution. Implementers never merge,
  push, or remove worktrees.
- Contract changes after W0: the implementer writes `CONTRACT-CHANGE-REQUEST` in its status file and
  returns; the orchestrator rules; one agent edits `models/`/`interfaces/`; dependants rebase.

## 3. Orchestration model

**Roles.**
- Orchestrator (main session): writes wave Workflow scripts, creates worktrees and branches, reads
  evidence from disk, rules on contract changes, fast-forwards `main`, pushes, keeps
  `.orchestration/LEDGER.md` and `.orchestration/ORCHESTRATOR-RESUME.md`. Never edits product code;
  committed docs change through a Sonnet docs clerk on a worktree.
- Implementer (Workflow `agent`, `agentType: 'general-purpose'`): owns one task card. May spawn its
  own Agent-tool subagents (Explore for research; general-purpose for an independent sub-piece in the
  same worktree, with a disjoint file list); waits for them in-turn; those children cannot spawn.
- Planner (T01, T06, T12, T15, T16 only): writes `docs/plans/tasks/<slug>.md` with
  superpowers:writing-plans in several Write/Edit steps, returns path + a summary of 10 lines or fewer.
- Judge: read-only, one per round, criteria correctness, simplicity/altitude, reuse, scope, test
  coverage; grepped evidence; round 2 only on a BLOCKER or several MAJORs.
- Reviewers: spec compliance, then code quality. Spikes get a fact-check reviewer instead that greps
  every cite. Fixer: fresh agent with the findings; two fix rounds at most, then the orchestrator.
- Integrator (Sonnet): merges a wave's branches into `integration/wave-N` in its own worktree, runs
  the gate. Cross-task reviewers (two, split by area) read the integration diff; a fixer repairs on
  the integration branch.

**Role preamble** (baked at the top of every agent prompt, every role): "You work on Anki Miner Game,
repo `/home/light/Projects/anki_miner_game`. The injected Anki Miner project CLAUDE.md does NOT apply
(different repo, gate and venv). Your rules: the global `~/.claude/CLAUDE.md` and
`/home/light/Projects/anki_miner_game/CLAUDE.md` (read it first). Your shell starts in Anki Miner:
`cd` to your worktree inside every Bash command; use absolute paths. Anki Miner is read-only."

**Models and effort.**

| Work | Model | Effort |
|---|---|---|
| T01-T04, T06, T07, T09-T16, T19-T27, T29, S1, S2, R1, R2, E1, T31, T32; judges; quality reviews of xhigh tasks; cross-task reviews; fixers | Opus 5 | xhigh |
| T05, T08, T17, T18, T28, T30, R3; spec-compliance and fact-check reviews; quality reviews of high tasks | Opus 5 | high |
| T00, O1, integrators, docs clerk, M0 spec amender | Sonnet 5 | high |

**Wave Workflow shape.** Inputs are literal constants (the `args` channel is unreliable); agents read
their task card from the committed master plan by path; returns are small schemas, documents go to
files.

```js
export const meta = { name: 'amg-wave-N', description: 'Anki Miner Game wave N: DAG of tasks, review, integrate',
  phases: [{ title: 'Plan' }, { title: 'Implement' }, { title: 'Review' }, { title: 'Integrate' }] }
const REPO = '/home/light/Projects/anki_miner_game'
const PLAN = REPO + '/docs/plans/2026-09-21-master-plan.md'
const TASKS = [ { slug: 't02-cues-srt', deps: [], effort: 'xhigh', judge: false }, { slug: 't06-finalise', deps: ['t02-cues-srt', 't04-naming'], judge: true }, ... ]
const done = {}                         // slug -> promise of {branch, head_sha, gate_exit}
const sem = semaphore(6)                // in-script limiter; the harness cap is 16
function run(t) {
  return done[t.slug] ??= (async () => {
    const deps = await Promise.all(t.deps.map(d => run(TASKS.find(x => x.slug === d))))
    if (deps.some(d => !d || d.gate_exit !== 0)) throw new Error(t.slug + ': dependency failed')
    // base = main, then each dep branch merged --no-ff into feat/<slug> (stacked)
    const plan = t.judge ? await sem(() => planAndJudge(t)) : null        // <= 2 judge rounds
    const impl = await sem(() => agent(PREAMBLE + IMPL(t, deps, plan), { phase: 'Implement', model: t.model, effort: t.effort, agentType: 'general-purpose', schema: IMPL_OUT }))
    if (!impl || impl.gate_exit !== 0) throw new Error(t.slug + ': implement failed')
    return await sem(() => reviewAndFix(t, impl))                          // spec -> quality -> fixer, <= 2 rounds
  })()
}
const results = await parallel(TASKS.map(t => () => run(t)))
const failed = TASKS.filter((t, i) => !results[i]).map(t => t.slug)
if (failed.length) { log('not integrated: ' + failed.join(', ')); return { failed, results } }   // no partial integration
const integ = await agent(PREAMBLE + INTEGRATOR(results), { phase: 'Integrate', model: 'sonnet', effort: 'high', schema: GATE_OUT })
// cross-task reviewers (2, by area) -> fixer on integration branch -> re-gate; return {integration_sha, gate}
```

**Idempotence and restart.** Every stage writes a marker to
`/home/light/Projects/anki_miner_game/.orchestration/status/<slug>.json` (`planned`, `judged`,
`implemented@sha`, `reviewed-clean@sha`) and checks it first, returning early when its work is done
and the branch head matches. After a session-limit kill the orchestrator re-runs the wave script
(or a trimmed copy listing only unfinished tasks) instead of relying on `resumeFromRunId`, whose
prefix cache does not survive a reordered parallel run. Implementers check `git log main..HEAD` and
their status file before starting.

**Per wave, the orchestrator:**
- [ ] Create each task's worktree, branch and `.venv` symlink (stacked tasks are created by their
  own implementer once dependencies finish).
- [ ] Run the wave Workflow; watch `/workflows`.
- [ ] Verify on disk: branch heads, `gate.log` SUMMARY and PASS/FAIL lines, the integration gate log,
  `git merge-base --is-ancestor`. Trust disk, never agent text; grep any symbol a report claims
  before it enters a durable doc.
- [ ] `git merge --ff-only integration/wave-N` on `main`; `git push`; check the CI run (D3); remove
  worktrees and branches.
- [ ] Ledger entry; status-doc row via the docs clerk.

**Continuity across 5-hour limits** (memory `overnight-session-continuity`): staggered background
Bash timers plus `CronCreate` one-shots after each reset; `ORCHESTRATOR-RESUME.md` holds state, rules
and what to check first; per-task status files hold the last green sha and the next step. Cancel every
timer and cron when the work ends.

**Cost.** About 40 agent-owned tasks at roughly 0.5-1M tokens each including review: 25-35M in total,
about five limit windows. The limiter of 6 keeps the work lost to a limit kill small.

## 4. Contracts (T01 codifies these; names are fixed, types may be refined)

Models (`anki_miner_game/models/`, no I/O, no app imports):
- `constants.py`: `SKIP_MS`, `MIN_CUE_MS`, `START_SHIFT_MS`, `MAX_LINE_CHARS = 300`,
  `TYPEWRITER_WINDOW_S = 2.0`, `SCHEMA = 1`.
- `lines.py`: `GameLine(text, raw, t_mono, source_id)`; `TimedLine(offset_ms, text, source_id)`.
- `cue.py`: `Cue(index, start_ms, end_ms, text, source_id)`; `Region(start_ms, end_ms)`.
- `config.py`: `AppConfig` + `ObsSettings(host, port: int | None, password_override)`,
  `TextSourceConfig(id, name, uri, enabled)`, `FeedSettings`, `RecordingSettings`, `CueSettings`,
  `VadSettings`; defaults per spec 5.
- `profile.py`: `GameProfile` + `CaptureSettings`, `AudioSettings`, `FilterSettings`, `OcrSettings`,
  `AutoSettings`; `TextMode`; `validate(profile) -> list[str]`; platform defaults.
- `manifest.py`: `SessionManifest` + `ObsRecord`, `ClockRecord`, `DriftSample`,
  `Counts(received, accepted, duplicate, no_letters, junk, paused, skip)`, `LiveCue`, `VadRecord`,
  `FilesRecord`; `ManifestState`; `Flag`; `to_json/from_json`.
- `pipeline.py`: `DropReason` (`empty`, `no_letters`, `junk`, `duplicate`) and
  `DROP_COUNTER: dict[DropReason, str]` (`empty -> "no_letters"`); `Accepted(line: GameLine)`,
  `Replaced(line: GameLine)`, `Dropped(reason)`.
- `messages.py`: actor inputs `LineReceived(raw, t_mono, source_id)`, `ObsEvent(name, data, t_mono)`,
  `UserCommand(kind: "arm"|"disarm"|"start"|"stop"|"toggle", slug)`, `Tick(t_mono)`; actor outputs
  `SessionEvent` = `StateChanged(state)`, `LineAccepted(line, offset_ms | None)`,
  `RecordingStarted(stem)`, `RecordingStopped(stem)`, `SessionFinalised(manifest_path)`;
  `AppState`; `SourceStatus`; `Banner(key, level, text)`.
- `obs.py`: `ObsInfo(obs_version, websocket_version, available_requests)`,
  `REQUIRED_REQUESTS` (the 26 of spec 3.3), `ObsCredentials(host, port, password)`,
  `WsConfig(server_enabled, port, password, auth_required)`, `WindowItem(name, value)`,
  `ProvisionResult(changed, needs_restart)`.

Protocols (`anki_miner_game/interfaces/`):
- `TextSource` (spec 8.1), `RecordClock` (spec 7), `ObsGateway` (spec 11.2), `Presenter`
  (`state_changed`, `source_status`, `line_accepted`, `banner`, `banner_cleared`,
  `session_finished`, `vad_progress`).
- `SessionControl`: `post(msg)` (thread-safe), `subscribe(cb: Callable[[SessionEvent], None])`, `state`.
- `ObsDiscovery`: `find_install()`, `config_root()`, `read_ws_config()`, `ensure_server_enabled()`,
  `is_running()`, `launch()`, `wait_ready(timeout_s=30)`, `credentials(cfg) -> ObsCredentials`.
- `Provisioner`: `async ensure_profile(cfg)`, `async ensure_collection(profile)`,
  `async list_windows()`, all returning `ProvisionResult` or `list[WindowItem]`.
- `Recorder`: `async start()`, `async stop()`.
- `AddonService` (`status()`, `size_bytes`, `async install(progress)`), `VadJobs` (`queue`, `rerun`,
  `restore`, by manifest path), `OcrAreaPicker` (`async pick(window_title) -> str | None`).

Injected time: `now: Callable[[], float] = time.monotonic` on `WebsocketSource`, `ClipboardSource`,
`ObsClient`, the session actor and `FakeHookerServer`, so integration tests are deterministic.

Module functions (names fixed now): `session.cues.build_cues(lines, stop_ms, shift_ms, cfg)`;
`session.srt_writer.format_srt`, `write_srt_atomic`, `format_timestamp`;
`session.clock.EventClock(capture_latency_ms=0, now=...)`, `OutputDurationClock()` with
`anchor(mono_mid, output_duration_ms)`; `session.naming.sanitise_title`, `session_stem`, `slugify`,
`parse_index`; `text.pipeline.TextPipeline(filters).process(msg: LineReceived)`;
`session.journal.Journal(path)`, `read_journal(path)`; `session.manifest.load_manifest`,
`write_manifest_atomic`, `reserve_index(game_dir, incoming, slug)`;
`session.finalise.finalise(manifest_path, cfg, *, sleep=time.sleep)`;
`vad.assign.assign(live_cues, regions, text_mode, cfg)`; `feed.FeedServer(ws_port, http_port)` with
`async start()`, `broadcast(text)`, `async stop()`; `addons.bootstrap.ensure_uv(home)`;
`paths.home()`; `store.load_config`, `save_config`, `load_profiles`, `save_profile`.

## 5. Waves and dependencies

```
W0  O1 OBS install (Flatpak, D1)  ||  T00 scaffold -> T01 contracts
W1  T02 T03 T04 T05 T07 T08 T09 T10 T11 S1 S2 R3        parallel
    T06 <- T02, T04          R1 <- S2 (+O1)          R2 <- R1
    M0 gate: findings -> spec amendments (or escalation to the user)
W2  T12 T13 T14 T15 T17 T18 T22 T23 T24                 parallel (T15/T17 code against Protocols)
    T16 <- T12 T13 T14 T15
    E1 Linux M1 exit probe, after integration
W3  T19 T20 T21 T25                                     parallel
    T26 wiring <- T19 T20 T21
W4  T27 T28 T30                                         parallel
    T29 <- T27 T28           release dry-run loop until GREEN
H3  VAD recordings (user) -> T31      H4  OCR sessions + owocr picker (user) -> T32
H5  manual QA (spec 18.4) + every Windows check (D2) + PipeWire capture -> tag v0.1.0 + notes
```

Everything before H3 runs unattended (D4). O1 is an agent step, not a stop.

## 6. Prompt blocks (baked literally into the wave scripts)

**Implementer** = role preamble +:
1. You are implementer `<slug>`. Task card: `### <slug>` in `<PLAN>`. Spec: `<REPO>/docs/specs/...`.
   Read both and the contract modules. If a plan file and judge notes exist, apply them.
2. Worktree `<REPO>/.worktrees/<slug>`, branch `feat/<slug>`. Missing: `git -C <REPO> worktree add`
   from `main`, merge each dependency branch `--no-ff`, `ln -sfn <REPO>/.venv <WT>/.venv`. If
   `git log main..HEAD` has your own commits, you are resuming: read your status file and continue.
3. TDD (superpowers:test-driven-development). Context7 before using obsws-python, websockets, PyQt6,
   pysubs2, hypothesis, PyAV or onnxruntime APIs.
4. Own only the files on your card and your tests. No installs (except the named `.venv-vad`
   exception on T10), no `uv run`, no bare `python3`. Commit atomically on your branch only; never
   merge to main, push, or remove the worktree.
5. Subagents allowed as in section 3; wait for them in-turn.
6. Done = card tests green + gate exit 0 with `<WT>/gate.log` kept + status file updated. Return
   `{branch, head_sha, gate_exit, gate_summary, files, contract_change_request, notes}`.

**Judge**: `~/.claude/skills/orchestrating-leads/reference/judge-brief.md` prompt, one lens holding
the five criteria; fixed decisions = spec section 2 + section 1 above.

**Spec reviewer**: every behaviour and test named on the card and in the cited spec rows exists;
nothing beyond the card; returns findings with file:line. **Quality reviewer**: correctness incl.
threads and lifecycle, error-matrix behaviour, simplicity, test quality, at most 10 findings.
**Cross-task reviewer**: "nobody has reviewed how these fit together"; given the integration diff,
the files several tasks touched, and the contracts.

## 7. Task cards

Each card: model/effort, judge, files owned, spec sections, required tests. "18.1 row X" means every
case in that row of the spec's unit-test table.

### O1 OBS install (Sonnet high, W0, no repo change)
- Add `com.obsproject.Studio` (and its runtime/extension refs) to
  `~/.config/flatpak/flathub-bottles-only.filter` in the file's own syntax, keeping every existing
  line; `flatpak install --user -y flathub com.obsproject.Studio`; record the version and whether
  obs-websocket ships in it; start it once in the nested display and quit, so the config root exists.
- Output: `.orchestration/m0/obs-install.md`.

### T00 scaffold (Sonnet high, W0)
- Orchestrator first: `mkdir`, `git init -b main`, empty initial commit, T00 worktree.
- Files: `pyproject.toml` (setuptools, line-length 120, `requires-python >=3.12`, runtime deps
  exactly the three, `[dev]` extra, script `anki_miner_game = anki_miner_game.launch:main`, pytest
  `pythonpath=["."]`, `asyncio_mode="auto"`, markers `vad obs_live network windows_only e2e`, mypy
  overrides from section 2), `uv.lock`, `LICENSE` (GPL-3.0 text from Anki Miner), `.gitignore`,
  `README.md` (three lines), `CLAUDE.md` (section 2 in full, release-notes pins: folder
  `docs/release_notes/`, version source `anki_miner_game/__init__.py:__version__`, British spelling),
  `scripts/health.sh`, the spec 4.1 package tree with empty `__init__.py`,
  `anki_miner_game/__init__.py` (`__version__ = "0.1.0"`), `tests/conftest.py` (offscreen QPA
  `setdefault`, autouse `ANKI_MINER_GAME_HOME` isolation modelled on Anki Miner's
  `tests/_home_isolation.py`, non-loopback socket tripwire), one smoke test,
  `.github/workflows/ci.yml` (D3 matrix: lint, typecheck, tests on 3.12/3.13 x Windows/Linux, push to
  `main` and `workflow_dispatch`), `docs/` (spec copy, status file, this plan as
  `docs/plans/2026-09-21-master-plan.md`).
- `.venv`: `/home/light/.local/bin/uv venv --python 3.12` then `uv sync --no-install-project --extra dev`.
- Acceptance: gate green. The orchestrator then runs
  `gh repo create 0xzerolight/anki_miner_game --private --source <REPO> --push`.

### T01 contracts (Opus xhigh, judge, W0)
- Files: `models/*`, `interfaces/*`, `paths.py`, `store.py`; `tests/models/`, `tests/test_store.py`.
- Spec 5, 6.1, 7, 8.1, 11.2 and section 4 above.
- Tests: JSON round trip with `schema` for AppConfig, GameProfile, SessionManifest; GameProfile
  validation rejects hook sources/clipboard in OCR mode and `audio.mode="app"` without
  `capture.window`; `max_cue_seconds` 5-60; platform defaults; frozen + `replace`; `DROP_COUNTER`
  maps every reason to a `Counts` field; store writes atomically and honours `ANKI_MINER_GAME_HOME`.
- Acceptance: every name in section 4 importable; mypy strict on `models/` and `interfaces/`.

### T02 cues + SRT writer (Opus xhigh, W1)
- Files: `session/cues.py`, `session/srt_writer.py`; tests. Spec 9; 18.1 rows `cues.py`, `srt_writer.py`.
- Tests: all seven rows of the D table; burst of click-through lines dropped whole; last line against
  the stop offset; OCR shift clamp at 0 and at `prev.start`; hypothesis invariant over arbitrary
  non-decreasing offsets; ms formatting at 0, 999, 3 599 999 and past one hour; byte-exact output;
  pysubs2 parse-back equals the cues; atomic write leaves no temp file.

### T03 record clocks (Opus xhigh, W1)
- Files: `session/clock.py`; tests. Spec 7; 18.1 row `clock.py`.
- `EventClock` per the formula; `OutputDurationClock` is pure (the actor schedules samples every
  10 s and calls `anchor`); non-negative, non-decreasing clamp; `None` while paused; drift-sample helper.
- Tests: both clocks, pause spans, lines during a pause, clamp, re-anchoring.

### T04 naming + contract test (Opus xhigh, W1)
- Files: `session/naming.py`; `tests/session/test_naming.py`;
  `tests/contract/anki_miner_episode_matcher.py` (vendor the whole `EpisodeNumberExtractor` class
  with the pattern class attributes and its methods from Anki Miner `utils/episode_matcher.py`,
  commit sha in the header); `tests/contract/test_naming_contract.py`;
  `scripts/diff_vendored_matcher.py`.
- Spec 10.1; 18.1 naming row and the contract-test paragraph.
- Tests: the ten-row table; the three known counterexamples above; property test with an adversarial
  strategy that interleaves technical tokens (the ones `_strip_technical_tokens` removes), S/E
  fragments, ` - `, digits and arbitrary Unicode, NN 1-9999: the vendored extractor returns exactly NN
  and no season; `parse_index` inverts `session_stem`.
- Spec amendment path: T04 generalises sanitiser step 3 minimally so the property holds, and records
  the new rule and the counterexamples in its status file for the M0 gate to write into the spec.

### T05 text pipeline (Opus high, W1)
- Files: `text/pipeline.py`; tests. Spec 8.2; 18.1 pipeline row.
- Tests: one per step with its counter (empty under `no_letters`); order between steps 4-8;
  typewriter merge on/off including え then えっと…; 2 s window by `t_mono`; `Replaced` keeps the
  previous offset.

### T06 journal, manifest I/O, finalise (Opus xhigh, judge, W1, after T02 + T04)
- Files: `session/journal.py`, `session/manifest.py`, `session/finalise.py`; tests.
  Spec 10.2, 10.3; 18.1 journal+finalise row.
- Tests: each journal record type, flush per write, a torn last line tolerated; finalise with a crash
  injected at every step boundary, re-run to the same end state; missing `stop` -> last offset + cap;
  `no_cues`; rename retry with injected `sleep` (locked N times then success; permanently locked ->
  `finalise_pending`); NN reservation with files only, manifests only, both, and `_incoming/`
  manifests of the same game.
- Not owned here: deciding which manifests are orphans (T15, reconcile).

### T07 websocket source + fake hooker (Opus xhigh, W1)
- Files: `text/sources/websocket_source.py` (GSM port header), `tests/fakes/fake_hooker.py`, tests.
  Spec 8.1, 3.2, 18.2; GSM `gametext.py::listen_on_websocket` at `479747fe` (clone into the
  scratchpad with `gh repo clone bpwhelan/GameSentenceMiner -- --depth 1`, then fetch that sha).
- Tests: plain frame; JSON dict with and without `sentence`; non-dict JSON and invalid JSON are the
  line; Luna path fallback once; backoff 1, 2, 5, 10, 10 s with injected sleep; `ping_interval=None`;
  `t_mono` from injected `now` at receipt; status transitions; two listeners both receive.

### T08 text feed (Opus high, W1)
- Files: `feed/ws_server.py`, `feed/http_server.py`, `feed/page.html`; tests. Spec 15.
- Tests: broadcast to two clients; bound to 127.0.0.1; page served as `text/html; charset=utf-8`;
  port in use raises a typed error carrying the port; page has no external references.

### T09 VAD assignment (Opus xhigh, W1)
- Files: `vad/assign.py` (VAD thresholds as module constants); tests. Spec 13.3; 18.1 row `assign.py`.
- Tests: the worked example exactly; a region in progress at window start skipped when another starts
  within 2 s; OCR start snap claims a region from the previous chain; no regions keeps live ends; end
  clamp against a snapped next start; invariant property test.

### T10 VAD worker (Opus xhigh, W1)
- Files: `vad/worker/vad_worker.py` (MIT header, faster-whisper `vad.py` port),
  `vad/worker/requirements.txt` (pinned onnxruntime, numpy, av), `vad/model_pin.py` (URL, sha256,
  filename of `silero_vad_v6.onnx`), tests (`vad` marker, `importorskip`, run the worker through
  `.venv-vad/bin/python` as a subprocess). Creates `/home/light/Projects/anki_miner_game/.venv-vad`
  from its pins with the real uv (the one install exception).
- Spec 13.1, 13.2, 18.2 VAD bullets.
- Tests: synthetic tone -> no regions; a small public-domain speech clip (licence noted) -> one
  region; a three-hour synthetic track stays under a fixed RSS ceiling; the JSON-lines protocol
  including `error`.

### T11 add-on bootstrap (Opus xhigh, W1)
- Files: `addons/bootstrap.py`; tests. Spec 19 last bullet; ideas from Anki Miner
  `services/_install_common.py`, copied not imported.
- Pinned `uv` for linux x86_64 and windows x86_64 (URL + sha256); HTTPS only; host allowlist; size
  cap; `.part`; hash check; atomic replace; exec bit; archive extraction.
- Tests with an injected transport: hash mismatch, oversize, non-HTTPS, foreign host, interrupted
  download leaves no target, success; one real download test marked `network`.

### S1 M0 source research (Opus xhigh, W1; fact-check review)
- Output `docs/m0/source-findings.md`; every claim cites repo + commit + file:line.
- From obs-studio and obs-websocket source: `basic.ini` keys for container and file split (Simple
  and Advanced); which provisioning rows apply to the next `StartRecord` without a restart; the
  Windows registry key the installer writes; pause output-state names and when they fire; minimum
  OBS version whose websocket has all 26 requests; window-string formats for `game_capture`,
  `window_capture`, `wasapi_process_output_capture` and whether they match; event order for profile
  and collection switches; `RecordStateChanged.outputPath` on STARTED/STOPPED; how an active
  recording's file can be matched to a manifest after a reconnect (`GetRecordStatus` has no path);
  Flatpak launch command; `GetInputPropertiesListPropertyItems` value format; obsws-python threading
  model and API names. owocr: newest version vs 1.26.8, the two coordinate log formats and when they
  print, flags unchanged.
- Runtime-only items are marked for R1/R2 (Linux) or H5 (Windows).

### S2 M0 tooling (Opus xhigh, W1)
- Files: `tools/nested_display.py` (starts `kwin_wayland --virtual --xwayland`, exports the nested
  `DISPLAY`, unsets `WAYLAND_DISPLAY` for children so OBS and the flasher run as X11 clients);
  `tools/obs_transcript_recorder.py` (websocket proxy logging every frame with direction and `t_mono`
  to JSONL, auth fields redacted); `tools/sync_probe/` (flasher: black window, white for three frames
  at scripted moments, sends a line to a local websocket at the same instant; analyser decodes the
  recording, finds the first white frame per flash, compares with candidate zero events in `--raw`
  mode or with cue starts in the app's `.srt` in `--app` mode); `tools/m0/obs_scenarios.py`; tests on
  synthetic video made with ffmpeg lavfi.
- Output-activation recipes in `tools/m0/README.md`: streaming to an `ffmpeg -listen 1` RTMP sink;
  replay buffer enabled by seeding `RecRB` in the profile before launch; virtual camera cannot be
  made active on this host (no v4l2loopback), so its refusal is tested with one field of a recorded
  response edited and labelled synthetic.

### R1 clock and sync spike (Opus xhigh, W1, after S2 + O1)
- Sync probe `--raw` with x264 (lookahead on), NVENC, and under encoder overload, at least ten flashes
  each, including after pause/resume. Disk rate at 1080p30 and 720p30 with OBS defaults.
- Output `docs/m0/clock.md`: zero event, `capture_latency_ms`, spread per encoder, pause result,
  pass/fail against 150 ms, GB/h. Spread > 100 ms -> escalation (calibration design) before W2.

### R2 OBS behaviour spike (Opus xhigh, W1, after R1; the one OBS is shared)
- Through the recorder proxy: normal session; pause/resume; **missed pause event** (proxy dropped,
  pause sent from a second client, reconnect); reconnect mid-session; OBS exit; profile + collection
  switch (timing, event order, with recording, streaming and replay buffer active); refused switch;
  settings-without-restart confirmed from the real `basic.ini`; `outputDuration` vs file timestamps;
  matching an active recording to its manifest after reconnect.
- Output: `tests/fixtures/obs_transcripts/*.jsonl` (redacted) + `docs/m0/obs-behaviour.md`.

### R3 owocr spike (Opus high, W1)
- `uv tool install "owocr[meikiocr]==<pin>"` into a scratch tool dir; OCR a synthetic text window in
  the nested Xwayland with explicit rectangles; capture log fixtures of both coordinate lines (from a
  run or, if only the picker prints them, from S1's source cite, labelled); prove the whole process
  tree dies through the uv shim with a process group.
- Output `docs/m0/owocr.md` + `tests/fixtures/owocr/*.log`. The Wayland clipboard spike is dropped:
  spec 8.1 already fixes the wizard text; behaviour is checked at H5.

### M0 gate (orchestrator + Sonnet amender)
- Read `docs/m0/*.md` and T04's sanitiser note; grep-check claims; the amender edits the repo spec
  copy (3.3 states, 7 constants, 10.1 sanitiser, 11.1 registry and minimum version, 11.3 keys and
  restart rows, Appendix B, plus the sections `docs/m0/wave-1-amendments.md` names: 7, 8.1, 8.2,
  9, 10.3, 11.2, 12, 13.1, 13.2, 13.3) in one commit, marking Windows-derived values provisional
  (D2).
- A finding that breaks the design beyond the spec's own fallbacks goes to the user.

### T12 OBS gateway + FakeObsServer (Opus xhigh, judge, W2)
- Files: `obs/client.py`, `tests/fakes/fake_obs_server.py`, tests. Spec 3.3, 11.2, 17 auth row, 18.2.
- obsws-python `ReqClient`/`EventClient`; requests through `run_in_executor` on the I/O loop; events
  only enqueue with the injected `now`; requests wait while `collection_changing`; reconnect backoff;
  credentials from `ObsDiscovery.credentials` at every connect; on auth failure re-read once, then a
  banner; `ObsInfo` check names the missing request.
- 207 `NotReady` (OBS still loading, or a collection change whose `...Changing` event has not
  arrived yet) is retried until a timeout, then `ObsRequestError`, in `connect()` and `request()`
  (S1 summary 5 and 11; contract change request from the W1 integration fix).
- Fake: obs-websocket v5 ops 0/1/2/5/6/7 with auth; replays R2 transcripts. One test per transcript.

### T13 OBS discovery + launch (Opus xhigh, W2)
- Files: `obs/discovery.py`; tests. Spec 11.1, 3.3 config roots, 17 rows 1-3.
- Install lookup (Windows ProgramFiles, then the S1 registry key; Linux PATH, then Flatpak); config
  roots incl. Flatpak and `$XDG_CONFIG_HOME` for native Linux OBS; websocket config read;
  enable-when-closed; password generated only when auth is required and none exists; running-OBS
  check; launch minimised with the right cwd; 30 s wait. `wait_ready` is true once `GetVersion`
  succeeds, not once the websocket accepts a connection (S1 summary 11).
- Tests with temp config roots, an injected registry reader and process runner.

### T14 OBS provisioning (Opus xhigh, W2)
- Files: `obs/provision.py` (GSM `parse_obs_window_target`, `get_video_source_priority` port
  headers); tests. Spec 11.3, 22.
- Profile (record dir, scaled output size, fps, container, split off with the M0 keys), scene
  collection and scene `Game`, per-platform inputs feature-detected with `GetInputKindList`, mic muted,
  window list; idempotent diff; `ProvisionResult.needs_restart` from the M0 restart rows.
  `list_windows` keeps each item's `itemEnabled` (`WindowItem.enabled`, contract change request)
  and reads `capture_window` on `xcomposite_input`, `window` on the Windows kinds (S1 summary 12).
- Tests: scaling math; second run sends no mutating request; each platform row with a fake gateway;
  provisioning transcript replay.

### T15 session actor + recorder (Opus xhigh, judge x2, W2)
- Files: `session/session.py`, `session/restore.py` (`obs_restore.json`), `obs/recorder.py`; tests.
- Spec 6 (all), 7 (zero event and latency from M0, drift samples), 10.2, 12 (actor side), 17 rows for
  arm, start, connection loss, OBS exit, split, zero cues, writability, free space.
- Owns: arming steps 1-4 incl. timeout restore; ownership rule; every reconcile row incl. matching
  an active recording to its manifest (S1/R2 method) and orphan finalise only after reconcile or once
  OBS is confirmed absent; ending without STOPPED; `RecordFileChanged`; pause edges; the auto-start
  first line held while `armed` and journalled at offset 0 on STARTED; `SessionEvent` publication;
  no-source and free-space banners.
- Pipeline and journal agree on "the previous line" (W1 integration): call `TextPipeline.reset()`
  after dropping an accepted line as `paused`, at STARTED unless the held auto-start line is the one
  journalled at offset 0, and after a split stop (`RecordFileChanged`). Journal a `Replaced` as
  `ReplaceRecord` only when its base line is the journal's last `LineRecord`; otherwise as a
  `LineRecord` at `clock.offset_ms(line.t_mono)` (`None`: drop, count `paused`).
- Finalise runs on one dedicated worker shared by every caller under the output root (a
  single-thread executor or a lock), one call at a time, never the default `run_in_executor` pool:
  its NN bump is check-then-act (`session/finalise.py` docstring).
- Tests with injected `now`, fake gateway/provisioner/discovery, deterministic.

### T16 runtime, composition, CLI verbs (Opus xhigh, judge, W2, after T12-T15)
- Files: `runtime/io_thread.py`, `gui/presenters/qt_presenter.py`, `app.py`, `launch.py`,
  `gui/cli_verbs.py`, minimal `gui/main_window.py` (Arm/Start/Stop, state, elapsed, cue count),
  logging to `<home>/anki_miner_game.log` (password never logged); tests.
- Spec 4.2, 16 global control, 17 feed-port row.
- Any `OSError` from `FeedServer.start()` means feed off + banner (`FeedPortInUseError` names the
  port; a missing `page.html` or another bind error gives its text). Launch-time orphan finalises go
  through T15's single finalise worker, never a shared pool.
- Tests: verbs reach the running instance; launch restores `obs_restore.json` and hands orphan
  handling to reconcile; feed port in use -> banner and feed off; offscreen launch with an isolated
  home writes `config.json` and the log.

### E1 Linux M1 exit probe (Opus xhigh, after W2 integration)
- Files: `tools/handoff_probe.py`, `docs/m0/m1-exit-linux.md`.
- Real Flatpak OBS in the nested display, a game profile with `capture.kind=xcomposite`,
  FakeHookerServer + flasher; drive a session with the CLI verbs; then the hand-off probe with Anki
  Miner's venv (read-only, isolated `ANKI_MINER_HOME`): same-stem auto-fill, batch pairing over three
  sessions, subtitle parse, audio clips for three cues; sync probe `--app` under 150 ms.

### T17 auto mode (Opus high, W2)
- `lifecycle/auto.py`; spec 12; subscribes to `SessionEvent`, sends `UserCommand`s; tests with fake
  clock and fake `SessionControl`. The window-closed check counts enabled items only (spec 12 as
  amended at the M0 gate, `docs/m0/wave-1-amendments.md` item 10).

### T18 clipboard source (Opus high, W2)
- `text/sources/clipboard_source.py`; spec 8.1; main thread, text only, ignores own changes, `t_mono`
  at the signal; pytest-qt tests.

### T22 Windows hotkey (Opus xhigh, W2)
- `gui/hotkey_win.py`; spec 16; hotkey-string parse tests on every platform; registration test
  `windows_only` runs on the CI Windows runner.

### T23 VAD add-on + trimmer (Opus xhigh, W2)
- `addons/vad_addon.py` (implements `AddonService`), `vad/trimmer.py` (implements `VadJobs`); spec 13,
  17 VAD row. Fake worker script in tests; re-run and restore from `live_cues`; manifest `vad`
  record and `vad_running` state; a real install test marked `network` + `vad`. Every uv call
  runs with `addons.bootstrap.uv_environment(home, "vad")`. Worker `total_ms` may be `null`
  (indeterminate progress; `Presenter.vad_progress` contract change request).

### T24 OCR add-on + supervisor (Opus xhigh, W2)
- `addons/ocr_addon.py` (`AddonService`, `OcrAreaPicker`), `text/sources/ocr_source.py`; spec 14,
  17 owocr row. Command builder; log parser against R3 fixtures; supervisor tree-kill with a fake
  child that spawns a grandchild; three restarts then a banner; Windows job-object test `windows_only`.
  Every uv call runs with `addons.bootstrap.uv_environment(home, "ocr")`.

### T19 main window, tray, banners, live list, recent sessions (Opus xhigh, W3)
- `gui/main_window.py` (replaces the minimal one), `gui/tray.py`, `gui/widgets/*`; spec 16, 17,
  Appendix C. Recent-session row: Open folder, context menu Re-run VAD / Restore untrimmed subtitle
  through `VadJobs`. Emits requests for dialogs and the wizard.

### T20 game profile + settings dialogs (Opus xhigh, W3)
- `gui/game_profile_dialog.py`, `gui/settings_dialog.py`; spec 5 tables, 11.3 window picker, 12
  settings texts, 14 area selection through `OcrAreaPicker`, cloud-OCR privacy text. The window
  picker offers enabled items only.

### T21 first-run wizard (Opus xhigh, W3)
- `gui/wizard.py`; spec 16 wizard, 11.1 (incl. restarting OBS when `needs_restart`), Wayland clipboard
  note, add-on sizes and installs through `AddonService`.

### T25 integration tests (Opus xhigh, W3)
- `tests/integration/test_scripted_sessions.py`: one run per R2 transcript through the real
  composition with FakeObsServer + FakeHookerServer + injected `now`; byte-exact `.srt`, manifest
  counts, final names.

### T26 GUI wiring (Opus xhigh, W3, after T19-T21)
- `app.py`, `gui/main_window.py` connections: dialogs, wizard, tray, hotkey, auto mode, clipboard,
  OCR, VAD; recent sessions refresh after finalise; offscreen launch smoke.

### T27 PyInstaller + bundle smoke (Opus xhigh, W4)
- `anki_miner_game.spec` (one-folder, data files `page.html`, `vad_worker.py`, `requirements.txt`,
  `excludes` onnxruntime/numpy/av/owocr), `scripts/bundle_smoke.sh` (offscreen launch, isolated home,
  asserts `config.json`, the log, and absent modules). Spec 19.
- `feed/page.html` is package data and the smoke fetches the page from a started feed.
- Frozen Linux HTTPS: at frozen launch, when OpenSSL's default CA file and directory are both
  missing, set `SSL_CERT_FILE` to the first distro bundle that exists (for example
  `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/certs/ca-certificates.crt`); the smoke makes one real
  `bootstrap.urllib_transport` HTTPS GET.

### T28 installers (Opus high, W4)
- `packaging/`: Inno Setup, AppImage, nfpm `.deb`, `.tar.gz`, shaped on Anki Miner's.

### T29 CI + release workflows (Opus xhigh, W4, after T27 + T28)
- `ci.yml` final (D3), `release.yml` (tag vs `__version__`, matrix, smokes, Windows installer smoke,
  `workflow_dispatch` dry run), `.github/release-matrix.json`, `scripts/release_dryrun.sh` (proves no
  tag and no release on dispatch).

### T30 user guide + README (Opus high, W4)
- `docs/user-guide.md`: quick start per hooker, OBS, Anki Miner hand-off and settings (Appendix A),
  troubleshooting from section 17, disk rate from R1, owocr `0.0.0.0` and cloud-OCR notes. README
  minimal per the global README rule. `docs/qa/h5-checklist.md`: spec 18.4 plus every D2 Windows
  item, the PipeWire capture row and the Wayland clipboard check.

### Dry-run loop (orchestrator, W4)
- `scripts/release_dryrun.sh` until `RELEASE DRY-RUN GREEN`; a red run gets an Opus xhigh fixer on a
  worktree.

### T31 VAD tuning (Opus xhigh, after H3) and T32 OCR tuning (Opus xhigh, after H4)
- T31: thresholds against at least three real recordings (voiced VN, voiced RPG with music,
  unvoiced); report plus sample clips for the user's ear check. T32: start shift, VAD start snap
  and `SNAP_LOOKBACK_MS` (provisional 10 s) against real OCR sessions; owocr picker round trip.

## 8. Verification

- Per task: card tests and the full gate in the worktree (kept `gate.log`), spec review, quality
  review (fact-check review for spikes).
- Per wave: integration gate on `integration/wave-N`, cross-task review, `--ff-only` to `main`, card
  tests re-run on `main`, CI green on the pushed `main` (D3).
- M0: `docs/m0/*.md` with measured numbers, transcripts committed, spec amended.
- M1: E1 on Linux; the Windows half at H5 (D2).
- Before release: sync probe `--app` under 150 ms on Linux (Windows at H5); release dry-run green; H5
  manual QA from `docs/qa/h5-checklist.md` (ten cards checked by ear and eye).

## 9. Close-out (orchestrator)

- `docs/IMPLEMENTATION_STATUS.md` rows with merge SHAs; the Anki Miner status file points to the repo.
- Memory: update `anki-miner-game-design` (repo exists, milestone state, traps); `MEMORY.md` line.
- Cancel timers and cron jobs.

## 10. Plan-judge disposition (round 1, 2026-09-21)

Adopted: W2 dependency graph (T16 after T12-T15; missing Protocols added to contracts); T06 stacked
on T02 + T04; orphan finalise moved behind reconcile; no partial integration + idempotent stages;
role preamble and absolute paths for every role; real uv path and the `.venv-vad` exception; sanitiser
counterexamples and full-class vendoring; missed-pause transcript; injected `now`; X11-in-nested-Xwayland
spikes with portal paths moved to H4/H5; output-activation recipes; Windows constants provisional;
auto-start first line owned by T15; parallel-safe test rules; W3 cross-dependencies through
Protocols; empty-line counter and constants module; T18/T22/T23/T24 moved to W2; judges only on five
tasks; Wayland clipboard spike dropped.
Not adopted: apt instead of Flathub OBS (owner chose D1); an early Windows probe kit (owner chose D2,
deferral to H5); restarting the orchestrator in the new repo (the owner asked for this session; the
preamble covers the CLAUDE.md mismatch at the cost of the extra injected text).
