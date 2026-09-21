# T15 session actor + recorder: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (one implementer, tasks in
> the order below: Tasks 3-6 grow the same module, `session/session.py`, step by step, so do not
> split them across agents) with superpowers:test-driven-development inside every task. Steps use
> checkbox (`- [ ]`) syntax.

**Goal:** The single-threaded session actor (spec 4.2, 6, 7, 10.2, 12 actor side, 17): arming and
the ownership rule, the recorder, the session's manifest and journal while recording, every way a
session ends, reconcile on every connect, orphan finalise and `obs_restore.json`, publishing every
`SessionEvent` the rest of the app follows.

**Architecture:** `SessionActor` owns one `asyncio.Queue` of `LineReceived`, `ObsEvent`,
`UserCommand` and `Tick`, consumed one message at a time on the I/O loop by `run()`; `post()` is
the only thread-safe entry. Handlers await OBS requests inline; an event a handler waits for (a
profile or collection `...Changed`) resolves a waiter as it is queued, so a switch never deadlocks
on its own queue. The actor holds the concrete record clocks (`EventClock`, `OutputDurationClock`,
T03), a `TextPipeline` per armed game (T05), the `Journal` and manifest of the running session
(T06), and finalises on one `FinaliseWorker` thread shared by every caller. OBS access goes through
the `ObsGateway`, `ObsDiscovery`, `Provisioner` and `Recorder` Protocols; text sources come from an
injected factory. Tests drive a real actor over in-process fakes with an injected clock.

**Tech Stack:** Python 3.12 stdlib (`asyncio`, `concurrent.futures`, `dataclasses`, `pathlib`,
`tempfile`, `shutil`); dev: pytest, pytest-asyncio (`asyncio_mode = "auto"`), black, ruff, mypy.
No new dependency.

**Spec:** `/home/light/Projects/anki_miner_game/.worktrees/t15-session/docs/specs/2026-09-20-anki-miner-game-design.md`
(sections 4.2, 6 all, 7, 8.2 last paragraph, 10.2, 10.3, 11.1 launch, 11.4, 12 auto-start, 17 rows
listed in the coverage map below, 18.1 row Reconcile) and the master plan
`/home/light/Projects/anki_miner_game/.worktrees/t15-session/docs/plans/2026-09-21-master-plan.md`
(sections 1-4 and the card `### T15 session actor + recorder`). M0 inputs:
`docs/m0/source-findings.md` (S1), `docs/m0/wave-1-amendments.md`, `docs/contracts.md`, and R1's
`docs/m0/clock.md` on branch `feat/r1-clock-sync` (read it with
`git -C /home/light/Projects/anki_miner_game show feat/r1-clock-sync:docs/m0/clock.md`).

## Global Constraints

Verbatim from the master plan; every task below inherits them.

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

## Working context

- Worktree `/home/light/Projects/anki_miner_game/.worktrees/t15-session`, branch
  `feat/t15-session`, BASE `052cc99` (`main` at planning time; the plan commit sits on top). No
  dependency branch is merged in: T15 codes against the Protocols in `interfaces/` and needs none
  of T12-T14's code. `.venv` is a symlink to `/home/light/Projects/anki_miner_game/.venv`.
- Read `/home/light/Projects/anki_miner_game/.worktrees/t15-session/CLAUDE.md` first. Never bare
  `python3`, never `uv run`, never `pip install -e`, no installs at all. Every Bash call starts
  with `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session &&`.
- Status file: `/home/light/Projects/anki_miner_game/.orchestration/status/t15-session.json`
  (keep `base_sha`).
- Files you own: `anki_miner_game/session/session.py`, `anki_miner_game/session/restore.py`,
  `anki_miner_game/obs/recorder.py`, `tests/session/conftest.py`, `tests/session/actor_harness.py`,
  `tests/session/test_restore.py`, `tests/session/test_session_actor.py`,
  `tests/session/test_session_arm.py`, `tests/session/test_session_recording.py`,
  `tests/session/test_session_endings.py`, `tests/session/test_session_reconcile.py`,
  `tests/obs/__init__.py`, `tests/obs/test_recorder.py`. Nothing else changes; `models/`,
  `interfaces/`, `store.py`, `paths.py` and the T02-T06 session modules are read-only here.
- File paths in the steps below are relative to the worktree root. `**Create**` writes the whole
  file; `**Replace** in` swaps the first code block for the second (the old text occurs exactly
  once); `**Append** to` adds the block at the end of the file.
- Single-file test runs pass `-n0 -p no:cacheprovider` to skip xdist start-up; Task 7 runs the real
  gate.
- Every piece of code in this plan was applied on 2026-09-21, task by task, to a scratch copy of
  this worktree at BASE: each "run it to fail" step failed as stated, each "run it to pass" step
  passed, and after Task 6 black, ruff and mypy were clean and the whole suite was green. Paste it
  as written.
- Before starting, re-read `docs/specs/2026-09-20-anki-miner-game-design.md` sections 6.3 and 7:
  if the M0 gate amended the degraded clock (decision 4 below), apply that amendment inside
  `_degraded_clock` and `_reanchor` only, and add a test beside `test_row3_*`.

## Decisions taken while planning

Answered from the card, the spec, the M0 inputs and the code; nobody else rules on these.

1. **One queue, handlers await inline, waiters for mid-handler events.** Every message is handled
   to completion before the next (spec 4.2). A switch waits for its `...Changed` event, which
   arrives through the same queue; `_enqueue` (on the loop) resolves a registered waiter before
   queueing the event, so the handler wakes while the event copy waits its turn and is then
   ignored. The waiter is registered before the request goes out, because obs-websocket sends
   events and responses from separate pool tasks with no order (source findings summary 7). The
   15 s switch timeout cancels only the actor's await: a request stuck in OBS (the restart question
   of source findings section 2 holds the `SetCurrentProfile` answer) keeps the gateway's request
   thread until the gateway's own request timeout drops the link (T12), and the restore that
   follows then fails and stays pending (decision 11).
2. **Finalise is awaited inside the handler.** The session's `STOPPED` handler closes the journal,
   writes the manifest, publishes `finalising`, awaits `FinaliseWorker.run` (up to ~10 s of rename
   retries on Windows) and returns to `armed`. Messages queue meanwhile; a `STARTED` that arrives
   during finalise is handled after it, in `armed`, and becomes the next session, which is right:
   OBS is on the app's profile and records into `_incoming/` (ownership by construction). The
   single worker keeps finalise's check-then-act NN bump safe (wave-1 amendment 8).
3. **Record clock constants from R1.** `ZERO_EVENT = "STARTED"` and `CAPTURE_LATENCY_MS = 10`
   (R1: `STARTED` receipt as the zero, 50 ms spread across encoders, 10 ms centres it). One named
   pair at the top of `session.py`; Linux values, the Windows ones provisional until H5 (D2).
4. **Degraded clock as the spec states it.** Reconcile rows 3 and 4 switch to the
   `OutputDurationClock` anchored on `GetRecordStatus.outputDuration` and re-anchored every 10 s,
   and flag `clock_degraded`. R1 measured that clock 0.46-5.6 s early (encoder latency) and
   proposed correcting it with a lag taken from the drift samples; that is an M0-gate decision not
   yet taken, so this plan implements the spec as written, confines the fallback to
   `_degraded_clock`/`_mark_degraded`/`_reanchor`, and the banner says the timing may be off by a
   few seconds.
5. **Matching an active recording to its manifest (reconcile row 4).** `GetOutputSettings` on the
   record output returns the file being written (source findings section 7). R2 is still running,
   but its run data already shows both halves: after a reconnect `simple_file_output`'s `path` is
   the file OBS reported at `STARTED`
   (`.orchestration/m0/data/r2-obs-behaviour/runs/reconnect-184034/reconnect.jsonl`, the
   `GetOutputSettings` answers before and after the drop), and after a split both the `path` and
   the `STOPPED` `outputPath` stay the first file's (`runs/split-184053/split.jsonl`). The actor tries `RECORD_OUTPUT_NAMES` (`simple_file_output`, `adv_file_output`,
   `adv_ffmpeg_output`, provisional until R2) and uses the first path returned; the manifest is
   `incoming_files(_incoming, path).manifest` in state `recording`, and the path must lie in
   `_incoming/`. When no output answers, the actor cannot tell which `_incoming/` session is live,
   so it finalises no orphan then; the next `STOPPED` or reconcile does. `GetOutputSettings` is not
   in `REQUIRED_REQUESTS`: a contract change request adding it (obs-websocket 5.0.0, floor
   unchanged) is filed in the status file at planning time. Nothing here depends on the ruling:
   the actor sends the request either way and treats a failure as "unknown".
6. **Rows 1-3 check the live file too.** While recording, reconcile treats the session as stopped
   when the record output is inactive (row 1) or writes a different file (stopped and restarted
   while the app was disconnected); the other recording is not a session.
7. **Stop offset without `STOPPED`** is the clock reading at `ExitStarted`, or at the
   `_ConnectionLost` that preceded reconcile row 1 or "OBS gone" (spec 6.4 "last clock reading").
   T03's `reading_ms` never returns less than the last journalled offset, so a line journalled
   after the loss becomes a zero-length click-through and is dropped by the cue rules instead of
   producing a cue past the video's end.
8. **"Reconcile finding OBS gone"** (spec 6.4): while recording with the connection lost, every
   `OBS_GONE_CHECK_S` (5 s) the actor asks `ObsDiscovery.is_running()` on a worker thread; `False`
   ends the session flagged `obs_exited`. The gateway keeps reconnecting on its own (T12).
9. **Ownership rule plus folder check.** `STARTED` makes a session only while `armed` (spec 6.2)
   and only when `outputPath` lies in the configured `_incoming/` (`Path.samefile`, so symlinks and
   Windows case do not matter). The failure it catches: the user switched OBS to another profile or
   record folder while the app was armed; without the check the actor would write a manifest for a
   video that is not in `_incoming/`, and finalise would fail on it at every launch. With it, a
   warning banner says that recording gets no subtitle.
10. **Arming order.** Writability of `_incoming/` first (creates it; nothing in OBS is touched on
    refusal), then connect (launching OBS when not running: `ensure_server_enabled`, `launch`,
    `wait_ready(30)`), then step 1 (an output OBS reports as not available, code 604, counts as
    inactive, so a host without a virtual camera can arm), step 2 (write `obs_restore.json` only
    when none exists and OBS is not already on both app names), step 3 (switch profile, then
    collection, each only when it exists and differs; a missing one is created and made current by
    the provisioner, T14), step 4 (`ensure_profile(cfg)`, `ensure_collection(profile)`, sources),
    free-space warning, `armed`. Any failure after step 1 undoes the arm (`_to_idle`: sources
    closed, state `idle`, OBS restored). Arming another game while armed runs the refusing checks
    first and stops the old sources only once they pass. `needs_restart` from `ensure_profile` is a
    warning banner; the wizard (T21) restarts OBS.
11. **Restore.** `_restore_obs` switches collection then profile back to the saved names and
    deletes the file. It runs at disarm, after a failed arm, on every `_Connected` while idle
    (which is the launch restore after an unclean exit) and every `RESTORE_RETRY_S` (10 s) while
    idle; it waits while any output is active (spec 6.2), skips a name OBS no longer has, and
    deletes an unreadable file.
12. **Sources.** Built by an injected `source_factory(cfg, profile)` at arm, started and stopped on
    the loop (TextSource contract); `_stop_sources` awaits `wait_closed()` for each. The status
    listener runs on the source's thread and is handed to the loop with `call_soon_threadsafe`
    before it becomes a `SourceStatusChanged`. The OBS light is `SourceStatusChanged(OBS_SOURCE_ID,
    ...)`: `connecting` while arming connects, `connected` / `disconnected` on the gateway's events.
13. **Counts.** A session's counters cover the frames from its start: `STARTED`, or the auto-start
    `START` whose held lines it journals (that `START` sets `received = accepted = 1` for the
    triggering line). `received` counts every frame while armed or recording; `duplicate`,
    `no_letters`, `junk` follow `DROP_COUNTER`; `paused` counts accepted lines dropped by the clock.
    `accepted` and `skip` are overwritten by finalise. The counts, `sources_used` and drift samples
    go into the manifest at every manifest write: `STARTED`, pause edges, split, degrade, end, quit.
14. **Lines that are not journalled are still published** (`LineAccepted(line, None)`): while
    armed, while paused, and after a split, so the live list and the text feed keep working.
    `replaces_previous=True` marks every typewriter merge, however it was journalled.
15. **Split** (spec 7): the first `RecordFileChanged` journals a stop at the clock reading, flags
    `split_unsupported`, resets the pipeline and raises a banner; later lines are not journalled
    and the final `STOPPED` adds no second stop. Finalise uses the manifest's first file; the second
    file stays in `_incoming/` without a manifest.
16. **Start failure.** `StartRecord` raising, no `STARTED` within `START_TIMEOUT_S` (10 s,
    provisional: OBS refuses some starts in a modal with no event, source findings section 6), or a
    `STOPPED` while a start is pending: `START_FAILED_BANNER_KEY` banner, held lines dropped, state
    stays `armed`. A late `STARTED` is still a session.
17. **Drift samples** (spec 7): `GetRecordStatus` at `STARTED` (after the manifest is written), at
    each pause edge, and on the Stop command before `StopRecord`; only while the output is active.
    A stop from OBS's own button has no stop sample.
18. **Quit** (`shutdown()`, awaited by T16 before the loop stops): armed -> disarm (OBS restored);
    recording -> sources closed, manifest written, journal closed, OBS keeps recording, and the next
    launch resumes the session (row 4) or finalises it (row 6).
19. **An unexpected exception in a handler** is logged and raises the `internal_error` banner; the
    actor keeps consuming. A subscriber that raises is logged and skipped.
20. **Resume without the profile** (row 4, profile deleted meanwhile): a `GameProfile(slug, title,
    text_mode)` built from the manifest, so the default sources still feed the journal.
21. **`FinaliseWorker` lives in `session.py`** (the card's files). T16 builds one instance and
    passes it to the actor and to anything else that finalises.
22. **Banner keys** are the `BannerKey` enum in `session.py` plus `START_FAILED_BANNER_KEY`; a key
    is cleared only after it was raised, so no stray `BannerCleared` events.

## M0 values and where they live

All at the top of `anki_miner_game/session/session.py`, in one block:

| Constant | Value | Source | Status |
|---|---|---|---|
| `ZERO_EVENT`, `CAPTURE_LATENCY_MS` | `"STARTED"`, `10` | R1 `docs/m0/clock.md` | Linux measured; Windows provisional until H5 |
| `START_TIMEOUT_S` | `10.0` | S1 section 6 (failed start sends no event), R1 (`STARTED` 7-184 ms after the request) | provisional until R2 |
| `RECORD_OUTPUT_NAMES` | `simple_file_output`, `adv_file_output`, `adv_ffmpeg_output` | S1 section 7, R2 reconnect run | provisional until R2 |
| `SWITCH_TIMEOUT_S` | `15.0` | spec 6.2 step 3 | fixed |
| `OBS_LAUNCH_TIMEOUT_S` | `30.0` | spec 17 | fixed |
| `REANCHOR_S` | `10.0` | spec 7 | fixed |
| `FREE_SPACE_WARN_BYTES` | `5 * 10**9` | spec 17 | fixed |
| `TICK_S`, `OBS_GONE_CHECK_S`, `RESTORE_RETRY_S` | `1.0`, `5.0`, `10.0` | this module's timers | fixed |
| `INVALID_RESOURCE_STATE` | `604` | obs-websocket `RequestStatus.h` | fixed |

## File structure

| File | Responsibility |
|---|---|
| `anki_miner_game/session/restore.py` | `obs_restore.json`: `ObsRestore`, `restore_path`, `load_restore`, `save_restore`, `delete_restore` |
| `anki_miner_game/obs/recorder.py` | `ObsRecorder`: `StartRecord` / `StopRecord` over the gateway |
| `anki_miner_game/session/session.py` | constants, `BannerKey`, `FinaliseWorker`, `SessionActor` |
| `tests/session/actor_harness.py` | `FakeObs`, `FakeGateway`, `FakeDiscovery`, `FakeProvisioner`, `FakeSource`, `FakeVadJobs`, `Harness`, `profile()` |
| `tests/session/conftest.py` | fixtures `rig` (built, not started) and `h` (running) |
| `tests/session/test_restore.py` | Task 1 |
| `tests/obs/__init__.py`, `tests/obs/test_recorder.py` | Task 2 |
| `tests/session/test_session_actor.py` | Task 3: post, subscribers, listener marshalling, ticker, finalise worker |
| `tests/session/test_session_arm.py` | Task 3: arming, disarming, restore |
| `tests/session/test_session_recording.py` | Task 4 |
| `tests/session/test_session_endings.py` | Task 5 |
| `tests/session/test_session_reconcile.py` | Task 6 |

## Interfaces produced

T16 (composition, CLI verbs), T17 (auto mode, already written against `SessionControl`), T19/T26 (GUI)
and T25 (integration) rely on exactly these names.

```python
# anki_miner_game/session/session.py
ZERO_EVENT: Final = "STARTED"; CAPTURE_LATENCY_MS: Final = 10
START_TIMEOUT_S: Final = 10.0; RECORD_OUTPUT_NAMES: Final = ("simple_file_output", "adv_file_output", "adv_ffmpeg_output")
SWITCH_TIMEOUT_S: Final = 15.0; OBS_LAUNCH_TIMEOUT_S: Final = 30.0; REANCHOR_S: Final = 10.0
FREE_SPACE_WARN_BYTES: Final = 5 * 10**9; TICK_S: Final = 1.0; OBS_GONE_CHECK_S: Final = 5.0; RESTORE_RETRY_S: Final = 10.0

class BannerKey(StrEnum):   # Banner.key values, besides START_FAILED_BANNER_KEY
    OBS, ARM, LOW_DISK, OBS_RESTART, RESTORE, NO_SOURCE, STOP_FAILED, FOREIGN_RECORDING,
    SESSION_FILES, SPLIT, CLOCK, OBS_EXITED, FINALISE, NO_CUES, INTERNAL

class FinaliseWorker:
    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None: ...
    async def run(self, manifest_path: Path, cfg: AppConfig) -> FinaliseResult: ...   # FinaliseError passes through
    def shutdown(self) -> None: ...

class SessionActor:                     # satisfies interfaces.session.SessionControl
    def __init__(self, *, loop: asyncio.AbstractEventLoop, gateway: ObsGateway, discovery: ObsDiscovery,
                 provisioner: Provisioner, recorder: Recorder, finaliser: FinaliseWorker,
                 get_config: Callable[[], AppConfig], get_profile: Callable[[str], GameProfile | None],
                 source_factory: Callable[[AppConfig, GameProfile], Sequence[TextSource]],
                 vad_jobs: VadJobs | None = None, now: Callable[[], float] = time.monotonic,
                 utc_now: Callable[[], datetime] = ..., disk_free: Callable[[Path], int] = ...,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
    def post(self, msg: SessionInput) -> None: ...          # any thread
    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None: ...
    @property
    def state(self) -> AppState: ...
    async def run(self) -> None: ...        # launch duties, then the queue, until shutdown(); schedule once on the I/O loop
    async def join(self) -> None: ...       # launch done and every queued message handled
    async def shutdown(self) -> None: ...   # on the I/O loop, while run() runs; awaits wait_closed of every source

# anki_miner_game/session/restore.py
RESTORE_FILENAME: Final = "obs_restore.json"
@dataclass(frozen=True, kw_only=True) class ObsRestore: profile: str; collection: str
def restore_path() -> Path: ...; def load_restore(path: Path) -> ObsRestore | None: ...
def save_restore(path: Path, saved: ObsRestore) -> None: ...; def delete_restore(path: Path) -> None: ...

# anki_miner_game/obs/recorder.py
class ObsRecorder:                      # satisfies interfaces.obs.Recorder
    def __init__(self, gateway: ObsGateway) -> None: ...
    async def start(self) -> None: ...; async def stop(self) -> None: ...
```

For T16: build `FinaliseWorker()` once; build the actor on the I/O loop's thread with that loop;
`gateway.subscribe` is done by the actor itself; the sink the sources get posts `LineReceived`
through `post`; forward `SessionEvent`s to the Presenter (and accepted lines to the feed) with
`actor.subscribe`; schedule `actor.run()` once; on quit `await actor.shutdown()`, then
`finaliser.shutdown()`, then close the gateway.

## Spec coverage map

| Requirement (card, spec) | Where | Tests |
|---|---|---|
| Arming steps 1-4, timeout restore (6.2) | Task 3 `_arm`, `_switch_to_app`, `_switch`, `_to_idle` | `test_session_arm.py` |
| Disarm, restore file, launch restore (6.2, 17 unclean exit) | Task 3 `_restore_obs`; Task 6 reconcile | `test_disarm_*`, `test_restore_*`, `test_launch_restores_*` |
| Ownership rule (6.2) | Task 4 `_on_started` | `test_a_recording_that_starts_while_idle_*`, `test_a_recording_outside_*` |
| Recorder (11.4) | Task 2 | `tests/obs/test_recorder.py` |
| Manifest + journal at `STARTED`, NN (10.2) | Task 4 `_on_started` | `test_started_writes_*`, `test_the_session_number_*` |
| Lines, pipeline reset, replace rule (8.2, W1 amendments 3) | Task 4 `_on_line`, `_accept`, `_replace`, `_journal_line` | `test_lines_while_armed_*`, `test_a_pause_*`, `test_a_merge_*` |
| Pause edges, drift samples (7) | Task 4 `_on_pause_edge`, `_sample_drift` | `test_a_pause_drops_lines_*`, `test_a_whole_session_*` |
| Auto-start hold (12, W2a) | Task 4 `_start`, `_accept`, `_replace`, `_on_started` | `test_auto_start_*`, `test_a_merge_into_a_held_line_*`, `test_a_failed_start_*` |
| StartRecord fails (17) | Task 4 `_start_failed`, `_on_tick` | `test_a_failed_start_record_*`, `test_no_started_within_*` |
| No text source at Start (17) | Task 4 `_on_started`, Task 3 `_on_source_status` | `test_no_connected_source_*` |
| Zero cues (17), finalise worker (10.3, W1 amendment 8) | Task 4 `_finalise`; Task 3 `FinaliseWorker` | `test_zero_cues_*`, `test_finalise_worker_*` |
| Writability, free space (17) | Task 3 `_writable`, `_arm` | `test_an_output_folder_*`, `test_low_free_space_*` |
| Split (7, 17) | Task 5 `_on_split` | `test_a_split_*` |
| OBS exit, connection loss (6.4, 17) | Task 5 `_obs_gone`, `_on_tick` | `test_exit_started_*`, `test_lines_keep_*`, `test_obs_gone_*` |
| Reconcile rows 1-6, matching (6.3, 18.1) | Task 6 `_reconcile`, `_live_output_path`, `_manifest_for`, `_resume` | `test_row1_*` ... `test_row6_*` |
| Degraded clock and re-anchor (7) | Task 6 `_degraded_clock`, `_mark_degraded`, `_reanchor` | `test_row3_*`, `test_row4_*` |
| Orphan finalise only after reconcile or with OBS absent (card) | Task 6 `_launch`, `_sweep_orphans`, `_on_stopped` | `test_row5_*`, `test_row6_*`, `test_orphans_*` |
| `SessionEvent` publication, `StateChanged.slug` (W2a) | Task 3 `_set_state` | `test_arming_another_game_*`, `test_a_whole_session_*` |
| Sources on the actor's thread, `wait_closed` at disarm, listener marshalling (W2a) | Task 3 `_start_sources`, `_stop_sources`, `_source_status_listener` | `test_sources_start_*`, `test_status_listener_*`, `test_disarm_*` |
| Quit (T16 relies on it) | Task 3/4 `_shutdown` | `test_quitting_*` |

---

### Task 1: `obs_restore.json`

**Files:**
- Create: `anki_miner_game/session/restore.py`
- Test: `tests/session/test_restore.py`

**Interfaces:**
- Consumes: `paths.home()`; `models.codec.dump_document`; `store._read`, `store._write` (the same
  private helpers `session/manifest.py` uses).
- Produces: `RESTORE_FILENAME`, `ObsRestore(profile, collection)`, `restore_path() -> Path`,
  `load_restore(path) -> ObsRestore | None`, `save_restore(path, saved) -> None`,
  `delete_restore(path) -> None`.

- [ ] **Step 1: Write the failing test**

**Create** `tests/session/test_restore.py`:

```python
"""``obs_restore.json`` (spec 6.2)."""

import json

import pytest

from anki_miner_game.session.restore import (
    RESTORE_FILENAME,
    ObsRestore,
    delete_restore,
    load_restore,
    restore_path,
    save_restore,
)
from anki_miner_game.store import CorruptFileError, FutureSchemaError


def test_restore_path_follows_the_isolated_home(_isolate_game_home):
    assert restore_path() == _isolate_game_home / RESTORE_FILENAME


def test_round_trip_carries_the_schema(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    save_restore(path, ObsRestore(profile="Untitled", collection="Streaming"))
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "profile": "Untitled",
        "collection": "Streaming",
    }
    assert load_restore(path) == ObsRestore(profile="Untitled", collection="Streaming")


def test_missing_file_is_none(tmp_path):
    assert load_restore(tmp_path / RESTORE_FILENAME) is None


def test_corrupt_and_future_files_raise(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptFileError):
        load_restore(path)
    path.write_text('{"schema": 2, "profile": "a", "collection": "b"}', encoding="utf-8")
    with pytest.raises(FutureSchemaError):
        load_restore(path)


def test_save_leaves_no_temporary_file(tmp_path):
    save_restore(tmp_path / RESTORE_FILENAME, ObsRestore(profile="a", collection="b"))
    assert sorted(p.name for p in tmp_path.iterdir()) == [RESTORE_FILENAME]


def test_delete_is_idempotent(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    save_restore(path, ObsRestore(profile="a", collection="b"))
    delete_restore(path)
    delete_restore(path)
    assert not path.exists()
```

- [ ] **Step 2: Run it to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_restore.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'anki_miner_game.session.restore'`).

- [ ] **Step 3: Implement**

**Create** `anki_miner_game/session/restore.py`:

```python
"""``obs_restore.json``: the OBS profile and scene collection to switch back to (spec 6.2).

Arming writes the user's current names here before it switches OBS to the app's own profile and
collection; disarm switches back and deletes the file. A file still present at launch means the app
exited while armed, and the session actor restores it at the first connection that allows it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from anki_miner_game import paths
from anki_miner_game.models.codec import dump_document
from anki_miner_game.store import _read, _write

RESTORE_FILENAME: Final = "obs_restore.json"


@dataclass(frozen=True, kw_only=True)
class ObsRestore:
    profile: str
    """``GetProfileList.currentProfileName`` before arming."""
    collection: str
    """``GetSceneCollectionList.currentSceneCollectionName`` before arming."""


def restore_path() -> Path:
    """``<home>/obs_restore.json``, read from ``paths.home()`` at call time."""
    return paths.home() / RESTORE_FILENAME


def load_restore(path: Path) -> ObsRestore | None:
    """The saved names, or ``None`` when there is no file.

    Raises ``store.CorruptFileError`` or ``store.FutureSchemaError`` when the file cannot be used and
    ``store.StoreError`` when it cannot be read.
    """
    try:
        return _read(ObsRestore, path)
    except FileNotFoundError:
        return None


def save_restore(path: Path, saved: ObsRestore) -> None:
    """Write through a temporary file and ``os.replace``; ``store.StoreWriteError`` leaves no file behind."""
    _write(path, dump_document(saved))


def delete_restore(path: Path) -> None:
    path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run it to pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_restore.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/session/restore.py tests/session/test_restore.py && git commit -m "feat(session): keep the OBS names to restore in obs_restore.json"
```

---

### Task 2: the recorder

**Files:**
- Create: `anki_miner_game/obs/recorder.py`
- Test: `tests/obs/__init__.py` (empty), `tests/obs/test_recorder.py`

**Interfaces:**
- Consumes: `interfaces.obs.ObsGateway.request`.
- Produces: `ObsRecorder(gateway)` with `async start()` (`StartRecord`) and `async stop()`
  (`StopRecord`); satisfies `interfaces.obs.Recorder`. `ObsError` reaches the caller unchanged.

- [ ] **Step 1: Write the failing test**

**Create** `tests/obs/__init__.py`:

```python
```

**Create** `tests/obs/test_recorder.py`:

```python
"""The recorder (spec 11.4)."""

from typing import Any

import pytest

from anki_miner_game.interfaces.obs import Recorder
from anki_miner_game.models.obs import ObsRequestError
from anki_miner_game.obs.recorder import ObsRecorder


class Gateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        self.sent.append((name, fields))
        if self.error is not None:
            raise self.error
        return {}


async def test_start_and_stop_send_one_request_each():
    gateway = Gateway()
    recorder: Recorder = ObsRecorder(gateway)
    await recorder.start()
    await recorder.stop()
    assert gateway.sent == [("StartRecord", {}), ("StopRecord", {})]


async def test_obs_failure_reaches_the_caller():
    gateway = Gateway(ObsRequestError("StartRecord", 500, "Output already running"))
    with pytest.raises(ObsRequestError, match="500"):
        await ObsRecorder(gateway).start()
```

- [ ] **Step 2: Run it to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/obs/test_recorder.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'anki_miner_game.obs.recorder'`).

- [ ] **Step 3: Implement**

**Create** `anki_miner_game/obs/recorder.py`:

```python
"""The recorder (spec 11.4): ``StartRecord`` and ``StopRecord``, nothing else.

It changes no state itself. The session actor follows ``RecordStateChanged`` events (spec 6), so a
recording started from the OBS window, a hotkey or this recorder behaves the same, and the actor
alone decides when a start is allowed (only while ``armed``).
"""

from anki_miner_game.interfaces.obs import ObsGateway


class ObsRecorder:
    """``Recorder`` over an ``ObsGateway``. Errors (``ObsError``) reach the caller unchanged."""

    def __init__(self, gateway: ObsGateway) -> None:
        self._gateway = gateway

    async def start(self) -> None:
        """``StartRecord``. OBS answers once the start is queued, before ``STARTING`` (source findings 18)."""
        await self._gateway.request("StartRecord")

    async def stop(self) -> None:
        await self._gateway.request("StopRecord")
```

- [ ] **Step 4: Run it to pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/obs/test_recorder.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/obs/recorder.py tests/obs/__init__.py tests/obs/test_recorder.py && git commit -m "feat(obs): add the recorder: StartRecord and StopRecord"
```

---

### Task 3: the actor, arming and disarming

**Files:**
- Create: `anki_miner_game/session/session.py`
- Test: `tests/session/actor_harness.py`, `tests/session/conftest.py`,
  `tests/session/test_session_actor.py`, `tests/session/test_session_arm.py`

**Interfaces:**
- Consumes: Task 1 (`restore_path`, `load_restore`, `save_restore`, `delete_restore`,
  `ObsRestore`); Task 2 (`ObsRecorder`, in the harness); `interfaces.obs` (`ObsGateway`,
  `ObsDiscovery`, `Provisioner`, `Recorder`), `interfaces.text_source.TextSource`,
  `interfaces.addons.VadJobs`; `models.messages` (`SessionInput`, `SessionEvent` members,
  `OBS_SOURCE_ID`, `START_FAILED_BANNER_KEY`); `models.obs` (errors, `ObsEventName`);
  `models.profile.validate`; `text.pipeline.TextPipeline(filters)`;
  `session.finalise.finalise(manifest_path, cfg, *, sleep) -> FinaliseResult`;
  `paths.incoming_dir(cfg)`.
- Produces: the module constants and `BannerKey` of "Interfaces produced"; `FinaliseWorker`;
  `SessionActor` with `post`, `subscribe`, `state`, `run`, `join`, `shutdown`; arming, disarming,
  restore, sources. Private names later tasks extend: `_handle`, `_on_command`, `_on_obs_event`,
  `_on_tick`, `_shutdown`, `_launch` (replaced whole), `_publish`, `_set_state`, `_banner`,
  `_clear`, `_obs_status`, `_to_idle`, `_active_outputs`, `_switch`, `_restore_obs`,
  `_ensure_connected`, `_start_sources`, `_stop_sources`.

- [ ] **Step 1: Write the test harness**

**Create** `tests/session/actor_harness.py`:

```python
"""Fakes and a running actor for the session actor tests (spec 18: injected ``now``, fake OBS).

``FakeObs`` is a small model of what OBS reports; ``FakeGateway`` answers the actor's requests from
it and, like the real gateway, hands events to the subscribed handler (the actor's ``post``).
``Harness`` builds a ``SessionActor`` over these fakes with an isolated output root, runs it on the
test's loop, and records every ``SessionEvent``. Ticks come only from the tests (``tick``).
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    AppState,
    BannerCleared,
    BannerRaised,
    CommandKind,
    LineAccepted,
    ObsEvent,
    SessionEvent,
    SessionFinalised,
    SourceStatus,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsConnectError,
    ObsEventName,
    ObsInfo,
    ObsRequestError,
    OutputState,
    ProvisionResult,
)
from anki_miner_game.models.profile import FilterSettings, GameProfile
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.session.session import FinaliseWorker, SessionActor

SLUG = "steins-gate"
TITLE = "Steins;Gate"
OBS_STEM = "2026-10-02 18-04-11"
T0 = 1000.0
"""The fake monotonic clock's start."""
UTC_NOW = datetime(2026, 10, 2, 18, 4, 11, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeObs:
    """What OBS reports. ``None`` for replay buffer or virtual camera: not available (604)."""

    profile: str = "Untitled"
    collection: str = "Untitled"
    profiles: list[str] = field(default_factory=lambda: ["Untitled", OBS_PROFILE_NAME])
    collections: list[str] = field(default_factory=lambda: ["Untitled", OBS_COLLECTION_NAME])
    stream_active: bool = False
    replay_active: bool | None = None
    vcam_active: bool | None = None
    record_active: bool = False
    record_paused: bool = False
    output_duration: int = 0
    output_path: str | None = None
    """``simple_file_output``'s ``path``; ``None`` makes ``GetOutputSettings`` fail for every name."""
    version: str = "32.2.2"
    websocket: str = "5.7.4"
    lost_switch_events: int = 0
    """How many of the next ``...Changed`` events after a ``SetCurrent...`` never arrive (the switch times out)."""


class FakeGateway:
    """``ObsGateway`` over ``FakeObs``. ``fail(name, error)`` makes the next ``name`` request raise."""

    def __init__(self, obs: FakeObs, clock: FakeClock) -> None:
        self.obs = obs
        self.clock = clock
        self.handlers: list[Callable[[ObsEvent], None]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, list[Exception]] = {}
        self.connect_error: Exception | None = None
        self.connects = 0
        self.connected = False

    # ObsGateway
    async def connect(self) -> ObsInfo:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.emit(ObsEventName.CONNECTED)
        return ObsInfo(self.obs.version, self.obs.websocket, frozenset(REQUIRED_REQUESTS))

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None:
        self.handlers.append(handler)

    @property
    def collection_changing(self) -> bool:
        return False

    async def close(self) -> None:
        self.connected = False

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        self.sent.append((name, fields))
        pending = self.errors.get(name)
        if pending:
            raise pending.pop(0)
        if not self.connected:
            raise ObsConnectError("not connected")
        return self._answer(name, fields)

    # test side
    def emit(self, name: str, data: dict[str, Any] | None = None, t: float | None = None) -> None:
        event = ObsEvent(name, data or {}, self.clock() if t is None else t)
        for handler in self.handlers:
            handler(event)

    def record_event(self, state: OutputState, path: str | None = None, t: float | None = None) -> None:
        self.emit(ObsEventName.RECORD_STATE_CHANGED, {"outputState": state, "outputPath": path}, t)

    def names(self) -> list[str]:
        return [name for name, _ in self.sent]

    def fail(self, name: str, error: Exception) -> None:
        self.errors.setdefault(name, []).append(error)

    def _answer(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        obs = self.obs
        match name:
            case "GetVersion":
                return {"obsVersion": obs.version, "obsWebSocketVersion": obs.websocket}
            case "GetStreamStatus":
                return {"outputActive": obs.stream_active}
            case "GetRecordStatus":
                return {
                    "outputActive": obs.record_active,
                    "outputPaused": obs.record_paused,
                    "outputDuration": obs.output_duration,
                }
            case "GetReplayBufferStatus" | "GetVirtualCamStatus":
                active = obs.replay_active if name == "GetReplayBufferStatus" else obs.vcam_active
                if active is None:
                    raise ObsRequestError(name, 604, "not available")
                return {"outputActive": active}
            case "GetProfileList":
                return {"currentProfileName": obs.profile, "profiles": list(obs.profiles)}
            case "GetSceneCollectionList":
                return {"currentSceneCollectionName": obs.collection, "sceneCollections": list(obs.collections)}
            case "SetCurrentProfile":
                obs.profile = fields["profileName"]
                self._switched(ObsEventName.CURRENT_PROFILE_CHANGED, {"profileName": obs.profile})
                return {}
            case "SetCurrentSceneCollection":
                obs.collection = fields["sceneCollectionName"]
                self._switched(ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED, {"sceneCollectionName": obs.collection})
                return {}
            case "GetOutputSettings":
                if fields["outputName"] != "simple_file_output" or obs.output_path is None:
                    raise ObsRequestError(name, 600, "No output was found")
                return {"outputSettings": {"path": obs.output_path, "muxer_settings": ""}}
            case "StartRecord" | "StopRecord":
                return {}
        raise AssertionError(f"unexpected request {name}")

    def _switched(self, event: str, data: dict[str, Any]) -> None:
        if self.obs.lost_switch_events:
            self.obs.lost_switch_events -= 1
        else:
            self.emit(event, data)


class FakeDiscovery:
    def __init__(self) -> None:
        self.running = True
        self.ready = True
        self.launches = 0
        self.enabled = 0
        self.running_checks = 0

    def is_running(self) -> bool:
        self.running_checks += 1
        return self.running

    def ensure_server_enabled(self) -> bool:
        self.enabled += 1
        return True

    def launch(self) -> None:
        self.launches += 1
        self.running = True

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        return self.ready


class FakeProvisioner:
    def __init__(self) -> None:
        self.profiles: list[AppConfig] = []
        self.collections: list[GameProfile] = []
        self.error: Exception | None = None
        self.needs_restart = False

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        self.profiles.append(cfg)
        if self.error is not None:
            raise self.error
        return ProvisionResult(changed=False, needs_restart=self.needs_restart)

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult:
        self.collections.append(profile)
        return ProvisionResult(changed=False, needs_restart=False)

    async def list_windows(self) -> list[Any]:
        return []


class FakeSource:
    """A text source; ``line`` calls the sink as the source's own thread would."""

    def __init__(self, source_id: str, status: SourceStatus = SourceStatus.CONNECTED) -> None:
        self._id = source_id
        self._status = status
        self.sink: LineSink | None = None
        self.listener: StatusListener | None = None
        self.starts = 0
        self.stops = 0
        self.closed = 0
        self.start_thread: int | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return self._status

    def start(self, sink: LineSink) -> None:
        self.starts += 1
        self.start_thread = threading.get_ident()
        self.sink = sink

    def stop(self) -> None:
        self.stops += 1
        self.set_status(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)
        self.closed += 1

    def set_status_listener(self, cb: StatusListener) -> None:
        self.listener = cb

    def set_status(self, status: SourceStatus) -> None:
        if status is self._status:
            return
        self._status = status
        if self.listener is not None:
            self.listener(self._id, status)

    def line(self, raw: str, t_mono: float) -> None:
        assert self.sink is not None, "source not started"
        self.sink(raw, t_mono, self._id)


class FakeVadJobs:
    def __init__(self) -> None:
        self.queued: list[Path] = []

    def queue(self, manifest_path: Path) -> None:
        self.queued.append(manifest_path)

    def rerun(self, manifest_path: Path) -> None:
        raise AssertionError("the actor never re-runs")

    def restore(self, manifest_path: Path) -> None:
        raise AssertionError("the actor never restores")


def profile(*, typewriter: bool = False, slug: str = SLUG, title: str = TITLE) -> GameProfile:
    return GameProfile(slug=slug, title=title, filters=FilterSettings(typewriter_merge=typewriter))


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()


class Harness:
    def __init__(self, tmp_path: Path, *, sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        """``sleep`` paces the actor's ``Tick`` timer; by default it never fires (tests call ``tick``)."""
        self.output_root = tmp_path / "out"
        self.cfg = AppConfig(output_root=str(self.output_root))
        self.incoming = self.output_root / "_incoming"
        self.clock = FakeClock()
        self.obs = FakeObs()
        self.gateway = FakeGateway(self.obs, self.clock)
        self.discovery = FakeDiscovery()
        self.provisioner = FakeProvisioner()
        self.vad = FakeVadJobs()
        self.profiles = {SLUG: profile()}
        self.sources = [FakeSource("textractor")]
        self.free_bytes = 100 * 10**9
        self.events: list[SessionEvent] = []
        self.finaliser = FinaliseWorker(sleep=lambda _s: None)
        self.actor = SessionActor(
            loop=asyncio.get_running_loop(),
            gateway=self.gateway,
            discovery=self.discovery,
            provisioner=self.provisioner,
            recorder=ObsRecorder(self.gateway),
            finaliser=self.finaliser,
            get_config=lambda: self.cfg,
            get_profile=self.profiles.get,
            source_factory=lambda _cfg, _game: self.sources,
            vad_jobs=self.vad,
            now=self.clock,
            utc_now=lambda: UTC_NOW,
            disk_free=lambda _folder: self.free_bytes,
            sleep=sleep or _never,
        )
        self.actor.subscribe(self.events.append)
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.actor.run())
        await self.settle()

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            await self.actor.shutdown()
            await self.task
        self.finaliser.shutdown()

    async def settle(self) -> None:
        """Let every posted message, and every message those post, be handled."""
        for _ in range(5):
            await asyncio.sleep(0)
            await self.actor.join()

    async def send(self, kind: CommandKind, slug: str | None = None, line: GameLine | None = None) -> None:
        self.actor.post(UserCommand(kind, slug=slug, line=line))
        await self.settle()

    async def tick(self, t: float) -> None:
        self.clock.t = t
        self.actor.post(Tick(t))
        await self.settle()

    async def arm(self, slug: str = SLUG) -> None:
        await self.send(CommandKind.ARM, slug)

    async def emit(self, name: str, data: dict[str, Any] | None = None, t: float | None = None) -> None:
        """OBS sends an event at ``t`` (default: now); the fake clock moves to ``t``."""
        if t is not None:
            self.clock.t = t
        self.gateway.emit(name, data, t)
        await self.settle()

    def video_path(self, stem: str = OBS_STEM) -> Path:
        return self.incoming / f"{stem}.mkv"

    async def started(self, t: float = T0 + 1.0, stem: str = OBS_STEM) -> Path:
        """OBS starts recording into ``_incoming/``: the video appears, then ``STARTED`` arrives at ``t``."""
        video = self.video_path(stem)
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
        self.obs.record_active = True
        self.obs.output_path = str(video)
        self.clock.t = t
        self.gateway.record_event(OutputState.STARTED, str(video), t)
        await self.settle()
        return video

    async def stopped(self, t: float, stem: str = OBS_STEM) -> None:
        self.obs.record_active = False
        self.obs.record_paused = False
        self.clock.t = t
        self.gateway.record_event(OutputState.STOPPED, str(self.video_path(stem)), t)
        await self.settle()

    async def line(self, raw: str, t: float, source: int = 0) -> None:
        self.clock.t = t
        self.sources[source].line(raw, t)
        await self.settle()

    def states(self) -> list[tuple[AppState, str | None]]:
        return [(e.state, e.slug) for e in self.events if isinstance(e, StateChanged)]

    def banners(self) -> dict[str, str]:
        """Banners currently shown, by key."""
        shown: dict[str, str] = {}
        for e in self.events:
            if isinstance(e, BannerRaised):
                shown[e.banner.key] = e.banner.text
            elif isinstance(e, BannerCleared):
                shown.pop(e.key, None)
        return shown

    def accepted(self) -> list[LineAccepted]:
        return [e for e in self.events if isinstance(e, LineAccepted)]

    def finalised(self) -> list[Path]:
        return [e.manifest_path for e in self.events if isinstance(e, SessionFinalised)]

    def game_dir(self) -> Path:
        return self.output_root / TITLE
```

**Create** `tests/session/conftest.py`:

```python
"""Fixtures for the session actor tests (``tests/session/actor_harness.py``)."""

import pytest

from tests.session.actor_harness import Harness


@pytest.fixture
async def rig(tmp_path):
    """A ``Harness`` that is built but not started: set the fakes up, then ``await rig.start()``."""
    harness = Harness(tmp_path)
    yield harness
    await harness.stop()


@pytest.fixture
async def h(rig):
    """A running ``Harness``: OBS runs, the launch connected, the actor is idle."""
    await rig.start()
    return rig
```

- [ ] **Step 2: Write the failing tests**

**Create** `tests/session/test_session_actor.py`:

```python
"""The actor's own machinery: thread-safe ``post``, marshalled status listener, ticker, finalise
worker (spec 4.2, 10.3; wave 2a contracts)."""

import asyncio
import threading
import time

from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    CommandKind,
    SourceStatus,
    SourceStatusChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.finalise import FinaliseResult
from anki_miner_game.session.session import BannerKey, FinaliseWorker, SessionActor
from tests.session.actor_harness import (
    SLUG,
    FakeClock,
    FakeDiscovery,
    FakeGateway,
    FakeObs,
    FakeProvisioner,
    Harness,
)


def test_the_actor_is_a_session_control(h: Harness):
    control: SessionControl = h.actor
    assert control.state is AppState.IDLE


async def test_launch_connects_to_a_running_obs(h: Harness):
    assert h.gateway.connects == 1
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.CONNECTED) in h.events


async def test_post_from_another_thread_is_handled_on_the_loop(h: Harness):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda _event: seen.append(threading.get_ident()))
    worker = threading.Thread(target=h.actor.post, args=(UserCommand(CommandKind.ARM, slug=SLUG),))
    worker.start()
    worker.join()
    await h.settle()
    assert h.actor.state is AppState.ARMED
    assert seen and set(seen) == {loop_thread}


async def test_status_listener_from_another_thread_is_published_on_the_loop(h: Harness):
    await h.arm()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda e: seen.append(threading.get_ident()) if isinstance(e, SourceStatusChanged) else None)
    worker = threading.Thread(target=h.sources[0].set_status, args=(SourceStatus.RECEIVING,))
    worker.start()
    worker.join()
    await h.settle()
    assert SourceStatusChanged("textractor", SourceStatus.RECEIVING) in h.events
    assert seen == [loop_thread]


async def test_sources_start_on_the_loop_thread(h: Harness):
    await h.arm()
    assert h.sources[0].start_thread == threading.get_ident()


async def test_a_failing_subscriber_does_not_stop_the_actor(h: Harness):
    def broken(_event: object) -> None:
        raise RuntimeError("subscriber bug")

    h.actor.subscribe(broken)
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_an_unexpected_error_is_a_banner_and_the_actor_goes_on(h: Harness):
    h.provisioner.error = RuntimeError("bug")
    await h.arm()
    assert BannerKey.INTERNAL in h.banners()
    h.provisioner.error = None
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_run_posts_a_tick_every_tick_s(tmp_path):
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    rig = Harness(tmp_path, sleep=sleep)
    try:
        await rig.start()
        await asyncio.sleep(0.01)
    finally:
        await rig.stop()
    assert slept and set(slept) == {session_mod.TICK_S}


def test_a_message_posted_after_the_loop_closed_is_dropped():
    loop = asyncio.new_event_loop()
    loop.close()
    gateway = FakeGateway(FakeObs(), FakeClock())
    finaliser = FinaliseWorker()
    actor = SessionActor(
        loop=loop,
        gateway=gateway,
        discovery=FakeDiscovery(),
        provisioner=FakeProvisioner(),
        recorder=ObsRecorder(gateway),
        finaliser=finaliser,
        get_config=AppConfig,
        get_profile=lambda _slug: None,
        source_factory=lambda _cfg, _game: [],
    )
    actor.post(Tick(0.0))  # the app is quitting: dropped, no RuntimeError
    finaliser.shutdown()


async def test_finalise_worker_runs_one_call_at_a_time_on_its_own_thread(monkeypatch, tmp_path):
    active = 0
    overlaps = 0
    threads: set[str] = set()

    def fake_finalise(manifest_path, cfg, *, sleep):
        nonlocal active, overlaps
        active += 1
        overlaps += active > 1
        threads.add(threading.current_thread().name)
        time.sleep(0.02)
        active -= 1
        return FinaliseResult(manifest_path, None, queue_vad=False)

    monkeypatch.setattr(session_mod, "finalise", fake_finalise)
    worker = FinaliseWorker()
    try:
        await asyncio.gather(*(worker.run(tmp_path / f"{i}.session.json", AppConfig()) for i in range(4)))
    finally:
        worker.shutdown()
    assert overlaps == 0
    assert len(threads) == 1 and next(iter(threads)).startswith("finalise")
```

**Create** `tests/session/test_session_arm.py`:

```python
"""Arming, disarming and the restore file (spec 6.2; 17 rows OBS not running, missing request, active
output, switch timeout, output folder not writable, free space)."""

import pytest

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import OBS_SOURCE_ID, AppState, CommandKind, SourceStatus, SourceStatusChanged
from anki_miner_game.models.obs import ObsAuthError, ObsError, ObsUnsupportedError
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.restore import ObsRestore, load_restore, restore_path, save_restore
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import SLUG, FakeSource, Harness, profile

SWITCH_REQUESTS = ["SetCurrentProfile", "SetCurrentSceneCollection"]


async def test_arm_switches_obs_to_the_app_profile_and_collection(h: Harness):
    h.gateway.sent.clear()
    await h.arm()
    assert h.gateway.names() == [
        "GetStreamStatus",
        "GetRecordStatus",
        "GetReplayBufferStatus",
        "GetVirtualCamStatus",
        "GetProfileList",
        "GetSceneCollectionList",
        "SetCurrentProfile",
        "SetCurrentSceneCollection",
    ]
    assert (h.obs.profile, h.obs.collection) == (OBS_PROFILE_NAME, OBS_COLLECTION_NAME)
    assert load_restore(restore_path()) == ObsRestore(profile="Untitled", collection="Untitled")
    assert h.provisioner.profiles == [h.cfg]
    assert h.provisioner.collections == [h.profiles[SLUG]]
    assert h.sources[0].starts == 1
    assert h.states() == [(AppState.ARMED, SLUG)]
    assert h.banners() == {}


async def test_arm_creates_the_output_folder(h: Harness):
    await h.arm()
    assert h.incoming.is_dir()


async def test_arm_on_the_app_profile_writes_no_restore_file(h: Harness):
    h.obs.profile, h.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    await h.arm()
    assert not restore_path().exists()
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())
    assert h.actor.state is AppState.ARMED


async def test_arm_keeps_the_names_of_an_earlier_unfinished_arm(h: Harness):
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    await h.arm()
    assert load_restore(restore_path()) == ObsRestore(profile="Mine", collection="Scenes")


async def test_a_missing_app_profile_is_left_to_the_provisioner(h: Harness):
    h.obs.profiles = ["Untitled"]
    h.obs.collections = ["Untitled"]
    await h.arm()
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())
    assert h.provisioner.profiles == [h.cfg]
    assert h.actor.state is AppState.ARMED


@pytest.mark.parametrize(
    ("attribute", "label"),
    [
        ("stream_active", "stream"),
        ("record_active", "recording"),
        ("replay_active", "replay buffer"),
        ("vcam_active", "virtual camera"),
    ],
)
async def test_arm_refuses_while_an_output_is_active(h: Harness, attribute: str, label: str):
    setattr(h.obs, attribute, True)
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert label in h.banners()[BannerKey.ARM]
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())
    assert not restore_path().exists()
    assert h.sources[0].starts == 0


async def test_unavailable_replay_buffer_and_virtual_camera_do_not_block(h: Harness):
    assert h.obs.replay_active is None and h.obs.vcam_active is None  # 604 "not available"
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_arm_launches_obs_when_it_is_not_running(rig: Harness):
    rig.discovery.running = False
    await rig.start()
    assert rig.gateway.connects == 0
    await rig.arm()
    assert (rig.discovery.enabled, rig.discovery.launches, rig.gateway.connects) == (1, 1, 1)
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.CONNECTING) in rig.events
    assert rig.actor.state is AppState.ARMED


async def test_obs_that_never_answers_after_launch_is_a_banner(rig: Harness):
    rig.discovery.running = False
    rig.discovery.ready = False
    await rig.start()
    await rig.arm()
    assert "30 s" in rig.banners()[BannerKey.OBS]
    assert rig.gateway.connects == 0
    assert rig.actor.state is AppState.IDLE


async def test_missing_request_names_the_request_and_the_version(rig: Harness):
    rig.gateway.connect_error = ObsUnsupportedError("29.1.3", ("SetRecordDirectory",))
    await rig.start()
    await rig.arm()
    text = rig.banners()[BannerKey.OBS]
    assert "29.1.3" in text and "SetRecordDirectory" in text
    assert rig.actor.state is AppState.IDLE


async def test_authentication_failure_asks_for_the_password(rig: Harness):
    rig.gateway.connect_error = ObsAuthError("authentication failed")
    await rig.start()
    await rig.arm()
    assert "password" in rig.banners()[BannerKey.OBS]
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.DISCONNECTED) in rig.events


async def test_a_switch_that_times_out_is_undone(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "SWITCH_TIMEOUT_S", 0.05)
    h.obs.lost_switch_events = 1  # OBS switches, but the CurrentProfileChanged event never comes
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "within" in h.banners()[BannerKey.ARM]
    assert h.obs.profile == "Untitled"
    assert not restore_path().exists()
    assert h.provisioner.profiles == []


async def test_a_provisioning_failure_restores_obs(h: Harness):
    h.provisioner.error = ObsError("CreateInput failed")
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "CreateInput failed" in h.banners()[BannerKey.ARM]
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()
    assert h.sources[0].starts == 0


async def test_an_output_folder_that_cannot_be_written_refuses_arming(h: Harness):
    h.output_root.parent.mkdir(parents=True, exist_ok=True)
    h.output_root.write_text("a file where the folder should be", encoding="utf-8")
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "cannot be written" in h.banners()[BannerKey.ARM]
    assert "GetProfileList" not in h.gateway.names()


async def test_low_free_space_warns_and_arms_anyway(h: Harness):
    h.free_bytes = 3_200_000_000
    await h.arm()
    assert h.actor.state is AppState.ARMED
    assert "3.2 GB" in h.banners()[BannerKey.LOW_DISK]


async def test_settings_that_need_an_obs_restart_are_a_warning(h: Harness):
    h.provisioner.needs_restart = True
    await h.arm()
    assert h.actor.state is AppState.ARMED
    assert "Restart OBS" in h.banners()[BannerKey.OBS_RESTART]


async def test_unknown_and_invalid_games_are_refused(h: Harness):
    await h.arm("no-such-game")
    assert "no-such-game" in h.banners()[BannerKey.ARM]
    h.profiles["bad"] = profile(slug="bad", title=" ")
    await h.arm("bad")
    assert "title is empty" in h.banners()[BannerKey.ARM]
    assert h.actor.state is AppState.IDLE


async def test_arming_another_game_restarts_the_sources_and_names_it(h: Harness):
    h.profiles["zero"] = profile(slug="zero", title="Zero Escape")
    first = h.sources
    await h.arm()
    h.sources = [FakeSource("agent")]
    await h.arm("zero")
    assert (first[0].stops, first[0].closed) == (1, 1)
    assert h.sources[0].starts == 1
    assert h.states() == [(AppState.ARMED, SLUG), (AppState.ARMED, "zero")]
    assert h.provisioner.collections[-1].slug == "zero"
    assert load_restore(restore_path()) == ObsRestore(profile="Untitled", collection="Untitled")


async def test_arming_another_game_is_refused_while_a_stream_runs(h: Harness):
    h.profiles["zero"] = profile(slug="zero", title="Zero Escape")
    await h.arm()
    h.obs.stream_active = True
    await h.arm("zero")
    assert h.states() == [(AppState.ARMED, SLUG)]
    assert h.sources[0].stops == 0


async def test_disarm_closes_the_sources_and_restores_obs(h: Harness):
    await h.arm()
    await h.send(CommandKind.DISARM)
    assert (h.sources[0].stops, h.sources[0].closed) == (1, 1)
    assert h.states() == [(AppState.ARMED, SLUG), (AppState.IDLE, None)]
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_restore_waits_for_an_active_stream(h: Harness):
    await h.arm()
    h.obs.stream_active = True
    await h.send(CommandKind.DISARM)
    assert h.actor.state is AppState.IDLE
    assert h.obs.profile == OBS_PROFILE_NAME
    assert restore_path().exists()
    h.obs.stream_active = False
    await h.tick(h.clock.t + session_mod.RESTORE_RETRY_S)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_restore_skips_a_profile_obs_no_longer_has(h: Harness):
    await h.arm()
    h.obs.profiles.remove("Untitled")
    await h.send(CommandKind.DISARM)
    assert h.obs.profile == OBS_PROFILE_NAME
    assert h.obs.collection == "Untitled"
    assert not restore_path().exists()


async def test_disarm_while_idle_does_nothing(h: Harness):
    await h.send(CommandKind.DISARM)
    assert h.states() == []


async def test_quitting_while_armed_disarms(h: Harness):
    await h.arm()
    await h.stop()
    assert (h.sources[0].stops, h.sources[0].closed) == (1, 1)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
```

- [ ] **Step 3: Run them to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_actor.py tests/session/test_session_arm.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'anki_miner_game.session.session'`).

- [ ] **Step 4: Implement the actor core, arming and disarming**

**Create** `anki_miner_game/session/session.py`:

```python
"""The session actor (spec 4.2, 6, 7, 10.2, 12 actor side, 17) and the finalise worker (spec 10.3).

One queue of ``LineReceived``, ``ObsEvent``, ``UserCommand`` and ``Tick`` messages, consumed on the
I/O loop one at a time; every state change happens here. The actor arms a game (OBS on the app's
profile and scene collection, inputs provisioned, text sources started), turns a recording that
starts while armed into a session (manifest and journal in ``_incoming/``, lines journalled at their
record-clock offsets), ends it on ``STOPPED`` or without one (OBS exit, OBS gone after a lost
connection), and finalises it on the one ``FinaliseWorker``. Reconcile (spec 6.3) runs on every
``_Connected`` before later events are handled.

Rules this module keeps (wave-1 amendments 3 and 8, wave 2a contracts):

- The pipeline's "previous accepted line" is the previous journalled line: ``TextPipeline.reset()``
  after an accepted line is dropped as ``paused``, at ``STARTED`` unless held lines are journalled,
  and after a split. A ``Replaced`` is journalled as a ``ReplaceRecord`` only when its base is the
  journal's last ``LineRecord``; otherwise as a ``LineRecord`` at ``clock.offset_ms(line.t_mono)``.
- A ``START`` carrying ``UserCommand.line`` (auto mode) holds that line and every line accepted
  until ``STARTED``; on ``STARTED`` they are journalled in order (the clamp puts every line from
  before the zero at offset 0) with no pipeline reset. A failed start drops them.
- Finalise runs on ``FinaliseWorker``, one call at a time, never on the default executor pool.
- Text sources' ``start``/``stop``/``wait_closed`` run on the actor's thread; their status listener
  runs on the source's thread and is handed to the loop with ``call_soon_threadsafe``.
"""

import asyncio
import contextlib
import functools
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from anki_miner_game import paths
from anki_miner_game.interfaces.addons import VadJobs
from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, Provisioner, Recorder
from anki_miner_game.interfaces.text_source import TextSource
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import Counts
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    START_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineReceived,
    ObsEvent,
    SessionEvent,
    SessionInput,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsError,
    ObsEventName,
    ObsRequestError,
    ObsUnsupportedError,
)
from anki_miner_game.models.profile import GameProfile, validate
from anki_miner_game.session.finalise import FinaliseResult, finalise
from anki_miner_game.session.restore import ObsRestore, delete_restore, load_restore, restore_path, save_restore
from anki_miner_game.store import StoreError
from anki_miner_game.text.pipeline import TextPipeline

log = logging.getLogger(__name__)

# Record clock (spec 7), measured by R1 (docs/m0/clock.md): the zero is the receipt time of
# RecordStateChanged STARTED and the capture latency is 10 ms. Linux numbers; the Windows values
# stay provisional until H5 (master plan D2).
ZERO_EVENT: Final = "STARTED"
CAPTURE_LATENCY_MS: Final = 10

# Provisional until R2 (docs/m0/obs-behaviour.md); read from source in docs/m0/source-findings.md.
START_TIMEOUT_S: Final = 10.0
"""``StartRecord`` answered but no ``STARTED``: OBS refused the start in a modal and sent no event
(source findings section 6). R1 saw ``STARTED`` 7-184 ms after the request. Provisional until R2."""
RECORD_OUTPUT_NAMES: Final = ("simple_file_output", "adv_file_output", "adv_ffmpeg_output")
"""Outputs whose ``GetOutputSettings`` ``path`` is the file being recorded, tried in order (source
findings section 7; R2 read ``simple_file_output``'s path after a reconnect). Provisional until R2."""

# Fixed by the spec.
SWITCH_TIMEOUT_S: Final = 15.0
"""Spec 6.2 step 3: one profile or scene collection switch, request and ``...Changed`` event."""
OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""Spec 17: Arm launches OBS and waits up to 30 s for it to answer."""
REANCHOR_S: Final = 10.0
"""Spec 7: the ``OutputDurationClock`` re-anchors every 10 s."""
FREE_SPACE_WARN_BYTES: Final = 5 * 10**9
"""Spec 17: under 5 GB free at Arm, arm anyway with a warning."""

# This module's own timers.
TICK_S: Final = 1.0
"""How often ``run`` posts a ``Tick``; the resolution of every timer below."""
OBS_GONE_CHECK_S: Final = 5.0
"""While recording with the connection lost: how often to ask whether OBS still runs (spec 6.4)."""
RESTORE_RETRY_S: Final = 10.0
"""While idle with ``obs_restore.json`` still present: how often to try the restore again."""

# obs-websocket request status codes (obs-websocket@1ef34bf4 src/requesthandler/types/RequestStatus.h).
INVALID_RESOURCE_STATE: Final = 604
"""``GetReplayBufferStatus`` / ``GetVirtualCamStatus`` when that output is not available at all."""

OUTPUT_CHECKS: Final = (
    ("stream", "GetStreamStatus"),
    ("recording", "GetRecordStatus"),
    ("replay buffer", "GetReplayBufferStatus"),
    ("virtual camera", "GetVirtualCamStatus"),
)
"""Spec 6.2 step 1: arming (and the restore) refuses while any of these is active."""

_LIVE: Final = frozenset({SourceStatus.CONNECTED, SourceStatus.RECEIVING})


class BannerKey(StrEnum):
    """``Banner.key`` of every banner the actor raises; ``START_FAILED_BANNER_KEY`` is the one more."""

    OBS = "obs"
    ARM = "arm"
    LOW_DISK = "low_disk"
    OBS_RESTART = "obs_restart"
    RESTORE = "obs_restore"
    NO_SOURCE = "no_source"
    STOP_FAILED = "stop_failed"
    FOREIGN_RECORDING = "foreign_recording"
    SESSION_FILES = "session_files"
    SPLIT = "split_unsupported"
    CLOCK = "clock_degraded"
    OBS_EXITED = "obs_exited"
    FINALISE = "finalise"
    NO_CUES = "no_cues"
    INTERNAL = "internal_error"


class FinaliseWorker:
    """Runs ``session.finalise.finalise`` on one dedicated thread, one call at a time (spec 10.3).

    Finalise's NN bump is check-then-act, so every caller under one output root shares this one
    worker: the actor, and the composition's launch-time work (wave-1 amendment 8). Never the
    default ``run_in_executor`` pool.
    """

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalise")
        self._sleep = sleep

    async def run(self, manifest_path: Path, cfg: AppConfig) -> FinaliseResult:
        """``finalise(manifest_path, cfg)`` on the worker thread; its ``FinaliseError`` passes through."""
        call = functools.partial(finalise, manifest_path, cfg, sleep=self._sleep)
        return await asyncio.get_running_loop().run_in_executor(self._executor, call)

    def shutdown(self) -> None:
        """Wait for a running call, then stop the thread."""
        self._executor.shutdown(wait=True)


@dataclass(frozen=True)
class _Switch:
    what: str
    request: str
    event: str
    field: str


_PROFILE: Final = _Switch("profile", "SetCurrentProfile", ObsEventName.CURRENT_PROFILE_CHANGED, "profileName")
_COLLECTION: Final = _Switch(
    "scene collection",
    "SetCurrentSceneCollection",
    ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED,
    "sceneCollectionName",
)


class _SwitchTimeoutError(Exception):
    """A profile or scene collection switch outlived ``SWITCH_TIMEOUT_S``."""


@dataclass
class _Armed:
    profile: GameProfile
    cfg: AppConfig


@dataclass(frozen=True)
class _Shutdown:
    done: "asyncio.Future[None]"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _disk_free(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def _writable(folder: Path) -> bool:
    """Create ``folder`` when missing and prove a file can be made in it (spec 17)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        return False
    return True


class SessionActor:
    """The ``SessionControl`` of the app: one queue, consumed on the I/O loop by ``run``.

    ``post`` is safe from any thread; subscribers run on the loop and must return quickly. ``run``
    does the launch duties, then handles one message at a time until ``shutdown``; ``join`` waits
    until everything posted so far has been handled (tests and the integration suite use it).
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        gateway: ObsGateway,
        discovery: ObsDiscovery,
        provisioner: Provisioner,
        recorder: Recorder,
        finaliser: FinaliseWorker,
        get_config: Callable[[], AppConfig],
        get_profile: Callable[[str], GameProfile | None],
        source_factory: Callable[[AppConfig, GameProfile], Sequence[TextSource]],
        vad_jobs: VadJobs | None = None,
        now: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
        disk_free: Callable[[Path], int] = _disk_free,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``get_profile`` runs on the loop: pass a lookup over the profiles already loaded.

        ``source_factory(cfg, profile)`` builds the text sources of an armed game (spec 8.1).
        ``sleep`` paces only the ``Tick`` timer of ``run``.
        """
        self._loop = loop
        self._gateway = gateway
        self._discovery = discovery
        self._provisioner = provisioner
        self._recorder = recorder
        self._finaliser = finaliser
        self._get_config = get_config
        self._get_profile = get_profile
        self._source_factory = source_factory
        self._vad_jobs = vad_jobs
        self._now = now
        self._utc_now = utc_now
        self._disk_free = disk_free
        self._sleep = sleep

        self._queue: asyncio.Queue[SessionInput | _Shutdown] = asyncio.Queue()
        self._launched = asyncio.Event()
        self._subscribers: list[Callable[[SessionEvent], None]] = []
        self._waiters: list[tuple[Callable[[ObsEvent], bool], asyncio.Future[ObsEvent]]] = []
        self._banners: set[str] = set()

        self._state = AppState.IDLE
        self._armed: _Armed | None = None
        self._pipeline: TextPipeline | None = None
        self._sources: list[TextSource] = []
        self._source_status: dict[str, SourceStatus] = {}
        self._counts = Counts()
        self._held: list[GameLine] | None = None
        """Auto-start: lines accepted between a ``START`` carrying a line and ``STARTED``."""
        self._start_deadline: float | None = None
        """A ``StartRecord`` was answered; ``STARTED`` is due before this ``now()``."""

        self._connected = False
        self._obs_versions = ("unknown", "unknown")
        self._next_restore = 0.0

        gateway.subscribe(self.post)

    # --- SessionControl -------------------------------------------------------------------------

    def post(self, msg: SessionInput) -> None:
        """Enqueue ``msg``; safe from any thread. A message posted after the loop closed is dropped."""
        with contextlib.suppress(RuntimeError):  # the loop is closed: the app is quitting
            self._loop.call_soon_threadsafe(self._enqueue, msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        self._subscribers.append(cb)

    @property
    def state(self) -> AppState:
        return self._state

    # --- running --------------------------------------------------------------------------------

    async def run(self) -> None:
        """Launch duties (``_launch``), then messages one at a time until ``shutdown``.

        Posts a ``Tick`` every ``TICK_S`` meanwhile.
        """
        ticker = asyncio.create_task(self._tick_forever())
        try:
            await self._guarded(self._launch())
            self._launched.set()
            while True:
                msg = await self._queue.get()
                try:
                    if isinstance(msg, _Shutdown):
                        await self._guarded(self._shutdown())
                        msg.done.set_result(None)
                        return
                    await self._guarded(self._handle(msg))
                finally:
                    self._queue.task_done()
        finally:
            ticker.cancel()

    async def join(self) -> None:
        """Return once ``run`` has done the launch duties and handled every message already queued."""
        await self._launched.wait()
        await self._queue.join()

    async def shutdown(self) -> None:
        """Quit at the next message boundary; call it on the I/O loop while ``run`` runs.

        Stops every text source and awaits ``wait_closed``. While armed it disarms (OBS goes back
        to the user's profile). While recording OBS keeps recording: the journal is closed and the
        next launch resumes the session (reconcile row 4) or finalises it (last row).
        """
        done: asyncio.Future[None] = self._loop.create_future()
        self._queue.put_nowait(_Shutdown(done))
        await done

    async def _launch(self) -> None:
        """With OBS running, connect; the ``_Connected`` event does the rest."""
        if await asyncio.to_thread(self._discovery.is_running):
            await self._ensure_connected()

    async def _handle(self, msg: SessionInput) -> None:
        match msg:
            case ObsEvent():
                await self._on_obs_event(msg)
            case UserCommand():
                await self._on_command(msg)
            case Tick():
                await self._on_tick(msg.t_mono)

    def _enqueue(self, msg: SessionInput) -> None:
        """On the loop: wake a switch waiting for this event, then queue it like any message."""
        if isinstance(msg, ObsEvent):
            for predicate, waiter in self._waiters:
                if not waiter.done() and predicate(msg):
                    waiter.set_result(msg)
        self._queue.put_nowait(msg)

    async def _guarded(self, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:  # the actor must keep consuming, or the whole session freezes
            log.exception("session actor: unhandled error")
            self._banner(BannerKey.INTERNAL, BannerLevel.ERROR, "Something went wrong in the session; see the log.")

    async def _tick_forever(self) -> None:
        while True:
            await self._sleep(TICK_S)
            self.post(Tick(self._now()))

    async def _shutdown(self) -> None:
        if self._state is AppState.ARMED:
            await self._to_idle()
        else:
            await self._stop_sources()

    async def _on_tick(self, t: float) -> None:
        if self._state is AppState.IDLE and self._connected and t >= self._next_restore and restore_path().exists():
            self._next_restore = t + RESTORE_RETRY_S
            await self._restore_obs()

    # --- events out -----------------------------------------------------------------------------

    def _publish(self, event: SessionEvent) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # a subscriber's bug must not stop the session
                log.exception("session subscriber failed on %s", type(event).__name__)

    def _set_state(self, state: AppState) -> None:
        self._state = state
        slug = None if state is AppState.IDLE or self._armed is None else self._armed.profile.slug
        self._publish(StateChanged(state, slug))

    def _banner(self, key: str, level: BannerLevel, text: str) -> None:
        self._banners.add(key)
        self._publish(BannerRaised(Banner(key, level, text)))

    def _clear(self, *keys: str) -> None:
        for key in keys:
            if key in self._banners:
                self._banners.discard(key)
                self._publish(BannerCleared(key))

    def _obs_status(self, status: SourceStatus) -> None:
        self._publish(SourceStatusChanged(OBS_SOURCE_ID, status))

    # --- OBS events -----------------------------------------------------------------------------

    async def _on_obs_event(self, ev: ObsEvent) -> None:
        match ev.name:
            case ObsEventName.CONNECTED:
                self._connected = True
                self._obs_status(SourceStatus.CONNECTED)
                self._clear(BannerKey.OBS)
                if self._state is AppState.IDLE:
                    await self._restore_obs()
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)

    # --- commands -------------------------------------------------------------------------------

    async def _on_command(self, cmd: UserCommand) -> None:
        match cmd.kind:
            case CommandKind.ARM:
                if cmd.slug is not None:
                    await self._arm(cmd.slug)
            case CommandKind.DISARM:
                await self._disarm()

    # --- arming (spec 6.2) ----------------------------------------------------------------------

    async def _arm(self, slug: str) -> None:
        if self._state is AppState.RECORDING:
            self._banner(BannerKey.ARM, BannerLevel.INFO, "Stop the recording before arming another game.")
            return
        profile = self._get_profile(slug)
        if profile is None:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"There is no game profile {slug!r}.")
            return
        problems = validate(profile)
        if problems:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"{profile.title} cannot be armed: {'; '.join(problems)}.")
            return
        cfg = self._get_config()
        incoming = paths.incoming_dir(cfg)
        if not _writable(incoming):
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"The output folder {incoming} cannot be written to.")
            return
        if not await self._ensure_connected():
            return
        try:
            active = await self._active_outputs()
        except ObsError as exc:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"Cannot read OBS's outputs: {exc}")
            return
        if active:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"OBS has an active {', '.join(active)}; stop it first.")
            return
        await self._stop_sources()  # arming another game while armed
        try:
            await self._switch_to_app()
            result = await self._provisioner.ensure_profile(cfg)
            await self._provisioner.ensure_collection(profile)
        except (_SwitchTimeoutError, ObsError, StoreError) as exc:
            await self._to_idle()
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"Could not prepare OBS for {profile.title}: {exc}")
            return
        if result.needs_restart:
            self._banner(
                BannerKey.OBS_RESTART,
                BannerLevel.WARNING,
                "Restart OBS before recording: the app's recording settings take effect after a restart.",
            )
        self._armed = _Armed(profile=profile, cfg=cfg)
        self._pipeline = TextPipeline(profile.filters)
        self._held = None
        self._start_deadline = None
        self._counts = Counts()
        self._start_sources(cfg, profile)
        self._clear(BannerKey.ARM)
        free = self._disk_free(incoming)
        if free < FREE_SPACE_WARN_BYTES:
            self._banner(
                BannerKey.LOW_DISK,
                BannerLevel.WARNING,
                f"Only {free / 1e9:.1f} GB free in {incoming}; OBS stops recording when the disk is full.",
            )
        else:
            self._clear(BannerKey.LOW_DISK)
        self._set_state(AppState.ARMED)

    async def _ensure_connected(self) -> bool:
        """Connect, launching OBS first when it is not running (spec 11.1, 17); a banner on failure."""
        if self._connected:
            return True
        self._obs_status(SourceStatus.CONNECTING)
        try:
            if not await asyncio.to_thread(self._discovery.is_running):
                await asyncio.to_thread(self._discovery.ensure_server_enabled)
                await asyncio.to_thread(self._discovery.launch)
                if not await self._discovery.wait_ready(OBS_LAUNCH_TIMEOUT_S):
                    raise ObsConnectError(
                        f"OBS did not answer within {OBS_LAUNCH_TIMEOUT_S:g} s; an OBS dialog may be waiting"
                    )
            info = await self._gateway.connect()
        except ObsUnsupportedError as exc:
            self._obs_failed(
                f"OBS {exc.obs_version} lacks {', '.join(exc.missing)}; update OBS to version 30.0 or newer."
            )
            return False
        except ObsAuthError:
            self._obs_failed("OBS rejected the websocket password; enter it in the app's settings.")
            return False
        except ObsConfigError as exc:
            self._obs_failed(f"OBS's websocket settings cannot be read ({exc}); run the setup wizard again.")
            return False
        except ObsError as exc:
            self._obs_failed(f"Cannot connect to OBS: {exc}")
            return False
        self._obs_versions = (info.obs_version, info.websocket_version)
        self._connected = True
        self._obs_status(SourceStatus.CONNECTED)
        self._clear(BannerKey.OBS)
        return True

    def _obs_failed(self, text: str) -> None:
        self._obs_status(SourceStatus.DISCONNECTED)
        self._banner(BannerKey.OBS, BannerLevel.ERROR, text)

    async def _active_outputs(self) -> list[str]:
        """Spec 6.2 step 1. An output OBS reports as not available (604) cannot be active."""
        active: list[str] = []
        for label, request in OUTPUT_CHECKS:
            try:
                status = await self._gateway.request(request)
            except ObsRequestError as exc:
                if exc.code == INVALID_RESOURCE_STATE:
                    continue
                raise
            if status.get("outputActive"):
                active.append(label)
        return active

    async def _switch_to_app(self) -> None:
        """Spec 6.2 steps 2-3: remember the user's names, then switch profile, then collection.

        An existing ``obs_restore.json`` is kept: it holds the names from before an earlier arm the
        app never disarmed. A profile or collection that does not exist yet is created (and made
        current) by the provisioner in step 4.
        """
        profiles = await self._gateway.request("GetProfileList")
        collections = await self._gateway.request("GetSceneCollectionList")
        current_profile = str(profiles.get("currentProfileName", ""))
        current_collection = str(collections.get("currentSceneCollectionName", ""))
        path = restore_path()
        try:
            saved = load_restore(path)
        except StoreError as exc:
            log.warning("replacing an unusable %s: %s", path.name, exc)
            saved = None
        if saved is None and (current_profile, current_collection) != (OBS_PROFILE_NAME, OBS_COLLECTION_NAME):
            save_restore(path, ObsRestore(profile=current_profile, collection=current_collection))
        if current_profile != OBS_PROFILE_NAME and OBS_PROFILE_NAME in (profiles.get("profiles") or []):
            await self._switch(_PROFILE, OBS_PROFILE_NAME)
        if current_collection != OBS_COLLECTION_NAME and OBS_COLLECTION_NAME in (
            collections.get("sceneCollections") or []
        ):
            await self._switch(_COLLECTION, OBS_COLLECTION_NAME)

    async def _switch(self, kind: _Switch, name: str) -> None:
        """``Set...`` and its ``...Changed`` event within ``SWITCH_TIMEOUT_S`` (spec 6.2 step 3).

        The waiter is registered before the request: the event can arrive before the answer
        (source findings 7). Nothing else is sent until both are in.
        """
        waiter: asyncio.Future[ObsEvent] = self._loop.create_future()
        entry = (lambda ev: ev.name == kind.event and ev.data.get(kind.field) == name, waiter)
        self._waiters.append(entry)
        try:
            async with asyncio.timeout(SWITCH_TIMEOUT_S):
                await self._gateway.request(kind.request, **{kind.field: name})
                await waiter
        except TimeoutError:
            raise _SwitchTimeoutError(
                f"OBS did not switch to the {kind.what} {name!r} within {SWITCH_TIMEOUT_S:g} s"
            ) from None
        finally:
            self._waiters.remove(entry)
            waiter.cancel()

    async def _restore_obs(self) -> bool:
        """Switch OBS back to ``obs_restore.json`` and delete it (spec 6.2); ``False`` when it has to wait.

        It waits while not connected, while any output is active, or after OBS refused; the next
        ``_Connected`` and every ``RESTORE_RETRY_S`` while idle try again. A name OBS no longer has
        is skipped.
        """
        path = restore_path()
        try:
            saved = load_restore(path)
        except StoreError as exc:
            log.warning("deleting an unusable %s: %s", path.name, exc)
            delete_restore(path)
            return True
        if saved is None:
            return True
        if not self._connected:
            return False
        try:
            if await self._active_outputs():
                return False
            collections = await self._gateway.request("GetSceneCollectionList")
            if saved.collection != collections.get("currentSceneCollectionName") and saved.collection in (
                collections.get("sceneCollections") or []
            ):
                await self._switch(_COLLECTION, saved.collection)
            profiles = await self._gateway.request("GetProfileList")
            if saved.profile != profiles.get("currentProfileName") and saved.profile in (
                profiles.get("profiles") or []
            ):
                await self._switch(_PROFILE, saved.profile)
        except (_SwitchTimeoutError, ObsError) as exc:
            self._banner(
                BannerKey.RESTORE,
                BannerLevel.WARNING,
                f"Could not switch OBS back to your profile {saved.profile!r} ({exc}); the app tries again.",
            )
            return False
        delete_restore(path)
        self._clear(BannerKey.RESTORE)
        return True

    async def _disarm(self) -> None:
        if self._state is AppState.RECORDING:
            self._banner(BannerKey.ARM, BannerLevel.INFO, "Stop the recording before disarming.")
            return
        if self._state is AppState.ARMED:
            await self._to_idle()

    async def _to_idle(self) -> None:
        """Disarm, or undo a failed arm: sources stopped and closed, state ``idle``, OBS restored."""
        await self._stop_sources()
        self._armed = None
        self._pipeline = None
        self._held = None
        self._start_deadline = None
        self._clear(BannerKey.NO_SOURCE, BannerKey.LOW_DISK, BannerKey.OBS_RESTART, START_FAILED_BANNER_KEY)
        if self._state is not AppState.IDLE:
            self._set_state(AppState.IDLE)
        await self._restore_obs()

    # --- text sources (spec 8.1) ----------------------------------------------------------------

    def _start_sources(self, cfg: AppConfig, profile: GameProfile) -> None:
        self._sources = list(self._source_factory(cfg, profile))
        self._source_status = {}
        for source in self._sources:
            source.set_status_listener(self._source_status_listener)
            self._source_status[source.id] = source.status
            self._publish(SourceStatusChanged(source.id, source.status))
            source.start(self._sink)

    async def _stop_sources(self) -> None:
        """Stop every source, then await ``wait_closed`` for each (owocr's tree dead, spec 14)."""
        sources, self._sources = self._sources, []
        for source in sources:
            source.stop()
        results = await asyncio.gather(*(source.wait_closed() for source in sources), return_exceptions=True)
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("text source %s did not close cleanly: %r", source.id, result)

    def _sink(self, raw: str, t_mono: float, source_id: str) -> None:
        self.post(LineReceived(raw=raw, t_mono=t_mono, source_id=source_id))

    def _source_status_listener(self, source_id: str, status: SourceStatus) -> None:
        """Runs on the source's thread (the Qt main thread for the clipboard): hand it to the loop."""
        with contextlib.suppress(RuntimeError):  # the loop is closed: the app is quitting
            self._loop.call_soon_threadsafe(self._on_source_status, source_id, status)

    def _on_source_status(self, source_id: str, status: SourceStatus) -> None:
        self._source_status[source_id] = status
        self._publish(SourceStatusChanged(source_id, status))
        if status in _LIVE:
            self._clear(BannerKey.NO_SOURCE)
```

- [ ] **Step 5: Run them to pass, then lint and type-check**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_actor.py tests/session/test_session_arm.py -q`
Expected: PASS (37 passed).

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: PASS (black and ruff clean; mypy "Success: no issues found").

- [ ] **Step 6: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/session/session.py tests/session/actor_harness.py tests/session/conftest.py tests/session/test_session_actor.py tests/session/test_session_arm.py && git commit -m "feat(session): add the session actor with arming, disarming and restore"
```

---

### Task 4: recording

**Files:**
- Modify: `anki_miner_game/session/session.py`
- Test: `tests/session/test_session_recording.py`

**Interfaces:**
- Consumes: Task 3's actor; `session.clock.EventClock(capture_latency_ms, now=)` with `start`,
  `pause`, `resume`, `paused`, `offset_ms`, `reading_ms`, `drift_sample` (T03);
  `TextPipeline.process` / `reset` and `DROP_COUNTER` (T05); `Journal`, `read_journal` and the
  record types, `incoming_files`, `game_folder`, `reserve_index`, `write_manifest_atomic`,
  `finalise`'s `FinaliseResult` / `FinaliseError` (T06); `Counts.incremented`.
- Produces: `START`, `STOP`, `TOGGLE`; `STARTED` (ownership rule, manifest, journal, clock, held
  lines), lines, pause edges, drift samples, `STOPPED` -> finalise -> `armed`; `LineAccepted`,
  `RecordingStarted`, `RecordingStopped`, `SessionFinalised`. Private names Tasks 5-6 use:
  `_Session`, `_session`, `_with_flag`, `_end_session`, `_finalise`, `_write_manifest`,
  `_append`, `_record_status`, `_sample_drift`, `_start_failed`, `_in_incoming`, `_utc_stamp`.

- [ ] **Step 1: Write the failing tests**

**Create** `tests/session/test_session_recording.py`:

```python
"""Recording: ownership, the session's files, lines, pauses, auto-start holds and the normal stop
(spec 6.2 ownership, 7, 8.2, 10.2, 10.3, 12 actor side; 17 rows StartRecord fails, no text source
connected at Start, zero cues)."""

import json
import threading

from anki_miner_game.models.config import AppConfig, VadSettings
from anki_miner_game.models.manifest import ClockKind, ClockRecord, Counts, DriftSample, Flag, ManifestState
from anki_miner_game.models.messages import (
    START_FAILED_BANNER_KEY,
    AppState,
    CommandKind,
    LineAccepted,
    LineReceived,
    RecordingStarted,
    RecordingStopped,
    SourceStatus,
)
from anki_miner_game.models.obs import ObsRequestError, OutputState
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import LineRecord, PauseRecord, ReplaceRecord, ResumeRecord, read_journal
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.restore import restore_path
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, SLUG, T0, TITLE, Harness, profile

ZERO = T0 + 1.0
"""When ``STARTED`` arrives in these tests: offset = (t - ZERO) * 1000 + 10."""


def incoming_manifest(h: Harness):
    return load_manifest(h.incoming / f"{OBS_STEM}.session.json")


def journal(h: Harness):
    return read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")


async def test_a_whole_session_leaves_the_pair_in_the_game_folder(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    assert "StartRecord" in h.gateway.names()
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 5.0)
    await h.line("さようなら", ZERO + 9.0)
    h.clock.t = ZERO + 18.0
    h.obs.output_duration = 17_400
    await h.send(CommandKind.STOP)
    assert h.gateway.names()[-2:] == ["GetRecordStatus", "StopRecord"]
    await h.stopped(ZERO + 19.0)

    stem = f"{TITLE} - 01"
    assert (h.game_dir() / f"{stem}.srt").read_bytes() == (
        "1\n00:00:05,010 --> 00:00:08,660\nこんにちは\n\n2\n00:00:09,010 --> 00:00:18,660\nさようなら\n"
    ).encode()
    assert (h.game_dir() / f"{stem}.mkv").exists()
    assert sorted(p.name for p in h.incoming.iterdir()) == []
    manifest = load_manifest(h.game_dir() / f"{stem}.session.json")
    assert manifest.state is ManifestState.READY
    assert (manifest.game.slug, manifest.game.title, manifest.index) == (SLUG, TITLE, 1)
    assert (manifest.started_at, manifest.stopped_at) == ("2026-10-02T18:04:11Z", "2026-10-02T18:04:11Z")
    assert manifest.obs.output_path == str(h.video_path())
    assert (manifest.obs.version, manifest.obs.websocket) == ("32.2.2", "5.7.4")
    assert manifest.clock == ClockRecord(
        kind=ClockKind.EVENT,
        zero_event="STARTED",
        capture_latency_ms=10,
        drift_samples=(
            DriftSample(at_ms=10, output_duration_ms=0),
            DriftSample(at_ms=18_010, output_duration_ms=17_400),
        ),
    )
    assert manifest.counts == Counts(received=2, accepted=2)
    assert manifest.sources_used == ("textractor",)
    assert h.vad.queued == [h.game_dir() / f"{stem}.session.json"]
    assert h.finalised() == [h.game_dir() / f"{stem}.session.json"]
    assert h.states() == [
        (AppState.ARMED, SLUG),
        (AppState.RECORDING, SLUG),
        (AppState.FINALISING, SLUG),
        (AppState.ARMED, SLUG),
    ]
    assert RecordingStarted(OBS_STEM) in h.events and RecordingStopped(OBS_STEM) in h.events


async def test_started_writes_the_manifest_and_journal_at_once(h: Harness):
    await h.arm()
    await h.started(ZERO)
    manifest = incoming_manifest(h)
    assert manifest.state is ManifestState.RECORDING
    assert manifest.index == 1
    assert (manifest.obs.profile, manifest.obs.collection) == ("Anki Miner Game", "Anki Miner Game")
    assert (h.incoming / f"{OBS_STEM}.lines.jsonl").exists()
    await h.line("こんにちは", ZERO + 5.0)
    assert journal(h) == [LineRecord(offset_ms=5010, text="こんにちは", source="textractor")]
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, 5010)


async def test_the_manifest_on_disk_carries_schema_1(h: Harness):
    await h.arm()
    await h.started(ZERO)
    data = json.loads((h.incoming / f"{OBS_STEM}.session.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["state"] == "recording"


async def test_the_session_number_follows_the_sessions_already_there(h: Harness):
    h.game_dir().mkdir(parents=True)
    (h.game_dir() / f"{TITLE} - 04.mkv").write_bytes(b"")
    await h.arm()
    await h.started(ZERO)
    assert incoming_manifest(h).index == 5


async def test_a_recording_that_starts_while_idle_is_not_a_session(h: Harness):
    await h.started(ZERO)
    assert not (h.incoming / f"{OBS_STEM}.session.json").exists()
    assert h.actor.state is AppState.IDLE


async def test_a_recording_outside_the_output_folder_is_not_a_session(h: Harness, tmp_path):
    await h.arm()
    elsewhere = tmp_path / "Videos"
    elsewhere.mkdir()
    (elsewhere / "mine.mkv").write_bytes(b"")
    await h.emit("RecordStateChanged", {"outputState": OutputState.STARTED, "outputPath": str(elsewhere / "mine.mkv")})
    assert h.actor.state is AppState.ARMED
    assert "outside the app's folder" in h.banners()[BannerKey.FOREIGN_RECORDING]
    assert not any(h.incoming.glob("*.session.json"))


async def test_lines_before_arming_are_ignored(h: Harness):
    h.actor.post(LineReceived("こんにちは", T0 + 1, "textractor"))
    await h.settle()
    assert h.accepted() == []


async def test_a_line_from_another_thread_is_handled_on_the_loop(h: Harness):
    await h.arm()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda e: seen.append(threading.get_ident()) if isinstance(e, LineAccepted) else None)
    worker = threading.Thread(target=h.sources[0].line, args=("こんにちは", T0 + 5))
    worker.start()
    worker.join()
    await h.settle()
    assert [e.line.text for e in h.accepted()] == ["こんにちは"]
    assert seen == [loop_thread]


async def test_lines_while_armed_are_shown_but_not_journalled(h: Harness):
    await h.arm()
    await h.line("こんにちは", T0 + 0.5)
    assert h.accepted() == [LineAccepted(h.accepted()[0].line, None)]
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 1.0)  # not a duplicate: STARTED reset the pipeline
    assert journal(h) == [LineRecord(offset_ms=1010, text="こんにちは", source="textractor")]


async def test_dropped_lines_are_counted_in_the_manifest(h: Harness):
    await h.arm()
    await h.started(ZERO)
    for raw, t in (("こんにちは", 1.0), ("こんにちは", 2.0), ("123", 3.0), ("", 4.0), ("あ" * 301, 5.0)):
        await h.line(raw, ZERO + t)
    await h.stopped(ZERO + 20.0)
    manifest = load_manifest(h.finalised()[0])
    assert manifest.counts == Counts(received=5, accepted=1, duplicate=1, no_letters=2, junk=1)


async def test_a_pause_drops_lines_and_is_journalled_with_drift_samples(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("はい", ZERO + 1.0)
    h.obs.output_duration = 2_000
    h.obs.record_paused = True
    await h.emit("RecordStateChanged", {"outputState": OutputState.PAUSED, "outputPath": None}, ZERO + 2.0)
    await h.line("いいえ", ZERO + 3.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)
    h.obs.record_paused = False
    await h.emit("RecordStateChanged", {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 5.0)
    await h.line("いいえ", ZERO + 6.0)  # not a duplicate: the paused line was never journalled
    assert journal(h) == [
        LineRecord(offset_ms=1010, text="はい", source="textractor"),
        PauseRecord(offset_ms=2010),
        ResumeRecord(offset_ms=2010),
        LineRecord(offset_ms=3010, text="いいえ", source="textractor"),
    ]
    manifest = incoming_manifest(h)
    assert manifest.counts.paused == 1
    assert manifest.clock.drift_samples[1:] == (
        DriftSample(at_ms=2010, output_duration_ms=2_000),
        DriftSample(at_ms=2010, output_duration_ms=2_000),
    )


async def test_a_merge_into_the_last_journalled_line_is_a_replace_record(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.started(ZERO)
    await rig.line("え", ZERO + 1.0)
    await rig.line("えっと…", ZERO + 1.5)
    assert journal(rig) == [LineRecord(offset_ms=1010, text="え", source="textractor"), ReplaceRecord(text="えっと…")]
    assert rig.accepted()[-1] == LineAccepted(rig.accepted()[-1].line, 1010, replaces_previous=True)


async def test_auto_start_holds_lines_until_started_and_journals_them_at_zero(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    trigger = h.accepted()[0].line
    await h.send(CommandKind.START, line=trigger)
    await h.line("つづき", T0 + 0.6)
    await h.started(ZERO)
    await h.line("つづき", ZERO + 1.0)  # a duplicate of the held line: no pipeline reset at STARTED
    assert journal(h) == [
        LineRecord(offset_ms=0, text="はじまり", source="textractor"),
        LineRecord(offset_ms=0, text="つづき", source="textractor"),
    ]


async def test_a_merge_into_a_held_line_replaces_it(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.line("え", T0 + 0.2)
    await rig.send(CommandKind.START, line=rig.accepted()[0].line)
    await rig.line("えっと…", T0 + 0.4)
    await rig.started(ZERO)
    assert journal(rig) == [LineRecord(offset_ms=0, text="えっと…", source="textractor")]


async def test_a_merge_whose_base_was_never_journalled_is_a_new_line(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.line("はじまり", T0 + 0.1)
    trigger = rig.accepted()[0].line
    await rig.line("え", T0 + 0.2)  # accepted before auto mode's START arrives: not held
    await rig.send(CommandKind.START, line=trigger)
    await rig.started(ZERO)
    await rig.line("えっと…", ZERO + 0.5)  # merges into "え", which the journal never saw
    assert journal(rig) == [
        LineRecord(offset_ms=0, text="はじまり", source="textractor"),
        LineRecord(offset_ms=0, text="えっと…", source="textractor"),
    ]


async def test_a_start_without_a_line_holds_nothing(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    await h.line("まえ", T0 + 0.5)
    await h.started(ZERO)
    assert journal(h) == []


async def test_a_failed_start_record_is_a_banner_and_drops_the_held_lines(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    h.gateway.fail("StartRecord", ObsRequestError("StartRecord", 500, "Output already running"))
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    assert "Output already running" in h.banners()[START_FAILED_BANNER_KEY]
    assert h.actor.state is AppState.ARMED
    await h.send(CommandKind.START)
    await h.started(ZERO)
    assert journal(h) == []
    assert START_FAILED_BANNER_KEY not in h.banners()


async def test_no_started_within_the_timeout_is_a_failed_start(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    await h.tick(h.clock.t + session_mod.START_TIMEOUT_S)
    assert START_FAILED_BANNER_KEY in h.banners()
    await h.send(CommandKind.START)  # may be tried again
    assert h.gateway.names().count("StartRecord") == 2


async def test_a_stopped_while_starting_is_a_failed_start(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    await h.stopped(T0 + 0.5)
    assert START_FAILED_BANNER_KEY in h.banners()


async def test_toggle_starts_and_stops(h: Harness):
    await h.arm()
    await h.send(CommandKind.TOGGLE)
    await h.started(ZERO)
    await h.send(CommandKind.TOGGLE)
    assert [n for n in h.gateway.names() if n.endswith("Record")] == ["StartRecord", "StopRecord"]


async def test_a_failed_stop_record_is_a_banner(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.gateway.fail("StopRecord", ObsRequestError("StopRecord", 501, "Output not running"))
    await h.send(CommandKind.STOP)
    assert "Output not running" in h.banners()[BannerKey.STOP_FAILED]
    assert h.actor.state is AppState.RECORDING


async def test_no_connected_source_at_start_is_a_warning_until_one_connects(h: Harness):
    h.sources[0].set_status(SourceStatus.CONNECTING)
    await h.arm()
    await h.started(ZERO)
    assert BannerKey.NO_SOURCE in h.banners()
    h.sources[0].set_status(SourceStatus.CONNECTED)
    await h.settle()
    assert BannerKey.NO_SOURCE not in h.banners()


async def test_zero_cues_keep_the_video_without_a_subtitle(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.stopped(ZERO + 30.0)
    manifest = load_manifest(h.finalised()[0])
    assert Flag.NO_CUES in manifest.flags
    assert manifest.files is not None and manifest.files.subtitle is None
    assert list(h.game_dir().glob("*.srt")) == []
    assert "no lines" in h.banners()[BannerKey.NO_CUES]
    assert h.vad.queued == []


async def test_arm_and_disarm_are_refused_while_recording(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.send(CommandKind.DISARM)
    await h.arm()
    assert h.actor.state is AppState.RECORDING
    assert "Stop the recording" in h.banners()[BannerKey.ARM]


async def test_a_finalise_failure_is_a_banner_and_the_game_stays_armed(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.video_path().unlink()
    await h.stopped(ZERO + 5.0)
    assert "gone" in h.banners()[BannerKey.FINALISE]
    assert h.actor.state is AppState.ARMED


async def test_vad_is_not_queued_when_disabled(h: Harness):
    h.cfg = AppConfig(output_root=h.cfg.output_root, vad=VadSettings(enabled=False))
    await h.arm()
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 1.0)
    await h.stopped(ZERO + 5.0)
    assert h.finalised() and h.vad.queued == []


async def test_quitting_while_recording_leaves_the_session_to_the_next_launch(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    await h.stop()
    manifest = incoming_manifest(h)
    assert manifest.state is ManifestState.RECORDING
    assert (manifest.counts.accepted, manifest.sources_used) == (1, ("textractor",))
    assert h.sources[0].closed == 1
    assert restore_path().exists()
```

- [ ] **Step 2: Run them to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_recording.py -q`
Expected: FAIL (most tests: no session files, no `LineAccepted`, `StartRecord` never sent).

- [ ] **Step 3: Add the imports, the session record and its helpers**

**Replace** in `anki_miner_game/session/session.py`:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from anki_miner_game import paths
```

**with**:

```python
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from anki_miner_game import __version__, paths
```

**Replace** in `anki_miner_game/session/session.py`:

```python
from anki_miner_game.models.manifest import Counts
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    START_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineReceived,
    ObsEvent,
    SessionEvent,
    SessionInput,
```

**with**:

```python
from anki_miner_game.models.manifest import (
    ClockKind,
    ClockRecord,
    Counts,
    DriftSample,
    Flag,
    GameRef,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    START_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    LineReceived,
    ObsEvent,
    RecordingStarted,
    RecordingStopped,
    SessionEvent,
    SessionFinalised,
    SessionInput,
```

**Replace** in `anki_miner_game/session/session.py`:

```python
    ObsUnsupportedError,
)
from anki_miner_game.models.profile import GameProfile, validate
from anki_miner_game.session.finalise import FinaliseResult, finalise
```

**with**:

```python
    ObsUnsupportedError,
    OutputState,
)
from anki_miner_game.models.pipeline import DROP_COUNTER, Accepted, Dropped
from anki_miner_game.models.profile import GameProfile, validate
from anki_miner_game.session.clock import EventClock, OutputDurationClock
from anki_miner_game.session.finalise import FinaliseError, FinaliseResult, finalise
from anki_miner_game.session.journal import (
    Journal,
    JournalRecord,
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
)
from anki_miner_game.session.manifest import (
    IncomingFiles,
    game_folder,
    incoming_files,
    reserve_index,
    write_manifest_atomic,
)
```

**Replace** in `anki_miner_game/session/session.py`:

```python
@dataclass
class _Armed:
    profile: GameProfile
    cfg: AppConfig
```

**with**:

```python
@dataclass
class _Armed:
    profile: GameProfile
    cfg: AppConfig


@dataclass
class _Session:
    manifest_path: Path
    manifest: SessionManifest
    """As last written, apart from the fields ``_write_manifest`` fills from the live state."""
    files: IncomingFiles
    journal: Journal
    clock: EventClock | OutputDurationClock
    """``EventClock`` from ``STARTED``; ``OutputDurationClock`` after reconcile rows 3 and 4."""
    samples: list[DriftSample] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    tail: GameLine | None = None
    """The line behind the journal's last ``LineRecord``, with its latest text."""
    tail_offset: int | None = None
    split: bool = False
    """``RecordFileChanged`` came: the journal holds the split's stop; later lines are not journalled."""
    next_anchor: float | None = None
    """While on the ``OutputDurationClock`` (``clock_degraded``): when to re-anchor next."""
```

**Replace** in `anki_miner_game/session/session.py`:

```python
    except OSError:
        return False
    return True
```

**with**:

```python
    except OSError:
        return False
    return True


def _same_line(a: GameLine, b: GameLine) -> bool:
    """A typewriter merge keeps its base line's ``t_mono`` and ``source_id`` (``TextPipeline``)."""
    return a.t_mono == b.t_mono and a.source_id == b.source_id


def _with_flag(flags: tuple[Flag, ...], flag: Flag) -> tuple[Flag, ...]:
    return flags if flag in flags else (*flags, flag)
```

**Replace** in `anki_miner_game/session/session.py`:

```python
        self._source_status: dict[str, SourceStatus] = {}
        self._counts = Counts()
```

**with**:

```python
        self._source_status: dict[str, SourceStatus] = {}
        self._session: _Session | None = None
        self._counts = Counts()
```

- [ ] **Step 4: Route lines, the new commands and the record events**

**Replace** in `anki_miner_game/session/session.py`:

```python
    async def _handle(self, msg: SessionInput) -> None:
        match msg:
            case ObsEvent():
```

**with**:

```python
    async def _handle(self, msg: SessionInput) -> None:
        match msg:
            case LineReceived():
                self._on_line(msg)
            case ObsEvent():
```

**Replace** in `anki_miner_game/session/session.py`:

```python
    async def _shutdown(self) -> None:
        if self._state is AppState.ARMED:
            await self._to_idle()
        else:
            await self._stop_sources()

    async def _on_tick(self, t: float) -> None:
        if self._state is AppState.IDLE and self._connected and t >= self._next_restore and restore_path().exists():
```

**with**:

```python
    async def _shutdown(self) -> None:
        if self._state is AppState.ARMED:
            await self._to_idle()
            return
        await self._stop_sources()
        s = self._session
        if s is not None:
            self._write_manifest(s)
            s.journal.close()

    async def _on_tick(self, t: float) -> None:
        if self._start_deadline is not None and t >= self._start_deadline:
            self._start_failed(f"OBS did not start recording within {START_TIMEOUT_S:g} s; check OBS for a message.")
        if self._state is AppState.IDLE and self._connected and t >= self._next_restore and restore_path().exists():
```

**Replace** in `anki_miner_game/session/session.py`:

```python
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)
```

**with**:

```python
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)
            case ObsEventName.RECORD_STATE_CHANGED:  # keyed on outputState: PAUSED has outputActive false
                match ev.data.get("outputState"):
                    case OutputState.STARTED:
                        await self._on_started(ev)
                    case OutputState.STOPPED:
                        await self._on_stopped(ev)
                    case OutputState.PAUSED:
                        await self._on_pause_edge(ev, paused=True)
                    case OutputState.RESUMED:
                        await self._on_pause_edge(ev, paused=False)
```

**Replace** in `anki_miner_game/session/session.py`:

```python
            case CommandKind.DISARM:
                await self._disarm()
```

**with**:

```python
            case CommandKind.DISARM:
                await self._disarm()
            case CommandKind.START:
                await self._start(cmd.line)
            case CommandKind.STOP:
                await self._stop()
            case CommandKind.TOGGLE:
                if self._state is AppState.RECORDING:
                    await self._stop()
                elif self._state is AppState.ARMED:
                    await self._start(None)
```

- [ ] **Step 5: Add the recording methods**

**Append** to `anki_miner_game/session/session.py`:

```python
    # --- recording ------------------------------------------------------------------------------

    def _count(self, name: str) -> None:
        self._counts = self._counts.incremented(name)

    async def _start(self, line: GameLine | None) -> None:
        """``StartRecord`` while armed (spec 11.4); the session begins at ``STARTED``, whoever starts it."""
        if self._state is not AppState.ARMED or self._start_deadline is not None:
            return
        if line is not None:  # auto mode: the session's counts start with the line that started it
            self._held = [line]
            self._counts = Counts(received=1, accepted=1)
        try:
            await self._recorder.start()
        except ObsError as exc:
            self._start_failed(f"OBS did not start recording: {exc}")
            return
        self._start_deadline = self._now() + START_TIMEOUT_S

    def _start_failed(self, text: str) -> None:
        """Spec 17: banner with OBS's message, state stays ``armed``; held lines are dropped."""
        self._start_deadline = None
        self._held = None
        self._banner(START_FAILED_BANNER_KEY, BannerLevel.ERROR, text)

    async def _stop(self) -> None:
        if self._state is not AppState.RECORDING:
            return
        await self._sample_drift()  # spec 7: outputDuration sampled at stop
        try:
            await self._recorder.stop()
        except ObsError as exc:
            self._banner(BannerKey.STOP_FAILED, BannerLevel.ERROR, f"OBS did not stop recording: {exc}")

    def _on_line(self, msg: LineReceived) -> None:
        pipeline = self._pipeline
        if pipeline is None:  # idle: a late frame from a source that was stopped
            return
        self._count("received")
        result = pipeline.process(msg)
        if isinstance(result, Dropped):
            self._count(DROP_COUNTER[result.reason])
        elif isinstance(result, Accepted):
            self._count("accepted")
            self._accept(result.line)
        else:
            self._replace(result.line)

    def _accept(self, line: GameLine) -> None:
        s = self._session
        if s is not None and not s.split:
            self._publish(LineAccepted(line, self._journal_line(s, line)))
            return
        if self._held is not None:
            self._held.append(line)
        self._publish(LineAccepted(line, None))

    def _replace(self, line: GameLine) -> None:
        """A typewriter merge (spec 8.2 step 9); see the module docstring for how it is journalled."""
        s = self._session
        if s is not None and not s.split:
            if s.tail is not None and _same_line(s.tail, line):
                self._append(s, ReplaceRecord(text=line.text))
                s.tail = line
                offset = s.tail_offset
            else:
                offset = self._journal_line(s, line)
            self._publish(LineAccepted(line, offset, replaces_previous=True))
            return
        if self._held is not None:
            if self._held and _same_line(self._held[-1], line):
                self._held[-1] = line
            else:
                self._held.append(line)
        self._publish(LineAccepted(line, None, replaces_previous=True))

    def _journal_line(self, s: _Session, line: GameLine) -> int | None:
        """A ``LineRecord`` at the line's offset; ``None`` (dropped, counted ``paused``) while paused."""
        offset = s.clock.offset_ms(line.t_mono)
        if offset is None:
            self._count("paused")
            if self._pipeline is not None:
                self._pipeline.reset()  # this line is not journalled, so it is no "previous line"
            return None
        self._append(s, LineRecord(offset_ms=offset, text=line.text, source=line.source_id))
        s.tail, s.tail_offset = line, offset
        if line.source_id not in s.sources_used:
            s.sources_used.append(line.source_id)
        return offset

    def _append(self, s: _Session, record: JournalRecord) -> None:
        try:
            s.journal.append(record)
        except OSError as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot write the session journal ({exc.strerror or exc}); lines are being lost.",
            )

    async def _on_started(self, ev: ObsEvent) -> None:
        """Ownership rule (spec 6.2): only a recording that starts while armed, into ``_incoming/``, is a session."""
        armed = self._armed
        if self._state is not AppState.ARMED or armed is None or self._pipeline is None:
            return
        self._start_deadline = None
        held, self._held = self._held, None
        output_path = ev.data.get("outputPath")
        if not isinstance(output_path, str) or not self._in_incoming(output_path):
            self._banner(
                BannerKey.FOREIGN_RECORDING,
                BannerLevel.WARNING,
                f"OBS is recording to {output_path or 'an unknown file'}, outside the app's folder, so this "
                "recording gets no subtitle. Arm the game again to put OBS back on the app's profile.",
            )
            return
        incoming = paths.incoming_dir(armed.cfg)
        profile = armed.profile
        try:
            files = incoming_files(incoming, output_path)
            manifest = SessionManifest(
                app_version=__version__,
                game=GameRef(slug=profile.slug, title=profile.title),
                index=reserve_index(game_folder(incoming, profile.title), incoming, profile.slug),
                state=ManifestState.RECORDING,
                started_at=self._utc_stamp(),
                obs=ObsRecord(
                    version=self._obs_versions[0],
                    websocket=self._obs_versions[1],
                    profile=OBS_PROFILE_NAME,
                    collection=OBS_COLLECTION_NAME,
                    output_path=output_path,
                ),
                clock=ClockRecord(kind=ClockKind.EVENT, zero_event=ZERO_EVENT, capture_latency_ms=CAPTURE_LATENCY_MS),
                text_mode=profile.text_mode,
            )
            write_manifest_atomic(files.manifest, manifest)
            journal = Journal(files.journal)
        except (StoreError, OSError, ValueError) as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot write the session files in {incoming} ({exc}), so this recording gets no subtitle.",
            )
            return
        clock = EventClock(CAPTURE_LATENCY_MS, now=self._now)
        clock.start(ev.t_mono)
        s = self._session = _Session(files.manifest, manifest, files, journal, clock)
        self._clear(START_FAILED_BANNER_KEY, BannerKey.FOREIGN_RECORDING, BannerKey.SESSION_FILES)
        if held:
            for line in held:
                self._journal_line(s, line)
        else:
            self._counts = Counts()
            self._pipeline.reset()
        self._set_state(AppState.RECORDING)
        self._publish(RecordingStarted(files.video.stem))
        if not any(status in _LIVE for status in self._source_status.values()):
            self._banner(
                BannerKey.NO_SOURCE,
                BannerLevel.WARNING,
                "No text source is connected: the recording gets no lines until one connects.",
            )
        await self._sample_drift()

    async def _on_pause_edge(self, ev: ObsEvent, *, paused: bool) -> None:
        s = self._session
        if s is None or s.clock.paused == paused:
            return
        record: JournalRecord
        if paused:
            s.clock.pause(ev.t_mono)
            record = PauseRecord(offset_ms=s.clock.reading_ms(ev.t_mono))
        else:
            s.clock.resume(ev.t_mono)
            record = ResumeRecord(offset_ms=s.clock.reading_ms(ev.t_mono))
        self._append(s, record)
        await self._sample_drift()
        self._write_manifest(s)

    async def _on_stopped(self, ev: ObsEvent) -> None:
        if self._session is not None:
            await self._end_session(ev.t_mono)
        elif self._start_deadline is not None:
            self._start_failed("OBS stopped the recording as it started; check OBS for a message.")

    async def _sample_drift(self) -> None:
        """Spec 7: ``outputDuration`` beside the clock's reading, kept in ``clock.drift_samples``."""
        s = self._session
        if s is None or not self._connected:
            return
        try:
            status, mid = await self._record_status()
        except ObsError:
            return
        if s is self._session and status.get("outputActive"):
            s.samples.append(s.clock.drift_sample(int(status.get("outputDuration") or 0), mid))

    async def _record_status(self) -> tuple[dict[str, Any], float]:
        """``GetRecordStatus`` and the monotonic midpoint of its round trip."""
        before = self._now()
        status = await self._gateway.request("GetRecordStatus")
        return status, (before + self._now()) / 2

    async def _end_session(self, stop_t: float, flag: Flag | None = None) -> None:
        """Journal the stop at the clock's reading for ``stop_t``, then finalise and return to ``armed``."""
        s, armed = self._session, self._armed
        if s is None or armed is None:
            return
        if not s.split:
            self._append(s, StopRecord(offset_ms=s.clock.reading_ms(stop_t)))
        s.journal.close()
        flags = s.manifest.flags if flag is None else _with_flag(s.manifest.flags, flag)
        self._write_manifest(s, stopped_at=self._utc_stamp(), flags=flags)
        self._session = None
        self._clear(BannerKey.NO_SOURCE, BannerKey.STOP_FAILED)
        self._set_state(AppState.FINALISING)
        self._publish(RecordingStopped(s.files.video.stem))
        await self._finalise(s.manifest_path, armed.cfg)
        if self._pipeline is not None:
            self._pipeline.reset()
        self._counts = Counts()
        self._set_state(AppState.ARMED)

    async def _finalise(self, manifest_path: Path, cfg: AppConfig) -> None:
        try:
            result = await self._finaliser.run(manifest_path, cfg)
        except FinaliseError as exc:
            self._banner(
                BannerKey.FINALISE,
                BannerLevel.ERROR,
                f"Could not finish the session {manifest_path.name} ({exc}); the app tries again at next launch.",
            )
            return
        self._publish(SessionFinalised(result.manifest_path))
        manifest = result.manifest
        if manifest.state is ManifestState.FINALISE_PENDING:
            self._banner(
                BannerKey.FINALISE,
                BannerLevel.WARNING,
                f"{manifest.game.title}: the video is still in use, so the session could not be moved; "
                "the app tries again at next launch.",
            )
        if Flag.NO_CUES in manifest.flags:
            self._banner(
                BannerKey.NO_CUES,
                BannerLevel.WARNING,
                f"{manifest.game.title}: no lines were recorded, so the video was kept without a subtitle.",
            )
        if result.queue_vad and self._vad_jobs is not None:
            self._vad_jobs.queue(result.manifest_path)

    def _write_manifest(self, s: _Session, **changes: Any) -> None:
        """Write the session's manifest with ``changes`` and the live counts, sources and drift samples."""
        manifest = replace(s.manifest, **changes)
        s.manifest = replace(
            manifest,
            counts=self._counts,
            sources_used=tuple(s.sources_used),
            clock=replace(manifest.clock, drift_samples=tuple(s.samples)),
        )
        try:
            write_manifest_atomic(s.manifest_path, s.manifest)
        except StoreError as exc:
            self._banner(BannerKey.SESSION_FILES, BannerLevel.ERROR, f"Cannot update the session manifest ({exc}).")

    def _in_incoming(self, output_path: str) -> bool:
        """Whether OBS's ``output_path`` lies directly in the configured ``_incoming/`` (symlinks resolved)."""
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        try:
            return Path(output_path).parent.samefile(paths.incoming_dir(cfg))
        except OSError:
            return False

    def _utc_stamp(self) -> str:
        return self._utc_now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 6: Run them to pass, then lint and type-check**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_recording.py tests/session/test_session_actor.py tests/session/test_session_arm.py -q`
Expected: PASS (64 passed).

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/session/session.py tests/session/test_session_recording.py && git commit -m "feat(session): record a session from STARTED to finalise"
```

---

### Task 5: ending without a plain `STOPPED`

**Files:**
- Modify: `anki_miner_game/session/session.py`
- Test: `tests/session/test_session_endings.py`

**Interfaces:**
- Consumes: Task 4's `_end_session(stop_t, flag)`, `_append`, `_write_manifest`, `_with_flag`;
  `ObsDiscovery.is_running`; `clock.reading_ms`.
- Produces: `RecordFileChanged` (split), `ExitStarted`, lost connection + OBS gone (spec 6.4);
  `_lost_at` and `_obs_gone(stop_t)`, which Task 6's reconcile uses.

- [ ] **Step 1: Write the failing tests**

**Create** `tests/session/test_session_endings.py`:

```python
"""Ending a session without a plain STOPPED: split, OBS exit, lost connection (spec 6.4, 7; 17 rows
RecordFileChanged, connection lost while recording, OBS exits while recording)."""

from anki_miner_game.models.manifest import Flag
from anki_miner_game.models.messages import AppState, LineAccepted
from anki_miner_game.models.obs import ObsEventName
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import LineRecord, StopRecord, read_journal
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, T0, TITLE, Harness

ZERO = T0 + 1.0
SECOND_STEM = "2026-10-02 18-05-00"


def srt(h: Harness) -> str:
    return (h.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8")


async def test_a_split_finalises_the_first_file_and_flags_it(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    second = h.video_path(SECOND_STEM)
    second.write_bytes(b"the second file")
    await h.emit(ObsEventName.RECORD_FILE_CHANGED, {"newOutputPath": str(second)}, ZERO + 5.0)
    await h.line("あと", ZERO + 6.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl") == [
        LineRecord(offset_ms=1010, text="まえ", source="textractor"),
        StopRecord(offset_ms=5010),
    ]
    assert "split" in h.banners()[BannerKey.SPLIT]
    await h.stopped(ZERO + 10.0)
    assert srt(h) == "1\n00:00:01,010 --> 00:00:04,660\nまえ\n"
    assert Flag.SPLIT_UNSUPPORTED in load_manifest(h.finalised()[0]).flags
    assert sorted(p.name for p in h.incoming.iterdir()) == [f"{SECOND_STEM}.mkv"]


async def test_exit_started_ends_the_session_flagged_obs_exited(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    await h.emit(ObsEventName.EXIT_STARTED, {}, ZERO + 8.0)
    assert srt(h) == "1\n00:00:01,010 --> 00:00:07,660\nまえ\n"
    assert Flag.OBS_EXITED in load_manifest(h.finalised()[0]).flags
    assert "OBS closed" in h.banners()[BannerKey.OBS_EXITED]
    assert h.actor.state is AppState.ARMED


async def test_lines_keep_being_journalled_while_the_connection_is_lost(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST, {}, ZERO + 2.0)
    await h.line("まだ", ZERO + 3.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl") == [
        LineRecord(offset_ms=3010, text="まだ", source="textractor")
    ]
    checks = h.discovery.running_checks
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S - 0.5)
    assert h.discovery.running_checks == checks  # not due yet
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S)
    assert h.discovery.running_checks == checks + 1  # OBS still runs: the recording goes on
    assert h.actor.state is AppState.RECORDING


async def test_obs_gone_after_a_lost_connection_ends_the_session(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST, {}, ZERO + 2.0)
    h.discovery.running = False
    await h.line("あと", ZERO + 3.0)
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S)
    # The stop is the reading when the connection dropped, never below the last line: 3010 here, so
    # the line journalled after the loss is a click-through (D = 0) and only the first line is a cue.
    assert srt(h) == "1\n00:00:01,010 --> 00:00:02,660\nまえ\n"
    manifest = load_manifest(h.finalised()[0])
    assert Flag.OBS_EXITED in manifest.flags
    assert manifest.counts.skip == 1
    assert h.actor.state is AppState.ARMED
```

- [ ] **Step 2: Run them to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_endings.py -q`
Expected: FAIL (4 failed).

- [ ] **Step 3: Implement**

**Replace** in `anki_miner_game/session/session.py`:

```python
        self._obs_versions = ("unknown", "unknown")
        self._next_restore = 0.0
```

**with**:

```python
        self._obs_versions = ("unknown", "unknown")
        self._lost_at: float | None = None
        """While recording: when the connection dropped; the stop offset if OBS turns out gone."""
        self._next_gone_check = 0.0
        self._next_restore = 0.0
```

**Replace** in `anki_miner_game/session/session.py`:

```python
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)
            case ObsEventName.RECORD_STATE_CHANGED:  # keyed on outputState: PAUSED has outputActive false
```

**with**:

```python
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)
                if self._session is not None:  # spec 17: lines keep being journalled on the EventClock
                    self._lost_at = ev.t_mono
                    self._next_gone_check = ev.t_mono + OBS_GONE_CHECK_S
            case ObsEventName.EXIT_STARTED:
                if self._session is not None:
                    await self._obs_gone(ev.t_mono)
            case ObsEventName.RECORD_FILE_CHANGED:
                await self._on_split(ev)
            case ObsEventName.RECORD_STATE_CHANGED:  # keyed on outputState: PAUSED has outputActive false
```

**Replace** in `anki_miner_game/session/session.py`:

```python
            self._start_failed(f"OBS did not start recording within {START_TIMEOUT_S:g} s; check OBS for a message.")
```

**with**:

```python
            self._start_failed(f"OBS did not start recording within {START_TIMEOUT_S:g} s; check OBS for a message.")
        s = self._session
        if s is not None and not self._connected and self._lost_at is not None and t >= self._next_gone_check:
            self._next_gone_check = t + OBS_GONE_CHECK_S
            if not await asyncio.to_thread(self._discovery.is_running):
                await self._obs_gone(self._lost_at)  # spec 6.4: reconcile found OBS gone
                return
```

**Replace** in `anki_miner_game/session/session.py`:

```python
        self._session = None
        self._clear(BannerKey.NO_SOURCE, BannerKey.STOP_FAILED)
```

**with**:

```python
        self._session = None
        self._lost_at = None
        self._clear(BannerKey.NO_SOURCE, BannerKey.STOP_FAILED)
```

**Append** to `anki_miner_game/session/session.py`:

```python
    # --- ending without STOPPED (spec 6.4, 7) ---------------------------------------------------

    async def _on_split(self, ev: ObsEvent) -> None:
        """Spec 7: finalise against the first file; the split's stop is the first stop in the journal."""
        s = self._session
        if s is None or s.split:
            return
        self._append(s, StopRecord(offset_ms=s.clock.reading_ms(ev.t_mono)))
        s.split = True
        if self._pipeline is not None:
            self._pipeline.reset()
        self._write_manifest(s, flags=_with_flag(s.manifest.flags, Flag.SPLIT_UNSUPPORTED))
        self._banner(
            BannerKey.SPLIT,
            BannerLevel.WARNING,
            "OBS split the recording into a second file: lines from the split on get no subtitle.",
        )

    async def _obs_gone(self, stop_t: float) -> None:
        """Spec 6.4: ``ExitStarted``, or OBS gone after a lost connection."""
        await self._end_session(stop_t, Flag.OBS_EXITED)
        self._banner(
            BannerKey.OBS_EXITED,
            BannerLevel.WARNING,
            "OBS closed during the recording; the session was saved up to that moment.",
        )
```

- [ ] **Step 4: Run them to pass, then lint and type-check**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_endings.py tests/session/test_session_recording.py tests/session/test_session_actor.py tests/session/test_session_arm.py -q`
Expected: PASS (68 passed).

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/session/session.py tests/session/test_session_endings.py && git commit -m "feat(session): end a session on a split, an OBS exit or OBS gone"
```

---

### Task 6: reconcile, launch and orphans

**Files:**
- Modify: `anki_miner_game/session/session.py`
- Test: `tests/session/test_session_reconcile.py`

**Interfaces:**
- Consumes: Tasks 3-5; `session.clock.OutputDurationClock(now=)` with `anchor(mono_mid,
  output_duration_ms)`, `pause`, `resume`, `paused` (T03); `load_manifest`, `MANIFEST_SUFFIX`,
  `incoming_files` (T06); `Journal(path)` appending to an existing journal (T06).
- Produces: every row of spec 6.3 on each `_Connected`, matching an active recording to its
  manifest, the degraded clock and its re-anchoring, orphan finalise, the full launch duties.

- [ ] **Step 1: Write the failing tests**

**Create** `tests/session/test_session_reconcile.py`:

```python
"""Reconcile on connect: every row of spec 6.3, launch duties and orphan finalise (spec 6.2, 6.3, 10.3;
17 row "Unclean previous exit"; 18.1 row Reconcile)."""

from pathlib import Path

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.manifest import (
    ClockKind,
    Flag,
    GameRef,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.messages import AppState, CommandKind, LineAccepted, RecordingStarted
from anki_miner_game.models.obs import ObsConnectError, ObsEventName, OutputState
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session.journal import (
    Journal,
    JournalRecord,
    LineRecord,
    PauseRecord,
    ResumeRecord,
    read_journal,
)
from anki_miner_game.session.manifest import load_manifest, write_manifest_atomic
from anki_miner_game.session.restore import ObsRestore, restore_path, save_restore
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, SLUG, T0, TITLE, Harness

ZERO = T0 + 1.0
ORPHAN_STEM = "2026-10-01 20-00-00"


def leave_session(
    h: Harness,
    stem: str,
    *,
    state: ManifestState = ManifestState.RECORDING,
    records: tuple[JournalRecord, ...] = (),
) -> Path:
    """What a crashed or restarted app leaves in ``_incoming/``: video, manifest and journal."""
    video = h.incoming / f"{stem}.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
    manifest = SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug=SLUG, title=TITLE),
        index=1,
        state=state,
        started_at="2026-10-01T20:00:00Z",
        obs=ObsRecord(
            version="32.2.2",
            websocket="5.7.4",
            profile=OBS_PROFILE_NAME,
            collection=OBS_COLLECTION_NAME,
            output_path=str(video),
        ),
        text_mode=TextMode.HOOK,
    )
    write_manifest_atomic(h.incoming / f"{stem}.session.json", manifest)
    if state is ManifestState.RECORDING:
        journal = Journal(h.incoming / f"{stem}.lines.jsonl")
        for record in records:
            journal.append(record)
        journal.close()
    return video


async def recording(h: Harness) -> None:
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)


async def reconnect(h: Harness, lost: float, back: float) -> None:
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST, {}, lost)
    h.gateway.connected = True
    await h.emit(ObsEventName.CONNECTED, {}, back)


async def test_launch_reconciles_first(h: Harness):
    assert h.gateway.names()[:2] == ["GetVersion", "GetRecordStatus"]


async def test_every_connect_reconciles_again(h: Harness):
    await h.send(CommandKind.ARM, SLUG)
    before = h.gateway.names().count("GetVersion")
    await h.emit(ObsEventName.CONNECTED)
    assert h.gateway.names().count("GetVersion") == before + 1


async def test_row1_a_recording_that_stopped_while_disconnected_is_treated_as_stopped(h: Harness):
    await recording(h)
    h.obs.record_active = False
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 30.0)
    assert h.actor.state is AppState.ARMED
    # stop offset = the reading when the connection dropped (2010)
    assert (h.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,010 --> 00:00:01,660\nまえ\n"
    )


async def test_row1_a_new_recording_after_the_drop_ends_ours_and_is_not_a_session(h: Harness):
    await recording(h)
    other = h.video_path("2026-10-02 18-30-00")
    other.write_bytes(b"")
    h.obs.output_path = str(other)
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 30.0)
    assert h.actor.state is AppState.ARMED
    assert len(h.finalised()) == 1
    assert not (h.incoming / "2026-10-02 18-30-00.session.json").exists()


async def test_row2_the_same_recording_continues(h: Harness):
    await recording(h)
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 3.0)
    await h.line("あと", ZERO + 4.0)
    assert h.actor.state is AppState.RECORDING
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, 4010)
    assert Flag.CLOCK_DEGRADED not in load_manifest(h.incoming / f"{OBS_STEM}.session.json").flags


async def test_row3_a_missed_pause_switches_to_the_output_duration_clock(h: Harness):
    await recording(h)
    h.obs.record_paused = True
    h.obs.output_duration = 1_500
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 10.0)
    manifest = load_manifest(h.incoming / f"{OBS_STEM}.session.json")
    assert (manifest.clock.kind, manifest.clock.degraded) == (ClockKind.OUTPUT_DURATION, True)
    assert Flag.CLOCK_DEGRADED in manifest.flags
    assert BannerKey.CLOCK in h.banners()
    await h.line("ていし", ZERO + 11.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)  # paused: dropped
    h.obs.record_paused = False
    await h.emit(
        ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 20.0
    )
    h.obs.output_duration = 2_800
    await h.tick(ZERO + 21.0)  # re-anchor: 10 s after the anchor taken at the reconnect
    await h.line("あと", ZERO + 22.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")[-3:] == [
        PauseRecord(offset_ms=1_500),  # the missed edge, at the new clock's reading
        ResumeRecord(offset_ms=1_500),
        LineRecord(offset_ms=3_800, text="あと", source="textractor"),  # 2 800 re-anchored + 1 000
    ]


async def test_row4_an_app_restart_resumes_the_running_session(rig: Harness):
    video = leave_session(rig, OBS_STEM, records=(LineRecord(offset_ms=5_000, text="まえ", source="textractor"),))
    rig.obs.record_active = True
    rig.obs.output_path = str(video)
    rig.obs.output_duration = 60_000
    await rig.start()
    assert rig.states() == [(AppState.RECORDING, SLUG)]
    assert RecordingStarted(OBS_STEM) in rig.events
    assert rig.sources[0].starts == 1
    manifest = load_manifest(rig.incoming / f"{OBS_STEM}.session.json")
    assert manifest.clock.kind is ClockKind.OUTPUT_DURATION and Flag.CLOCK_DEGRADED in manifest.flags
    await rig.line("あと", T0 + 1.0)
    await rig.stopped(T0 + 4.0)
    assert (rig.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:05,000 --> 00:00:20,000\nまえ\n\n2\n00:01:01,000 --> 00:01:03,650\nあと\n"
    )


async def test_row5_an_active_recording_without_a_manifest_is_not_ours(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.obs.record_active = True
    rig.obs.output_path = str(rig.video_path("2026-10-02 19-00-00"))
    await rig.start()
    assert rig.actor.state is AppState.IDLE
    assert len(rig.finalised()) == 1  # the orphan: the live file is known and is not it
    await rig.arm()
    assert "recording" in rig.banners()[BannerKey.ARM]


async def test_row5_an_unmatchable_recording_holds_the_orphans_back(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.obs.record_active = True
    rig.obs.output_path = None  # GetOutputSettings answers for no output
    await rig.start()
    assert rig.finalised() == []
    assert (rig.incoming / f"{ORPHAN_STEM}.session.json").exists()
    rig.obs.record_active = False
    await rig.emit(ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.STOPPED, "outputPath": "x.mkv"})
    assert len(rig.finalised()) == 1  # nothing records any more


async def test_row6_orphans_are_finalised_after_reconcile(rig: Harness):
    leave_session(rig, ORPHAN_STEM, records=(LineRecord(offset_ms=2_000, text="まえ", source="textractor"),))
    leave_session(rig, "2026-10-01 21-00-00", state=ManifestState.FINALISE_PENDING)
    await rig.start()
    placed = sorted(path.name for path in rig.finalised())
    assert placed == [f"{TITLE} - 01.session.json", f"{TITLE} - 02.session.json"]
    assert list(rig.incoming.iterdir()) == []
    # no stop record: stop = the last offset + max_cue_seconds, so the cue runs to the 15 s cap
    assert (rig.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:02,000 --> 00:00:16,650\nまえ\n"
    )


async def test_orphans_wait_while_obs_runs_but_cannot_be_reached(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.gateway.connect_error = ObsConnectError("connection refused")
    await rig.start()
    assert rig.finalised() == []
    assert "connection refused" in rig.banners()[BannerKey.OBS]


async def test_orphans_are_finalised_at_launch_when_obs_is_not_running(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    rig.discovery.running = False
    await rig.start()
    assert len(rig.finalised()) == 1
    assert rig.gateway.connects == 0
    assert restore_path().exists()  # waits for the next connection


async def test_launch_restores_the_profile_an_unclean_exit_left(rig: Harness):
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    rig.obs.profiles.append("Mine")
    rig.obs.collections.append("Scenes")
    rig.obs.profile, rig.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    await rig.start()
    assert (rig.obs.profile, rig.obs.collection) == ("Mine", "Scenes")
    assert not restore_path().exists()
```

- [ ] **Step 2: Run them to fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session/test_session_reconcile.py -q`
Expected: FAIL (every test except `test_row2_*`, `test_orphans_wait_*` and `test_launch_restores_*`).

- [ ] **Step 3: Implement**

**Replace** in `anki_miner_game/session/session.py`:

```python
from pathlib import Path
from typing import Any, Final
```

**with**:

```python
from pathlib import Path, PureWindowsPath
from typing import Any, Final
```

**Replace** in `anki_miner_game/session/session.py`:

```python
from anki_miner_game.session.manifest import (
    IncomingFiles,
    game_folder,
    incoming_files,
    reserve_index,
    write_manifest_atomic,
)
```

**with**:

```python
from anki_miner_game.session.manifest import (
    MANIFEST_SUFFIX,
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    reserve_index,
    write_manifest_atomic,
)
```

**Replace** in `anki_miner_game/session/session.py`:

```python
def _with_flag(flags: tuple[Flag, ...], flag: Flag) -> tuple[Flag, ...]:
    return flags if flag in flags else (*flags, flag)
```

**with**:

```python
def _with_flag(flags: tuple[Flag, ...], flag: Flag) -> tuple[Flag, ...]:
    return flags if flag in flags else (*flags, flag)


def _file_name(output_path: str) -> str:
    return PureWindowsPath(output_path).name  # both separators, like session.manifest.incoming_files
```

**Replace** in `anki_miner_game/session/session.py`:

```python
    async def _launch(self) -> None:
        """With OBS running, connect; the ``_Connected`` event does the rest."""
        if await asyncio.to_thread(self._discovery.is_running):
            await self._ensure_connected()
```

**with**:

```python
    async def _launch(self) -> None:
        """Launch duties (spec 6.2, 6.3, 10.3, 17 "Unclean previous exit").

        OBS running: connect; the ``_Connected`` event reconciles, restores ``obs_restore.json``
        and finalises orphans. OBS not running: nothing can be recording, so every session left in
        ``_incoming/`` is finalised now; the restore waits for the next connection.
        """
        if await asyncio.to_thread(self._discovery.is_running):
            await self._ensure_connected()
        else:
            await self._sweep_orphans(exclude=None)
```

**Replace** in `anki_miner_game/session/session.py`:

```python
            case ObsEventName.CONNECTED:
                self._connected = True
                self._obs_status(SourceStatus.CONNECTED)
                self._clear(BannerKey.OBS)
                if self._state is AppState.IDLE:
                    await self._restore_obs()
```

**with**:

```python
            case ObsEventName.CONNECTED:
                self._connected = True
                self._obs_status(SourceStatus.CONNECTED)
                self._clear(BannerKey.OBS)
                await self._reconcile()
                self._lost_at = None
```

**Replace** in `anki_miner_game/session/session.py`:

```python
    async def _on_stopped(self, ev: ObsEvent) -> None:
        if self._session is not None:
            await self._end_session(ev.t_mono)
        elif self._start_deadline is not None:
            self._start_failed("OBS stopped the recording as it started; check OBS for a message.")
```

**with**:

```python
    async def _on_stopped(self, ev: ObsEvent) -> None:
        if self._session is not None:
            await self._end_session(ev.t_mono)
            return
        if self._start_deadline is not None:
            self._start_failed("OBS stopped the recording as it started; check OBS for a message.")
        await self._sweep_orphans(exclude=None)  # nothing records now: every _incoming/ session is an orphan
```

**Replace** in `anki_miner_game/session/session.py`:

```python
                await self._obs_gone(self._lost_at)  # spec 6.4: reconcile found OBS gone
                return
```

**with**:

```python
                await self._obs_gone(self._lost_at)  # spec 6.4: reconcile found OBS gone
                return
        if s is not None and self._connected and s.next_anchor is not None and t >= s.next_anchor:
            s.next_anchor = t + REANCHOR_S
            await self._reanchor(s)
```

**Append** to `anki_miner_game/session/session.py`:

```python
    # --- reconcile (spec 6.3) and orphans (spec 10.3) -------------------------------------------

    async def _reconcile(self) -> None:
        """Every row of spec 6.3, run on each ``_Connected`` before any later event is handled."""
        try:
            version = await self._gateway.request("GetVersion")
            status, mid = await self._record_status()
            active = bool(status.get("outputActive"))
            live_path = await self._live_output_path() if active else None
        except ObsError as exc:  # the link dropped again: the next _Connected reconciles
            log.info("reconcile postponed: %s", exc)
            return
        self._obs_versions = (
            str(version.get("obsVersion") or "unknown"),
            str(version.get("obsWebSocketVersion") or "unknown"),
        )
        s = self._session
        if s is not None:
            ours = active and (live_path is None or _file_name(live_path) == _file_name(s.manifest.obs.output_path))
            if not ours:  # row 1 (or it stopped and a new recording started while disconnected)
                await self._end_session(self._lost_at if self._lost_at is not None else mid)
            elif bool(status.get("outputPaused")) != s.clock.paused:  # row 3: a pause edge was missed
                s.clock = self._degraded_clock(status, mid)
                reading = s.clock.reading_ms(mid)
                self._append(s, PauseRecord(offset_ms=reading) if s.clock.paused else ResumeRecord(offset_ms=reading))
                self._mark_degraded(s, mid)
            # row 2: continue
        elif active:
            found = self._manifest_for(live_path) if live_path is not None else None
            if found is not None:  # row 4: the app restarted mid-session
                await self._resume(found, status, mid)
            elif live_path is None:  # row 5, and which _incoming/ session is live is unknown: sweep nothing
                return
            # row 5: not ours; arming refuses while it runs (spec 6.2 step 1)
        live = self._session.manifest_path if self._session is not None else None
        await self._sweep_orphans(exclude=live)  # row 6
        if self._state is AppState.IDLE:
            await self._restore_obs()

    async def _live_output_path(self) -> str | None:
        """The file the active recording writes (``GetOutputSettings``, source findings section 7).

        ``None`` when no output in ``RECORD_OUTPUT_NAMES`` answers with a path. A split does not
        change it: the path stays the first file's (R2 split run).
        """
        for name in RECORD_OUTPUT_NAMES:
            try:
                data = await self._gateway.request("GetOutputSettings", outputName=name)
            except ObsRequestError:
                continue
            settings = data.get("outputSettings")
            path = settings.get("path") if isinstance(settings, dict) else None
            if isinstance(path, str) and path:
                return path
        return None

    def _manifest_for(self, output_path: str) -> Path | None:
        """Spec 6.3: the ``_incoming/`` manifest of ``output_path`` in state ``recording``, if any."""
        if not self._in_incoming(output_path):
            return None
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        try:
            path = incoming_files(paths.incoming_dir(cfg), output_path).manifest
            manifest = load_manifest(path)
        except (FileNotFoundError, StoreError, ValueError):
            return None
        return path if manifest.state is ManifestState.RECORDING else None

    async def _resume(self, manifest_path: Path, status: dict[str, Any], mid: float) -> None:
        """Row 4: continue the session's journal on the ``OutputDurationClock``, flag ``clock_degraded``."""
        try:
            manifest = load_manifest(manifest_path)
            files = incoming_files(manifest_path.parent, manifest.obs.output_path)
            journal = Journal(files.journal)
        except (FileNotFoundError, StoreError, OSError, ValueError) as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot resume the session {manifest_path.name} ({exc}).",
            )
            return
        await self._stop_sources()
        cfg = self._get_config()
        profile = self._get_profile(manifest.game.slug) or GameProfile(
            slug=manifest.game.slug, title=manifest.game.title, text_mode=manifest.text_mode
        )
        s = self._session = _Session(
            manifest_path,
            manifest,
            files,
            journal,
            self._degraded_clock(status, mid),
            samples=list(manifest.clock.drift_samples),
            sources_used=list(manifest.sources_used),
        )
        self._armed = _Armed(profile=profile, cfg=cfg)
        self._pipeline = TextPipeline(profile.filters)
        self._held = None
        self._start_deadline = None
        self._counts = manifest.counts
        self._mark_degraded(s, mid)
        self._start_sources(cfg, profile)
        self._set_state(AppState.RECORDING)
        self._publish(RecordingStarted(files.video.stem))

    def _degraded_clock(self, status: dict[str, Any], mid: float) -> OutputDurationClock:
        """Spec 7 fallback, anchored on this ``GetRecordStatus``; paused when OBS says so."""
        clock = OutputDurationClock(now=self._now)
        clock.anchor(mid, int(status.get("outputDuration") or 0))
        if status.get("outputPaused"):
            clock.pause(mid)
        return clock

    def _mark_degraded(self, s: _Session, mid: float) -> None:
        s.next_anchor = mid + REANCHOR_S
        self._write_manifest(
            s,
            clock=replace(s.manifest.clock, kind=ClockKind.OUTPUT_DURATION, degraded=True),
            flags=_with_flag(s.manifest.flags, Flag.CLOCK_DEGRADED),
        )
        self._banner(
            BannerKey.CLOCK,
            BannerLevel.WARNING,
            "OBS's recording changed while the app was not watching: subtitle timing for the rest of "
            "this session may be off by up to a few seconds.",
        )

    async def _reanchor(self, s: _Session) -> None:
        """Spec 7: re-anchor the ``OutputDurationClock`` every ``REANCHOR_S``, following OBS's pause flag."""
        try:
            status, mid = await self._record_status()
        except ObsError:
            return
        clock = s.clock
        if s is not self._session or not status.get("outputActive") or not isinstance(clock, OutputDurationClock):
            return
        paused = bool(status.get("outputPaused"))
        if paused and not clock.paused:
            clock.pause(mid)
        elif clock.paused and not paused:
            clock.resume(mid)
        clock.anchor(mid, int(status.get("outputDuration") or 0))

    async def _sweep_orphans(self, exclude: Path | None) -> None:
        """Finalise every session left in ``_incoming/`` (spec 6.3 last row, 10.3) except ``exclude``.

        Called only when no recording can be writing one of them: after reconcile, after a
        ``STOPPED`` with no session, and at launch with OBS not running.
        """
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        for path in sorted(paths.incoming_dir(cfg).glob(f"*{MANIFEST_SUFFIX}")):
            if path == exclude:
                continue
            try:
                manifest = load_manifest(path)
            except (FileNotFoundError, StoreError) as exc:
                log.warning("skipping %s: %s", path.name, exc)
                continue
            if manifest.state in (ManifestState.RECORDING, ManifestState.FINALISE_PENDING):
                await self._finalise(path, cfg)
```

- [ ] **Step 4: Run them to pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/pytest -n0 -p no:cacheprovider tests/session tests/obs -q`
Expected: PASS (every test in both folders).

- [ ] **Step 5: Lint and type-check**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && git add anki_miner_game/session/session.py tests/session/test_session_reconcile.py && git commit -m "feat(session): reconcile on every connect and finalise orphans"
```

---

### Task 7: gate and status

**Files:** none changed; `gate.log` is kept untracked as evidence (it is in `.gitignore`).

- [ ] **Step 1: Run the Definition-of-Done gate**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash scripts/health.sh > gate.log 2>&1`
Expected: PASS (exit 0; `gate.log` ends with `SUMMARY` and `all green`; `PASS black`, `PASS ruff`,
`PASS mypy`, `PASS pytest`).

Read the whole `gate.log` (never `tail` it into the report). A red step is fixed on this branch with
its own commit, then the gate runs again.

- [ ] **Step 2: Record the result in the status file**

Keep `base_sha`; set `stage` to `implemented`, `head_sha` to `git rev-parse HEAD`, `gate_exit` to
`0`, `gate_summary` to the PASS lines of `gate.log`, and leave `contract_change_request` as the
planner filed it (decision 5). With the project interpreter:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t15-session && .venv/bin/python - <<'EOF'
import json, subprocess
from pathlib import Path
path = Path("/home/light/Projects/anki_miner_game/.orchestration/status/t15-session.json")
status = json.loads(path.read_text(encoding="utf-8"))
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
passes = [line for line in Path("gate.log").read_text(encoding="utf-8").splitlines() if line.startswith(("PASS ", "FAIL "))]
status.update(stage="implemented", head_sha=head, gate_exit=0, gate_summary=", ".join(passes) + "; all green")
path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
EOF
```

- [ ] **Step 3: Report**

Return `{branch, head_sha, gate_exit, gate_summary, files, contract_change_request, notes}`. In
`notes` name anything the M0 gate or R2 changed that you applied (decision 4, the provisional
constants), and repeat the hand-off for T16 from "Interfaces produced".

## Self-review (planner)

- Spec coverage: every row of the coverage map points at code in Tasks 1-6 and at named tests;
  18.1's Reconcile row has one test per spec 6.3 row plus the matching cases (decision 5, 6).
- Card items: arming steps 1-4 with timeout restore; ownership rule; every reconcile row incl.
  matching; orphan finalise only after reconcile or with OBS absent; ending without `STOPPED`;
  `RecordFileChanged`; pause edges; auto-start hold journalled at offset 0 on `STARTED`;
  `SessionEvent` publication with `StateChanged.slug`; no-source and free-space banners; pipeline
  resets at the three places; `Replaced` journalling rule; single finalise worker;
  `START_FAILED_BANNER_KEY`; sources started, stopped and closed on the actor's thread; status
  listener marshalled with `call_soon_threadsafe`.
- Not here (other cards): feed broadcast and Presenter forwarding (T16 subscribes), auto mode
  (T17), the GUI, `GetOutputSettings` in `REQUIRED_REQUESTS` (contract change request, decision 5).
- Placeholders: none; every code step carries the code, every run step its command and outcome.
- Names: `SessionActor`, `FinaliseWorker`, `BannerKey`, `ObsRestore`, `ObsRecorder` and every
  private method are spelled the same in every task; the verification run applied the steps
  literally.

