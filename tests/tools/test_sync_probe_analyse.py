"""tools/sync_probe/analyse.py: --raw (candidate zero events) and --app (cue starts) comparisons."""

import json

import pytest

from tests.tools.lavfi import make_flash_video, needs_ffmpeg
from tools import obs_transcript_recorder as rec
from tools.sync_probe import analyse as an


def _frame(t: float, direction: str, msg: dict, conn: int = 1) -> dict:
    return rec.frame_record(t, conn, direction, json.dumps(msg))


def _request(t: float, rtype: str) -> dict:
    return _frame(t, rec.TO_OBS, {"op": 6, "d": {"requestType": rtype, "requestId": "1"}})


def _response(t: float, rtype: str) -> dict:
    status = {"result": True, "code": 100}
    return _frame(t, rec.FROM_OBS, {"op": 7, "d": {"requestType": rtype, "requestId": "1", "requestStatus": status}})


def _state(t: float, state: str, conn: int = 2) -> dict:
    data = {"outputActive": state not in ("STOPPED",), "outputState": f"OBS_WEBSOCKET_OUTPUT_{state}"}
    return _frame(t, rec.FROM_OBS, {"op": 5, "d": {"eventType": "RecordStateChanged", "eventData": data}}, conn)


SESSION = [
    {"t_mono": 99.0, "conn": 2, "event": "open", "subprotocol": None, "upstream": "ws://127.0.0.1:4455"},
    _request(99.90, "StartRecord"),
    _state(99.95, "STARTING"),
    _response(99.99, "StartRecord"),
    _state(100.00, "STARTED"),
    _state(100.00, "STARTED", conn=3),  # a second event client sees the same event
    _state(102.00, "PAUSED"),
    _state(102.50, "RESUMED"),
    _state(106.00, "STOPPING"),
    _state(106.10, "STOPPED"),
]

FLASHES = [
    an.FlashEvent(0, 101.0, "sync probe flash 000"),
    an.FlashEvent(1, 103.5, "sync probe flash 001"),
    an.FlashEvent(2, 102.2, "sync probe flash 002"),  # during the pause: never recorded
    an.FlashEvent(3, 107.0, "sync probe flash 003"),  # after the stop
]


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


# --- transcript and flasher log --------------------------------------------------------------


def test_the_timeline_takes_every_candidate_zero_and_the_pauses_from_the_transcript(tmp_path):
    timeline = an.load_obs_timeline(_write_jsonl(tmp_path / "t.jsonl", SESSION))
    assert timeline.candidates == {"request": 99.90, "starting": 99.95, "response": 99.99, "started": 100.00}
    assert timeline.pauses == [(102.0, 102.5)]
    assert timeline.end == 106.0


def test_a_later_recording_in_the_same_transcript_is_selected_by_index(tmp_path):
    second = [
        _request(200.0, "StartRecord"),
        _state(200.05, "STARTING"),
        _response(200.06, "StartRecord"),
        _state(200.1, "STARTED"),
        _state(203.0, "STOPPING"),
    ]
    timeline = an.load_obs_timeline(_write_jsonl(tmp_path / "t.jsonl", SESSION + second), session=1)
    assert timeline.candidates["started"] == 200.1
    assert timeline.pauses == []
    assert timeline.end == 203.0


def test_a_transcript_without_that_recording_is_an_error(tmp_path):
    with pytest.raises(an.AnalysisError, match="StartRecord"):
        an.load_obs_timeline(_write_jsonl(tmp_path / "t.jsonl", SESSION), session=1)


def test_the_flasher_log_yields_the_flashes_in_order(tmp_path):
    path = _write_jsonl(
        tmp_path / "f.jsonl",
        [
            {"kind": "start", "t_mono": 100.5, "port": 1234, "fps": 30, "frames": 3},
            {"kind": "flash", "index": 0, "t_mono": 101.0, "text": "sync probe flash 000", "clients": 0},
            {"kind": "flash", "index": 1, "t_mono": 103.5, "text": "sync probe flash 001", "clients": 0},
        ],
    )
    assert an.load_flasher_log(path) == FLASHES[:2]


# --- the arithmetic ----------------------------------------------------------------------------


def test_paused_time_before_a_moment():
    pauses = [(102.0, 102.5), (104.0, None)]
    assert an.paused_ms(101.0, pauses) == 0
    assert an.paused_ms(103.0, pauses) == pytest.approx(500)
    assert an.paused_ms(102.2, pauses) is None  # inside a pause: not recorded
    assert an.paused_ms(105.0, pauses) is None  # after a pause that never ended


def test_raw_mode_gives_each_candidate_its_latency_and_spread():
    timeline = an.ObsTimeline(
        candidates={"request": 99.90, "starting": 99.95, "response": 99.99, "started": 100.00},
        pauses=[(102.0, 102.5)],
        end=106.0,
    )
    report = an.analyse_raw(FLASHES, timeline, [1100, 3133])
    started = report["started"]
    assert [(m.flash_index, m.predicted_ms, m.measured_ms) for m in started.matches] == [
        (0, 1000, 1100),
        (1, 3000, 3133),
    ]
    assert started.errors_ms == [100, 133]
    assert started.median_ms == pytest.approx(116.5)
    assert started.spread_ms == 33
    assert started.max_abs_residual_ms == pytest.approx(16.5)
    assert started.not_recorded == [2, 3]
    assert started.unmatched == []
    assert report["starting"].errors_ms == [50, 83]
    assert report["response"].errors_ms == [90, 123]
    assert report["request"].errors_ms == [0, 33]


def test_raw_mode_reports_a_flash_without_a_white_frame_nearby():
    timeline = an.ObsTimeline(candidates={"started": 100.0}, pauses=[], end=None)
    report = an.analyse_raw(FLASHES[:2], timeline, [1100], window_ms=500)
    assert report["started"].errors_ms == [100]
    assert report["started"].unmatched == [1]


def test_raw_mode_uses_each_detection_once():
    timeline = an.ObsTimeline(candidates={"started": 100.0}, pauses=[], end=None)
    flashes = [an.FlashEvent(0, 101.0, "a"), an.FlashEvent(1, 101.2, "b")]
    report = an.analyse_raw(flashes, timeline, [1150], window_ms=500)
    assert [m.flash_index for m in report["started"].matches] == [0]
    assert report["started"].unmatched == [1]


def test_app_mode_passes_when_every_cue_starts_within_150_ms_of_its_flash():
    cues = [(1100, "sync probe flash 000"), (3120, "sync probe flash 001")]
    result = an.analyse_app(cues, [1100, 3133])
    assert result.passed
    assert [p.diff_ms for p in result.pairs] == [0, -13]


@pytest.mark.parametrize(
    ("cues", "detections"),
    [
        ([(1100, "a"), (3300, "b")], [1100, 3133]),  # 167 ms late
        ([(1100, "a"), (2983, "b")], [1100, 3133]),  # 150 ms early: the bound is strict
        ([(1100, "a")], [1100, 3133]),  # a flash without a cue
        ([(1100, "a"), (2000, "b"), (3133, "c")], [1100, 3133]),  # a cue without a flash
    ],
)
def test_app_mode_fails_otherwise(cues, detections):
    assert not an.analyse_app(cues, detections).passed


def test_app_mode_reads_cues_from_an_srt(tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text(
        "1\n00:00:01,100 --> 00:00:03,000\nsync probe flash 000\n\n"
        "2\n01:00:03,120 --> 01:00:05,000\nsync probe flash 001\n",
        encoding="utf-8",
    )
    assert an.load_cues(srt) == [(1100, "sync probe flash 000"), (3_603_120, "sync probe flash 001")]


# --- end to end on synthetic recordings ----------------------------------------------------


def _flasher_log(tmp_path):
    return _write_jsonl(
        tmp_path / "flasher.jsonl",
        [{"kind": "start", "t_mono": 100.5, "port": 0, "fps": 30, "frames": 3}]
        + [{"kind": "flash", "index": f.index, "t_mono": f.t_mono, "text": f.text, "clients": 1} for f in FLASHES],
    )


@needs_ffmpeg
def test_cli_raw_mode_on_a_synthetic_recording(tmp_path, capsys):
    # STARTED at 100.0 and 100 ms of capture latency: flashes at 101.0 and 103.5 (after a
    # 0.5 s pause) land at 1.1 s and 3.1 s of the recording.
    video = make_flash_video(tmp_path / "rec.mkv", [1.1, 3.1], duration_s=5)
    transcript = _write_jsonl(tmp_path / "t.jsonl", SESSION)
    out = tmp_path / "report.json"
    argv = ["--raw", "--recording", str(video), "--flasher-log", str(_flasher_log(tmp_path))]
    argv += ["--obs-transcript", str(transcript), "--json", str(out)]
    assert an.main(argv) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "raw"
    assert report["candidates"]["started"]["errors_ms"] == [100, 100]
    assert report["candidates"]["starting"]["median_ms"] == 50
    assert report["candidates"]["started"]["not_recorded"] == [2, 3]
    assert "started" in capsys.readouterr().out


@needs_ffmpeg
def test_cli_app_mode_passes_and_fails_on_a_synthetic_recording(tmp_path):
    video = make_flash_video(tmp_path / "rec.mkv", [1.1, 3.1], duration_s=5)
    good, late = tmp_path / "good.srt", tmp_path / "late.srt"
    good.write_text(
        "1\n00:00:01,050 --> 00:00:02,000\nsync probe flash 000\n\n"
        "2\n00:00:03,200 --> 00:00:04,000\nsync probe flash 001\n",
        encoding="utf-8",
    )
    late.write_text(
        "1\n00:00:01,050 --> 00:00:02,000\nsync probe flash 000\n\n"
        "2\n00:00:03,300 --> 00:00:04,000\nsync probe flash 001\n",
        encoding="utf-8",
    )
    assert an.main(["--app", "--recording", str(video), "--srt", str(good)]) == 0
    assert an.main(["--app", "--recording", str(video), "--srt", str(late)]) == 1


def test_cli_needs_exactly_one_mode(capsys):
    with pytest.raises(SystemExit) as exc:
        an.main(["--recording", "x.mkv"])
    assert exc.value.code == 2
