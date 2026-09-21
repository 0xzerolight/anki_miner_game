"""Finalise (spec 10.3; 18.1 row ``session/journal.py`` + finalise; 17 rows zero cues and rename)."""

import errno
import math
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
from anki_miner_game.session.finalise import RENAME_BACKOFF_S, FinaliseError, finalise
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


def _session(root: Path, records: list[JournalRecord] | None, *, taken: tuple[str, ...] = (), **changes) -> Path:
    """A stopped session in ``root/_incoming`` as the actor leaves it; returns the manifest path.

    ``records=None``: the journal was never created. ``taken``: files already in the game folder.
    """
    for name in taken:
        (root / TITLE).mkdir(parents=True, exist_ok=True)
        (root / TITLE / name).write_bytes(b"the user's own file")
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


def test_a_session_with_several_stop_records_ends_at_the_first(tmp_path):
    # Spec 7: a split recording is finalised against the first file. Every record after that stop is
    # outside the session: its lines are not cues, not skips, and a replace there rewrites nothing.
    records: list[JournalRecord] = [
        LineRecord(1_000, "あ", "agent"),
        StopRecord(5_000),
        ReplaceRecord("あいう"),
        LineRecord(6_000, "い", "agent"),
        StopRecord(20_000),
    ]
    result = finalise(_session(tmp_path, records), CFG)
    assert result.manifest.live_cues == (LiveCue(i=1, start_ms=1_000, end_ms=4_650, text="あ", source="agent"),)
    assert (result.manifest.counts.accepted, result.manifest.counts.skip) == (1, 0)


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


def test_a_session_number_no_stem_can_carry_is_reported(tmp_path):
    # A hand-edited manifest: session_stem accepts 1-9999 only.
    with pytest.raises(FinaliseError, match="index 0"):
        finalise(_session(tmp_path, RECORDS, index=0), CFG)


@pytest.mark.parametrize("output_path", ["", ".."])
def test_a_manifest_whose_output_path_names_no_video_is_reported_and_moves_nothing(tmp_path, output_path):
    # Another session is still recording in _incoming/; the malformed manifest must not carry it off.
    _session(tmp_path, RECORDS)
    broken = tmp_path / "_incoming" / "broken.session.json"
    write_manifest_atomic(broken, replace(RECORDED, obs=replace(RECORDED.obs, output_path=output_path)))
    before = _tree(tmp_path)
    with pytest.raises(FinaliseError, match="names no video file") as caught:
        finalise(broken, CFG)
    assert caught.value.manifest_path == broken
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("records", "changes"),
    [
        (RECORDS, {}),
        (RECORDS, {"stopped_at": "2026-10-02T19:00:00Z"}),
        (None, {"state": ManifestState.FINALISE_PENDING, "live_cues": LIVE_CUES}),
    ],
    ids=["recording", "recording-stopped-at", "pending"],
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


@pytest.mark.parametrize(
    ("records", "taken", "steps"),
    [(RECORDS, (), 8), (NO_CUE_RECORDS, (), 6), (RECORDS, ("Steins;Gate - 03.srt",), 9)],
    ids=["cues", "no-cues", "nn-taken"],
)
def test_a_crash_between_any_two_steps_is_repaired_by_running_again(tmp_path, monkeypatch, records, taken, steps):
    ops = _FileOps(monkeypatch)
    reference = _session(tmp_path / "reference", records, taken=taken)
    ops.count = 0
    finalise(reference, CFG)
    assert ops.count == steps
    expected = _tree(tmp_path / "reference")

    for crash_at in range(steps):
        root = tmp_path / f"crash-{crash_at}"
        path = _session(root, records, taken=taken)
        ops.count, ops.crash_at = 0, crash_at
        with pytest.raises(_Crash):
            finalise(path, CFG)
        result = finalise(path, CFG)
        assert _tree(root) == expected, f"crash before file operation {crash_at}"
        assert result.manifest.state is ManifestState.READY


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


def test_a_video_that_cannot_move_for_another_reason_is_reported_at_once(tmp_path, monkeypatch):
    path = _session(tmp_path, RECORDS)
    video = tmp_path / "_incoming" / f"{OBS_STEM}.mkv"
    real = os.replace

    def cross_device(src, dst, *args, **kwargs):
        if Path(src) == video:
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", cross_device)
    waits: list[float] = []
    with pytest.raises(FinaliseError):
        finalise(path, CFG, sleep=waits.append)
    assert waits == []  # only a lock is worth waiting for
    assert load_manifest(path).state is ManifestState.FINALISE_PENDING


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
