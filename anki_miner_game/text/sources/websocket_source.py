"""Text source for a hooker's websocket server (spec 3.2, 8.1).

Ported from GameSentenceMiner ``GameSentenceMiner/gametext.py::listen_on_websocket`` (line 629) at
commit 479747fe82d64f66980797a50bd6782ea06f58fa (GPL-3.0). Kept: connect to ``ws://<uri>``, the
LunaTranslator path fallback, ``ping_interval=None``, plain/JSON frame parsing and reconnecting.
Changed: the non-dict JSON guard (GSM calls ``.get`` on any JSON value), the spec's 1, 2, 5, 10 s
backoff, and the JSON ``source`` and ``time`` fields are not used: a line belongs to the configured
source and its time is read here at receipt. Also changed: a JSON object with ``"type":
"translate"`` is dropped, not GSM behaviour but needed for the Agent hooker (0xDC00/agent), whose
default settings send it as a second frame per line carrying its own machine translation
(``libWebSocket.js``: ``broadcast`` fires on both its ``copyText`` and ``translate`` events, the
only two types it ever sends); the ``copyText`` frame beside it already carries the line. Dropped:
rate limiting, overlay, database and pause plumbing.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.messages import SourceStatus

logger = logging.getLogger(__name__)

BACKOFF_S: Final = (1.0, 2.0, 5.0, 10.0)
"""Waits before successive reconnect attempts; the last one repeats. A connection resets it."""

CLOSE_TIMEOUT_S: Final = 1.0
"""How long ``stop`` waits for the hooker to answer the close frame; hookers never answer it, and
websockets' default of 10 s would hold every shutdown and reconfiguration that long."""

LUNA_PATH: Final = "/api/ws/text/origin"
"""Where LunaTranslator serves its text; tried once when the handshake on the plain URI is refused."""


def parse_frame(frame: str | bytes) -> str | None:
    """The line a hooker frame carries, or ``None`` for a frame that is never a line.

    A JSON object's ``sentence`` string is the line; any other frame (plain text, invalid JSON, a
    JSON value that is not an object, an object without a string ``sentence``) is the line itself.
    Binary frames are decoded as UTF-8. The one exception: a JSON object with ``"type":
    "translate"`` is a machine translation, not a line, and parses to ``None`` regardless of its
    ``sentence`` (Agent, the only hooker known to send a ``type`` field, sends it as a second frame
    beside the ``copyText`` frame that already carries the line; every other ``type``, known or not,
    is left alone so a real line is never dropped on a guess).
    """
    text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if isinstance(data, dict):
        if data.get("type") == "translate":
            return None
        if isinstance(data.get("sentence"), str):
            return str(data["sentence"])
    return text


class WebsocketSource:
    """A ``TextSource`` listening to one hooker's websocket server.

    Lives on the I/O loop (spec 4.2): call ``start`` and ``stop`` on the loop's thread. The sink and
    the status listener run there too, so each must only hand its value on; neither may call
    ``stop``. Status: ``CONNECTING`` during a connection attempt, ``CONNECTED`` once the handshake
    succeeds, ``RECEIVING`` from the first frame on that connection, ``DISCONNECTED`` while waiting
    to reconnect and after ``stop``. Hookers do not answer pings and always run on this machine, so
    the client sends no keepalive pings and ignores proxy settings.
    """

    def __init__(
        self,
        source_id: str,
        uri: str,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``uri`` is ``host:port[/path]`` without the scheme (``TextSourceConfig.uri``)."""
        self._id = source_id
        self._urls = (f"ws://{uri}", f"ws://{uri.rstrip('/')}{LUNA_PATH}")
        self._now = now
        self._sleep = sleep
        self._status = SourceStatus.DISCONNECTED
        self._listener: StatusListener | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped_task: asyncio.Task[None] | None = None

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
        self._task = asyncio.get_running_loop().create_task(self._run(sink), name=f"text-source-{self._id}")

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        self._stopped_task = task
        self._set_status(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        """Return once the task ended by the last ``stop`` has finished closing its connection."""
        if self._stopped_task is not None:
            await asyncio.wait({self._stopped_task})

    def _set_status(self, status: SourceStatus) -> None:
        if status == self._status:
            return
        self._status = status
        if self._listener is not None:
            try:
                self._listener(self._id, status)
            except Exception:
                logger.exception("%s: the status listener failed", self._id)

    async def _run(self, sink: LineSink) -> None:
        attempt = 0
        while True:
            self._set_status(SourceStatus.CONNECTING)
            ws = await self._open()
            if ws is not None:
                attempt = 0
                await self._receive(ws, sink)
            self._set_status(SourceStatus.DISCONNECTED)
            await self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1

    async def _open(self) -> ClientConnection | None:
        """Connect to the URI, or to the LunaTranslator path once when the handshake is refused."""
        for url in self._urls:
            try:
                ws = await connect(url, ping_interval=None, close_timeout=CLOSE_TIMEOUT_S, proxy=None)
            except InvalidHandshake as exc:
                logger.debug("%s: handshake with %s refused: %s", self._id, url, exc)
                continue
            except (OSError, ValueError, WebSocketException) as exc:  # ValueError: a malformed port
                logger.debug("%s: cannot connect to %s: %s", self._id, url, exc)
                return None
            logger.info("%s: connected to %s", self._id, url)
            return ws
        return None

    async def _receive(self, ws: ClientConnection, sink: LineSink) -> None:
        self._set_status(SourceStatus.CONNECTED)
        try:
            async with ws:
                async for frame in ws:
                    t_mono = self._now()
                    self._set_status(SourceStatus.RECEIVING)
                    line = parse_frame(frame)
                    if line is None:
                        continue
                    try:
                        sink(line, t_mono, self._id)
                    except Exception:
                        logger.exception("%s: the text sink failed; line dropped", self._id)
        except (OSError, WebSocketException) as exc:
            logger.debug("%s: connection lost: %s", self._id, exc)
        logger.info("%s: disconnected", self._id)
