"""The PyInstaller spec and the bundle smoke script (spec 19): a one-folder build that carries the data
files the code opens beside its modules and excludes the add-ons' packages, and a smoke that launches
it offscreen in an isolated home and fails on any miss."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki_miner_game.addons.vad_addon import REQUIREMENTS
from anki_miner_game.feed.http_server import PAGE_PATH
from anki_miner_game.runtime.bundle_smoke import ABSENT_MODULES, FAIL_MARKER, PASS_MARKER, SMOKE_ENV
from anki_miner_game.vad.trimmer import WORKER_SCRIPT

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "anki_miner_game.spec"
SMOKE_SCRIPT = REPO / "scripts" / "bundle_smoke.sh"
APP_NAME = "AnkiMinerGame"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake bundle is a POSIX shell script")


# --- the spec -----------------------------------------------------------------------------------


def run_spec() -> dict[str, SimpleNamespace]:
    """Execute the spec the way PyInstaller does, with its build steps recorded instead of run."""
    calls: dict[str, SimpleNamespace] = {}

    def step(kind: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> SimpleNamespace:
            calls[kind] = SimpleNamespace(args=args, kwargs=kwargs, pure=[], scripts=[], binaries=[], datas=[])
            return calls[kind]

        return record

    namespace = {
        "SPEC": str(SPEC),
        "SPECPATH": str(REPO),
        **{kind: step(kind) for kind in ("Analysis", "PYZ", "EXE", "COLLECT")},
    }
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), namespace)
    return calls


def test_the_spec_builds_one_folder_from_the_entry_point():
    calls = run_spec()
    analysis = calls["Analysis"]
    assert [Path(script).resolve() for script in analysis.args[0]] == [REPO / "anki_miner_game" / "launch.py"]
    assert [Path(path).resolve() for path in analysis.kwargs["pathex"]] == [REPO]
    exe, collect = calls["EXE"], calls["COLLECT"]
    assert exe.kwargs["exclude_binaries"] is True  # binaries go to COLLECT: one folder, not one file
    assert exe.kwargs["name"] == collect.kwargs["name"] == APP_NAME


def test_the_spec_excludes_every_add_on_package():
    excludes = run_spec()["Analysis"].kwargs["excludes"]
    assert set(ABSENT_MODULES) <= set(excludes)


def test_the_spec_keeps_setuptools_out():
    """obsws-python's tomli fallback (Python < 3.11) otherwise makes PyInstaller bundle setuptools' copy of it."""
    assert "setuptools" in run_spec()["Analysis"].kwargs["excludes"]


def test_the_spec_bundles_each_file_the_code_opens_where_the_code_looks_for_it():
    """A frozen module's ``__file__`` sits in the bundle as the module would, so each file keeps its folder."""
    datas = run_spec()["Analysis"].kwargs["datas"]
    bundled = {Path(src).resolve(): Path(dest) for src, dest in datas}
    wanted = [PAGE_PATH, WORKER_SCRIPT, REQUIREMENTS]
    assert set(bundled) == {path.resolve() for path in wanted}
    for path in wanted:
        source = path.resolve()
        assert source.is_file()
        assert bundled[source] == source.parent.relative_to(REPO)


def test_every_bundled_data_file_is_also_package_data():
    """A wheel carries the same files as the bundle (``.py`` files ship with their package anyway)."""
    package_data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"][
        "package-data"
    ]
    declared = {
        REPO.joinpath(*package.split("."), name).resolve() for package, names in package_data.items() for name in names
    }
    for src, _dest in run_spec()["Analysis"].kwargs["datas"]:
        source = Path(src).resolve()
        if source.suffix != ".py":
            assert source in declared, source


# --- the smoke script ---------------------------------------------------------------------------

FAKE_APP = """#!/usr/bin/env bash
# Stands in for the frozen app: reports its environment, then behaves as FAKE_MODE says.
env > "$FAKE_REPORT"
mkdir -p "$ANKI_MINER_GAME_HOME"
log="$ANKI_MINER_GAME_HOME/anki_miner_game.log"
case "$FAKE_MODE" in
  pass)      echo '{}' > "$ANKI_MINER_GAME_HOME/config.json"; echo "x PASSMARK: modules, feed, https" > "$log" ;;
  no-config) echo "x PASSMARK: modules, feed, https" > "$log" ;;
  no-log)    echo '{}' > "$ANKI_MINER_GAME_HOME/config.json" ;;
  failed)    echo '{}' > "$ANKI_MINER_GAME_HOME/config.json"; echo "x FAILMARK: https: no route" > "$log"; exit 1 ;;
  silent)    echo '{}' > "$ANKI_MINER_GAME_HOME/config.json"; echo "x started" > "$log" ;;
esac
""".replace("PASSMARK", PASS_MARKER).replace("FAILMARK", FAIL_MARKER)


def fake_bundle(tmp_path: Path) -> Path:
    dist = tmp_path / "dist" / APP_NAME
    (dist / "_internal").mkdir(parents=True)
    app = dist / APP_NAME
    app.write_text(FAKE_APP, encoding="utf-8")
    app.chmod(0o755)
    return dist


def run_smoke(dist: Path, tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    bash = shutil.which("bash")
    assert bash is not None
    report = tmp_path / f"env-{mode}.txt"
    env = dict(os.environ, FAKE_MODE=mode, FAKE_REPORT=str(report))
    result = subprocess.run(
        [bash, str(SMOKE_SCRIPT), str(dist)], capture_output=True, text=True, timeout=60, env=env, check=False
    )
    seen = dict(line.split("=", 1) for line in report.read_text(encoding="utf-8").splitlines() if "=" in line)
    return result, seen


@posix_only
def test_a_passing_bundle_passes_every_check(tmp_path):
    result, _seen = run_smoke(fake_bundle(tmp_path), tmp_path, "pass")
    assert result.returncode == 0, result.stdout + result.stderr
    for check in ("exit", "config.json", "log", "self-check", "absent modules"):
        assert f"PASS {check}" in result.stdout
    assert "FAIL" not in result.stdout


@posix_only
def test_the_app_runs_offscreen_as_the_smoke_in_an_isolated_home_that_is_removed(tmp_path):
    _result, seen = run_smoke(fake_bundle(tmp_path), tmp_path, "pass")
    assert seen["QT_QPA_PLATFORM"] == "offscreen"
    assert seen[SMOKE_ENV] == "1"
    root = Path(seen["ANKI_MINER_GAME_HOME"]).parent
    assert root != Path(os.environ["HOME"]) and not Path(os.environ["HOME"]).is_relative_to(root)
    for var in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(seen[var]).is_relative_to(root), var
    assert not root.exists()


@posix_only
@pytest.mark.parametrize(
    ("mode", "failed"),
    [
        ("no-config", "config.json"),
        ("no-log", "log"),
        ("failed", "exit"),
        ("failed", "self-check"),
        ("silent", "self-check"),
    ],
)
def test_each_miss_fails_its_check_and_the_smoke(tmp_path, mode, failed):
    result, _seen = run_smoke(fake_bundle(tmp_path), tmp_path, mode)
    assert result.returncode != 0
    assert f"FAIL {failed}" in result.stdout


@posix_only
@pytest.mark.parametrize("found", ["numpy", "onnxruntime", "av", "owocr", "numpy.libs", "libonnxruntime.so.1.24"])
def test_an_add_on_package_in_the_bundle_fails_the_smoke(tmp_path, found):
    dist = fake_bundle(tmp_path)
    (dist / "_internal" / found).mkdir()
    result, _seen = run_smoke(dist, tmp_path, "pass")
    assert result.returncode != 0
    assert "FAIL absent modules" in result.stdout
    assert found in result.stdout


@posix_only
def test_a_missing_bundle_is_a_usage_error(tmp_path):
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(
        [bash, str(SMOKE_SCRIPT), str(tmp_path / "nothing")], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 2
