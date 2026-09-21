"""The worker's failure report when the add-on environment is broken; runs in the gate."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "anki_miner_game" / "vad" / "worker" / "vad_worker.py"


@pytest.mark.skipif(
    all(importlib.util.find_spec(name) for name in ("av", "numpy", "onnxruntime")),
    reason="this interpreter has the add-on's packages",
)
def test_missing_packages_are_reported_as_one_error_line(tmp_path):
    proc = subprocess.run(
        [sys.executable, WORKER, "--video", tmp_path / "session.mkv", "--model", tmp_path / "model.onnx"],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert proc.stdout.endswith(b"\n") and proc.stdout.count(b"\n") == 1
    message = json.loads(proc.stdout.decode("ascii"))
    assert message["t"] == "error"
    assert message["message"].startswith("RuntimeError: the VAD add-on environment is incomplete: No module named")
