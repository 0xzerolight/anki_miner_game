"""Text source for OCR mode: owocr's websocket, with owocr run and restarted by a supervisor (spec 8.1, 14).

``start`` (at arm) launches owocr through the OCR add-on on a free port and listens to it with a
``WebsocketSource`` on loopback (owocr binds ``0.0.0.0``, spec 3.4); ``stop`` (at disarm) kills
owocr's whole process tree. When owocr exits on its own, the tree's leftovers are killed and owocr is
started again after 1, 2 and 5 s; a fourth exit in a row ends OCR for this arm with a banner, and the
recording goes on (spec 17). An exit on an error that the same command line would hit again (a bad
area, no engine; ``LogKind.CONFIG_ERROR``) gets the banner at once, as do an add-on that is not
installed, a Wayland session and a profile with no OCR area. A game window that is not open
(``LogKind.WINDOW_MISSING``) is restarted like any other exit, and the banner then names it. A run
that lasted ``STABLE_RUN_S`` counts as healthy, so only exits in a row add up.

An owocr that dropped its OCR area because the game window changed size, as at a minimise
(``LogKind.AREA_DISCARDED``), reads the whole window from then on: its frames are dropped, and once
it sends text again, so the window is back on screen, it is restarted with the same arguments,
which gets the area back. That restart is not an exit: it waits for no backoff, spends none of the
three attempts and shows no banner.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from anki_miner_game.addons.ocr_addon import OcrError, OwocrProcess, free_port, window_missing_text
from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.messages import Banner, BannerCleared, BannerLevel, BannerRaised, SourceStatus
from anki_miner_game.models.profile import OcrSettings
from anki_miner_game.text.sources.websocket_source import WebsocketSource

logger = logging.getLogger(__name__)

OCR_SOURCE_ID: Final = "ocr"
BANNER_KEY: Final = "ocr"

BACKOFF_S: Final = (1.0, 2.0, 5.0)
"""Waits before the first, second and third restart; there is no fourth (spec 14: three attempts)."""
STABLE_RUN_S: Final = 60.0
"""A run this long resets the count of exits in a row."""

BannerListener = Callable[[BannerRaised | BannerCleared], None]


class OwocrLauncher(Protocol):
    """What the source needs from ``OcrAddon``."""

    def unavailable_reason(self) -> str | None: ...

    async def launch(self, ocr: OcrSettings, port: int) -> OwocrProcess: ...


class OcrSource:
    """A ``TextSource`` fed by a supervised owocr.

    Lives on the I/O loop (spec 4.2): call ``start`` and ``stop`` there; the sink and the status
    listener run there too. Lines carry ``source_id`` and ``t_mono = now()`` read at frame receipt by
    the inner ``WebsocketSource``. The status is the inner source's, ``DISCONNECTED`` while owocr is
    down. ``on_banner`` hears ``BannerCleared("ocr")`` at every start and ``BannerRaised`` when OCR
    gives up; the composition passes it on to the ``Presenter``. ``sleep`` (the restart waits) and
    ``port_finder`` exist for tests.
    """

    def __init__(
        self,
        launcher: OwocrLauncher,
        settings: OcrSettings,
        *,
        source_id: str = OCR_SOURCE_ID,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_banner: BannerListener | None = None,
        port_finder: Callable[[], int] = free_port,
    ) -> None:
        self._launcher = launcher
        self._settings = settings
        self._id = source_id
        self._now = now
        self._sleep = sleep
        self._on_banner = on_banner
        self._port_finder = port_finder
        self._status = SourceStatus.DISCONNECTED
        self._listener: StatusListener | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped_task: asyncio.Task[None] | None = None
        self._ws: WebsocketSource | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return self._status

    def set_status_listener(self, cb: StatusListener) -> None:
        self._listener = cb

    def start(self, sink: LineSink) -> None:
        if self._task is not None:
            raise RuntimeError(f"text source {self._id!r} is already started")
        self._task = asyncio.get_running_loop().create_task(self._supervise(sink), name=f"text-source-{self._id}")

    def stop(self) -> None:
        """Stop listening at once and kill owocr's tree in the background; ``wait_closed`` waits for it."""
        task, self._task = self._task, None
        if task is None:
            return
        if self._ws is not None:
            self._ws.stop()
        task.cancel()
        self._stopped_task = task
        self._set_status(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        """Return once the owocr tree of the last ``stop`` is dead."""
        if self._stopped_task is not None:
            await asyncio.wait({self._stopped_task})

    async def _supervise(self, sink: LineSink) -> None:
        self._banner(BannerCleared(BANNER_KEY))
        reason = self._launcher.unavailable_reason()
        if reason is not None:
            self._give_up(reason)
            return
        exits = 0
        while True:
            port = self._port_finder()
            started = self._now()
            try:
                proc = await self._launcher.launch(self._settings, port)
            except (OcrError, OSError) as exc:
                self._give_up(f"OCR could not start: {exc}")
                return
            code = await self._run(proc, port, sink)
            if code is None:
                logger.info("owocr lost its OCR area when the game window changed size; restarting it")
                if self._now() - started >= STABLE_RUN_S:
                    exits = 0
                continue
            if proc.fatal is not None:
                self._give_up(f'OCR stopped: owocr reported "{proc.fatal}"')
                return
            exits = 1 if self._now() - started >= STABLE_RUN_S else exits + 1
            logger.warning("owocr exited with code %s (%d in a row): %s", code, exits, proc.last_message)
            if exits > len(BACKOFF_S):
                if proc.window_missing:
                    why = window_missing_text(self._settings.window_title)
                else:
                    why = f"its last message: {proc.last_message}"
                self._give_up(f"OCR stopped: owocr exited {exits} times in a row; {why}")
                return
            await self._sleep(BACKOFF_S[exits - 1])

    async def _run(self, proc: OwocrProcess, port: int, sink: LineSink) -> int | None:
        """Listen to one owocr until it exits, and return its exit code; or, once it has lost its OCR
        area and the window is back on screen (``OwocrProcess.wait_area_recoverable``), until it is
        killed for a restart: ``None``. Its tree is dead when this returns or raises."""

        def lines_only(raw: str, t_mono: float, source_id: str) -> None:
            if not proc.area_lost:  # whole-window text: scene text and name plates, not a line
                sink(raw, t_mono, source_id)

        ws = WebsocketSource(self._id, f"127.0.0.1:{port}", now=self._now)
        ws.set_status_listener(self._forward_status)
        self._ws = ws
        exited = asyncio.ensure_future(proc.wait())
        recoverable = asyncio.ensure_future(proc.wait_area_recoverable())
        try:
            ws.start(lines_only)
            await asyncio.wait({exited, recoverable}, return_when=asyncio.FIRST_COMPLETED)
            return exited.result() if exited.done() else None
        finally:
            exited.cancel()
            recoverable.cancel()
            ws.stop()
            self._ws = None
            await proc.kill_tree()
            await ws.wait_closed()

    def _forward_status(self, source_id: str, status: SourceStatus) -> None:
        if self._task is not None:
            self._set_status(status)

    def _set_status(self, status: SourceStatus) -> None:
        if status == self._status:
            return
        self._status = status
        if self._listener is not None:
            try:
                self._listener(self._id, status)
            except Exception:
                logger.exception("%s: the status listener failed", self._id)

    def _give_up(self, text: str) -> None:
        logger.warning("%s: %s", self._id, text)
        self._set_status(SourceStatus.DISCONNECTED)
        self._banner(BannerRaised(Banner(BANNER_KEY, BannerLevel.WARNING, text)))

    def _banner(self, event: BannerRaised | BannerCleared) -> None:
        if self._on_banner is None:
            return
        try:
            self._on_banner(event)
        except Exception:
            logger.exception("%s: the banner listener failed", self._id)
