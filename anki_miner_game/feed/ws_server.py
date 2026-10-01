"""Websocket half of the text feed (spec 15).

Sends every accepted line to every client as a plain-text frame, the format Textractor's
websocket extension emits, so texthooker pages pointed at this port work unchanged. Runs on
the I/O asyncio loop; :meth:`WsFeedServer.broadcast` must be called on that loop's thread.

Only these clients may connect (D-07, ``ALLOWED_ORIGINS``): ones that send no ``Origin`` (apps and
scripts), pages served from ``http://127.0.0.1`` or ``http://localhost`` on any port (the app's own
page, a self-hosted texthooker) and the hosted texthooker-ui (spec 15). Any other web page,
``null`` included (a sandboxed iframe), gets HTTP 403, so a site open in the browser cannot read
the lines, which in clipboard mode are whatever the user copies.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.typing import Origin

from anki_miner_game.feed.errors import FEED_HOST, FeedPortInUseError, is_port_unavailable

logger = logging.getLogger(__name__)

# Clients never send anything the feed reads; keep an inbound frame from costing memory.
_MAX_INBOUND_BYTES = 4096

ALLOWED_ORIGINS: Final[tuple[str | re.Pattern[str] | None, ...]] = (
    None,  # apps and scripts send no Origin
    re.compile(r"http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?"),  # the app's own page, a self-hosted texthooker
    "https://renji-xd.github.io",  # the hosted texthooker-ui (spec 15)
)
"""websockets tests a pattern with ``fullmatch`` and answers a refused Origin with HTTP 403."""


def _origins() -> list[Origin | re.Pattern[str] | None]:
    return [Origin(allowed) if isinstance(allowed, str) else allowed for allowed in ALLOWED_ORIGINS]


async def _handler(connection: ServerConnection) -> None:
    # Drain and discard until the client goes away; the connection stays in
    # ``server.connections`` for broadcast() meanwhile.
    async for _ in connection:
        pass


class WsFeedServer:
    def __init__(self, port: int) -> None:
        self._requested_port = port
        self._server: Server | None = None

    @property
    def port(self) -> int:
        """The bound port (the real one when constructed with 0); 0 when not running."""
        if self._server is None:
            return 0
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def client_count(self) -> int:
        return 0 if self._server is None else len(self._server.connections)

    def bound_address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    async def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = await serve(
                _handler, FEED_HOST, self._requested_port, max_size=_MAX_INBOUND_BYTES, origins=_origins()
            )
        except OSError as err:
            if is_port_unavailable(err):
                raise FeedPortInUseError(self._requested_port) from err
            raise
        logger.info("text feed websocket listening on %s:%d", FEED_HOST, self.port)

    def broadcast(self, text: str) -> None:
        if self._server is not None:
            broadcast(self._server.connections, text)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
