"""Websocket proxy that records an obs-websocket session as JSONL (M0 spikes R1, R2).

Point a client (the scenario driver in ``tools/m0/obs_scenarios.py``, later the app) at the proxy
instead of OBS; every frame is relayed unchanged and logged with its direction and the
``time.monotonic()`` of its receipt in the proxy::

    python -m tools.obs_transcript_recorder --upstream ws://127.0.0.1:4455 --port 4456 --out T.jsonl

``time.monotonic()`` is CLOCK_MONOTONIC on Linux, shared by every process on the host, so these
timestamps compare directly with the sync-probe flasher's log and the app's own ``t_mono``.

One JSON object per line, flushed per line:

- ``{"t_mono", "conn", "event": "open", "subprotocol", "upstream"}``
- ``{"t_mono", "conn", "dir": "client->obs" | "obs->client", "msg": <frame JSON, auth redacted>}``
- ``{"t_mono", "conn", "dir", "text": ...}`` for a text frame that is not JSON
- ``{"t_mono", "conn", "dir", "binary_b64": ...}`` for a binary (msgpack) frame
- ``{"t_mono", "conn", "event": "close", "by": "client" | "obs", "code", "reason"}``
- ``{"t_mono", "conn", "event": "upstream_failed", "error"}`` when OBS refused the connection;
  the client's handshake is then rejected with HTTP 502

Redaction touches only the log: Hello's ``authentication`` challenge and salt, Identify's
``authentication`` hash, and any ``password``-style field. OBS itself receives the real frames.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import http
import itertools
import json
import signal
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

REDACTED = "<redacted>"
REDACTED_KEYS = frozenset({"authentication", "password", "server_password", "serverPassword"})
TO_OBS = "client->obs"
FROM_OBS = "obs->client"
_UNSENDABLE = (1005, 1006, 1015)  # close statuses that exist only locally, never on the wire


def redact(value: Any) -> Any:
    """A copy of ``value`` with every auth/password field replaced by ``REDACTED``."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in REDACTED_KEYS:
                out[key] = dict.fromkeys(item, REDACTED) if isinstance(item, dict) else REDACTED
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def frame_record(t_mono: float, conn: int, direction: str, message: str | bytes) -> dict[str, Any]:
    record: dict[str, Any] = {"t_mono": t_mono, "conn": conn, "dir": direction}
    if isinstance(message, bytes):
        record["binary_b64"] = base64.b64encode(message).decode("ascii")
        return record
    try:
        record["msg"] = redact(json.loads(message))
    except ValueError:
        record["text"] = message
    return record


class TranscriptProxy:
    """Loopback websocket proxy to ``upstream`` that logs every frame to ``sink``."""

    def __init__(
        self,
        upstream: str,
        sink: TextIO,
        *,
        port: int = 0,
        now: Callable[[], float] = time.monotonic,
        open_timeout: float = 10.0,
    ) -> None:
        self.upstream = upstream
        self.host = "127.0.0.1"
        self.port = port
        self._sink = sink
        self._now = now
        self._open_timeout = open_timeout
        self._ids = itertools.count(1)
        self._pending: dict[ServerConnection, tuple[int, ClientConnection]] = {}
        self._server: Server | None = None

    def _write(self, record: dict[str, Any]) -> None:
        self._sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._sink.flush()

    async def start(self) -> int:
        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            process_request=self._process_request,
            select_subprotocol=self._select_subprotocol,
            ping_interval=None,
            max_size=None,
            compression=None,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for _, upstream in self._pending.values():
            await upstream.close()
        self._pending.clear()

    async def __aenter__(self) -> TranscriptProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """Open the upstream first, so a client of a closed OBS fails its handshake as it would."""
        conn = next(self._ids)
        offered = [
            Subprotocol(item.strip())
            for header in request.headers.get_all("Sec-WebSocket-Protocol")
            for item in header.split(",")
            if item.strip()
        ]
        try:
            upstream = await connect(
                self.upstream,
                subprotocols=offered or None,
                proxy=None,
                ping_interval=None,
                max_size=None,
                compression=None,
                open_timeout=self._open_timeout,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            self._write(
                {
                    "t_mono": self._now(),
                    "conn": conn,
                    "event": "upstream_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return connection.respond(http.HTTPStatus.BAD_GATEWAY, "OBS is not reachable\n")
        self._pending[connection] = (conn, upstream)
        return None

    def _select_subprotocol(
        self, connection: ServerConnection, subprotocols: Sequence[Subprotocol]
    ) -> Subprotocol | None:
        pending = self._pending.get(connection)
        return pending[1].subprotocol if pending is not None else None

    async def _pump(
        self,
        src: ServerConnection | ClientConnection,
        dst: ServerConnection | ClientConnection,
        direction: str,
        conn: int,
    ) -> ServerConnection | ClientConnection:
        """Relay src -> dst until one side closes; return the side that closed."""
        try:
            async for message in src:
                self._write(frame_record(self._now(), conn, direction, message))
                try:
                    await dst.send(message)
                except ConnectionClosed:
                    return dst
        except ConnectionClosed:
            pass
        return src

    async def _handler(self, client: ServerConnection) -> None:
        conn, upstream = self._pending.pop(client)
        self._write(
            {
                "t_mono": self._now(),
                "conn": conn,
                "event": "open",
                "subprotocol": client.subprotocol,
                "upstream": self.upstream,
            }
        )
        pumps = {
            asyncio.create_task(self._pump(client, upstream, TO_OBS, conn)),
            asyncio.create_task(self._pump(upstream, client, FROM_OBS, conn)),
        }
        try:
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            closed = done.pop().result()
            other: ServerConnection | ClientConnection = upstream if closed is client else client
            code, reason = closed.close_code, closed.close_reason
            self._write(
                {
                    "t_mono": self._now(),
                    "conn": conn,
                    "event": "close",
                    "by": "client" if closed is client else "obs",
                    "code": code,
                    "reason": reason,
                }
            )
            if code is None or code == 1006:
                other.transport.abort()  # the far side vanished: let the other end see 1006 too
            elif code in _UNSENDABLE:
                await other.close()
            else:
                await other.close(code, reason or "")
        finally:
            for task in pumps:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await upstream.close()


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="obs_transcript_recorder", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--upstream", required=True, help="OBS websocket URL, e.g. ws://127.0.0.1:4455")
    parser.add_argument("--port", type=int, required=True, help="loopback port to listen on (0 = any free port)")
    parser.add_argument("--out", type=Path, required=True, help="JSONL transcript file (created; see --append)")
    parser.add_argument("--append", action="store_true", help="append to an existing transcript")
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    with open(args.out, "a" if args.append else "x", encoding="utf-8") as sink:
        async with TranscriptProxy(args.upstream, sink, port=args.port) as proxy:
            print(
                f"recording ws://127.0.0.1:{proxy.port} -> {args.upstream} into {args.out}", file=sys.stderr, flush=True
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    asyncio.run(_serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
