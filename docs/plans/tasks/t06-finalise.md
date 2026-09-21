# T06 journal, manifest I/O, finalise: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (one implementer, tasks in
> the order below; each task imports the modules of the tasks before it, so do not split them across
> agents) with superpowers:test-driven-development inside every task. Steps use checkbox (`- [ ]`)
> syntax.

**Goal:** Make a session crash-safe on disk and turn a stopped session into the Anki Miner pair:
the append-only journal (`session/journal.py`), manifest file I/O and session-number reservation
(`session/manifest.py`), and the one idempotent finalise routine (`session/finalise.py`) that
builds the cues, writes the subtitle and places `<Game>/<Game> - NN.mkv/.srt/.session.json`.

**Architecture:** The journal is a JSON-lines file flushed per record; its reader skips any line
that is not a record, so a torn last line costs nothing. Finalise is a two-phase state machine on
the manifest's `state`: `recording` -> (journal, `build_cues`, `<obs stem>.srt`, manifest
`finalise_pending`, journal deleted) -> (video moved with a lock retry, subtitle and manifest
written at their final names from the manifest, `_incoming/` emptied) -> `ready`. Every file
operation is either atomic (`os.replace`) or re-derivable from the manifest, so running finalise
again after a crash at any operation reaches the same end state; a test crashes it before every
operation and proves that.

**Tech Stack:** Python 3.12 stdlib only (`json`, `dataclasses`, `pathlib`, `datetime`, `os`,
structural pattern matching); dev: pytest, black, ruff, mypy. Reuses T02's `build_cues` /
`write_srt_atomic`, T04's `session_stem` / `sanitise_title` / `parse_index`, and T01's
`store.write_text_atomic` machinery and `models.manifest`.

**Spec:** `/home/light/Projects/anki_miner_game/.worktrees/t06-finalise/docs/specs/2026-09-20-anki-miner-game-design.md`
(sections 5 `SessionManifest`, 6.3 and 6.4 for the callers, 9, 10.2, 10.3, 17 rows "Zero cues at
stop" and "Rename fails after retries", 18.1 row "`session/journal.py` + finalise") and the master
plan `/home/light/Projects/anki_miner_game/.worktrees/t06-finalise/docs/plans/2026-09-21-master-plan.md`
(sections 1-4 and the card `### T06 journal, manifest I/O, finalise`).

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

- Worktree `/home/light/Projects/anki_miner_game/.worktrees/t06-finalise`, branch
  `feat/t06-finalise`, BASE `abf14a3d9950657019e70d18142c31d5147d4819` (main `a8c2072` +
  `feat/t02-cues-srt` + `feat/t04-naming` merged `--no-ff`). `.venv` is a symlink to
  `/home/light/Projects/anki_miner_game/.venv`.
- Read `/home/light/Projects/anki_miner_game/.worktrees/t06-finalise/CLAUDE.md` first. Never bare
  `python3`, never `uv run`, never `pip install -e`, no installs at all. Every Bash call starts with
  `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise &&`.
- Status file: `/home/light/Projects/anki_miner_game/.orchestration/status/t06-finalise.json`
  (keep `base_sha`).
- Files you own: `anki_miner_game/session/journal.py`, `anki_miner_game/session/manifest.py`,
  `anki_miner_game/session/finalise.py`, `tests/session/test_journal.py`,
  `tests/session/test_manifest.py`, `tests/session/test_finalise.py`. Nothing else changes. In
  particular `session/cues.py`, `session/srt_writer.py`, `session/naming.py` (T02, T04), `store.py`,
  `models/` and `interfaces/` are read-only for this task.
- Single-file test runs below pass `-n0 -p no:cacheprovider` to skip xdist start-up; Task 6 runs the
  real gate configuration.
- Imports inside `anki_miner_game/` are absolute (`from anki_miner_game.session.cues import build_cues`).
- Every piece of code in this plan was run on 2026-09-21 in a scratch copy of this worktree at
  BASE, applying the tasks in order: each "run it to fail" step failed as stated, each "run it to
  pass" step passed, and after Task 5 `scripts/health.sh` was all green (black, ruff, mypy, 451
  tests). Paste it as written.

## Decisions taken while planning

Answered from the card, the spec and the code; nobody else rules on these.

1. **Finalise returns a `FinaliseResult`; it does not queue the VAD job itself.** The fixed
   signature `finalise(manifest_path, cfg, *, sleep=time.sleep)` has no `VadJobs`. The result
   carries `queue_vad` (true only from the run that placed a session with a subtitle, and only when
   `cfg.vad.enabled`); the caller calls `VadJobs.queue(result.manifest_path)`. Whether the add-on
   is installed stays the add-on's call (contract ledger: `VadSettings.enabled` is effective only
   with the add-on). Banners stay with the caller too: `FinaliseResult.manifest.flags` has
   `no_cues`, `state` is `finalise_pending` after a lock that outlived the retries.
2. **`finalise_pending` marks "cues built, files not yet placed".** Phase 1 (`recording`) writes
   the subtitle into `_incoming/`, then the manifest as `finalise_pending` with `live_cues`, then
   deletes the journal. From then on the manifest alone holds what finalise needs. A rename that
   outlives its retries leaves exactly that state, which is the spec's `finalise_pending`; a crash
   anywhere after phase 1 leaves it too, and the launch scan of `_incoming/` finds it either way.
3. **The journal is deleted at the end of phase 1, not after the renames (spec 10.3 step 5).** In
   the spec's order a crash after the manifest has left `_incoming/` strands the journal in
   `_incoming/` and leaves a non-`ready` manifest in the game folder, and nothing ever scans the
   game folder to repair either. Deleting the journal once the manifest holds `live_cues` removes
   both holes. Recorded as a spec amendment (Task 6).
4. **Only the video is renamed; the subtitle and the manifest are written at their final names and
   removed from `_incoming/`.** Both are re-derivable from the manifest, so re-writing them is
   idempotent with no per-file "already moved?" branch, and a subtitle lost from `_incoming/` is
   rebuilt from `live_cues`. The `_incoming/` manifest is never `ready`: the `ready` manifest is
   written straight into the game folder, then the `_incoming/` one is deleted. So "state is `ready`
   or `vad_running`" means "already placed" and finalise returns at once. The video is the one file
   that must be moved, and the one OBS can hold (spec 10.3), so only its move is retried. Recorded
   as a spec amendment.
5. **While the video is still in `_incoming/`, its three final names must be free; a taken name
   bumps NN.** `os.replace` overwrites silently on both platforms, and a user file named
   `<Game> - NN.*` placed after `STARTED` would be destroyed, or a stray `.srt` would sit beside
   the session's video (spec 3.1 forbids both). The bumped NN is written to the `_incoming/`
   manifest before the video moves, so a crash cannot split the session across two numbers. Once
   the video has moved, NN is fixed.
6. **Retry only `PermissionError`, with `RENAME_BACKOFF_S = (0.1, 0.2, 0.5, 1.0, 2.0, 2.0, 2.0,
   2.0)` (9.8 s, inside the spec's 10 s).** Windows reports a file another process holds as a
   sharing violation, which Python raises as `PermissionError`; nothing else is worth waiting for.
   Any other `OSError`, a corrupt or missing manifest, and a video that is gone from both places
   raise `FinaliseError(manifest_path, message)`; the files stay as the last completed step left
   them, so a later run resumes. `FinaliseError` is the one exception finalise raises.
7. **Counts.** Finalise writes `accepted = len(cues)` and `skip = len(lines) - len(cues)` (T02's
   documented identity) and keeps every other counter as the manifest holds it (the actor's
   pipeline counts: `received`, `duplicate`, `no_letters`, `junk`, `paused`). This matches the spec
   5 example, where `received = accepted + duplicate + no_letters + junk + paused + skip`
   (1412 = 1260 + 96 + 31 + 4 + 0 + 21) and VAD `trimmed + no_speech` = 1260 = the cue count.
   Recorded as a spec amendment so T15 counts the same way.
8. **`stopped_at`, when the actor left it `null`, is the video's modification time in UTC.** The
   journal holds offsets only; for an orphan finalised at the next launch the video's last write is
   the only true stop time. A value the actor wrote is kept.
9. **Stop offset without a `stop` record = the last record that carries an offset + the cap** (spec
   10.3 step 1 "the last record's offset"): `line`, `pause` and `resume` carry one, `replace` does
   not. With several `stop` records (never written by T15, but possible in a hand-edited file) the
   last wins.
10. **Journal records are module-local frozen dataclasses** (`LineRecord`, `ReplaceRecord`,
    `PauseRecord`, `ResumeRecord`, `StopRecord`) with a `TAG` class variable, not models: section 4
    names only `Journal(path)` and `read_journal(path)`, and nothing outside `session/` reads a
    journal. The reader validates types through `match` class patterns, so a hand-edited line with a
    string offset is skipped rather than crashing finalise. `timed_lines(records)` folds `replace`
    records into the line before them (spec 10.2: "new text for the previous line").
11. **Reopening a journal ends a torn last line first.** Reconcile resumes a journal after an app
    crash (spec 6.3); appending to a file whose last line was cut mid-write would glue the next
    record onto the fragment and lose it. `Journal(path)` writes one `\n` when the file does not end
    with one. The spec's "flushed after each write" is `flush()` per record; no `fsync` (an app crash
    is the failure named; the actor's loop should not wait on the disk per line).
12. **`reserve_index` counts what spec 10.2 lists and nothing more**: every `.mkv` (any case) and
    every `*.session.json` in the game folder, NN read from the `<title> - NN` name with T04's
    `parse_index`, plus every manifest in `_incoming/` whose `game.slug` is the given slug, NN read
    from its `index`. An unreadable `_incoming/` manifest is skipped (decision 5 still guards the
    final names). Missing folders count as empty.
13. **Manifest I/O reuses `store`'s document reader and writer** (`store._read`, `store._write`):
    same UTF-8 BOM tolerance, same `CorruptFileError` / `FutureSchemaError` / `StoreWriteError`
    types the rest of the app already turns into banners. They are module-private in `store.py`,
    which this task does not own; importing them is the smaller cost than a second copy of the error
    mapping. A later tidy-up may make them public.
14. **Where the files are.** `incoming_files(incoming, output_path)` names the video (OBS's own file
    name, taken from `outputPath` split on `/` and `\` alike, so a Windows or Flatpak sandbox path
    works), and `<obs stem>.srt`, `<obs stem>.lines.jsonl`, `<obs stem>.session.json` beside it.
    `game_folder(incoming, title)` is `incoming.parent / sanitise_title(title)`: the game folder sits
    beside `_incoming/` on the same volume, so the video move is a rename (spec 10.3), and reservation
    and placement use one rule. The final video keeps the video's own extension (`.mkv` from the
    app's profile).

## Interfaces for dependants

Section 4 fixes `Journal(path)`, `read_journal(path)`, `load_manifest`, `write_manifest_atomic`,
`reserve_index(game_dir, incoming, slug)` and `finalise(manifest_path, cfg, *, sleep=time.sleep)`.
This task adds the names below. How T15 (actor), T16 (launch) and T23 (VAD jobs) use them:

```python
# anki_miner_game/session/journal.py
LineRecord(offset_ms: int, text: str, source: str)      # TAG "line"
ReplaceRecord(text: str)                                 # TAG "replace"
PauseRecord(offset_ms: int) / ResumeRecord(offset_ms: int) / StopRecord(offset_ms: int)
JournalRecord = LineRecord | ReplaceRecord | PauseRecord | ResumeRecord | StopRecord
class Journal:
    def __init__(self, path: Path) -> None          # opens for append (creates), ends a torn last line
    def append(self, record: JournalRecord) -> None  # one line, flushed
    def close(self) -> None
def read_journal(path: Path) -> list[JournalRecord]  # FileNotFoundError when missing
def timed_lines(records: Iterable[JournalRecord]) -> list[TimedLine]

# anki_miner_game/session/manifest.py
MANIFEST_SUFFIX = ".session.json"; JOURNAL_SUFFIX = ".lines.jsonl"; SUBTITLE_SUFFIX = ".srt"; VIDEO_SUFFIX = ".mkv"
@dataclass(frozen=True) class IncomingFiles: video, subtitle, journal, manifest: Path
def incoming_files(incoming: Path, output_path: str) -> IncomingFiles
def game_folder(incoming: Path, title: str) -> Path
def load_manifest(path: Path) -> SessionManifest     # FileNotFoundError | store.StoreError
def write_manifest_atomic(path: Path, manifest: SessionManifest) -> None   # store.StoreWriteError
def reserve_index(game_dir: Path, incoming: Path, slug: str) -> int

# anki_miner_game/session/finalise.py
RENAME_BACKOFF_S: Final = (0.1, 0.2, 0.5, 1.0, 2.0, 2.0, 2.0, 2.0)
class FinaliseError(Exception): manifest_path: Path
@dataclass(frozen=True) class FinaliseResult: manifest_path: Path; manifest: SessionManifest; queue_vad: bool
def finalise(manifest_path: Path, cfg: AppConfig, *, sleep: Callable[[float], None] = time.sleep) -> FinaliseResult
```

- **At `STARTED` (T15):** `incoming = paths.incoming_dir(cfg)`;
  `files = incoming_files(incoming, event["outputPath"])`;
  `index = reserve_index(game_folder(incoming, profile.title), incoming, profile.slug)`;
  `write_manifest_atomic(files.manifest, SessionManifest(state=ManifestState.RECORDING, index=index, ...))`;
  `journal = Journal(files.journal)`.
- **While recording (T15):** `journal.append(LineRecord(...))` per `Accepted`,
  `journal.append(ReplaceRecord(text))` per `Replaced`, `PauseRecord`/`ResumeRecord` on the pause
  edges. Resuming after an app restart (spec 6.3 row 4) is `Journal(files.journal)` again.
- **At the stop (T15):** `journal.append(StopRecord(stop_ms))`, then `journal.close()` (Windows
  cannot delete an open file), then write the pipeline counts, `flags` (`obs_exited`,
  `split_unsupported`, `clock_degraded`) and, if it wants, `stopped_at` into the manifest, then run
  `finalise(files.manifest, cfg)` **off the I/O loop** (`run_in_executor`: it blocks for up to about
  10 s while a video is locked), one call at a time per manifest.
- **With the result (T15/T16):** publish `SessionFinalised(result.manifest_path)`; banner when
  `Flag.NO_CUES in result.manifest.flags` or `result.manifest.state is ManifestState.FINALISE_PENDING`;
  `if result.queue_vad: vad_jobs.queue(result.manifest_path)`. `FinaliseError` -> banner with
  `str(exc)`.
- **At launch / reconcile (T15/T16):** finalise every `*.session.json` in `_incoming/` whose recording
  is not active (deciding that is T15's reconcile, not this task). A manifest that is already placed
  is returned unchanged with `queue_vad=False`, so a repeated call is harmless.
- **VAD (T23):** reads `live_cues` from the placed manifest; finalise never sets `vad`.

## File map

| File | Responsibility |
|---|---|
| `anki_miner_game/session/journal.py` (create) | Journal record types, the append-only writer, the tolerant reader, `timed_lines` |
| `anki_miner_game/session/manifest.py` (create) | `_incoming/` file names, the game folder rule, manifest read/write, NN reservation |
| `anki_miner_game/session/finalise.py` (create) | The finalise state machine, lock retry, `FinaliseResult` / `FinaliseError` |
| `tests/session/test_journal.py` (create) | Record bytes, flush per write, torn lines, resume, `timed_lines` |
| `tests/session/test_manifest.py` (create) | Round trip, load/write errors, `incoming_files`, `game_folder`, `reserve_index` |
| `tests/session/test_finalise.py` (create) | End state, stop offset, no cues, crash at every file operation, lock retry, NN conflict, errors |

`tests/session/__init__.py` already exists (T02).

## Tasks

### Task 1: the session journal

**Files:**
- Create: `anki_miner_game/session/journal.py`
- Test: `tests/session/test_journal.py`

**Interfaces:**
- Consumes: `anki_miner_game.models.lines.TimedLine(offset_ms: int, text: str, source_id: str)`.
- Produces: `LineRecord`, `ReplaceRecord`, `PauseRecord`, `ResumeRecord`, `StopRecord`,
  `JournalRecord`, `Journal(path)` with `append(record)` / `close()`,
  `read_journal(path) -> list[JournalRecord]`, `timed_lines(records) -> list[TimedLine]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/session/test_journal.py`:

```python
"""Session journal (spec 10.2; 18.1 row ``session/journal.py`` + finalise)."""

import pytest

from anki_miner_game.models.lines import TimedLine
from anki_miner_game.session.journal import (
    Journal,
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
    read_journal,
    timed_lines,
)

# One record of each type, and the lines spec 10.2 shows for them.
RECORDS = [
    LineRecord(offset_ms=5230, text="…", source="textractor"),
    ReplaceRecord(text="…"),
    PauseRecord(offset_ms=61200),
    ResumeRecord(offset_ms=61200),
    StopRecord(offset_ms=5248120),
]
SPEC_LINES = (
    '{"t":"line","offset_ms":5230,"text":"…","source":"textractor"}\n'
    '{"t":"replace","text":"…"}\n'
    '{"t":"pause","offset_ms":61200}\n'
    '{"t":"resume","offset_ms":61200}\n'
    '{"t":"stop","offset_ms":5248120}\n'
)


def _write(path, records):
    journal = Journal(path)
    for record in records:
        journal.append(record)
    journal.close()


def test_each_record_type_is_one_json_line_as_the_spec_shows(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS)
    assert path.read_bytes() == SPEC_LINES.encode("utf-8")  # UTF-8 kept, "\n" even on Windows


def test_records_read_back_in_order(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS)
    assert read_journal(path) == RECORDS


def test_every_record_is_on_disk_as_soon_as_it_is_appended(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    journal = Journal(path)
    for count, record in enumerate(RECORDS, start=1):
        journal.append(record)
        assert read_journal(path) == RECORDS[:count]  # read through another handle, before close
    journal.close()


@pytest.mark.parametrize(
    "fragment",
    [
        b'{"t":"line","offset_ms":9',
        '{"t":"line","offset_ms":9,"text":"こ'.encode()[:-1],  # cut inside a UTF-8 sequence
        b'{"t":"sto',
    ],
)
def test_a_torn_last_line_is_skipped(tmp_path, fragment):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:2])
    with path.open("ab") as fh:
        fh.write(fragment)
    assert read_journal(path) == RECORDS[:2]


def test_a_journal_resumed_after_a_torn_line_keeps_every_new_record(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:1])
    with path.open("ab") as fh:
        fh.write(b'{"t":"line","offset_ms":7')
    _write(path, [PauseRecord(offset_ms=8000)])
    assert read_journal(path) == [RECORDS[0], PauseRecord(offset_ms=8000)]
    assert path.read_bytes().endswith(b'{"t":"pause","offset_ms":8000}\n')


def test_a_resumed_journal_appends_after_the_existing_records(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:2])
    _write(path, RECORDS[2:])
    assert path.read_bytes() == SPEC_LINES.encode("utf-8")


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"[1, 2]",
        b'"a sentence"',
        b'{"t":"shout","text":"a"}',
        b'{"offset_ms":5}',
        b'{"t":"line","offset_ms":"5","text":"a","source":"s"}',
        b'{"t":"line","offset_ms":5,"text":"a"}',
        b'{"t":"replace"}',
        b"\xff\xfe",
    ],
)
def test_a_line_that_is_not_a_record_is_skipped(tmp_path, line):
    path = tmp_path / "s.lines.jsonl"
    path.write_bytes(line + b"\n" + SPEC_LINES.encode("utf-8"))
    assert read_journal(path) == RECORDS


def test_a_replace_rewrites_the_line_before_it_and_keeps_its_offset():
    records = [
        LineRecord(offset_ms=1000, text="え", source="agent"),
        PauseRecord(offset_ms=1500),
        ReplaceRecord(text="えっと…"),
        ResumeRecord(offset_ms=1500),
        LineRecord(offset_ms=4000, text="岡部", source="agent"),
        StopRecord(offset_ms=9000),
    ]
    assert timed_lines(records) == [
        TimedLine(offset_ms=1000, text="えっと…", source_id="agent"),
        TimedLine(offset_ms=4000, text="岡部", source_id="agent"),
    ]


def test_a_replace_with_no_line_before_it_is_ignored():
    records = [ReplaceRecord(text="x"), LineRecord(offset_ms=10, text="y", source="luna")]
    assert timed_lines(records) == [TimedLine(offset_ms=10, text="y", source_id="luna")]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_journal.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ModuleNotFoundError: No module named 'anki_miner_game.session.journal'`.

- [ ] **Step 3: Write the journal**

Create `anki_miner_game/session/journal.py`:

```python
"""The session journal (spec 10.2): ``<obs stem>.lines.jsonl`` in ``_incoming/``, one JSON object per line.

Append-only and flushed after every record, so an accepted line survives an app crash the moment it
arrives. Cue ends are never journalled; finalise rebuilds them from the lines (spec 9, 10.3).
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import ClassVar

from anki_miner_game.models.lines import TimedLine


@dataclass(frozen=True)
class LineRecord:
    """An accepted line at its record-clock offset."""

    TAG: ClassVar[str] = "line"
    offset_ms: int
    text: str
    source: str


@dataclass(frozen=True)
class ReplaceRecord:
    """Typewriter merge (spec 8.2 step 9): new text for the line before it, which keeps its offset."""

    TAG: ClassVar[str] = "replace"
    text: str


@dataclass(frozen=True)
class PauseRecord:
    TAG: ClassVar[str] = "pause"
    offset_ms: int


@dataclass(frozen=True)
class ResumeRecord:
    TAG: ClassVar[str] = "resume"
    offset_ms: int


@dataclass(frozen=True)
class StopRecord:
    TAG: ClassVar[str] = "stop"
    offset_ms: int


JournalRecord = LineRecord | ReplaceRecord | PauseRecord | ResumeRecord | StopRecord


class Journal:
    """Appends records to one journal file. Used from the session actor's thread only.

    Opening a journal that already exists (the app restarted mid-session, spec 6.3) first ends a torn
    last line, so the next record starts on a line of its own. Close it before finalise, which deletes
    the file (Windows refuses to delete an open file).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        torn = _has_torn_tail(path)
        self._fh = path.open("a", encoding="utf-8", newline="\n")
        if torn:
            self._fh.write("\n")
            self._fh.flush()

    def append(self, record: JournalRecord) -> None:
        """Write one record as one line and flush it to the operating system."""
        self._fh.write(json.dumps({"t": record.TAG, **asdict(record)}, ensure_ascii=False, separators=(",", ":")))
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _has_torn_tail(path: Path) -> bool:
    """Whether ``path`` exists, is not empty, and does not end with a newline."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size == 0:
        return False
    with path.open("rb") as fh:
        fh.seek(size - 1)
        return fh.read(1) != b"\n"


def read_journal(path: Path) -> list[JournalRecord]:
    """Every record in ``path``, in file order; ``FileNotFoundError`` when there is no journal.

    A line that is not a valid record is skipped: a crash can leave the last line torn, and a journal
    resumed after that crash keeps the fragment on a line of its own.
    """
    records: list[JournalRecord] = []
    for raw in path.read_bytes().split(b"\n"):
        record = _decode(raw)
        if record is not None:
            records.append(record)
    return records


def _decode(raw: bytes) -> JournalRecord | None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError: a write cut short
        return None
    match obj:
        case {"t": "line", "offset_ms": int(offset), "text": str(text), "source": str(source)}:
            return LineRecord(offset_ms=offset, text=text, source=source)
        case {"t": "replace", "text": str(text)}:
            return ReplaceRecord(text=text)
        case {"t": "pause", "offset_ms": int(offset)}:
            return PauseRecord(offset_ms=offset)
        case {"t": "resume", "offset_ms": int(offset)}:
            return ResumeRecord(offset_ms=offset)
        case {"t": "stop", "offset_ms": int(offset)}:
            return StopRecord(offset_ms=offset)
    return None


def timed_lines(records: Iterable[JournalRecord]) -> list[TimedLine]:
    """The journalled lines in order, each with its latest text: the input of ``build_cues``.

    A replace record rewrites the line before it, which keeps its offset and source; one with no line
    before it is ignored. Pause, resume and stop records carry no line.
    """
    lines: list[TimedLine] = []
    for record in records:
        if isinstance(record, LineRecord):
            lines.append(TimedLine(offset_ms=record.offset_ms, text=record.text, source_id=record.source))
        elif isinstance(record, ReplaceRecord) and lines:
            lines[-1] = replace(lines[-1], text=record.text)
    return lines
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_journal.py -n0 -p no:cacheprovider -q`
Expected: `19 passed`.

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/black --check anki_miner_game/session tests/session && .venv/bin/ruff check anki_miner_game/session tests/session && .venv/bin/mypy anki_miner_game`
Expected: black unchanged, `All checks passed!`, `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git add anki_miner_game/session/journal.py tests/session/test_journal.py && git commit -m "feat(session): add the append-only session journal"
```

### Task 2: manifest files and NN reservation

**Files:**
- Create: `anki_miner_game/session/manifest.py`
- Test: `tests/session/test_manifest.py`

**Interfaces:**
- Consumes: `models.manifest.SessionManifest`, `models.manifest.to_json`;
  `session.naming.parse_index(stem) -> int | None`, `session.naming.sanitise_title(title) -> str`
  (T04); `store._read(cls, path)` (raises `FileNotFoundError` or a `StoreError`),
  `store._write(path, text)` (raises `StoreWriteError`), `store.StoreError`.
- Produces: `MANIFEST_SUFFIX`, `JOURNAL_SUFFIX`, `SUBTITLE_SUFFIX`, `VIDEO_SUFFIX`, `IncomingFiles`,
  `incoming_files(incoming: Path, output_path: str) -> IncomingFiles`,
  `game_folder(incoming: Path, title: str) -> Path`, `load_manifest(path: Path) -> SessionManifest`,
  `write_manifest_atomic(path: Path, manifest: SessionManifest) -> None`,
  `reserve_index(game_dir: Path, incoming: Path, slug: str) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/session/test_manifest.py`:

```python
"""Session manifest files (spec 5, 10.2; 18.1 row ``session/journal.py`` + finalise: NN reservation)."""

from dataclasses import replace
from pathlib import Path

import pytest

from anki_miner_game.models.manifest import GameRef, ManifestState, ObsRecord, SessionManifest
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session.manifest import (
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    reserve_index,
    write_manifest_atomic,
)
from anki_miner_game.store import CorruptFileError, FutureSchemaError, StoreWriteError

TITLE = "Steins;Gate"
MANIFEST = SessionManifest(
    app_version="0.1.0",
    game=GameRef(slug="steins-gate", title=TITLE),
    index=3,
    state=ManifestState.RECORDING,
    started_at="2026-10-02T18:04:11Z",
    obs=ObsRecord(
        version="31.0.2",
        websocket="5.5.4",
        profile="Anki Miner Game",
        collection="Anki Miner Game",
        output_path="/v/_incoming/2026-10-02 18-04-11.mkv",
    ),
    text_mode=TextMode.HOOK,
)


def test_a_manifest_round_trips_through_its_file(tmp_path):
    path = tmp_path / "2026-10-02 18-04-11.session.json"
    write_manifest_atomic(path, MANIFEST)
    assert load_manifest(path) == MANIFEST
    assert path.read_text(encoding="utf-8").startswith('{\n  "schema": 1,\n')
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_a_missing_manifest_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "x.session.json")


@pytest.mark.parametrize(
    ("text", "error"),
    [("{not json", CorruptFileError), ('{"schema": 1}', CorruptFileError), ('{"schema": 2}', FutureSchemaError)],
)
def test_an_unusable_manifest_is_a_store_error(tmp_path, text, error):
    path = tmp_path / "x.session.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(error):
        load_manifest(path)


def test_a_manifest_that_cannot_be_written_is_a_store_write_error(tmp_path):
    (tmp_path / "blocker").write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(StoreWriteError):
        write_manifest_atomic(tmp_path / "blocker" / "x.session.json", MANIFEST)


@pytest.mark.parametrize(
    "output_path",
    [
        "/home/u/Videos/Anki Miner Game/_incoming/2026-10-02 18-04-11.mkv",
        "C:\\Users\\u\\Videos\\Anki Miner Game\\_incoming\\2026-10-02 18-04-11.mkv",
        "2026-10-02 18-04-11.mkv",
    ],
)
def test_incoming_files_are_named_after_the_obs_file(tmp_path, output_path):
    assert incoming_files(tmp_path, output_path) == IncomingFiles(
        video=tmp_path / "2026-10-02 18-04-11.mkv",
        subtitle=tmp_path / "2026-10-02 18-04-11.srt",
        journal=tmp_path / "2026-10-02 18-04-11.lines.jsonl",
        manifest=tmp_path / "2026-10-02 18-04-11.session.json",
    )


def test_the_game_folder_is_the_sanitised_title_beside_incoming(tmp_path):
    assert game_folder(tmp_path / "_incoming", "Fate/stay night") == tmp_path / "Fate stay night"


def _touch(folder: Path, names: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"")


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], 1),
        (["Steins;Gate - 01.mkv", "Steins;Gate - 03.mkv"], 4),  # files only
        (["Steins;Gate - 02.session.json"], 3),  # manifests only (the user deleted the video)
        (["Steins;Gate - 02.mkv", "Steins;Gate - 02.srt", "Steins;Gate - 05.session.json"], 6),  # both
        (["Steins;Gate - 08.MKV"], 9),
        (["Steins;Gate - 07.srt", "Steins;Gate - 12.mp4", "notes - 40.txt", "Steins;Gate.mkv", "cover 2.mkv"], 1),
    ],
)
def test_nn_is_one_more_than_the_highest_in_the_game_folder(tmp_path, names, expected):
    _touch(tmp_path / TITLE, names)
    assert reserve_index(tmp_path / TITLE, tmp_path / "_incoming", "steins-gate") == expected


def test_nn_counts_manifests_of_the_same_game_in_incoming(tmp_path):
    _touch(tmp_path / TITLE, ["Steins;Gate - 03.mkv"])
    incoming = tmp_path / "_incoming"
    write_manifest_atomic(incoming / "a.session.json", replace(MANIFEST, index=7))
    write_manifest_atomic(incoming / "b.session.json", replace(MANIFEST, index=5))
    other = replace(MANIFEST, game=GameRef(slug="chaos-head", title="Chaos;Head"), index=12)
    write_manifest_atomic(incoming / "c.session.json", other)
    (incoming / "d.session.json").write_text("{torn", encoding="utf-8")
    _touch(incoming, ["2026-10-02 18-04-11.mkv", "2026-10-02 18-04-11.lines.jsonl"])
    assert reserve_index(tmp_path / TITLE, incoming, "steins-gate") == 8
```

The parametrised `reserve_index` cases have no `_incoming/` folder at all (`_touch` creates only
the game folder), so a missing folder is covered; the first case has an empty game folder.

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_manifest.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ModuleNotFoundError: No module named 'anki_miner_game.session.manifest'`.

- [ ] **Step 3: Write the module**

Create `anki_miner_game/session/manifest.py`:

```python
"""Session files on disk (spec 5, 10): names in ``_incoming/``, the game folder, manifest I/O, and NN.

While recording, a session's files sit in ``_incoming/`` named after OBS's file stem (spec 10.2);
finalise moves them to ``<output root>/<sanitised title>/<title> - NN.*`` (spec 10.3).
"""

from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Final

from anki_miner_game.models.manifest import SessionManifest, to_json
from anki_miner_game.session.naming import parse_index, sanitise_title
from anki_miner_game.store import StoreError, _read, _write

MANIFEST_SUFFIX: Final = ".session.json"
JOURNAL_SUFFIX: Final = ".lines.jsonl"
SUBTITLE_SUFFIX: Final = ".srt"
VIDEO_SUFFIX: Final = ".mkv"
"""The container the app's OBS profile records to (spec 10.3: ``.mkv`` survives an OBS crash)."""


@dataclass(frozen=True)
class IncomingFiles:
    """One session's files in ``_incoming/``, all named after OBS's file stem (spec 10.2)."""

    video: Path
    subtitle: Path
    journal: Path
    manifest: Path


def incoming_files(incoming: Path, output_path: str) -> IncomingFiles:
    """The files of the recording OBS reported as ``output_path`` (``RecordStateChanged.outputPath``).

    Only the file name of ``output_path`` is used, split on ``/`` and ``\\`` alike, so a Windows path
    or a Flatpak sandbox path names the same files inside ``incoming``.
    """
    video = PureWindowsPath(output_path).name  # PureWindowsPath treats both separators as separators
    stem = PurePath(video).stem
    return IncomingFiles(
        video=incoming / video,
        subtitle=incoming / f"{stem}{SUBTITLE_SUFFIX}",
        journal=incoming / f"{stem}{JOURNAL_SUFFIX}",
        manifest=incoming / f"{stem}{MANIFEST_SUFFIX}",
    )


def game_folder(incoming: Path, title: str) -> Path:
    """``<output root>/<sanitised title>``: the output root is the folder that holds ``incoming``."""
    return incoming.parent / sanitise_title(title)


def load_manifest(path: Path) -> SessionManifest:
    """Read a manifest.

    Raises ``FileNotFoundError`` when there is none, ``store.CorruptFileError`` or
    ``store.FutureSchemaError`` when it cannot be used, and ``store.StoreError`` when it cannot be read.
    """
    return _read(SessionManifest, path)


def write_manifest_atomic(path: Path, manifest: SessionManifest) -> None:
    """Write through a temporary file and ``os.replace``; ``store.StoreWriteError`` leaves the old file."""
    _write(path, to_json(manifest))


def reserve_index(game_dir: Path, incoming: Path, slug: str) -> int:
    """NN for a session that starts now (spec 10.2): 1 + the highest index already taken, else 1.

    Taken: every ``.mkv`` and every manifest in ``game_dir`` (NN read from the ``<title> - NN`` name),
    and every manifest in ``incoming`` whose game is ``slug`` (NN read from the manifest). A manifest
    in ``incoming`` that cannot be read is skipped. Either folder may be missing.
    """
    taken = [0]
    for path in _entries(game_dir):
        if path.name.endswith(MANIFEST_SUFFIX):
            index = parse_index(path.name.removesuffix(MANIFEST_SUFFIX))
        elif path.suffix.lower() == VIDEO_SUFFIX:
            index = parse_index(path.stem)
        else:
            continue
        if index is not None:
            taken.append(index)
    for path in _entries(incoming):
        if not path.name.endswith(MANIFEST_SUFFIX):
            continue
        try:
            manifest = load_manifest(path)
        except (FileNotFoundError, StoreError):  # gone meanwhile (finalised), or unreadable
            continue
        if manifest.game.slug == slug:
            taken.append(manifest.index)
    return max(taken) + 1


def _entries(folder: Path) -> list[Path]:
    try:
        return list(folder.iterdir())
    except FileNotFoundError:
        return []
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_manifest.py -n0 -p no:cacheprovider -q`
Expected: `17 passed`.

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/black --check anki_miner_game/session tests/session && .venv/bin/ruff check anki_miner_game/session tests/session && .venv/bin/mypy anki_miner_game`
Expected: black unchanged, `All checks passed!`, `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git add anki_miner_game/session/manifest.py tests/session/test_manifest.py && git commit -m "feat(session): add manifest I/O and session number reservation"
```

### Task 3: finalise, crash-safe

**Files:**
- Create: `anki_miner_game/session/finalise.py`
- Test: `tests/session/test_finalise.py`

**Interfaces:**
- Consumes: Task 1 (`read_journal`, `timed_lines`, `JournalRecord`, `ReplaceRecord`,
  `StopRecord`, `Journal` in tests), Task 2 (`IncomingFiles`, `incoming_files`, `game_folder`,
  `load_manifest`, `write_manifest_atomic`, `MANIFEST_SUFFIX`, `SUBTITLE_SUFFIX`);
  `session.cues.build_cues(lines, stop_ms, shift_ms, cfg: CueSettings) -> list[Cue]` and
  `session.srt_writer.write_srt_atomic(path, cues)` (T02); `session.naming.session_stem(title, index)`
  (T04); `models.constants.START_SHIFT_MS`; `models.manifest.LiveCue.from_cue` / `.to_cue`;
  `store.StoreError`.
- Produces: `FinaliseError(manifest_path, message)` with `.manifest_path`,
  `FinaliseResult(manifest_path, manifest, queue_vad)`,
  `finalise(manifest_path, cfg, *, sleep=time.sleep) -> FinaliseResult`. `sleep` is unused until
  Task 4 wires the lock retry; the signature is section 4's and stays fixed.

The file operations finalise performs, in order, are the "step boundaries" the crash test cuts at.
With cues: (1) `<obs stem>.srt` written in `_incoming/`, (2) manifest written `finalise_pending`,
(3) journal deleted, (4) video moved, (5) final `.srt` written, (6) `_incoming/` `.srt` deleted,
(7) final manifest written `ready`, (8) `_incoming/` manifest deleted. Without cues: (2), (3), (4),
(6) as a no-op delete, (7), (8): six. The test counts `os.replace` and `os.unlink` calls (every
atomic write ends in an `os.replace`; `Path.unlink` calls `os.unlink`), raises a `BaseException` in
place of call *k*, runs finalise again, and compares every byte under the output root with an
uninterrupted run.

- [ ] **Step 1: Write the failing tests**

Create `tests/session/test_finalise.py`:

```python
"""Finalise (spec 10.3; 18.1 row ``session/journal.py`` + finalise; 17 rows zero cues and rename)."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from anki_miner_game.models.config import AppConfig, VadSettings
from anki_miner_game.models.manifest import (
    Counts,
    FilesRecord,
    Flag,
    GameRef,
    LiveCue,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session.finalise import FinaliseError, finalise
from anki_miner_game.session.journal import (
    Journal,
    JournalRecord,
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
)
from anki_miner_game.session.manifest import incoming_files, load_manifest, write_manifest_atomic

CFG = AppConfig()
TITLE = "Steins;Gate"
OBS_STEM = "2026-10-02 18-04-11"
VIDEO_BYTES = b"\x1a\x45\xdf\xa3 not really matroska"
VIDEO_MTIME = 1_790_969_500
"""2026-10-02T19:31:40Z: when OBS last wrote the video."""

RECORDED = SessionManifest(
    app_version="0.1.0",
    game=GameRef(slug="steins-gate", title=TITLE),
    index=3,
    state=ManifestState.RECORDING,
    started_at="2026-10-02T18:04:11Z",
    obs=ObsRecord(
        version="31.0.2",
        websocket="5.5.4",
        profile="Anki Miner Game",
        collection="Anki Miner Game",
        output_path=f"C:\\Users\\u\\Videos\\Anki Miner Game\\_incoming\\{OBS_STEM}.mkv",
    ),
    text_mode=TextMode.HOOK,
    sources_used=("textractor",),
    counts=Counts(received=8, duplicate=2, no_letters=1),
)
"""The manifest as the actor left it; ``output_path`` is OBS's own string, only its name is used."""

RECORDS: list[JournalRecord] = [
    LineRecord(offset_ms=5_230, text="「こんにちは」", source="textractor"),
    LineRecord(offset_ms=9_760, text="え", source="textractor"),
    ReplaceRecord(text="えっと、岡部？"),
    PauseRecord(offset_ms=12_000),
    ResumeRecord(offset_ms=12_000),
    LineRecord(offset_ms=14_000, text="クリック", source="textractor"),  # shown for 0.1 s: skipped
    LineRecord(offset_ms=14_100, text="まゆしぃ", source="textractor"),
    StopRecord(offset_ms=30_000),
]
LIVE_CUES = (
    LiveCue(i=1, start_ms=5_230, end_ms=9_410, text="「こんにちは」", source="textractor"),
    LiveCue(i=2, start_ms=9_760, end_ms=13_750, text="えっと、岡部？", source="textractor"),
    LiveCue(i=3, start_ms=14_100, end_ms=29_100, text="まゆしぃ", source="textractor"),
)
SRT = (
    "1\n00:00:05,230 --> 00:00:09,410\n「こんにちは」\n\n"
    "2\n00:00:09,760 --> 00:00:13,750\nえっと、岡部？\n\n"
    "3\n00:00:14,100 --> 00:00:29,100\nまゆしぃ\n"
)
NO_CUE_RECORDS: list[JournalRecord] = [
    LineRecord(offset_ms=1_000, text="あ", source="agent"),
    LineRecord(offset_ms=1_100, text="い", source="agent"),
    StopRecord(offset_ms=1_200),
]


def _session(root: Path, records: list[JournalRecord] | None, **changes) -> Path:
    """A stopped session in ``root/_incoming`` as the actor leaves it; returns the manifest path.

    ``records=None``: the journal was never created.
    """
    manifest = replace(RECORDED, **changes)
    files = incoming_files(root / "_incoming", manifest.obs.output_path)
    files.video.parent.mkdir(parents=True)
    files.video.write_bytes(VIDEO_BYTES)
    os.utime(files.video, (VIDEO_MTIME, VIDEO_MTIME))
    write_manifest_atomic(files.manifest, manifest)
    if records is not None:
        journal = Journal(files.journal)
        for record in records:
            journal.append(record)
        journal.close()
    return files.manifest


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_a_stopped_session_lands_in_its_game_folder(tmp_path):
    result = finalise(_session(tmp_path, RECORDS), CFG)

    placed = tmp_path / TITLE / "Steins;Gate - 03.session.json"
    assert _tree(tmp_path) == {
        "Steins;Gate/Steins;Gate - 03.mkv": VIDEO_BYTES,
        "Steins;Gate/Steins;Gate - 03.srt": SRT.encode("utf-8"),
        "Steins;Gate/Steins;Gate - 03.session.json": placed.read_bytes(),
    }
    assert result.manifest_path == placed
    assert result.manifest == load_manifest(placed)
    assert result.manifest == replace(
        RECORDED,
        state=ManifestState.READY,
        stopped_at="2026-10-02T19:31:40Z",
        counts=Counts(received=8, accepted=3, duplicate=2, no_letters=1, skip=1),
        live_cues=LIVE_CUES,
        files=FilesRecord(video="Steins;Gate - 03.mkv", subtitle="Steins;Gate - 03.srt"),
    )
    assert result.queue_vad


def test_the_vad_pass_is_not_queued_when_it_is_off(tmp_path):
    result = finalise(_session(tmp_path, RECORDS), replace(CFG, vad=VadSettings(enabled=False)))
    assert result.manifest.state is ManifestState.READY
    assert not result.queue_vad


def test_an_ocr_session_starts_its_cues_one_second_early(tmp_path):
    result = finalise(_session(tmp_path, RECORDS, text_mode=TextMode.OCR), CFG)
    assert [cue.start_ms for cue in result.manifest.live_cues] == [4_230, 8_760, 13_100]


@pytest.mark.parametrize(
    ("records", "last_end_ms"),
    [
        # stop = 5000 + 15 s cap = 20000; the last cue ends end_gap_ms before it
        ([LineRecord(1_000, "あ", "agent"), LineRecord(5_000, "い", "agent")], 19_650),
        # the pause is the last record: stop = 8000 + 15 s = 23000; the cue is capped at 15 s
        ([LineRecord(1_000, "あ", "agent"), LineRecord(5_000, "い", "agent"), PauseRecord(8_000)], 20_000),
    ],
)
def test_without_a_stop_record_the_stop_is_the_last_offset_plus_the_cap(tmp_path, records, last_end_ms):
    result = finalise(_session(tmp_path, records), CFG)
    assert result.manifest.live_cues[-1].end_ms == last_end_ms


@pytest.mark.parametrize(("records", "skip"), [(NO_CUE_RECORDS, 2), ([], 0), (None, 0)])
def test_a_session_without_cues_keeps_the_video_and_writes_no_subtitle(tmp_path, records, skip):
    result = finalise(_session(tmp_path, records), CFG)

    assert sorted(_tree(tmp_path)) == ["Steins;Gate/Steins;Gate - 03.mkv", "Steins;Gate/Steins;Gate - 03.session.json"]
    assert result.manifest.flags == (Flag.NO_CUES,)
    assert result.manifest.files == FilesRecord(video="Steins;Gate - 03.mkv", subtitle=None)
    assert result.manifest.live_cues == ()
    assert (result.manifest.counts.accepted, result.manifest.counts.skip) == (0, skip)
    assert not result.queue_vad


def test_what_the_actor_recorded_is_kept(tmp_path):
    path = _session(tmp_path, NO_CUE_RECORDS, flags=(Flag.OBS_EXITED,), stopped_at="2026-10-02T19:00:00Z")
    result = finalise(path, CFG)
    assert result.manifest.flags == (Flag.OBS_EXITED, Flag.NO_CUES)
    assert result.manifest.stopped_at == "2026-10-02T19:00:00Z"


def test_a_placed_session_is_left_alone(tmp_path):
    first = finalise(_session(tmp_path, RECORDS), CFG)
    before = _tree(tmp_path)
    again = finalise(first.manifest_path, CFG)
    assert (again.manifest_path, again.manifest, again.queue_vad) == (first.manifest_path, first.manifest, False)
    assert _tree(tmp_path) == before


def test_a_pending_session_whose_subtitle_was_lost_gets_it_back_from_live_cues(tmp_path):
    path = _session(tmp_path, None, state=ManifestState.FINALISE_PENDING, live_cues=LIVE_CUES)
    finalise(path, CFG)
    assert (tmp_path / TITLE / "Steins;Gate - 03.srt").read_text(encoding="utf-8") == SRT


def test_finalise_reports_what_stops_it(tmp_path):
    missing = tmp_path / "_incoming" / "gone.session.json"
    with pytest.raises(FinaliseError) as caught:
        finalise(missing, CFG)
    assert caught.value.manifest_path == missing

    corrupt = _session(tmp_path / "corrupt", RECORDS)
    corrupt.write_text("{torn", encoding="utf-8")
    with pytest.raises(FinaliseError):
        finalise(corrupt, CFG)


@pytest.mark.parametrize(
    ("records", "changes"),
    [(RECORDS, {}), (None, {"state": ManifestState.FINALISE_PENDING, "live_cues": LIVE_CUES})],
    ids=["recording", "pending"],
)
def test_a_session_whose_video_is_gone_is_reported_and_left_as_it_was(tmp_path, records, changes):
    path = _session(tmp_path, records, **changes)
    (tmp_path / "_incoming" / f"{OBS_STEM}.mkv").unlink()
    before = _tree(tmp_path)
    with pytest.raises(FinaliseError, match=OBS_STEM):
        finalise(path, CFG)
    assert _tree(tmp_path) == before


class _Crash(BaseException):
    """The process dies here: a BaseException, so nothing in finalise can catch it."""


class _FileOps:
    """Counts ``os.replace`` and ``os.unlink`` calls, the points where finalise changes a file, and can
    raise ``_Crash`` in place of call number ``crash_at`` (0-based, counted from ``count = 0``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        self.crash_at: int | None = None
        for name in ("replace", "unlink"):
            monkeypatch.setattr(os, name, self._counted(getattr(os, name)))

    def _counted(self, real):
        def op(*args, **kwargs):
            if self.count == self.crash_at:
                self.crash_at = None  # one crash; the dying write still removes its temporary file
                raise _Crash
            self.count += 1
            return real(*args, **kwargs)

        return op


@pytest.mark.parametrize(("records", "steps"), [(RECORDS, 8), (NO_CUE_RECORDS, 6)], ids=["cues", "no-cues"])
def test_a_crash_between_any_two_steps_is_repaired_by_running_again(tmp_path, monkeypatch, records, steps):
    ops = _FileOps(monkeypatch)
    reference = _session(tmp_path / "reference", records)
    ops.count = 0
    finalise(reference, CFG)
    assert ops.count == steps
    expected = _tree(tmp_path / "reference")

    for crash_at in range(steps):
        root = tmp_path / f"crash-{crash_at}"
        path = _session(root, records)
        ops.count, ops.crash_at = 0, crash_at
        with pytest.raises(_Crash):
            finalise(path, CFG)
        result = finalise(path, CFG)
        assert _tree(root) == expected, f"crash before file operation {crash_at}"
        assert result.manifest.state is ManifestState.READY
```

Why the fixture values: `RECORDS` give starts 5230, 9760, 14000, 14100 and a stop at 30000. The
line at 14000 is followed 100 ms later, below `SKIP_MS`, so it is dropped (`skip = 1`); the typewriter
`replace` rewrites the line at 9760. Ends by spec 9 with `end_gap_ms` 350 and the 15 s cap:
9760 - 350 = 9410, 14100 - 350 = 13750, min(14100 + 15000, 30000 - 350) = 29100. In OCR mode every
start moves 1000 ms earlier and the same line is dropped. `output_path` is a Windows path on purpose:
only its file name is used, whatever the host.

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_finalise.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ModuleNotFoundError: No module named 'anki_miner_game.session.finalise'`.

- [ ] **Step 3: Write finalise**

Create `anki_miner_game/session/finalise.py`:

```python
"""Finalise (spec 10.3): a stopped session in ``_incoming/`` becomes ``<Game>/<Game> - NN.*``.

One routine for a normal stop, for reconcile and at launch. It moves the manifest's ``state``
forward in two phases, and each step first checks whether it has already happened, so a crash
between any two file operations is repaired by running it again:

- ``recording``: read the journal, build the cues, write ``<obs stem>.srt`` and the manifest
  (``live_cues``, counts, ``no_cues``, ``stopped_at``) as ``finalise_pending``; delete the journal.
  From here on the manifest alone holds what the journal held.
- ``finalise_pending``: move the video into the game folder, write the subtitle and the manifest
  there (``ready``, ``files``) from the manifest, delete them from ``_incoming/``.

A manifest in ``_incoming/`` is never ``ready``, so a ``ready`` one has already been placed.
"""

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import START_SHIFT_MS
from anki_miner_game.models.manifest import FilesRecord, Flag, LiveCue, ManifestState, SessionManifest
from anki_miner_game.session.cues import build_cues
from anki_miner_game.session.journal import JournalRecord, ReplaceRecord, StopRecord, read_journal, timed_lines
from anki_miner_game.session.manifest import (
    MANIFEST_SUFFIX,
    SUBTITLE_SUFFIX,
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    write_manifest_atomic,
)
from anki_miner_game.session.naming import session_stem
from anki_miner_game.session.srt_writer import write_srt_atomic
from anki_miner_game.store import StoreError


class FinaliseError(Exception):
    """Finalise cannot go on: the manifest is missing or unusable, a file cannot be written, or the
    video is gone. The files stay as the last completed step left them; running it again resumes."""

    def __init__(self, manifest_path: Path, message: str) -> None:
        super().__init__(message)
        self.manifest_path = manifest_path


@dataclass(frozen=True)
class FinaliseResult:
    manifest_path: Path
    """Where the manifest is now: the game folder when ``ready``, ``_incoming/`` when ``finalise_pending``."""
    manifest: SessionManifest
    queue_vad: bool
    """Queue the VAD pass (``VadJobs.queue(manifest_path)``): true only from the run that placed a
    session with a subtitle, and only when ``cfg.vad.enabled``."""


def finalise(manifest_path: Path, cfg: AppConfig, *, sleep: Callable[[float], None] = time.sleep) -> FinaliseResult:
    """Finalise the session whose manifest is ``manifest_path``; see the module docstring.

    Blocking file I/O: call it off the I/O loop. Anything that stops it raises ``FinaliseError``.

    Counts: ``accepted`` becomes the number of cues and ``skip`` the journalled lines the skip rule
    dropped; the other counters are kept as the manifest holds them (the actor's pipeline counts).
    """
    try:
        manifest = load_manifest(manifest_path)
        if manifest.state in (ManifestState.READY, ManifestState.VAD_RUNNING):
            return FinaliseResult(manifest_path, manifest, queue_vad=False)
        files = incoming_files(manifest_path.parent, manifest.obs.output_path)
        if manifest.state is ManifestState.RECORDING:
            manifest = _build(manifest_path, manifest, files, cfg)
        files.journal.unlink(missing_ok=True)
        return _place(manifest_path, manifest, files, cfg)
    except (StoreError, OSError) as exc:
        raise FinaliseError(manifest_path, str(exc)) from exc


def _build(manifest_path: Path, manifest: SessionManifest, files: IncomingFiles, cfg: AppConfig) -> SessionManifest:
    """Steps 1-3: journal -> cues -> ``<obs stem>.srt`` beside the video, manifest ``finalise_pending``."""
    stopped_at = manifest.stopped_at or _utc_stamp(files.video.stat().st_mtime)  # the video's last write
    try:
        records = read_journal(files.journal)
    except FileNotFoundError:  # stopped before the actor created the journal
        records = []
    lines = timed_lines(records)
    cues = build_cues(lines, _stop_ms(records, cfg.cue.max_cue_seconds), START_SHIFT_MS[manifest.text_mode], cfg.cue)
    if cues:
        write_srt_atomic(files.subtitle, cues)
    built = replace(
        manifest,
        state=ManifestState.FINALISE_PENDING,
        stopped_at=stopped_at,
        counts=replace(manifest.counts, accepted=len(cues), skip=len(lines) - len(cues)),
        flags=manifest.flags if cues else (*manifest.flags, Flag.NO_CUES),
        live_cues=tuple(LiveCue.from_cue(cue) for cue in cues),
    )
    write_manifest_atomic(manifest_path, built)
    return built


def _stop_ms(records: Sequence[JournalRecord], max_cue_seconds: int) -> int:
    """The stop record's offset; without one (a crash), the last offset in the journal + the cap."""
    stops = [record.offset_ms for record in records if isinstance(record, StopRecord)]
    if stops:
        return stops[-1]
    offsets = [record.offset_ms for record in records if not isinstance(record, ReplaceRecord)]
    return (offsets[-1] if offsets else 0) + max_cue_seconds * 1000


def _utc_stamp(timestamp: float) -> str:
    """``started_at``'s format: UTC, whole seconds, ``Z``."""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _place(manifest_path: Path, manifest: SessionManifest, files: IncomingFiles, cfg: AppConfig) -> FinaliseResult:
    """Steps 4-5: video, subtitle and manifest to ``<Game>/<Game> - NN.*``; ``_incoming/`` emptied."""
    folder = game_folder(manifest_path.parent, manifest.game.title)
    video, subtitle, placed = _targets(folder, manifest.game.title, manifest.index, files.video.suffix)
    if files.video.exists():
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(files.video, video)
    elif not video.exists():
        raise FinaliseError(manifest_path, f"the video {files.video} is gone")
    if manifest.live_cues:
        write_srt_atomic(subtitle, [cue.to_cue() for cue in manifest.live_cues])
    files.subtitle.unlink(missing_ok=True)
    ready = replace(
        manifest,
        state=ManifestState.READY,
        files=FilesRecord(video=video.name, subtitle=subtitle.name if manifest.live_cues else None),
    )
    write_manifest_atomic(placed, ready)
    manifest_path.unlink()
    return FinaliseResult(placed, ready, queue_vad=cfg.vad.enabled and bool(ready.live_cues))


def _targets(folder: Path, title: str, index: int, video_suffix: str) -> tuple[Path, Path, Path]:
    """The final video, subtitle and manifest paths for session ``index``."""
    stem = session_stem(title, index)
    return folder / f"{stem}{video_suffix}", folder / f"{stem}{SUBTITLE_SUFFIX}", folder / f"{stem}{MANIFEST_SUFFIX}"
```

Notes for the implementer:
- `stopped_at` is computed first, so a missing video raises before anything is written (the
  "video is gone" test compares the whole tree).
- `FinaliseError` raised inside the `try` is not caught by `except (StoreError, OSError)`; it is not
  a subclass of either.
- `files.video.suffix` is read from the name, so it is right even after the video has moved.

- [ ] **Step 4: Run them to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session -n0 -p no:cacheprovider -q`
Expected: `190 passed` (16 of them in `test_finalise.py`: the parametrised cases count one each).

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: black unchanged, `All checks passed!`, `Success: no issues found in 39 source files`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git add anki_miner_game/session/finalise.py tests/session/test_finalise.py && git commit -m "feat(session): finalise a stopped session into its game folder"
```

### Task 4: a locked video is retried, then left pending

**Files:**
- Modify: `anki_miner_game/session/finalise.py` (imports, new constant, `finalise` docstring and
  call, `_place` signature and video move, new `_move`)
- Test: `tests/session/test_finalise.py` (imports, `_Lock`, two tests)

**Interfaces:**
- Consumes: Task 3's `finalise`, `_place`.
- Produces: `RENAME_BACKOFF_S: Final = (0.1, 0.2, 0.5, 1.0, 2.0, 2.0, 2.0, 2.0)`; `finalise` now
  returns a `finalise_pending` result (manifest still in `_incoming/`, `queue_vad=False`) when the
  video stays locked; the injected `sleep` receives each wait.

- [ ] **Step 1: Write the failing tests**

In `tests/session/test_finalise.py` replace

```python
import os
from dataclasses import replace
```

with

```python
import math
import os
from dataclasses import replace
```

and replace

```python
from anki_miner_game.session.finalise import FinaliseError, finalise
```

with

```python
from anki_miner_game.session.finalise import RENAME_BACKOFF_S, FinaliseError, finalise
```

Then append to the end of the file:

```python


class _Lock:
    """``os.replace`` of ``path`` fails with ``PermissionError`` ``times`` times, as a Windows file OBS
    still holds does; set ``times = 0`` to release it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path, times: float) -> None:
        self.path = path
        self.times = times
        real = os.replace

        def locked_replace(src, dst, *args, **kwargs):
            if Path(src) == self.path and self.times > 0:
                self.times -= 1
                raise PermissionError(13, "The process cannot access the file", str(src))
            return real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", locked_replace)


def test_a_locked_video_is_retried_with_backoff_until_it_moves(tmp_path, monkeypatch):
    path = _session(tmp_path, RECORDS)
    _Lock(monkeypatch, tmp_path / "_incoming" / f"{OBS_STEM}.mkv", times=3)
    waits: list[float] = []
    result = finalise(path, CFG, sleep=waits.append)
    assert waits == list(RENAME_BACKOFF_S[:3])
    assert result.manifest.state is ManifestState.READY
    assert (tmp_path / TITLE / "Steins;Gate - 03.mkv").read_bytes() == VIDEO_BYTES


def test_a_video_locked_past_the_retries_leaves_the_session_pending_until_the_next_run(tmp_path, monkeypatch):
    path = _session(tmp_path, RECORDS)
    lock = _Lock(monkeypatch, tmp_path / "_incoming" / f"{OBS_STEM}.mkv", times=math.inf)
    waits: list[float] = []

    result = finalise(path, CFG, sleep=waits.append)

    assert waits == list(RENAME_BACKOFF_S)
    assert sum(waits) <= 10
    assert result.manifest_path == path
    assert result.manifest.state is ManifestState.FINALISE_PENDING
    assert load_manifest(path) == result.manifest
    assert not result.queue_vad
    # The same-stem pair stays usable in _incoming/, and nothing reached the game folder.
    assert sorted(_tree(tmp_path)) == [
        f"_incoming/{OBS_STEM}.mkv",
        f"_incoming/{OBS_STEM}.session.json",
        f"_incoming/{OBS_STEM}.srt",
    ]

    lock.times = 0  # the next launch
    again = finalise(path, CFG, sleep=waits.append)
    assert again.manifest.state is ManifestState.READY
    assert again.manifest.live_cues == LIVE_CUES
    assert again.queue_vad
```

(The file must end with exactly one newline after `assert again.queue_vad`; black checks it.)

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_finalise.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ImportError: cannot import name 'RENAME_BACKOFF_S' from 'anki_miner_game.session.finalise'`.

- [ ] **Step 3: Retry the video move**

In `anki_miner_game/session/finalise.py` make these six edits.

(a) Replace

```python
from pathlib import Path

from anki_miner_game.models.config import AppConfig
```

with

```python
from pathlib import Path
from typing import Final

from anki_miner_game.models.config import AppConfig
```

(b) Replace

```python
from anki_miner_game.store import StoreError


class FinaliseError(Exception):
```

with

```python
from anki_miner_game.store import StoreError

RENAME_BACKOFF_S: Final = (0.1, 0.2, 0.5, 1.0, 2.0, 2.0, 2.0, 2.0)
"""Waits between attempts to move a locked video: 9.8 s in all, inside spec 10.3's 10 s. On Windows
OBS can hold the file briefly after ``STOPPED``."""


class FinaliseError(Exception):
```

(c) Replace

```python
    Blocking file I/O: call it off the I/O loop. Anything that stops it raises ``FinaliseError``.
```

with

```python
    Blocking file I/O plus up to ``sum(RENAME_BACKOFF_S)`` of waiting: call it off the I/O loop. A
    video still locked after the waits leaves the session ``finalise_pending`` in ``_incoming/``,
    which is returned, not raised. Anything else that stops it raises ``FinaliseError``.
```

(d) Replace

```python
        return _place(manifest_path, manifest, files, cfg)
```

with

```python
        return _place(manifest_path, manifest, files, cfg, sleep)
```

(e) Replace

```python
def _place(manifest_path: Path, manifest: SessionManifest, files: IncomingFiles, cfg: AppConfig) -> FinaliseResult:
```

with

```python
def _place(
    manifest_path: Path,
    manifest: SessionManifest,
    files: IncomingFiles,
    cfg: AppConfig,
    sleep: Callable[[float], None],
) -> FinaliseResult:
```

(f) Replace

```python
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(files.video, video)
```

with

```python
        folder.mkdir(parents=True, exist_ok=True)
        if not _move(files.video, video, sleep):
            return FinaliseResult(manifest_path, manifest, queue_vad=False)
```

Then append to the end of the file:

```python


def _move(src: Path, dst: Path, sleep: Callable[[float], None]) -> bool:
    """``os.replace(src, dst)``, retried after each ``RENAME_BACKOFF_S`` wait while the file is locked
    (Windows reports a sharing violation as ``PermissionError``). False when it stays locked."""
    waits = iter(RENAME_BACKOFF_S)
    while True:
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            wait = next(waits, None)
            if wait is None:
                return False
            sleep(wait)
```

The pending return needs no manifest write: phase 1 already left the `_incoming/` manifest as
`finalise_pending`, which is exactly the state to retry from at the next launch.

- [ ] **Step 4: Run them to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session -n0 -p no:cacheprovider -q`
Expected: `192 passed`.

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: black unchanged, `All checks passed!`, `Success: no issues found in 39 source files`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git add anki_miner_game/session/finalise.py tests/session/test_finalise.py && git commit -m "feat(session): retry a locked video, then leave the session pending"
```

### Task 5: placing never overwrites a file in the game folder

**Files:**
- Modify: `anki_miner_game/session/finalise.py` (`_place`: pick a free NN before the video moves)
- Test: `tests/session/test_finalise.py` (`_session` gains `taken`, a crash-test row, one test)

**Interfaces:**
- Consumes: Task 4's `_place`, `_targets`, `write_manifest_atomic`.
- Produces: no new names. Behaviour: while the video is still in `_incoming/`, NN goes up until
  `<Game> - NN.<video ext>`, `.srt` and `.session.json` are all free; the new NN is written to the
  `_incoming/` manifest before the video moves.

- [ ] **Step 1: Write the failing tests**

In `tests/session/test_finalise.py` replace

```python
def _session(root: Path, records: list[JournalRecord] | None, **changes) -> Path:
    """A stopped session in ``root/_incoming`` as the actor leaves it; returns the manifest path.

    ``records=None``: the journal was never created.
    """
    manifest = replace(RECORDED, **changes)
```

with

```python
def _session(root: Path, records: list[JournalRecord] | None, *, taken: tuple[str, ...] = (), **changes) -> Path:
    """A stopped session in ``root/_incoming`` as the actor leaves it; returns the manifest path.

    ``records=None``: the journal was never created. ``taken``: files already in the game folder.
    """
    for name in taken:
        (root / TITLE).mkdir(parents=True, exist_ok=True)
        (root / TITLE / name).write_bytes(b"the user's own file")
    manifest = replace(RECORDED, **changes)
```

replace

```python
@pytest.mark.parametrize(("records", "steps"), [(RECORDS, 8), (NO_CUE_RECORDS, 6)], ids=["cues", "no-cues"])
def test_a_crash_between_any_two_steps_is_repaired_by_running_again(tmp_path, monkeypatch, records, steps):
    ops = _FileOps(monkeypatch)
    reference = _session(tmp_path / "reference", records)
```

with

```python
@pytest.mark.parametrize(
    ("records", "taken", "steps"),
    [(RECORDS, (), 8), (NO_CUE_RECORDS, (), 6), (RECORDS, ("Steins;Gate - 03.srt",), 9)],
    ids=["cues", "no-cues", "nn-taken"],
)
def test_a_crash_between_any_two_steps_is_repaired_by_running_again(tmp_path, monkeypatch, records, taken, steps):
    ops = _FileOps(monkeypatch)
    reference = _session(tmp_path / "reference", records, taken=taken)
```

replace

```python
        path = _session(root, records)
        ops.count, ops.crash_at = 0, crash_at
```

with

```python
        path = _session(root, records, taken=taken)
        ops.count, ops.crash_at = 0, crash_at
```

and append to the end of the file:

```python


def test_a_session_never_overwrites_a_file_already_in_the_game_folder(tmp_path):
    result = finalise(_session(tmp_path, RECORDS, taken=("Steins;Gate - 03.srt", "Steins;Gate - 04.mkv")), CFG)

    assert result.manifest.index == 5
    assert result.manifest.files == FilesRecord(video="Steins;Gate - 05.mkv", subtitle="Steins;Gate - 05.srt")
    assert _tree(tmp_path / TITLE) == {
        "Steins;Gate - 03.srt": b"the user's own file",
        "Steins;Gate - 04.mkv": b"the user's own file",
        "Steins;Gate - 05.mkv": VIDEO_BYTES,
        "Steins;Gate - 05.srt": SRT.encode("utf-8"),
        "Steins;Gate - 05.session.json": (tmp_path / TITLE / "Steins;Gate - 05.session.json").read_bytes(),
    }
```

The `nn-taken` crash row has nine file operations: the bumped NN is written to the `_incoming/`
manifest (operation 4) before the video moves. A crash between that write and the move, or right
after the move, must resume under the new NN; without the write, the re-run would look for the
video under the old NN and report it gone.

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session/test_finalise.py -n0 -p no:cacheprovider -q`
Expected: `2 failed, 18 passed`; the failures are
`test_a_crash_between_any_two_steps_is_repaired_by_running_again[nn-taken]` (8 operations, not 9)
and `test_a_session_never_overwrites_a_file_already_in_the_game_folder` (index 3, and the user's
`Steins;Gate - 03.srt` overwritten).

- [ ] **Step 3: Pick a free NN**

In `anki_miner_game/session/finalise.py` replace

```python
    folder = game_folder(manifest_path.parent, manifest.game.title)
    video, subtitle, placed = _targets(folder, manifest.game.title, manifest.index, files.video.suffix)
```

with

```python
    folder = game_folder(manifest_path.parent, manifest.game.title)
    if files.video.exists():  # nothing has moved yet, so NN can still change
        index = manifest.index
        while any(path.exists() for path in _targets(folder, manifest.game.title, index, files.video.suffix)):
            index += 1  # never overwrite a file that is already there
        if index != manifest.index:
            manifest = replace(manifest, index=index)
            write_manifest_atomic(manifest_path, manifest)
    video, subtitle, placed = _targets(folder, manifest.game.title, manifest.index, files.video.suffix)
```

- [ ] **Step 4: Run them to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/pytest tests/session -n0 -p no:cacheprovider -q`
Expected: `194 passed`.

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && .venv/bin/black --check . && .venv/bin/ruff check . && .venv/bin/mypy anki_miner_game`
Expected: black unchanged, `All checks passed!`, `Success: no issues found in 39 source files`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git add anki_miner_game/session/finalise.py tests/session/test_finalise.py && git commit -m "feat(session): never overwrite a file already in the game folder"
```

### Task 6: gate, evidence, status

**Files:** none in the repository. Writes `<worktree>/gate.log` (gitignored) and the status file.

- [ ] **Step 1: Run the full gate**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash scripts/health.sh > gate.log 2>&1; echo "exit=$?"`
Expected: `exit=0`.

Then: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && grep -E '^(PASS|FAIL) |passed|failed|SUMMARY|all green|FAILED' gate.log`
Expected: `PASS black`, `PASS ruff`, `PASS mypy`, `451 passed`, `PASS pytest`, `all green`. Keep
`gate.log`; never pipe the gate through `tail`. A red step: fix it at the root, re-run the step's
single-file command from the task that owns the file, commit the fix (`fix(session): ...`), re-run
the gate.

- [ ] **Step 2: Check the branch**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t06-finalise && git status --short && git log --oneline main..HEAD`
Expected: a clean tree (only the ignored `gate.log`), and on top of BASE's two merge commits the
plan commit plus the five task commits:
`docs(plan): t06-finalise task plan`,
`feat(session): add the append-only session journal`,
`feat(session): add manifest I/O and session number reservation`,
`feat(session): finalise a stopped session into its game folder`,
`feat(session): retry a locked video, then leave the session pending`,
`feat(session): never overwrite a file already in the game folder`.

- [ ] **Step 3: Update the status file**

Rewrite `/home/light/Projects/anki_miner_game/.orchestration/status/t06-finalise.json` keeping
`base_sha`, with `stage` `"implemented"`, `head_sha` (`git rev-parse HEAD`), `gate_exit` `0`,
`gate_summary` (the PASS lines and the test count from `gate.log`), `contract_change_request` `""`,
and `spec_amendments` set to exactly these strings, for the M0 gate amender:

```json
[
  "spec 10.3: steps 1-3 end by writing the manifest as finalise_pending (live_cues, counts, no_cues, stopped_at) and deleting the journal; the manifest alone carries the session from there. finalise_pending therefore also means 'cues built, files not yet placed' after a crash, not only after a rename that outlived its retries. In the spec's order a crash after the manifest left _incoming/ stranded the journal and a non-ready manifest that no launch scan revisits.",
  "spec 10.3 step 4: only the video is renamed (retried up to 10 s on a lock); the subtitle and the manifest are written at their final names from the manifest and deleted from _incoming/, the manifest last and already ready. The _incoming/ manifest is never ready, so a ready manifest has been placed.",
  "spec 10.3 step 4: while the video is still in _incoming/, a final name that already exists raises NN until <Game> - NN.<ext>, .srt and .session.json are all free; the new NN is written to the manifest before the video moves. os.replace would otherwise overwrite a user's file.",
  "spec 10.3 step 5: finalise does not queue the VAD job; it returns queue_vad (placed now, has a subtitle, cfg.vad.enabled) and the caller calls VadJobs.queue.",
  "spec 5 counts: finalise writes accepted = number of live cues and skip = journalled lines the skip rule dropped; received, duplicate, no_letters, junk and paused stay the actor's. Matches the spec 5 example (received 1412 = accepted 1260 + duplicate 96 + no_letters 31 + junk 4 + paused 0 + skip 21).",
  "spec 5 stopped_at: when the manifest has none at finalise (an orphan), it is the video's modification time in UTC."
]
```

and `notes` naming what T15 must do (from "Interfaces for dependants" above): close the journal before
finalise, run finalise off the I/O loop one call per manifest at a time, queue VAD from `queue_vad`,
banner on `no_cues` / `finalise_pending` / `FinaliseError`.

## Acceptance map

| Card item (master plan T06, spec 18.1) | Test |
|---|---|
| Each journal record type | `test_each_record_type_is_one_json_line_as_the_spec_shows`, `test_records_read_back_in_order` |
| Flush per write | `test_every_record_is_on_disk_as_soon_as_it_is_appended` |
| A torn last line tolerated | `test_a_torn_last_line_is_skipped` (3 cuts), `test_a_journal_resumed_after_a_torn_line_keeps_every_new_record` |
| Finalise with a crash at every step boundary, re-run to the same end state | `test_a_crash_between_any_two_steps_is_repaired_by_running_again` (cues, no-cues, nn-taken) |
| Missing `stop` -> last offset + cap | `test_without_a_stop_record_the_stop_is_the_last_offset_plus_the_cap` |
| `no_cues` | `test_a_session_without_cues_keeps_the_video_and_writes_no_subtitle` (skip-only, empty journal, no journal) |
| Rename retry, locked N times then success | `test_a_locked_video_is_retried_with_backoff_until_it_moves` |
| Permanently locked -> `finalise_pending` | `test_a_video_locked_past_the_retries_leaves_the_session_pending_until_the_next_run` |
| NN reservation: files only, manifests only, both | `test_nn_is_one_more_than_the_highest_in_the_game_folder` |
| NN reservation: `_incoming/` manifests of the same game | `test_nn_counts_manifests_of_the_same_game_in_incoming` |
| Spec 10.3 step 3 byte-exact subtitle, step 4 names, step 5 `ready` | `test_a_stopped_session_lands_in_its_game_folder` |
| Spec 17 "Zero cues at stop": video kept, no subtitle, flag | `test_a_session_without_cues_keeps_the_video_and_writes_no_subtitle` |
| One start shift per session (`START_SHIFT_MS`) | `test_an_ocr_session_starts_its_cues_one_second_early` |

## Out of scope

- Deciding which `_incoming/` manifests are orphans, reconcile, and calling finalise at launch (T15,
  T16). Banners and `SessionFinalised` publication (T15).
- Queuing the VAD job and the `vad` record (T23); finalise only reports `queue_vad`.
- Removing a `.<name>.*.tmp` left by a process killed inside an atomic write: hidden, never a subtitle
  extension, harmless to Anki Miner.
- NN above 9999: `session_stem` raises `ValueError`; unreachable without 9999 earlier sessions.
- Making `store._read` / `store._write` public (decision 13).
