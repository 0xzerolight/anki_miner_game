"""Sync-probe flasher (spec 18.3): a black window that flashes white while a line is sent.

At each scripted moment the window turns white for ``--frames`` frames (at the recording's
``--fps``) and, at the same instant, the line ``sync probe flash NNN`` goes to every client of a
hooker-style websocket server on ``127.0.0.1:--port``, the way Textractor's websocket plugin sends
text. The app under test connects to that server as it would to a hooker. Each flash is logged
to JSONL with the ``time.monotonic()`` read at the moment it was triggered::

    python -m tools.sync_probe.flasher --log FLASHER.jsonl --port 6677 --count 10 --interval 3

Run it inside the nested display (``tools/nested_display.py``) so OBS can capture the window with
``xcomposite_input``; the window title is ``--title`` (default ``amg-sync-probe``).

Log records: ``{"kind": "start", "t_mono", "port", "fps", "frames", "schedule", "title"}``, then
``{"kind": "flash", "index", "t_mono", "text", "clients"}`` per flash, then ``{"kind": "end", ...}``.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import random
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, TextIO, TypeVar

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget
from websockets.asyncio.server import Server, ServerConnection, broadcast, serve

T = TypeVar("T")
MIN_SEPARATION_S = 1.0  # the analyser pairs within a window; closer flashes would be ambiguous


def flash_schedule(*, count: int, interval_s: float, initial_delay_s: float, jitter_s: float, seed: int) -> list[float]:
    """Flash times in seconds after start: ``initial + i * interval + U(0, jitter)``.

    The jitter spreads the flashes over the capture frame period, so the measured latency is an
    average over phases rather than one phase repeated.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if interval_s - jitter_s < MIN_SEPARATION_S:
        raise ValueError(f"flashes must stay at least {MIN_SEPARATION_S:g} s apart (interval - jitter)")
    rng = random.Random(seed)
    return [initial_delay_s + i * interval_s + (rng.uniform(0, jitter_s) if jitter_s else 0.0) for i in range(count)]


def line_text(index: int) -> str:
    """The line sent with flash ``index``: letters (not dropped), fixed width (never a prefix)."""
    return f"sync probe flash {index:03d}"


class HookerServer:
    """Websocket server on loopback that sends each line to every connected client.

    The asyncio loop runs in its own thread; ``broadcast`` and ``client_count`` hop onto it, since
    the server's connection set may only be read on the loop thread.
    """

    def __init__(self, port: int = 0) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self._loop = asyncio.new_event_loop()
        self._server: Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self, timeout_s: float = 5.0) -> int:
        self._thread = threading.Thread(target=self._run, name="sync-probe-hooker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise TimeoutError("hooker server did not start")
        if self._error is not None:
            raise self._error
        return self.port

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._server = self._loop.run_until_complete(self._open())
        except OSError as exc:
            self._error = exc
            self._ready.set()
            self._loop.close()
            return
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()
        self._server.close()
        self._loop.run_until_complete(self._server.wait_closed())
        self._loop.close()

    async def _open(self) -> Server:
        return await serve(self._handler, self.host, self.port, ping_interval=None)

    async def _handler(self, connection: ServerConnection) -> None:
        await connection.wait_closed()  # hookers only send; anything the client sends is ignored

    def _call(self, fn: Callable[[], T]) -> T:
        future: concurrent.futures.Future[T] = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(fn())
            except BaseException as exc:  # handed to the caller's thread
                future.set_exception(exc)

        self._loop.call_soon_threadsafe(run)
        return future.result(timeout=5)

    def _connections(self) -> set[ServerConnection]:
        assert self._server is not None
        return self._server.connections

    def broadcast(self, text: str) -> int:
        """Send ``text`` to every connected client; return how many there were."""

        def send() -> int:
            connections = self._connections()
            broadcast(connections, text)
            return len(connections)

        return self._call(send)

    def client_count(self) -> int:
        return self._call(lambda: len(self._connections()))

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)


class FlasherWindow(QWidget):
    """Black window that turns white for ``frames / fps`` seconds at each scheduled moment."""

    finished = pyqtSignal()
    colour_changed = pyqtSignal(bool)  # True = white

    def __init__(
        self,
        schedule: Sequence[float],
        *,
        fps: float,
        frames: int,
        on_flash: Callable[[int, float], None],
        now: Callable[[], float] = time.monotonic,
        title: str = "amg-sync-probe",
        size: tuple[int, int] = (640, 360),
        tail_s: float = 1.0,
    ) -> None:
        super().__init__()
        self._schedule = list(schedule)
        self._flash_ms = max(1, round(frames * 1000 / fps))
        self._on_flash = on_flash
        self._now = now
        self._tail_ms = round(tail_s * 1000)
        self._white = False
        self.setWindowTitle(title)
        self.setFixedSize(*size)
        self.setAutoFillBackground(True)
        self._paint(False)

    def is_white(self) -> bool:
        return self._white

    def _paint(self, white: bool) -> None:
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(Qt.GlobalColor.white if white else Qt.GlobalColor.black))
        self.setPalette(palette)
        self._white = white
        self.repaint()  # synchronous: the frame is out before the line is sent
        self.colour_changed.emit(white)

    def start(self) -> float:
        """Schedule every flash relative to now; return the start ``t_mono``."""
        t0 = self._now()
        for index, offset in enumerate(self._schedule):
            QTimer.singleShot(
                max(0, round(offset * 1000)), Qt.TimerType.PreciseTimer, partial(self._flash, index, t0 + offset)
            )
        if not self._schedule:
            QTimer.singleShot(0, self.finished.emit)
        return t0

    def _flash(self, index: int, due: float) -> None:
        wait = due - self._now()
        if wait > 0.0005:  # a coarse wake-up: never flash early
            QTimer.singleShot(max(1, round(wait * 1000)), Qt.TimerType.PreciseTimer, partial(self._flash, index, due))
            return
        t = self._now()
        self._paint(True)
        self._on_flash(index, t)
        last = index == len(self._schedule) - 1
        QTimer.singleShot(self._flash_ms, Qt.TimerType.PreciseTimer, partial(self._end_flash, last))

    def _end_flash(self, last: bool) -> None:
        self._paint(False)
        if last:
            QTimer.singleShot(self._tail_ms, Qt.TimerType.PreciseTimer, self.finished.emit)


class Probe:
    """Sends each flash's line and logs it."""

    def __init__(self, server: HookerServer, log: TextIO, now: Callable[[], float] = time.monotonic) -> None:
        self._server = server
        self._log = log
        self._now = now

    def _write(self, record: dict[str, Any]) -> None:
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()

    def log_start(self, window: FlasherWindow, *, schedule: Sequence[float], fps: float, frames: int) -> None:
        self._write(
            {
                "kind": "start",
                "t_mono": self._now(),
                "port": self._server.port,
                "fps": fps,
                "frames": frames,
                "schedule": list(schedule),
                "title": window.windowTitle(),
            }
        )

    def on_flash(self, index: int, t_mono: float) -> None:
        # Runs in a Qt slot, where an escaping exception aborts the process: log it instead,
        # so the flashes (and their timestamps) go on.
        text = line_text(index)
        record: dict[str, Any] = {"kind": "flash", "index": index, "t_mono": t_mono, "text": text}
        try:
            record["clients"] = self._server.broadcast(text)
        except Exception as exc:
            record |= {"clients": None, "error": f"{type(exc).__name__}: {exc}"}
        self._write(record)

    def log_end(self) -> None:
        self._write({"kind": "end", "t_mono": self._now()})


def _size(text: str) -> tuple[int, int]:
    width, sep, height = text.partition("x")
    if not sep or not width.isdigit() or not height.isdigit():
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}")
    return int(width), int(height)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="flasher", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--log", required=True, help="JSONL log to create")
    parser.add_argument("--port", type=int, default=0, help="hooker websocket port on 127.0.0.1 (0 = any)")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--interval", type=float, default=3.0, help="seconds between flashes")
    parser.add_argument("--initial-delay", type=float, default=10.0, help="seconds before the first flash")
    parser.add_argument("--jitter", type=float, default=0.1, help="random extra delay per flash, seconds")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--fps", type=float, default=30.0, help="recording frame rate")
    parser.add_argument("--frames", type=int, default=3, help="white frames per flash")
    parser.add_argument("--size", type=_size, default=(640, 360), metavar="WxH")
    parser.add_argument("--title", default="amg-sync-probe")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    schedule = flash_schedule(
        count=args.count,
        interval_s=args.interval,
        initial_delay_s=args.initial_delay,
        jitter_s=args.jitter,
        seed=args.seed,
    )
    app = QApplication(sys.argv[:1])
    server = HookerServer(args.port)
    server.start()
    try:
        with open(args.log, "x", encoding="utf-8") as log:
            probe = Probe(server, log)
            window = FlasherWindow(
                schedule, fps=args.fps, frames=args.frames, on_flash=probe.on_flash, title=args.title, size=args.size
            )
            window.finished.connect(app.quit)
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: app.quit())
            wake = QTimer()  # lets Python run its signal handlers while Qt's loop runs
            wake.start(200)
            window.show()
            probe.log_start(window, schedule=schedule, fps=args.fps, frames=args.frames)
            print(
                f"flasher: ws://127.0.0.1:{server.port}, {len(schedule)} flashes from {schedule[0]:.1f} s",
                file=sys.stderr,
                flush=True,
            )
            window.start()
            rc = app.exec()
            probe.log_end()
    finally:
        server.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
