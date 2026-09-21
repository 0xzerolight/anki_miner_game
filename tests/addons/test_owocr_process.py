"""``OwocrProcess``: owocr's process tree and its log (spec 14 supervisor; M0 R3 kill order).

Every process here is ``tests/fakes/fake_owocr.py`` under this venv's Python, never the real owocr.
The POSIX tests need ``killpg``; the job-object test runs on the CI Windows runner.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from anki_miner_game.addons import ocr_addon
from anki_miner_game.addons.ocr_addon import LogEvent, LogKind, OwocrProcess, spawn_owocr

FAKE = Path(__file__).parent.parent / "fakes" / "fake_owocr.py"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "owocr"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


def _env(**plan) -> dict[str, str]:
    return {**os.environ, "FAKE_OWOCR_PLAN": json.dumps(plan)}


async def _spawn(**plan) -> OwocrProcess:
    return await spawn_owocr([sys.executable, str(FAKE), "-wp", "0"], _env(**plan))


async def _record(path: Path) -> dict:
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _win_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie waiting for its new parent to reap it is already dead
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _win_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


async def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def spawned():
    """Processes a test started; each tree is killed afterwards whatever the test did."""
    procs: list[OwocrProcess] = []
    yield procs
    for proc in procs:
        await proc.kill_tree(grace_s=0.1)


async def _events(proc: OwocrProcess) -> list[LogEvent]:
    events = []
    async with asyncio.timeout(10):
        while (event := await proc.next_event()) is not None:
            events.append(event)
    return events


# --- log ---------------------------------------------------------------------------------------------


async def test_events_come_from_the_log_and_end_after_exit(spawned):
    proc = await _spawn(log_file=str(FIXTURES / "synthetic-window-picker.log"), exit=0)
    spawned.append(proc)
    assert await _events(proc) == [LogEvent(LogKind.WINDOW_COORDINATES, "0,540,1280,720")]
    assert await proc.wait() == 0
    assert proc.fatal is None
    assert proc.last_message == "Selected window coordinates: 0,540,1280,720"


async def test_a_config_error_is_kept_as_fatal(spawned):
    proc = await _spawn(log_file=str(FIXTURES / "linux-x11-off-screen-rect.log"), exit=1)
    spawned.append(proc)
    assert await _events(proc) == [LogEvent(LogKind.CONFIG_ERROR, "Invalid coordinate set(s) in screen_capture_area")]
    assert await proc.wait() == 1
    assert proc.fatal == "Invalid coordinate set(s) in screen_capture_area"
    assert proc.last_message == "Terminated!"


async def test_an_overlong_line_is_skipped_not_fatal(spawned, monkeypatch):
    monkeypatch.setattr(ocr_addon, "LINE_LIMIT", 1024)
    proc = await _spawn(log=["10:00:00 | " + "x" * 5000, "10:00:01 | Selected coordinates: 1,2,3,4"], exit=0)
    spawned.append(proc)
    assert await _events(proc) == [LogEvent(LogKind.COORDINATES, "1,2,3,4")]


async def test_the_log_ends_after_exit_even_while_an_orphan_holds_the_pipe(spawned, monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_addon, "LOG_DRAIN_S", 0.2)
    record = tmp_path / "record.json"
    proc = await _spawn(record=str(record), grandchild=True, log=["10:00:00 | No engines available!"], exit=1)
    spawned.append(proc)
    grandchild = (await _record(record))["grandchild"]
    assert await _events(proc) == [LogEvent(LogKind.CONFIG_ERROR, "No engines available!")]
    assert _alive(grandchild), "the orphan should still be there until the tree is killed"
    await proc.kill_tree(grace_s=0.2)
    assert await _gone(grandchild)


# --- killing the tree (POSIX process group) ------------------------------------------------------------


@posix_only
async def test_kill_tree_takes_the_grandchild_that_ignores_sigterm(spawned, tmp_path):
    record = tmp_path / "record.json"
    proc = await _spawn(record=str(record), grandchild=True)
    spawned.append(proc)
    pids = await _record(record)
    assert pids["pid"] == proc.pid
    assert os.getpgid(pids["grandchild"]) == proc.pid, "owocr leads its own group; its children stay in it"
    await proc.kill_tree(grace_s=0.3)
    assert proc.returncode == -15, "SIGTERM first"
    assert await _gone(pids["pid"])
    assert await _gone(pids["grandchild"]), "SIGKILL to the group after the grace period"


@posix_only
async def test_kill_tree_stops_at_sigterm_when_that_is_enough(spawned, tmp_path):
    record = tmp_path / "record.json"
    proc = await _spawn(record=str(record))
    spawned.append(proc)
    await _record(record)
    started = time.monotonic()
    await proc.kill_tree(grace_s=5.0)
    assert time.monotonic() - started < 2.0
    assert proc.returncode == -15


@posix_only
async def test_a_cancelled_kill_still_kills_the_group(spawned, tmp_path):
    record = tmp_path / "record.json"
    proc = await _spawn(record=str(record), grandchild=True)
    spawned.append(proc)
    grandchild = (await _record(record))["grandchild"]
    kill = asyncio.create_task(proc.kill_tree(grace_s=30.0))
    await asyncio.sleep(0.2)
    kill.cancel()
    with pytest.raises(asyncio.CancelledError):
        await kill
    assert await _gone(grandchild)


async def test_kill_tree_twice_and_after_exit_is_harmless(spawned):
    proc = await _spawn(exit=3)
    spawned.append(proc)
    assert await proc.wait() == 3
    await proc.kill_tree(grace_s=0.1)
    await proc.kill_tree(grace_s=0.1)
    assert proc.returncode == 3
    assert await proc.next_event() is None


# --- killing the tree (Windows job object) ---------------------------------------------------------------


@pytest.mark.windows_only
@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows")
async def test_job_object_takes_the_whole_tree(spawned, tmp_path):
    record = tmp_path / "record.json"
    proc = await _spawn(record=str(record), grandchild=True, log=["10:00:00 | Selected coordinates: 1,2,3,4"])
    spawned.append(proc)
    pids = await _record(record)
    assert await proc.next_event() == LogEvent(LogKind.COORDINATES, "1,2,3,4"), "resumed after joining the job"
    await proc.kill_tree()
    assert await _gone(pids["pid"])
    assert await _gone(pids["grandchild"])
