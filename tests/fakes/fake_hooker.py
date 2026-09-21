"""FakeHookerServer: a text hooker's websocket server for tests (spec 3.2, 18.2).

Textractor's websocket extension, Agent and LunaTranslator each run a websocket server and send
every line to every connected client, so a texthooker page and this app can listen side by side.
The fake does the same on 127.0.0.1 with an OS-assigned port. Lines come from a script of
``(seconds_after_start, frame)`` pairs played on an injected clock: ``pump()`` sends every frame
whose time has come, so a test advances its fake clock and pumps, and nothing depends on real
time. ``broadcast`` sends a frame at once. ``accept_paths`` answers 404 to any other path, which is
how LunaTranslator treats a client that does not ask for ``/api/ws/text/origin``.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable, Collection, Iterable
from http import HTTPStatus
from types import TracebackType
from typing import Self

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

Frame = str | bytes


class FakeHookerServer:
    def __init__(
        self,
        script: Iterable[tuple[float, Frame]] = (),
        *,
        now: Callable[[], float] = time.monotonic,
        accept_paths: Collection[str] | None = None,
    ) -> None:
        self._script = sorted(script, key=lambda item: item[0])  # stable: equal times keep script order
        self._next = 0
        self._now = now
        self._t0 = 0.0
        self._accept_paths = accept_paths
        self._server: Server | None = None
        self._clients: set[ServerConnection] = set()
        self._clients_changed = asyncio.Condition()
        self.requested_paths: list[str] = []
        """Path of every handshake request, accepted or refused, in arrival order."""

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0, process_request=self._check_path, ping_interval=None)
        self._t0 = self._now()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "start() first"
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def uri(self) -> str:
        """``host:port`` without the scheme, the form ``TextSourceConfig.uri`` takes."""
        return f"127.0.0.1:{self.port}"

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def wait_for_clients(self, n: int, timeout: float = 5.0) -> None:
        """Wait until at least ``n`` clients are connected; ``TimeoutError`` after ``timeout`` seconds."""
        async with asyncio.timeout(timeout), self._clients_changed:
            await self._clients_changed.wait_for(lambda: len(self._clients) >= n)

    async def broadcast(self, frame: Frame) -> None:
        """Send ``frame`` to every connected client, as a text frame for ``str`` and binary for ``bytes``."""
        for client in list(self._clients):
            with contextlib.suppress(ConnectionClosed):
                await client.send(frame)

    async def pump(self) -> list[Frame]:
        """Broadcast, in script order, every scripted frame due by ``now()``; return the frames sent."""
        elapsed = self._now() - self._t0
        due: list[Frame] = []
        while self._next < len(self._script) and self._script[self._next][0] <= elapsed:
            due.append(self._script[self._next][1])
            self._next += 1
        for frame in due:
            await self.broadcast(frame)
        return due

    async def drop_clients(self) -> None:
        """Close every connection, as a hooker that restarts does; return once all are unregistered."""
        dropped = set(self._clients)
        for client in dropped:
            await client.close()
        async with self._clients_changed:
            await self._clients_changed.wait_for(lambda: not dropped & self._clients)

    def _check_path(self, connection: ServerConnection, request: Request) -> Response | None:
        self.requested_paths.append(request.path)
        if self._accept_paths is not None and request.path not in self._accept_paths:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not Found\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        async with self._clients_changed:
            self._clients.add(connection)
            self._clients_changed.notify_all()
        try:
            async for _ in connection:  # hookers ignore what clients send; drain it
                pass
        except ConnectionClosed:
            pass
        finally:
            async with self._clients_changed:
                self._clients.discard(connection)
                self._clients_changed.notify_all()
