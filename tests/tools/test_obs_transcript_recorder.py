"""tools/obs_transcript_recorder.py: a logging websocket proxy between a client and OBS.

A fake obs-websocket upstream runs on loopback port 0; nothing here talks to a real OBS.
"""

import asyncio
import io
import itertools
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tools import obs_transcript_recorder as rec

CHALLENGE = "c-9f8e7d6c5b4a"
SALT = "s-0a1b2c3d4e5f"
AUTH = "a-SECRET-DERIVED-HASH"
HELLO = {
    "op": 0,
    "d": {
        "obsWebSocketVersion": "5.7.4",
        "rpcVersion": 1,
        "authentication": {"challenge": CHALLENGE, "salt": SALT},
    },
}
IDENTIFY = {"op": 1, "d": {"rpcVersion": 1, "authentication": AUTH, "eventSubscriptions": 1023}}
IDENTIFIED = {"op": 2, "d": {"negotiatedRpcVersion": 1}}
REQUEST = {"op": 6, "d": {"requestType": "GetRecordStatus", "requestId": "7"}}
RESPONSE = {
    "op": 7,
    "d": {
        "requestType": "GetRecordStatus",
        "requestId": "7",
        "requestStatus": {"result": True, "code": 100},
        "responseData": {"outputActive": False},
    },
}
EVENT = {
    "op": 5,
    "d": {
        "eventType": "RecordStateChanged",
        "eventIntent": 64,
        "eventData": {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED", "outputPath": "/v/a.mkv"},
    },
}


class FakeObs:
    """obs-websocket v5 in miniature: hello, identify, one request, one event, then a scripted close."""

    def __init__(self, close_code: int | None = None, close_reason: str = "") -> None:
        self.close_code = close_code
        self.close_reason = close_reason
        self.received: list[str | bytes] = []
        self.offered: list[str] = []
        self.closed = asyncio.Event()
        self.close_seen: tuple[int | None, str | None] | None = None
        self.port = 0
        self._server = None

    def _select(self, connection, subprotocols):
        self.offered = list(subprotocols)
        return "obswebsocket.json" if "obswebsocket.json" in subprotocols else None

    async def _handler(self, ws):
        await ws.send(json.dumps(HELLO))
        try:
            identify = await ws.recv()
            self.received.append(identify)
            await ws.send(json.dumps(IDENTIFIED))
            if self.close_code is not None:
                await ws.close(self.close_code, self.close_reason)
                return
            async for message in ws:
                self.received.append(message)
                if isinstance(message, bytes):
                    await ws.send(message)
                    continue
                if json.loads(message)["op"] == 6:
                    await ws.send(json.dumps(RESPONSE))
                    await ws.send(json.dumps(EVENT))
        except ConnectionClosed:
            pass
        finally:
            self.close_seen = (ws.close_code, ws.close_reason)
            self.closed.set()

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0, select_subprotocol=self._select)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def _clock():
    ticks = itertools.count(1)
    return lambda: 100.0 + next(ticks) / 1000


def _records(sink: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def _client(port: int, **kwargs):
    return connect(f"ws://127.0.0.1:{port}", proxy=None, open_timeout=5, **kwargs)


async def _session(port: int) -> list:
    received = []
    async with _client(port, subprotocols=["obswebsocket.json"]) as ws:
        received.append(json.loads(await ws.recv()))  # Hello
        await ws.send(json.dumps(IDENTIFY))
        received.append(json.loads(await ws.recv()))  # Identified
        await ws.send(json.dumps(REQUEST))
        received.append(json.loads(await ws.recv()))  # response
        received.append(json.loads(await ws.recv()))  # event
        received.append(ws.subprotocol)
    return received


async def test_frames_pass_through_unchanged_in_both_directions():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        received = await _session(proxy.port)
        await asyncio.wait_for(obs.closed.wait(), 5)
    assert received == [HELLO, IDENTIFIED, RESPONSE, EVENT, "obswebsocket.json"]
    assert [json.loads(m) for m in obs.received] == [IDENTIFY, REQUEST]  # the real hash reaches OBS
    assert obs.offered == ["obswebsocket.json"]


async def test_every_frame_is_logged_with_direction_connection_and_t_mono():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        await _session(proxy.port)
        await asyncio.wait_for(obs.closed.wait(), 5)
    records = _records(sink)
    frames = [r for r in records if "dir" in r]
    assert [(r["dir"], r["msg"]["op"]) for r in frames] == [
        ("obs->client", 0),
        ("client->obs", 1),
        ("obs->client", 2),
        ("client->obs", 6),
        ("obs->client", 7),
        ("obs->client", 5),
    ]
    assert {r["conn"] for r in records} == {1}
    t_monos = [r["t_mono"] for r in records]
    assert t_monos == sorted(t_monos) and all(100.0 < t < 101.0 for t in t_monos)
    assert frames[4]["msg"] == RESPONSE
    assert frames[5]["msg"] == EVENT
    assert records[0]["event"] == "open"
    assert records[0]["subprotocol"] == "obswebsocket.json"
    assert records[-1]["event"] == "close"


async def test_auth_fields_are_redacted_in_the_log_only():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        await _session(proxy.port)
        await asyncio.wait_for(obs.closed.wait(), 5)
    text = sink.getvalue()
    for secret in (CHALLENGE, SALT, AUTH):
        assert secret not in text
    frames = [r for r in _records(sink) if "dir" in r]
    assert frames[0]["msg"]["d"]["authentication"] == {"challenge": rec.REDACTED, "salt": rec.REDACTED}
    assert frames[1]["msg"]["d"]["authentication"] == rec.REDACTED
    assert frames[1]["msg"]["d"]["eventSubscriptions"] == 1023


def test_redact_reaches_nested_password_fields_without_mutating_the_input():
    message = {"op": 7, "d": {"responseData": {"settings": {"server_password": "p", "port": 4455}}}}
    redacted = rec.redact(message)
    assert redacted["d"]["responseData"]["settings"] == {"server_password": rec.REDACTED, "port": 4455}
    assert message["d"]["responseData"]["settings"]["server_password"] == "p"


async def test_an_obs_close_code_reaches_the_client_and_the_log():
    sink = io.StringIO()
    obs = FakeObs(close_code=4009, close_reason="Authentication failed.")
    proxy = rec.TranscriptProxy("", sink, now=_clock())
    async with obs:
        proxy.upstream = f"ws://127.0.0.1:{obs.port}"
        async with proxy, _client(proxy.port) as ws:
            await ws.recv()
            await ws.send(json.dumps(IDENTIFY))
            await ws.recv()
            with pytest.raises(ConnectionClosed) as closed:
                await ws.recv()
    assert closed.value.rcvd is not None
    assert (closed.value.rcvd.code, closed.value.rcvd.reason) == (4009, "Authentication failed.")
    close = [r for r in _records(sink) if r.get("event") == "close"][0]
    assert (close["by"], close["code"], close["reason"]) == ("obs", 4009, "Authentication failed.")


async def test_a_client_close_reaches_obs_and_the_log():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        async with _client(proxy.port) as ws:
            await ws.recv()
            await ws.close(1000, "bye")
        await asyncio.wait_for(obs.closed.wait(), 5)
    assert obs.close_seen == (1000, "bye")
    close = [r for r in _records(sink) if r.get("event") == "close"][0]
    assert (close["by"], close["code"], close["reason"]) == ("client", 1000, "bye")


async def test_binary_frames_are_relayed_and_logged_as_base64():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        async with _client(proxy.port) as ws:
            await ws.recv()
            await ws.send(json.dumps(IDENTIFY))
            await ws.recv()
            await ws.send(b"\x81\xa2op\x06")
            assert await ws.recv() == b"\x81\xa2op\x06"
        await asyncio.wait_for(obs.closed.wait(), 5)
    binary = [r for r in _records(sink) if "binary_b64" in r]
    assert [(r["dir"], r["binary_b64"]) for r in binary] == [("client->obs", "gaJvcAY="), ("obs->client", "gaJvcAY=")]


async def test_a_missing_upstream_rejects_the_handshake_and_is_logged():
    sink = io.StringIO()
    async with FakeObs() as obs:
        dead_port = obs.port
    async with rec.TranscriptProxy(f"ws://127.0.0.1:{dead_port}", sink, now=_clock()) as proxy:
        with pytest.raises(InvalidStatus) as rejected:
            async with _client(proxy.port):
                pass
    assert rejected.value.response.status_code == 502
    assert [r["event"] for r in _records(sink)] == ["upstream_failed"]


async def test_connections_are_numbered_in_order():
    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:
        for _ in range(2):
            async with _client(proxy.port) as ws:
                await ws.recv()
    assert sorted({r["conn"] for r in _records(sink)}) == [1, 2]


def test_the_proxy_listens_on_loopback_only():
    assert rec.TranscriptProxy("ws://127.0.0.1:4455", io.StringIO()).host == "127.0.0.1"


def test_cli_needs_an_upstream_and_an_output(capsys):
    with pytest.raises(SystemExit) as exc:
        rec.main(["--port", "0"])
    assert exc.value.code == 2


async def test_an_obsws_python_client_works_through_the_proxy():
    # The scenario driver (and later the app) talk to OBS through obsws-python.
    import obsws_python

    sink = io.StringIO()
    async with FakeObs() as obs, rec.TranscriptProxy(f"ws://127.0.0.1:{obs.port}", sink, now=_clock()) as proxy:

        def drive():
            client = obsws_python.ReqClient(host="127.0.0.1", port=proxy.port, password="pw", timeout=5)
            try:
                return client.send("GetRecordStatus", raw=True)
            finally:
                client.disconnect()

        data = await asyncio.to_thread(drive)
        await asyncio.wait_for(obs.closed.wait(), 5)
    assert data == {"outputActive": False}
    identify = [r for r in _records(sink) if r.get("dir") == "client->obs"][0]
    assert identify["msg"]["d"]["authentication"] == rec.REDACTED
