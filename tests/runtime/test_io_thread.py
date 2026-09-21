"""The I/O thread (spec 4.2): one asyncio loop in a QThread."""

import asyncio
import sys
import threading

import pytest

from anki_miner_game.runtime.io_thread import IoThread


@pytest.fixture
def io(qapp):
    thread = IoThread()
    thread.start_loop()
    yield thread
    thread.stop_loop()


def test_coroutines_run_on_the_io_thread_not_the_callers(io):
    async def where() -> tuple[int, bool]:
        return threading.get_ident(), asyncio.get_running_loop() is io.loop

    ident, on_loop = io.submit(where()).result(timeout=5)
    assert on_loop
    assert ident != threading.get_ident()


def test_the_io_thread_is_named_in_the_log(io):
    async def name() -> str:
        return threading.current_thread().name

    assert io.submit(name()).result(timeout=5) == "io"


def test_the_loop_can_run_subprocesses(io):
    """owocr and uv run as asyncio subprocesses (a selector loop on Windows cannot)."""

    async def run() -> bytes:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return out

    assert io.submit(run()).result(timeout=20).strip() == b"ok"


def test_stop_cancels_what_is_left_and_closes_the_loop(qapp):
    io = IoThread()
    loop = io.start_loop()
    running = threading.Event()
    cleaned = threading.Event()

    async def forever() -> None:
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    io.submit(forever())
    assert running.wait(5)
    io.stop_loop()
    assert cleaned.is_set()
    assert loop.is_closed()
    assert not io.isRunning()


def test_stop_waits_for_worker_threads_the_loop_started(qapp):
    io = IoThread()
    io.start_loop()
    finished = threading.Event()
    started = threading.Event()

    def blocking() -> None:
        started.set()
        threading.Event().wait(0.2)
        finished.set()

    async def spawn() -> None:
        asyncio.get_running_loop().run_in_executor(None, blocking)

    io.submit(spawn()).result(timeout=5)
    assert started.wait(5)
    io.stop_loop()
    assert finished.is_set()
