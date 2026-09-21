"""FakeObsServer: an obs-websocket v5 server for tests (spec 18.2), plus the R2 transcript loader.

Speaks the part of the protocol the app uses, as obs-websocket 5.7.4 does (``obs-websocket@1ef34bf4
src/websocketserver/WebSocketServer_Protocol.cpp``): Hello (op 0) with an authentication challenge
when ``password`` is set, Identify (op 1) checked with obs-websocket's SHA-256 scheme and refused
with close code 4009, Identified (op 2), Request (op 6) and RequestResponse (op 7), Event (op 5) sent
only to clients whose ``eventSubscriptions`` cover the event's intent. It binds 127.0.0.1 on an
OS-assigned port.

Two modes:

- Live (no ``transcript``): requests are answered from a reply table (``set_reply``), ``GetVersion``
  from ``version``; ``emit`` sends events; ``stall``, ``refuse_connections`` and ``drop_clients``
  make OBS hang, stay closed or vanish.
- Replay (``transcript=load_transcript(path)``): plays a session recorded from a real OBS by
  ``tools/obs_transcript_recorder.py`` (M0 spike R2, ``tests/fixtures/obs_transcripts/*.jsonl``).
  See ``load_transcript`` for how a recording becomes steps and ``FakeObsServer._replay`` for how
  they are played.
"""

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from anki_miner_game.models.obs import REQUIRED_REQUESTS

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_EVENT, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 5, 6, 7
RPC_VERSION = 1
SUCCESS = 100
NOT_READY = 207
NOT_READY_COMMENT = "OBS is not ready to perform the request."
AUTH_FAILED = 4009
NOT_IDENTIFIED = 4007
SUBSCRIBE_ALL = 4095
"""obs-websocket 5.7.4 ``EventSubscription::All`` (General through Canvases)."""

EVENT_CATEGORY: dict[str, int] = {
    "ExitStarted": 1,
    "CurrentSceneCollectionChanging": 2,
    "CurrentSceneCollectionChanged": 2,
    "SceneCollectionListChanged": 2,
    "CurrentProfileChanging": 2,
    "CurrentProfileChanged": 2,
    "ProfileListChanged": 2,
    "SceneCreated": 4,
    "InputCreated": 8,
    "StreamStateChanged": 64,
    "RecordStateChanged": 64,
    "RecordFileChanged": 64,
    "ReplayBufferStateChanged": 64,
    "VirtualcamStateChanged": 64,
}
"""``eventIntent`` of the events tests emit (obs-websocket ``EventSubscription``); replayed events carry their own."""

CLOSE_TIMEOUT_S = 0.5
"""How long a close waits for the client's close frame: an idle obsws-python ``ReqClient`` never reads it."""

_UNSENDABLE_CLOSE = (None, 1005, 1006, 1015)


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The Identify ``authentication`` value obs-websocket expects (``Utils::Crypto``)."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode()).digest()).decode()


def version_data(
    *,
    obs_version: str = "32.2.2",
    websocket_version: str = "5.7.4",
    available_requests: Iterable[str] = REQUIRED_REQUESTS,
) -> dict[str, Any]:
    """A ``GetVersion`` ``responseData``; by default an OBS that has every request the app needs."""
    return {
        "obsVersion": obs_version,
        "obsWebSocketVersion": websocket_version,
        "rpcVersion": RPC_VERSION,
        "availableRequests": list(available_requests),
        "platform": "fake",
    }


@dataclass(frozen=True)
class Reply:
    data: Mapping[str, Any] | None = None
    """``responseData``; ``None`` sends none."""
    code: int = SUCCESS
    comment: str | None = None


NOT_READY_REPLY = Reply(code=NOT_READY, comment=NOT_READY_COMMENT)


@dataclass(frozen=True)
class RecordedRequest:
    client: int
    """1-based order in which the client connected."""
    request_type: str
    request_data: Mapping[str, Any]


# --- the server ---------------------------------------------------------------------------------


@dataclass(eq=False)
class _Client:
    conn: ServerConnection
    index: int
    salt: str
    challenge: str
    identified: bool = False
    subs: int = 0


class FakeObsServer:
    def __init__(
        self,
        *,
        password: str | None = None,
        version: Mapping[str, Any] | None = None,
    ) -> None:
        self.password = password
        """Checked at every Identify; ``None`` = authentication off. Change it to refuse later logins."""
        self.version: dict[str, Any] = dict(version) if version is not None else version_data()
        self.refuse_connections = False
        """``True``: every handshake gets HTTP 503, as a closed OBS refuses it."""
        self.handshakes = 0
        """Handshake attempts, refused ones included."""
        self.identifies: list[dict[str, Any]] = []
        """Every Identify ``d`` received, in order."""
        self.auth_failures = 0
        self.requests: list[RecordedRequest] = []
        """Every request received, ``GetVersion`` included, in order."""
        self.errors: list[str] = []
        """Protocol violations by a client."""
        self._replies: dict[str, list[Reply]] = {}
        self._stalled: set[str] = set()
        self._ids = itertools.count(1)
        self._clients: list[_Client] = []
        self._changed = asyncio.Condition()
        self._server: Server | None = None

    # lifecycle

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            ping_interval=None,
            close_timeout=CLOSE_TIMEOUT_S,
        )

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
    def client_count(self) -> int:
        """Identified clients still connected."""
        return sum(1 for client in self._clients if client.identified)

    async def wait_for_client_count(self, n: int, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout), self._changed:
            await self._changed.wait_for(lambda: self.client_count == n)

    # live behaviour

    def set_reply(self, request_type: str, *replies: Reply) -> None:
        """Answer the next requests of ``request_type`` with ``replies`` in turn; the last one repeats."""
        self._replies[request_type] = list(replies)

    def stall(self, request_type: str) -> None:
        """Never answer ``request_type`` (a blocking request held up by a modal dialog)."""
        self._stalled.add(request_type)

    def request_types(self) -> list[str]:
        return [request.request_type for request in self.requests]

    async def emit(self, event_type: str, data: Mapping[str, Any] | None = None, *, intent: int | None = None) -> None:
        """Send an event to every identified client subscribed to its intent (``EVENT_CATEGORY`` by default)."""
        d: dict[str, Any] = {
            "eventType": event_type,
            "eventIntent": EVENT_CATEGORY[event_type] if intent is None else intent,
        }
        if data is not None:
            d["eventData"] = dict(data)
        for client in list(self._clients):
            if client.identified and client.subs & d["eventIntent"]:
                await self._send(client, OP_EVENT, d)

    async def drop_clients(self, code: int | None = None, reason: str = "") -> None:
        """Close every connection: with ``code`` as OBS closes them, or abruptly (``None``) as a crash does."""
        dropped = list(self._clients)
        await asyncio.gather(*(self._close(client, code, reason) for client in dropped))
        async with asyncio.timeout(5.0), self._changed:
            await self._changed.wait_for(lambda: not any(client in self._clients for client in dropped))

    # protocol

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.handshakes += 1
        if self.refuse_connections:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "OBS is not running\n")
        return None

    async def _handle(self, conn: ServerConnection) -> None:
        client = _Client(conn, next(self._ids), secrets.token_urlsafe(16), secrets.token_urlsafe(16))
        async with self._changed:
            self._clients.append(client)
            self._changed.notify_all()
        try:
            hello: dict[str, Any] = {
                "obsStudioVersion": self.version.get("obsVersion", ""),
                "obsWebSocketVersion": self.version.get("obsWebSocketVersion", ""),
                "rpcVersion": RPC_VERSION,
            }
            if self.password is not None:
                hello["authentication"] = {"challenge": client.challenge, "salt": client.salt}
            await self._send(client, OP_HELLO, hello)
            async for message in conn:
                await self._on_message(client, message)
        except ConnectionClosed:
            pass
        finally:
            async with self._changed:
                self._clients.remove(client)
                self._changed.notify_all()

    async def _on_message(self, client: _Client, message: str | bytes) -> None:
        try:
            frame = json.loads(message)
            op, d = frame["op"], frame["d"]
        except (ValueError, KeyError, TypeError):
            self.errors.append(f"client {client.index}: unreadable frame {message!r}")
            return
        if op == OP_IDENTIFY:
            await self._identify(client, d)
        elif not client.identified:
            self.errors.append(f"client {client.index}: op {op} before Identify")
            await client.conn.close(NOT_IDENTIFIED, "You must identify before sending other messages.")
        elif op == OP_REQUEST:
            await self._request(client, d)
        else:
            self.errors.append(f"client {client.index}: unexpected op {op}")

    async def _identify(self, client: _Client, d: dict[str, Any]) -> None:
        self.identifies.append(dict(d))
        if self.password is not None and d.get("authentication") != auth_string(
            self.password, client.salt, client.challenge
        ):
            self.auth_failures += 1
            await client.conn.close(AUTH_FAILED, "Authentication failed.")
            return
        async with self._changed:
            client.subs = int(d.get("eventSubscriptions", SUBSCRIBE_ALL))
            client.identified = True
            self._changed.notify_all()
        await self._send(client, OP_IDENTIFIED, {"negotiatedRpcVersion": RPC_VERSION})

    async def _request(self, client: _Client, d: dict[str, Any]) -> None:
        request_type, request_id = d["requestType"], d["requestId"]
        request_data = dict(d.get("requestData") or {})
        self.requests.append(RecordedRequest(client.index, request_type, request_data))
        if request_type in self._stalled:
            return
        queue = self._replies.get(request_type)
        if queue:
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            reply = Reply(self.version) if request_type == "GetVersion" else Reply()
        await self._send(client, OP_RESPONSE, _response(request_type, request_id, reply))

    async def _send(self, client: _Client, op: int, d: Mapping[str, Any]) -> None:
        with contextlib.suppress(ConnectionClosed):
            await client.conn.send(json.dumps({"op": op, "d": d}))

    async def _close(self, client: _Client | None, code: int | None, reason: str) -> None:
        if client is None:
            return
        if code in _UNSENDABLE_CLOSE:
            client.conn.transport.abort()
        else:
            await client.conn.close(code, reason)


def _response(request_type: str, request_id: Any, reply: Reply) -> dict[str, Any]:
    status: dict[str, Any] = {"result": reply.code == SUCCESS, "code": reply.code}
    if reply.comment:
        status["comment"] = reply.comment
    d: dict[str, Any] = {"requestType": request_type, "requestId": request_id, "requestStatus": status}
    if reply.data is not None:
        d["responseData"] = dict(reply.data)
    return d
