"""HTTP half of the text feed (spec 15): serves ``page.html`` and nothing else.

stdlib ``http.server`` on a daemon thread. The page is read once at start and the
websocket port is written into it, so the page connects back to the right port.
"""

from __future__ import annotations

import logging
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from anki_miner_game.feed.errors import FEED_HOST, FeedPortInUseError, is_port_unavailable

logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).with_name("page.html")
WS_PORT_PLACEHOLDER = "__FEED_WS_PORT__"
_PAGE_ROUTES = frozenset({"/", "/index.html"})


class _PageServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a bind take over a port another socket is listening
    # on, which would hide "port in use"; elsewhere it only permits TIME_WAIT reuse.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, page: bytes) -> None:
        self.page = page
        super().__init__((FEED_HOST, port), _PageHandler)


class _PageHandler(BaseHTTPRequestHandler):
    server: _PageServer

    def do_GET(self) -> None:
        self._respond(with_body=True)

    def do_HEAD(self) -> None:
        self._respond(with_body=False)

    def _respond(self, *, with_body: bool) -> None:
        if self.path.split("?", 1)[0] not in _PAGE_ROUTES:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = self.server.page
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("text feed http: %s", format % args)


def render_page(ws_port: int) -> bytes:
    return PAGE_PATH.read_text(encoding="utf-8").replace(WS_PORT_PLACEHOLDER, str(ws_port)).encode("utf-8")


class HttpFeedServer:
    def __init__(self, port: int, ws_port: int) -> None:
        self._requested_port = port
        self._ws_port = ws_port
        self._server: _PageServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port (the real one when constructed with 0); 0 when not running."""
        return 0 if self._server is None else int(self._server.server_address[1])

    def bound_address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            server = _PageServer(self._requested_port, render_page(self._ws_port))
        except OSError as err:
            if is_port_unavailable(err):
                raise FeedPortInUseError(self._requested_port) from err
            raise
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="feed-http", daemon=True)
        self._thread.start()
        logger.info("text feed page at http://%s:%d/", FEED_HOST, self.port)

    def stop(self) -> None:
        """Blocking: waits for the serve loop to exit (up to its 0.5 s poll)."""
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join()
