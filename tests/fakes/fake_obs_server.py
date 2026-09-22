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
import collections
import contextlib
import hashlib
import itertools
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

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

Role = Literal["requests", "events"]

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


# --- transcripts --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    conn: int
    kind: Literal["open", "request", "response", "event", "close"]
    d: Mapping[str, Any] = field(default_factory=dict)
    """The frame's ``d`` for a request, response or event."""
    by: str | None = None
    """``client`` or ``obs`` for a close."""
    code: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class Exchange:
    """One request as the gateway sends it, and OBS's final answer."""

    generation: int
    request_type: str
    request_data: Mapping[str, Any]
    response: Mapping[str, Any]
    """The response's ``d``."""


@dataclass(frozen=True)
class Transcript:
    name: str
    roles: Mapping[int, Role]
    generations: tuple[tuple[int, int], ...]
    """``(requests conn, events conn)`` of each connection the recorded client made, in order."""
    steps: tuple[Step, ...]
    version: Mapping[str, Any] | None
    """The first successful recorded ``GetVersion`` answer."""

    def generation_of(self, conn: int) -> int:
        return next(g for g, pair in enumerate(self.generations) if conn in pair)

    def is_last_generation(self, conn: int) -> bool:
        return self.generation_of(conn) == len(self.generations) - 1

    def close_of(self, conn: int) -> Step | None:
        return next((s for s in self.steps if s.conn == conn and s.kind == "close"), None)

    def exchanges(self) -> list[Exchange]:
        """Every request with its answer, in sending order (the loader refused unanswered ones).

        An answer belongs to the oldest unanswered request on its connection: obsws-python's request
        ids are random and repeat, so they cannot pair them.
        """
        pending: dict[int, collections.deque[Step]] = collections.defaultdict(collections.deque)
        found: list[Exchange] = []
        for step in self.steps:
            if step.kind == "request":
                pending[step.conn].append(step)
            elif step.kind == "response":
                request = pending[step.conn].popleft()
                found.append(
                    Exchange(
                        self.generation_of(request.conn),
                        request.d["requestType"],
                        dict(request.d.get("requestData") or {}),
                        step.d,
                    )
                )
        return found

    def expected_events(self, subscriptions: int, connected: str, lost: str) -> list[tuple[str, dict[str, Any]]]:
        """``(name, data)`` a gateway subscribed to ``subscriptions`` should hand on, in order.

        ``connected`` opens each generation; ``lost`` follows a connection OBS closed or one the
        recorded client dropped before reconnecting.
        """
        out: list[tuple[str, dict[str, Any]]] = []
        for _, events_conn in self.generations:
            out.append((connected, {}))
            for step in self.steps:
                if step.conn == events_conn and step.kind == "event" and step.d["eventIntent"] & subscriptions:
                    out.append((step.d["eventType"], dict(step.d.get("eventData") or {})))
            close = self.close_of(events_conn)
            if close is not None and (close.by == "obs" or not self.is_last_generation(events_conn)):
                out.append((lost, {}))
        return out


def load_transcript(path: Path) -> Transcript:
    """Read a ``tools/obs_transcript_recorder.py`` JSONL file into replay steps.

    - A connection's role comes from its Identify: ``eventSubscriptions`` 0 is a request
      connection, anything else an event connection.
    - A generation is an event connection plus the request connection opened most recently before
      it and still open (obsws-python clients connect ``ReqClient`` first). Connections outside
      every generation (a second polling client, a refused handshake) are another client's: their
      frames are dropped. Generations must not overlap in time.
    - An answer pairs with the oldest unanswered request on its own connection, never by
      ``requestId``: obsws-python draws ids at random and they repeat.
    - ``GetVersion`` exchanges are dropped: the fake answers ``GetVersion`` live, with the first
      successful recorded answer, because the gateway sends its own at every connect.
    - Hello, Identify and Identified are dropped: the fake runs the handshake live, without
      authentication (the recorded authentication is redacted).
    - A request on a generation's connections that OBS never answered is refused: no recording
      has one yet, and the replay does not take that shape.
    """
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    opened: dict[int, int] = {}
    closed: dict[int, int] = {}
    roles: dict[int, Role] = {}
    for index, record in enumerate(records):
        conn = record["conn"]
        if record.get("event") == "open":
            opened[conn] = index
        elif record.get("event") == "close":
            closed[conn] = index
        elif "msg" in record and record["msg"].get("op") == OP_IDENTIFY:
            roles[conn] = "events" if record["msg"]["d"].get("eventSubscriptions", SUBSCRIBE_ALL) else "requests"
        elif "text" in record or "binary_b64" in record:
            raise ValueError(f"{path.name}: conn {conn} has a non-JSON frame, which obsws-python never sends")

    def open_at(conn: int, index: int) -> bool:
        return opened[conn] < index < closed.get(conn, len(records))

    generations: list[tuple[int, int]] = []
    for events_conn in sorted((c for c, r in roles.items() if r == "events"), key=opened.__getitem__):
        taken = {req for req, _ in generations}
        candidates = [
            c for c, r in roles.items() if r == "requests" and c not in taken and open_at(c, opened[events_conn])
        ]
        if candidates:
            generations.append((max(candidates, key=opened.__getitem__), events_conn))
    for (req_a, evt_a), (req_b, evt_b) in itertools.pairwise(generations):
        end = max(closed.get(req_a, len(records)), closed.get(evt_a, len(records)))
        if end > min(opened[req_b], opened[evt_b]):
            raise ValueError(f"{path.name}: connections {req_a}/{evt_a} and {req_b}/{evt_b} overlap")
    if not generations:
        raise ValueError(f"{path.name}: no request connection paired with an event connection")
    ours = {conn for pair in generations for conn in pair}

    dropped: set[int] = set()
    version: Mapping[str, Any] | None = None
    pending: dict[int, collections.deque[int]] = collections.defaultdict(collections.deque)
    for index, record in enumerate(records):
        msg = record.get("msg")
        if record["conn"] not in ours or msg is None:
            continue
        if msg["op"] == OP_REQUEST:
            pending[record["conn"]].append(index)
        elif msg["op"] == OP_RESPONSE:
            request_index = pending[record["conn"]].popleft()
            if msg["d"]["requestType"] == "GetVersion":
                dropped.update((request_index, index))
                if version is None and msg["d"]["requestStatus"]["result"]:
                    version = msg["d"].get("responseData")
    for conn, leftovers in pending.items():
        if leftovers:
            request_type = records[leftovers[0]]["msg"]["d"]["requestType"]
            raise ValueError(f"{path.name}: conn {conn} sent {request_type}, which OBS never answered")

    steps: list[Step] = []
    for index, record in enumerate(records):
        conn = record["conn"]
        if conn not in ours or index in dropped:
            continue
        if record.get("event") == "open":
            steps.append(Step(conn, "open"))
        elif record.get("event") == "close":
            steps.append(Step(conn, "close", by=record["by"], code=record["code"], reason=record["reason"] or ""))
        elif record["msg"]["op"] == OP_REQUEST:
            steps.append(Step(conn, "request", record["msg"]["d"]))
        elif record["msg"]["op"] == OP_RESPONSE:
            steps.append(Step(conn, "response", record["msg"]["d"]))
        elif record["msg"]["op"] == OP_EVENT:
            steps.append(Step(conn, "event", record["msg"]["d"]))
    return Transcript(path.stem, roles, tuple(generations), tuple(steps), version)


# --- the server ---------------------------------------------------------------------------------


@dataclass(eq=False)
class _Client:
    conn: ServerConnection
    index: int
    salt: str
    challenge: str
    identified: bool = False
    subs: int = 0
    bound: bool = False
    inbox: asyncio.Queue[tuple[Any, str, dict[str, Any]]] = field(default_factory=asyncio.Queue)
    """Replay: requests waiting for the step that answers them."""
    pending: collections.deque[Any] = field(default_factory=collections.deque)
    """Replay: ids of the requests taken by a step, oldest first; each answer takes the oldest."""

    @property
    def role(self) -> Role:
        return "events" if self.subs else "requests"


class FakeObsServer:
    def __init__(
        self,
        *,
        password: str | None = None,
        version: Mapping[str, Any] | None = None,
        transcript: Transcript | None = None,
    ) -> None:
        self.password = password
        """Checked at every Identify; ``None`` = authentication off. Change it to refuse later logins."""
        if version is None:
            version = transcript.version if transcript is not None and transcript.version else version_data()
        self.version: dict[str, Any] = dict(version)
        self.transcript = transcript
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
        self.unscripted: list[str] = []
        """Replay: requests that did not match the recording."""
        self.replay_finished = asyncio.Event()
        self.replay_position = ""
        """Replay: the step the replay is waiting on, for failure messages."""
        self._replies: dict[str, list[Reply]] = {}
        self._stalled: set[str] = set()
        self._ids = itertools.count(1)
        self._clients: list[_Client] = []
        self._changed = asyncio.Condition()
        self._server: Server | None = None
        self._replay_task: asyncio.Task[None] | None = None

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
        if self.transcript is not None:
            self._replay_task = asyncio.get_running_loop().create_task(self._replay(self.transcript))

    async def stop(self) -> None:
        if self._replay_task is not None:
            self._replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task
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
        if self.transcript is not None and request_type != "GetVersion":
            await client.inbox.put((request_id, request_type, request_data))
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

    # replay

    async def _take_unbound(self, role: Role) -> _Client:
        """The earliest identified client of ``role`` not yet bound to a recorded connection."""

        def first() -> _Client | None:
            return next((c for c in self._clients if c.identified and not c.bound and c.role == role), None)

        async with self._changed:
            await self._changed.wait_for(lambda: first() is not None)
            client = first()
            assert client is not None
            client.bound = True
            return client

    async def _replay(self, transcript: Transcript) -> None:
        """Play the recorded steps in order.

        - ``open``: bind the next identified client of that connection's role.
        - ``request``: wait until the bound client sends a request; one that differs from the
          recording is noted in ``unscripted`` and takes the recorded one's place.
        - ``response``: send the recorded answer with the id of the client's oldest unanswered
          request (answers pair by order, as in the loader).
        - ``event``: send it when the client's subscriptions cover its ``eventIntent``.
        - ``close`` by OBS: close with the recorded code (abort for 1006); after the last
          generation, refuse further handshakes as a gone OBS does. ``close`` by the recorded
          client with a later generation: cut the connection, since that client reconnected.
        """
        bound: dict[int, _Client] = {}
        for number, step in enumerate(transcript.steps):
            self.replay_position = f"{transcript.name} step {number}: {step.kind} on conn {step.conn} {dict(step.d)}"
            if step.kind == "open":
                bound[step.conn] = await self._take_unbound(transcript.roles[step.conn])
                continue
            client = bound[step.conn]
            if step.kind == "request":
                request_id, request_type, request_data = await client.inbox.get()
                if (request_type, request_data) != (step.d["requestType"], dict(step.d.get("requestData") or {})):
                    self.unscripted.append(f"{self.replay_position}: got {request_type} {request_data}")
                client.pending.append(request_id)
            elif step.kind == "response":
                await self._send(client, OP_RESPONSE, {**step.d, "requestId": client.pending.popleft()})
            elif step.kind == "event":
                if client.subs & step.d["eventIntent"]:
                    await self._send(client, OP_EVENT, step.d)
            elif step.by == "obs":
                if transcript.is_last_generation(step.conn):
                    self.refuse_connections = True
                await self._close(client, step.code, step.reason)
            elif not transcript.is_last_generation(step.conn):
                # close, not abort: on Windows an RST discards the frames just sent before the client reads them
                client.conn.transport.close()
        self.replay_position = f"{transcript.name}: finished"
        self.replay_finished.set()


def _response(request_type: str, request_id: Any, reply: Reply) -> dict[str, Any]:
    status: dict[str, Any] = {"result": reply.code == SUCCESS, "code": reply.code}
    if reply.comment:
        status["comment"] = reply.comment
    d: dict[str, Any] = {"requestType": request_type, "requestId": request_id, "requestStatus": status}
    if reply.data is not None:
        d["responseData"] = dict(reply.data)
    return d
