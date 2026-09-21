"""The VAD worker end to end, run the way the add-on runs it (spec 13.2, 18.2).

Needs the add-on environment at the repository root (a symlink to the main checkout's in a
worktree): ``.venv-vad`` built from ``anki_miner_game/vad/worker/requirements.txt``, with the model
from ``anki_miner_game/vad/model_pin.py`` saved in it as ``silero_vad_v6.onnx``::

    uv venv --python 3.12 .venv-vad
    uv pip install --python .venv-vad/bin/python -r anki_miner_game/vad/worker/requirements.txt

Run with ``.venv/bin/pytest -m vad tests/vad``; the gate deselects the ``vad`` marker.
"""

import array
import hashlib
import json
import math
import os
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from anki_miner_game.vad import model_pin

pytestmark = pytest.mark.vad

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORKER = REPO / "anki_miner_game" / "vad" / "worker" / "vad_worker.py"
SPEECH_CLIP = REPO / "tests" / "fixtures" / "vad" / "librivox_autumn_excerpt.ogg"
# faster-whisper v1.2.1's get_speech_timestamps on the clip, with the spec 13.2 parameters.
CLIP_REGION_MS = (1244, 4036)
TOLERANCE_MS = 64  # two model windows, room for a decoder or runtime update to move an edge
RSS_CEILING_BYTES = 200 * 1024 * 1024  # 104 MB measured; the 3 h track alone is 346 MB as int16


def _venv_python() -> Path:
    venv = REPO / ".venv-vad"
    return venv / "Scripts" / "python.exe" if sys.platform == "win32" else venv / "bin" / "python"


@pytest.fixture(scope="module")
def vad_python() -> Path:
    python = _venv_python()
    if not python.exists():
        pytest.skip(f"no VAD add-on environment at {python.parent.parent}")
    probe = subprocess.run([python, "-c", "import av, numpy, onnxruntime"], capture_output=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"VAD add-on environment incomplete: {probe.stderr.decode(errors='replace').strip()}")
    return python


@pytest.fixture(scope="module")
def model() -> Path:
    path = REPO / ".venv-vad" / model_pin.MODEL_FILENAME
    if not path.exists():
        pytest.skip(f"no model at {path}: download {model_pin.MODEL_URL}")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == model_pin.MODEL_SHA256
    return path


@dataclass
class Result:
    returncode: int
    messages: list[dict]
    stderr: str

    @property
    def regions(self) -> list[tuple[int, int]]:
        return [(m["start_ms"], m["end_ms"]) for m in self.messages if m["t"] == "region"]

    @property
    def progress(self) -> list[dict]:
        return [m for m in self.messages if m["t"] == "progress"]


_KEYS = {
    "region": {"t", "start_ms", "end_ms"},
    "progress": {"t", "done_ms", "total_ms"},
    "done": {"t"},
    "error": {"t", "message"},
}


def _parse(stdout: bytes) -> list[dict]:
    """The protocol: ASCII, one JSON object per ``\\n``-terminated line, done or error last."""
    assert stdout.endswith(b"\n")
    messages = [json.loads(line) for line in stdout.decode("ascii").split("\n")[:-1]]
    for message in messages:
        assert set(message) == _KEYS[message["t"]], message
    assert [m["t"] for m in messages].count("done") + [m["t"] for m in messages].count("error") == 1
    assert messages[-1]["t"] in ("done", "error")
    return messages


def _run(python: Path, video: Path, model: Path, *extra: str, timeout: float = 120) -> Result:
    proc = subprocess.run(
        [python, WORKER, "--video", video, "--model", model, *extra], capture_output=True, timeout=timeout
    )
    return Result(proc.returncode, _parse(proc.stdout), proc.stderr.decode(errors="replace"))


def _assert_done(result: Result) -> None:
    assert result.returncode == 0, result.messages[-1:] + [result.stderr]
    assert result.messages[-1] == {"t": "done"}


def _tone_wav(path: Path, seconds: int, *, rate: int, channels: int, sampwidth: int) -> Path:
    """A 440 Hz tone at half scale, written one second at a time."""
    if sampwidth == 1:
        second = bytes(
            int(128 + 63 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate) for _ in range(channels)
        )
    else:
        samples = (int(16000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate) for _ in range(channels))
        second = array.array("h", samples).tobytes()  # WAV is little-endian, as every supported host
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(rate)
        for _ in range(seconds):
            out.writeframesraw(second)
    return path


@pytest.fixture(scope="module")
def recording(vad_python: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Video, then a silent audio track 0 and the speech clip as track 1, both from 0.5 s."""
    out = tmp_path_factory.mktemp("mkv") / "session.mkv"
    subprocess.run([vad_python, HERE / "_make_mkv.py", SPEECH_CLIP, out, "0.5"], check=True, timeout=120)
    return out


def _near(actual: tuple[int, int], expected: tuple[int, int]) -> bool:
    return all(abs(a - e) <= TOLERANCE_MS for a, e in zip(actual, expected, strict=True))


def test_tone_yields_no_regions(vad_python, model, tmp_path):
    tone = _tone_wav(tmp_path / "tone.wav", 10, rate=44100, channels=2, sampwidth=2)
    result = _run(vad_python, tone, model)
    _assert_done(result)
    assert result.regions == []
    assert result.progress[-1] == {"t": "progress", "done_ms": 10000, "total_ms": 10000}


def test_speech_clip_yields_one_region(vad_python, model):
    result = _run(vad_python, SPEECH_CLIP, model)
    _assert_done(result)
    assert len(result.regions) == 1
    assert _near(result.regions[0], CLIP_REGION_MS), result.regions


def test_track_counts_audio_tracks_and_regions_keep_the_file_timeline(vad_python, model, recording):
    default = _run(vad_python, recording, model)
    _assert_done(default)
    assert default.regions == []  # track 0 is the silent one, not the video stream

    speech = _run(vad_python, recording, model, "--track", "1")
    _assert_done(speech)
    assert len(speech.regions) == 1
    assert _near(speech.regions[0], (CLIP_REGION_MS[0] + 500, CLIP_REGION_MS[1] + 500)), speech.regions


def test_a_recording_cut_off_mid_write_still_finishes(vad_python, model, recording, tmp_path):
    cut = tmp_path / "cut.mkv"
    cut.write_bytes(recording.read_bytes()[: recording.stat().st_size * 3 // 4])
    _assert_done(_run(vad_python, cut, model, "--track", "1"))


def test_a_lost_span_keeps_later_regions_on_the_file_timeline(vad_python, model, tmp_path):
    clean, damaged = tmp_path / "clean.mkv", tmp_path / "damaged.mkv"
    subprocess.run([vad_python, HERE / "_make_damaged_mkv.py", SPEECH_CLIP, clean, damaged], check=True, timeout=120)
    expected = _run(vad_python, clean, model)
    _assert_done(expected)
    assert len(expected.regions) == 2  # the clip, 5 s of silence, the clip again

    result = _run(vad_python, damaged, model)
    _assert_done(result)
    assert result.regions[0][1] < expected.regions[0][1] - 500  # the damage cut the first line short
    assert _near(result.regions[-1], expected.regions[1]), result.regions


@pytest.mark.skipif(not hasattr(os, "wait4"), reason="peak RSS of one child needs os.wait4")
def test_three_hour_track_stays_under_the_rss_ceiling(vad_python, model, tmp_path):
    hours = 3
    track = _tone_wav(tmp_path / "long.wav", hours * 3600, rate=8000, channels=1, sampwidth=1)
    proc = subprocess.Popen(
        [vad_python, WORKER, "--video", track, "--model", model], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    proc.stdout.close()
    _, status, usage = os.wait4(proc.pid, 0)
    proc.returncode = os.waitstatus_to_exitcode(status)

    result = Result(proc.returncode, _parse(stdout), "")
    _assert_done(result)
    total_ms = hours * 3600 * 1000
    assert result.progress[-1] == {"t": "progress", "done_ms": total_ms, "total_ms": total_ms}
    assert len(result.progress) > 100  # streamed batch by batch, not decoded whole first
    peak_rss_bytes = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    assert peak_rss_bytes < RSS_CEILING_BYTES


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing video", "FileNotFoundError"),
        ("no such track", "no audio track 1"),
        ("not media", "InvalidDataError"),
        ("not a model", "ONNXRuntimeError"),
    ],
)
def test_failures_end_with_one_error_line(vad_python, model, tmp_path, case, expected):
    tone = _tone_wav(tmp_path / "tone.wav", 1, rate=16000, channels=1, sampwidth=2)
    text = tmp_path / "notes.txt"
    text.write_text("not media, not a model\n", encoding="utf-8")
    args = {
        "missing video": (tmp_path / "missing.mkv", model),
        "no such track": (tone, model, "--track", "1"),
        "not media": (text, model),
        "not a model": (tone, text),
    }[case]
    result = _run(vad_python, *args)
    assert result.returncode == 1
    assert result.messages[-1]["t"] == "error"
    assert expected in result.messages[-1]["message"]
    assert result.regions == []
