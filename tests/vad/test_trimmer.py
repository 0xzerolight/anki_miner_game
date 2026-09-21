"""``vad/trimmer.py``: VAD jobs over finalised sessions (spec 13, 17 VAD row).

The worker is ``tests/fakes/fake_vad_worker.py``, run by this interpreter the way the trimmer runs
the real one, so the protocol, the subprocess handling and the manifest writes are all real.
"""

import errno
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import AppConfig, CueSettings, VadSettings
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.manifest import (
    FilesRecord,
    GameRef,
    LiveCue,
    ManifestState,
    ObsRecord,
    SessionManifest,
    VadRecord,
    VadState,
)
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session.manifest import load_manifest, write_manifest_atomic
from anki_miner_game.session.srt_writer import format_srt, write_srt_atomic
from anki_miner_game.vad.trimmer import WORKER_SCRIPT, WRITE_BACKOFF_S, VadTrimmer

FAKE_WORKER = Path(__file__).resolve().parents[1] / "fakes" / "fake_vad_worker.py"
JOIN_S = 30

# Spec 13.3 worked example, hook mode.
LIVE_CUES = (
    LiveCue(i=1, start_ms=5230, end_ms=13850, text="「こんにちは」", source="textractor"),
    LiveCue(i=2, start_ms=14200, end_ms=29200, text="まゆしぃ", source="textractor"),
)
EXAMPLE_REGIONS = [(5310, 8920), (9600, 11050), (14200, 16000)]
LIVE_SRT = format_srt([c.to_cue() for c in LIVE_CUES])
TRIMMED_SRT = "1\n00:00:05,230 --> 00:00:11,200\n「こんにちは」\n\n" "2\n00:00:14,200 --> 00:00:16,150\nまゆしぃ\n"


def region(start_ms: int, end_ms: int) -> str:
    return json.dumps({"t": "region", "start_ms": start_ms, "end_ms": end_ms})


DONE = json.dumps({"t": "done"})


def worker_lines(regions: list[tuple[int, int]]) -> list[str]:
    return [region(*r) for r in regions] + [DONE]


class FakeAddon:
    """``VadRuntime``: this interpreter runs the fake worker."""

    def __init__(self, model_path: Path, status: AddonStatus = AddonStatus.READY) -> None:
        self._status = status
        self._model_path = model_path

    def status(self) -> AddonStatus:
        return self._status

    @property
    def python_path(self) -> Path:
        return Path(sys.executable)

    @property
    def model_path(self) -> Path:
        return self._model_path


class RecordingPresenter:
    def __init__(self) -> None:
        self.progress: list[tuple[Path, int, int | None]] = []
        self.finished: list[tuple[Path, VadState]] = []

    def vad_progress(self, manifest_path: Path, done_ms: int, total_ms: int | None) -> None:
        self.progress.append((manifest_path, done_ms, total_ms))

    def vad_finished(self, manifest_path: Path, state: VadState) -> None:
        self.finished.append((manifest_path, state))


@dataclass
class Session:
    manifest: Path
    video: Path
    subtitle: Path

    def script(self, lines: list[str], *, code: int = 0, stderr: str = "", wait_for: Path | None = None) -> None:
        data = {"lines": lines, "exit": code, "stderr": stderr, "wait_for": str(wait_for) if wait_for else None}
        Path(f"{self.video}.fake.json").write_text(json.dumps(data), encoding="utf-8")

    @property
    def started(self) -> dict | None:
        path = Path(f"{self.video}.argv.json")
        return json.loads(path.read_text()) if path.exists() else None

    def load(self) -> SessionManifest:
        return load_manifest(self.manifest)

    def srt(self) -> str:
        return self.subtitle.read_text(encoding="utf-8")


def make_session(
    folder: Path,
    index: int = 3,
    *,
    live_cues: tuple[LiveCue, ...] = LIVE_CUES,
    text_mode: TextMode = TextMode.HOOK,
    state: ManifestState = ManifestState.READY,
) -> Session:
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"Game - {index:02d}"
    video, subtitle, manifest_path = (folder / f"{stem}{suffix}" for suffix in (".mkv", ".srt", ".session.json"))
    video.write_bytes(b"not really matroska")
    if live_cues:
        write_srt_atomic(subtitle, [c.to_cue() for c in live_cues])
    manifest = SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug="game", title="Game"),
        index=index,
        state=state,
        started_at="2026-10-02T18:04:11Z",
        stopped_at="2026-10-02T19:31:40Z",
        obs=ObsRecord(version="32.2.2", websocket="5.7.3", profile="p", collection="c", output_path="x.mkv"),
        text_mode=text_mode,
        live_cues=live_cues,
        files=FilesRecord(video=video.name, subtitle=subtitle.name if live_cues else None),
    )
    write_manifest_atomic(manifest_path, manifest)
    return Session(manifest_path, video, subtitle)


@pytest.fixture
def model(tmp_path) -> Path:
    path = tmp_path / "addon" / "silero_vad_v6.onnx"
    path.parent.mkdir()
    path.write_bytes(b"model")
    return path


@pytest.fixture
def presenter() -> RecordingPresenter:
    return RecordingPresenter()


@pytest.fixture
def sleeps() -> list[float]:
    """The waits the trimmer asked for before writing a manifest again."""
    return []


@pytest.fixture
def make_trimmer(model, presenter, sleeps):
    made: list[VadTrimmer] = []

    def make(status: AddonStatus = AddonStatus.READY, cfg: AppConfig | None = None) -> VadTrimmer:
        config = cfg or AppConfig()
        trimmer = VadTrimmer(
            FakeAddon(model, status), presenter, lambda: config, worker_script=FAKE_WORKER, sleep=sleeps.append
        )
        made.append(trimmer)
        return trimmer

    yield make
    for trimmer in made:
        trimmer.close()


@pytest.fixture
def session(tmp_path) -> Session:
    return make_session(tmp_path / "Game")


def run_jobs(trimmer: VadTrimmer) -> None:
    assert trimmer.join(JOIN_S), "VAD jobs did not finish"


# --- the pass ---------------------------------------------------------------


def test_the_worker_script_ships_beside_the_trimmer():
    assert Path(__file__).resolve().parents[2] / "anki_miner_game" / "vad" / "worker" / "vad_worker.py" == WORKER_SCRIPT
    assert WORKER_SCRIPT.is_file()


def test_a_pass_writes_the_trimmed_subtitle_and_records_done(make_trimmer, presenter, session):
    session.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == TRIMMED_SRT
    manifest = session.load()
    assert manifest.state is ManifestState.READY
    assert manifest.vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=2, no_speech=0)
    assert manifest.live_cues == LIVE_CUES  # a re-run or restore starts from here again
    assert presenter.finished == [(session.manifest, VadState.DONE)]


def test_the_worker_gets_the_video_and_the_model_in_an_isolated_interpreter(make_trimmer, model, session):
    session.script(worker_lines([]))
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.started == {"argv": ["--video", str(session.video), "--model", str(model)], "isolated": 1}


def test_the_assignment_follows_the_text_mode_and_the_cue_settings(make_trimmer, tmp_path):
    ocr = make_session(tmp_path / "Game", text_mode=TextMode.OCR)
    # OCR mode: cue 2's start snaps back to 13.9 s, taking that region from cue 1's chain; cue 1's
    # end is then clamped to 1000 ms (the configured end gap) before cue 2's new start.
    ocr.script(worker_lines([(5310, 13500), (13900, 16000)]))
    trimmer = make_trimmer(cfg=AppConfig(cue=CueSettings(end_gap_ms=1000)))

    trimmer.queue(ocr.manifest)
    run_jobs(trimmer)

    assert ocr.srt() == format_srt(
        [
            Cue(index=1, start_ms=5230, end_ms=12900, text="「こんにちは」", source_id="textractor"),
            Cue(index=2, start_ms=13900, end_ms=16150, text="まゆしぃ", source_id="textractor"),
        ]
    )


def test_progress_is_forwarded_including_an_unknown_total(make_trimmer, presenter, session):
    progress = [
        json.dumps({"t": "progress", "done_ms": 600000, "total_ms": 5248120}),
        json.dumps({"t": "progress", "done_ms": 700000, "total_ms": None}),
    ]
    session.script(progress + worker_lines([]))
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert presenter.progress == [(session.manifest, 600000, 5248120), (session.manifest, 700000, None)]


def test_the_manifest_reads_vad_running_while_the_worker_runs(make_trimmer, session, tmp_path):
    release = tmp_path / "release"
    session.script(worker_lines(EXAMPLE_REGIONS), wait_for=release)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    wait_until(lambda: session.started is not None)  # polls the worker, never the manifest being written
    running = session.load()
    release.write_text("go")
    run_jobs(trimmer)

    assert (running.state, running.vad) == (ManifestState.VAD_RUNNING, VadRecord(state=VadState.QUEUED))
    assert session.srt() == TRIMMED_SRT
    assert session.load().state is ManifestState.READY


def test_counts_split_the_cues_by_whether_their_final_span_holds_speech(make_trimmer, tmp_path):
    # Cue 1's voice runs to its live end, so the pass leaves it as it was; it still holds speech.
    # Cue 2 has no voice of its own: its chain is cue 1's tail and it shrinks to the 500 ms floor.
    cues = (
        LiveCue(i=1, start_ms=10000, end_ms=19650, text="voiced", source="agent"),
        LiveCue(i=2, start_ms=20000, end_ms=25000, text="silent", source="agent"),
    )
    s = make_session(tmp_path / "Game", live_cues=cues)
    s.script(worker_lines([(10100, 19900)]))
    trimmer = make_trimmer()

    trimmer.queue(s.manifest)
    run_jobs(trimmer)

    assert s.srt() == "1\n00:00:10,000 --> 00:00:19,650\nvoiced\n\n2\n00:00:20,000 --> 00:00:20,500\nsilent\n"
    assert (s.load().vad.trimmed, s.load().vad.no_speech) == (1, 1)


def test_no_regions_keep_the_live_subtitle_and_count_every_cue_as_no_speech(make_trimmer, session):
    session.script(worker_lines([]))
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == LIVE_SRT
    assert session.load().vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=0, no_speech=2)


def test_rerun_replaces_an_earlier_pass(make_trimmer, presenter, session):
    session.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()
    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    session.script(worker_lines([(5300, 6000)]))
    trimmer.rerun(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == format_srt(
        [
            Cue(index=1, start_ms=5230, end_ms=6150, text="「こんにちは」", source_id="textractor"),
            Cue(index=2, start_ms=14200, end_ms=29200, text="まゆしぃ", source_id="textractor"),
        ]
    )
    assert session.load().vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=1, no_speech=1)
    assert presenter.finished == [(session.manifest, VadState.DONE)] * 2


def test_a_session_left_vad_running_by_a_crash_can_be_rerun(make_trimmer, tmp_path):
    s = make_session(tmp_path / "Game", state=ManifestState.VAD_RUNNING)
    s.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()

    trimmer.rerun(s.manifest)
    run_jobs(trimmer)

    assert s.srt() == TRIMMED_SRT
    assert s.load().state is ManifestState.READY


# --- when the pass cannot run or fails: the live subtitle stands --------------


def trimmed_already(trimmer: VadTrimmer, session: Session) -> None:
    """Run one good pass first, so a later failure has a trimmed subtitle to undo."""
    session.script(worker_lines(EXAMPLE_REGIONS))
    trimmer.queue(session.manifest)
    run_jobs(trimmer)
    assert session.srt() == TRIMMED_SRT


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (AddonStatus.MISSING, "The VAD add-on is not installed."),
        (AddonStatus.BROKEN, "The VAD add-on is damaged or out of date; reinstall it."),
        (AddonStatus.INSTALLING, "The VAD add-on was still installing; re-run VAD once it is ready."),
    ],
)
def test_without_a_ready_addon_the_pass_is_unavailable_and_never_runs(
    make_trimmer, presenter, session, status, message
):
    session.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer(status)

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.started is None
    assert session.srt() == LIVE_SRT
    manifest = session.load()
    assert manifest.state is ManifestState.READY
    assert manifest.vad == VadRecord(state=VadState.UNAVAILABLE, message=message)
    assert presenter.finished == [(session.manifest, VadState.UNAVAILABLE)]


def test_a_worker_error_fails_the_pass_and_restores_the_live_subtitle(make_trimmer, presenter, session):
    trimmer = make_trimmer()
    trimmed_already(trimmer, session)
    error = json.dumps({"t": "error", "message": "InvalidDataError: [Errno 1094995529] Invalid data"})
    session.script([region(5310, 8920), error], code=1)

    trimmer.rerun(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == LIVE_SRT
    assert session.load().vad == VadRecord(
        state=VadState.FAILED,
        model="silero_vad_v6",
        message="The VAD worker failed: InvalidDataError: [Errno 1094995529] Invalid data",
    )
    assert session.load().state is ManifestState.READY
    assert presenter.finished[-1] == (session.manifest, VadState.FAILED)


@pytest.mark.parametrize(
    ("lines", "code", "stderr", "message"),
    [
        ([region(5310, 8920)], 0, "", "The VAD worker stopped before finishing"),
        (
            [region(5310, 8920)],
            3,
            "Traceback ...\nMemoryError\n",
            "The VAD worker exited with code 3: Traceback ...\nMemoryError",
        ),
        ([DONE], 1, "", "The VAD worker exited with code 1"),
    ],
    ids=["no done", "crash", "done then exit 1"],
)
def test_a_worker_that_does_not_finish_cleanly_fails_the_pass(make_trimmer, session, lines, code, stderr, message):
    session.script(lines, code=code, stderr=stderr)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.load().vad == VadRecord(state=VadState.FAILED, model="silero_vad_v6", message=message)
    assert session.srt() == LIVE_SRT


@pytest.mark.parametrize(
    "bad",
    [
        {"t": "region", "start_ms": 900, "end_ms": 900},
        {"t": "region", "start_ms": "5", "end_ms": 900},
        {"t": "region", "start_ms": 5},
        {"t": "region", "start_ms": True, "end_ms": 900},
    ],
)
def test_an_invalid_region_fails_the_pass(make_trimmer, session, bad):
    session.script([json.dumps(bad), DONE])
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    vad = session.load().vad
    assert vad.state is VadState.FAILED
    assert vad.message.startswith("The VAD worker sent an invalid region")


def test_lines_outside_the_protocol_are_ignored(make_trimmer, session):
    noise = ["onnxruntime says hello", json.dumps([1, 2]), json.dumps({"t": "something new"})]
    session.script(noise + worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.load().vad.state is VadState.DONE


def test_a_worker_that_cannot_start_fails_the_pass(model, presenter, session):
    class NoPython(FakeAddon):
        @property
        def python_path(self) -> Path:
            return model.parent / "missing-python"

    trimmer = VadTrimmer(NoPython(model), presenter, AppConfig, worker_script=FAKE_WORKER)
    try:
        trimmer.queue(session.manifest)
        run_jobs(trimmer)
    finally:
        trimmer.close()

    vad = session.load().vad
    assert vad.state is VadState.FAILED
    assert vad.message.startswith("The VAD worker could not start:")
    assert session.srt() == LIVE_SRT


# --- a manifest that cannot be written ----------------------------------------


def queued(data: dict) -> bool:
    return data["state"] == "ready" and data["vad"]["state"] == "queued"


def running(data: dict) -> bool:
    return data["state"] == "vad_running"


def settled(data: dict) -> bool:
    return data["state"] == "ready" and data["vad"]["state"] != "queued"


def refuse_manifest_writes(
    monkeypatch, match: Callable[[dict], bool], *, times: int | None = None, error: OSError | None = None
) -> list[Path]:
    """Fail ``os.replace`` onto a manifest for the writes ``match`` picks: ``times`` times, or every time.

    The default error is the one Windows raises while another handle has the manifest open (the GUI
    reading it, a virus scanner). Returns the refused targets.
    """
    real_replace = os.replace
    refused: list[Path] = []

    def replace(src, dst):
        manifest = str(dst).endswith(".session.json") and (times is None or len(refused) < times)
        if manifest and match(json.loads(Path(src).read_text(encoding="utf-8"))):
            refused.append(Path(dst))
            raise error or PermissionError(errno.EACCES, "The file is being used by another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return refused


@pytest.mark.parametrize("match", [running, settled], ids=["pass-start", "pass-end"])
def test_a_manifest_write_refused_once_is_retried(monkeypatch, make_trimmer, presenter, sleeps, session, match):
    session.script(worker_lines(EXAMPLE_REGIONS))
    refused = refuse_manifest_writes(monkeypatch, match, times=1)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert refused == [session.manifest]
    assert sleeps == [WRITE_BACKOFF_S[0]]
    assert session.srt() == TRIMMED_SRT
    assert session.load().state is ManifestState.READY
    assert session.load().vad.state is VadState.DONE
    assert presenter.finished == [(session.manifest, VadState.DONE)]


def test_a_queued_marker_that_cannot_be_written_still_runs_the_pass(monkeypatch, make_trimmer, presenter, session):
    session.script(worker_lines(EXAMPLE_REGIONS))
    refused = refuse_manifest_writes(monkeypatch, queued, times=1)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert refused == [session.manifest]
    assert session.srt() == TRIMMED_SRT
    assert session.load().vad.state is VadState.DONE
    assert presenter.finished == [(session.manifest, VadState.DONE)]


@pytest.mark.parametrize(
    ("error", "waits"),
    [(None, list(WRITE_BACKOFF_S)), (OSError(errno.ENOSPC, "No space left on device"), [])],
    ids=["locked", "disk-full"],
)
def test_a_manifest_that_cannot_be_marked_running_fails_the_pass(
    monkeypatch, make_trimmer, presenter, sleeps, session, error, waits
):
    session.script(worker_lines(EXAMPLE_REGIONS))
    refuse_manifest_writes(monkeypatch, running, error=error)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert session.started is None  # the worker never ran
    assert sleeps == waits  # only a sharing violation is waited out
    vad = session.load().vad
    assert vad.state is VadState.FAILED
    assert vad.message.startswith("The session file could not be written:")
    assert session.srt() == LIVE_SRT
    assert presenter.finished == [(session.manifest, VadState.FAILED)]


def test_the_job_ends_with_vad_finished_when_its_outcome_cannot_be_written(
    monkeypatch, make_trimmer, presenter, sleeps, session
):
    session.script(worker_lines(EXAMPLE_REGIONS))
    refuse_manifest_writes(monkeypatch, settled)
    trimmer = make_trimmer()

    trimmer.queue(session.manifest)
    run_jobs(trimmer)

    assert sleeps == list(WRITE_BACKOFF_S)
    assert session.load().vad.state is VadState.QUEUED  # re-queued at the next launch
    assert presenter.finished == [(session.manifest, VadState.DONE)]


# --- restore ----------------------------------------------------------------


def test_restore_rewrites_the_live_subtitle_and_records_restored(make_trimmer, presenter, session):
    trimmer = make_trimmer()
    trimmed_already(trimmer, session)

    trimmer.restore(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == LIVE_SRT
    assert session.load().vad == VadRecord(state=VadState.RESTORED)
    assert session.load().state is ManifestState.READY
    assert presenter.finished == [(session.manifest, VadState.DONE), (session.manifest, VadState.RESTORED)]


def test_restore_needs_no_addon(make_trimmer, session):
    session.subtitle.write_text("stale", encoding="utf-8")
    trimmer = make_trimmer(AddonStatus.MISSING)

    trimmer.restore(session.manifest)
    run_jobs(trimmer)

    assert session.srt() == LIVE_SRT
    assert session.load().vad == VadRecord(state=VadState.RESTORED)


# --- which sessions and jobs run ----------------------------------------------


def test_queue_does_nothing_while_vad_is_switched_off_but_rerun_still_runs(make_trimmer, presenter, session):
    session.script(worker_lines(EXAMPLE_REGIONS))
    before = session.manifest.read_bytes()
    trimmer = make_trimmer(cfg=AppConfig(vad=VadSettings(enabled=False)))

    trimmer.queue(session.manifest)
    run_jobs(trimmer)
    assert session.manifest.read_bytes() == before
    assert session.started is None and presenter.finished == []

    trimmer.rerun(session.manifest)
    run_jobs(trimmer)
    assert session.load().vad.state is VadState.DONE


@pytest.mark.parametrize("kind", ["no cues", "finalise pending"], ids=lambda kind: kind.replace(" ", "-"))
def test_a_session_with_nothing_to_trim_is_left_alone(make_trimmer, presenter, tmp_path, kind):
    if kind == "no cues":
        s = make_session(tmp_path / "Game", live_cues=())
    else:
        s = make_session(tmp_path / "Game", state=ManifestState.FINALISE_PENDING)
    s.script(worker_lines(EXAMPLE_REGIONS))
    before = s.manifest.read_bytes()
    trimmer = make_trimmer()

    for job in (trimmer.queue, trimmer.rerun, trimmer.restore):
        job(s.manifest)
    run_jobs(trimmer)

    assert s.manifest.read_bytes() == before
    assert s.started is None and presenter.finished == []


def test_a_missing_or_unreadable_manifest_is_skipped_and_later_jobs_still_run(make_trimmer, presenter, tmp_path):
    gone = tmp_path / "Game" / "Game - 01.session.json"
    broken = make_session(tmp_path / "Game", 2)
    broken.manifest.write_text("{not json", encoding="utf-8")
    good = make_session(tmp_path / "Game", 3)
    good.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()

    trimmer.queue(gone)
    trimmer.rerun(broken.manifest)
    trimmer.restore(broken.manifest)
    trimmer.queue(good.manifest)
    run_jobs(trimmer)

    assert presenter.finished == [(good.manifest, VadState.DONE)]
    assert broken.manifest.read_text(encoding="utf-8") == "{not json"


def test_jobs_run_one_at_a_time_in_the_order_they_were_asked_for(make_trimmer, presenter, tmp_path):
    release = tmp_path / "release"
    first = make_session(tmp_path / "Game", 1)
    first.script(worker_lines(EXAMPLE_REGIONS), wait_for=release)
    second = make_session(tmp_path / "Game", 2)
    second.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()

    trimmer.queue(first.manifest)
    trimmer.queue(second.manifest)
    trimmer.restore(first.manifest)
    wait_until(lambda: first.started is not None)
    assert second.started is None
    assert second.load().vad == VadRecord(state=VadState.QUEUED)
    release.write_text("go")
    run_jobs(trimmer)

    assert presenter.finished == [
        (first.manifest, VadState.DONE),
        (second.manifest, VadState.DONE),
        (first.manifest, VadState.RESTORED),
    ]
    assert first.srt() == LIVE_SRT and second.srt() == TRIMMED_SRT


def test_close_stops_a_running_pass_and_leaves_it_queued(make_trimmer, presenter, tmp_path):
    running = make_session(tmp_path / "Game", 1)
    running.script(worker_lines(EXAMPLE_REGIONS), wait_for=tmp_path / "never")
    pending = make_session(tmp_path / "Game", 2)
    pending.script(worker_lines(EXAMPLE_REGIONS))
    trimmer = make_trimmer()
    trimmer.queue(running.manifest)
    trimmer.queue(pending.manifest)
    wait_until(lambda: running.started is not None)

    closed = threading.Thread(target=trimmer.close)
    closed.start()
    closed.join(10)

    assert not closed.is_alive(), "close() did not stop the worker"
    for s in (running, pending):
        assert s.load().state is ManifestState.READY
        assert s.load().vad == VadRecord(state=VadState.QUEUED)
        assert s.srt() == LIVE_SRT
    assert pending.started is None
    assert presenter.finished == []

    trimmer.queue(pending.manifest)  # after close: ignored, never raises
    assert trimmer.join(1)
    assert pending.started is None


def wait_until(condition, timeout: float = 10) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


@pytest.mark.network
@pytest.mark.vad
def test_real_install_then_a_pass_over_the_speech_clip(presenter, tmp_path):
    """Installs the add-on for real (uv, a managed CPython 3.12, the pinned wheels and the model,
    about 130 MB) into the isolated home, then trims one cue over the public-domain speech clip."""
    import asyncio

    from anki_miner_game import paths
    from anki_miner_game.addons.vad_addon import VadAddon

    addon = VadAddon(paths.home())
    asyncio.run(addon.install(lambda done, total: None))
    assert addon.status() is AddonStatus.READY

    clip = Path(__file__).resolve().parents[1] / "fixtures" / "vad" / "librivox_autumn_excerpt.ogg"
    s = make_session(
        tmp_path / "Game", live_cues=(LiveCue(i=1, start_ms=1000, end_ms=5000, text="Autumn", source="agent"),)
    )
    s.video.write_bytes(clip.read_bytes())
    trimmer = VadTrimmer(addon, presenter, AppConfig)
    try:
        trimmer.queue(s.manifest)
        assert trimmer.join(300)
    finally:
        trimmer.close()

    assert s.load().vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=1, no_speech=0)
    # The clip's one region is 1244-4036 ms (faster-whisper's own result): the cue ends 150 ms later.
    (block,) = s.srt().strip().split("\n\n")
    start, end = block.split("\n")[1].split(" --> ")
    assert start == "00:00:01,000"
    assert abs(int(end[-3:]) + 1000 * int(end[-6:-4]) - 4186) <= 64
    assert presenter.progress and presenter.finished == [(s.manifest, VadState.DONE)]
