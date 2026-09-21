"""Text feed (spec 15): a websocket re-broadcast of accepted lines plus a page that shows them.

Two ports, both on 127.0.0.1. ``FeedServer`` is what ``app.py`` composes; a
:class:`FeedPortInUseError` from :meth:`FeedServer.start` means "disable the feed for the
run and raise a banner naming ``err.port``" (spec 17).
"""

from __future__ import annotations

import asyncio

from anki_miner_game.feed.errors import FEED_HOST, FeedPortInUseError
from anki_miner_game.feed.http_server import HttpFeedServer
from anki_miner_game.feed.ws_server import WsFeedServer

__all__ = ["FEED_HOST", "FeedPortInUseError", "FeedServer"]


class FeedServer:
    """Owns both halves. ``start``, ``broadcast`` and ``stop`` run on the I/O loop's thread."""

    def __init__(self, ws_port: int, http_port: int) -> None:
        self._ws = WsFeedServer(ws_port)
        self._http_port = http_port
        self._http: HttpFeedServer | None = None

    @property
    def ws_port(self) -> int:
        return self._ws.port

    @property
    def http_port(self) -> int:
        return 0 if self._http is None else self._http.port

    @property
    def page_url(self) -> str:
        return f"http://{FEED_HOST}:{self.http_port}/"

    @property
    def client_count(self) -> int:
        return self._ws.client_count

    def bound_addresses(self) -> list[tuple[str, int]]:
        found = [self._ws.bound_address(), None if self._http is None else self._http.bound_address()]
        return [addr for addr in found if addr is not None]

    async def start(self) -> None:
        """Bind both ports; on any failure nothing stays bound."""
        await self._ws.start()
        http = HttpFeedServer(self._http_port, ws_port=self._ws.port)
        try:
            http.start()
        except BaseException:
            await self._ws.stop()
            raise
        self._http = http

    def broadcast(self, text: str) -> None:
        """Send one accepted line to every connected client; a no-op when not running."""
        self._ws.broadcast(text)

    async def stop(self) -> None:
        http, self._http = self._http, None
        if http is not None:
            await asyncio.to_thread(http.stop)
        await self._ws.stop()
