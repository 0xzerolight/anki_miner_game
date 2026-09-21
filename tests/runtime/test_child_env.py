"""The environment of the programs the app starts: a frozen Linux build gives them back the
``LD_LIBRARY_PATH`` the PyInstaller bootloader replaced (PyInstaller "LD_LIBRARY_PATH / LIBPATH
considerations"), and never changes the app's own."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from anki_miner_game.addons.ocr_addon import OcrAddon
from anki_miner_game.addons.vad_addon import VadAddon
from anki_miner_game.models.config import AppConfig
from anki_miner_game.obs.discovery import SubprocessRunner
from anki_miner_game.runtime.child_env import child_environ
from anki_miner_game.vad.trimmer import VadTrimmer

BUNDLE = "/opt/app/_internal"
ORIGINAL = "/usr/local/lib/mine"

posix_only = pytest.mark.skipif(
    sys.platform in ("win32", "darwin"), reason="the bootloader sets LD_LIBRARY_PATH on Linux"
)


def test_a_frozen_build_gives_children_the_original_library_path():
    env = child_environ(
        {"LD_LIBRARY_PATH": BUNDLE, "LD_LIBRARY_PATH_ORIG": ORIGINAL, "A": "1"}, frozen=True, platform="linux"
    )
    assert env["LD_LIBRARY_PATH"] == ORIGINAL
    assert env["A"] == "1"


def test_a_frozen_build_without_an_original_drops_the_bundle_path():
    env = child_environ({"LD_LIBRARY_PATH": BUNDLE}, frozen=True, platform="linux")
    assert "LD_LIBRARY_PATH" not in env


def test_an_unfrozen_run_passes_the_environment_on_unchanged():
    given = {"LD_LIBRARY_PATH": ORIGINAL, "LD_LIBRARY_PATH_ORIG": "/elsewhere"}
    assert child_environ(given, frozen=False, platform="linux") == given


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_windows_and_macos_are_left_alone(platform):
    given = {"LD_LIBRARY_PATH": BUNDLE}
    assert child_environ(given, frozen=True, platform=platform) == given


def test_the_default_is_a_copy_of_the_apps_environment(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", ORIGINAL)
    env = child_environ(platform="linux")
    assert env["LD_LIBRARY_PATH"] == ORIGINAL
    assert os.environ["LD_LIBRARY_PATH"] == BUNDLE  # the app keeps its own search path


# Every place the app starts a program ------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch):
    """A frozen Linux build: the bootloader put the bundle first and kept the original."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", BUNDLE)
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", ORIGINAL)


class _RefusedError(OSError):
    pass


@pytest.fixture
def popen_envs(monkeypatch) -> list[dict[str, str] | None]:
    """Every ``subprocess.Popen`` records its ``env`` and fails to start (``subprocess.run`` goes through it)."""
    envs: list[dict[str, str] | None] = []

    def refuse(*_args: Any, **kwargs: Any) -> None:
        envs.append(kwargs.get("env"))
        raise _RefusedError("not in a test")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    return envs


@posix_only
def test_obs_and_discoverys_helpers_start_with_the_original_path(frozen, popen_envs):
    runner = SubprocessRunner()
    assert runner.run(["flatpak", "info", "com.obsproject.Studio"]) == (-1, "")
    with pytest.raises(_RefusedError):
        runner.spawn(["obs", "--minimize-to-tray"], None)
    assert [env and env.get("LD_LIBRARY_PATH") for env in popen_envs] == [ORIGINAL, ORIGINAL]


@posix_only
def test_owocr_starts_with_the_original_path(frozen, tmp_path):
    env = OcrAddon(tmp_path, platform="linux").owocr_environment()
    assert env["LD_LIBRARY_PATH"] == ORIGINAL


@posix_only
def test_the_vad_add_ons_uv_starts_with_the_original_path(frozen, tmp_path):
    seen: list[dict[str, str]] = []

    async def run_uv(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str]:
        seen.append(dict(env))
        return 0, ""

    asyncio.run(VadAddon(tmp_path, run_uv=run_uv)._uv(tmp_path / "uv", "venv", []))
    assert seen[0]["LD_LIBRARY_PATH"] == ORIGINAL
    assert seen[0]["UV_NO_CONFIG"] == "1"  # the add-on's own uv settings still apply


@posix_only
def test_the_vad_worker_starts_with_the_original_path(frozen, popen_envs, tmp_path):
    class Runtime:
        python_path = tmp_path / "python"
        model_path = tmp_path / "model.onnx"

    trimmer = VadTrimmer(Runtime(), presenter=None, config=AppConfig)  # type: ignore[arg-type]
    try:
        with pytest.raises(Exception, match="could not start"):
            trimmer._run_worker(tmp_path / "x.session.json", tmp_path / "x.mkv")
    finally:
        trimmer.close()
    assert popen_envs[0] is not None and popen_envs[0]["LD_LIBRARY_PATH"] == ORIGINAL
