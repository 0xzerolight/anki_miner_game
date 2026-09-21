"""The gate runs the project venv's tools."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = ("python", "black", "ruff", "mypy", "pytest")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="health.sh is a POSIX shell script")


def _copy_gate(root: Path) -> Path:
    script = root / "scripts" / "health.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "health.sh", script)
    return script


def _run_gate(script: Path, log: Path) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "TOOL_LOG": str(log)},
        check=False,
    )


@posix_only
def test_gate_refuses_to_run_without_the_project_venv(tmp_path):
    script = _copy_gate(tmp_path)
    result = _run_gate(script, tmp_path / "tools.log")
    assert result.returncode == 2
    assert ".venv" in result.stdout + result.stderr
    assert not (tmp_path / "tools.log").exists()


@posix_only
def test_gate_runs_every_tool_from_the_venv(tmp_path):
    script = _copy_gate(tmp_path)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in TOOLS:
        fake = bin_dir / tool
        fake.write_text('#!/bin/sh\necho "$0 $*" >> "$TOOL_LOG"\n', encoding="utf-8")
        fake.chmod(0o755)
    log = tmp_path / "tools.log"
    result = _run_gate(script, log)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert [Path(call.split()[0]).name for call in calls] == ["black", "ruff", "mypy", "pytest"]
    assert all(call.startswith((".venv/bin/", "./.venv/bin/")) for call in calls), calls
