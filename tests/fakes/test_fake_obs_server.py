"""FakeObsServer against a plain websockets client: the protocol, the live controls, the loader, the replay."""

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from anki_miner_game.models.obs import REQUIRED_REQUESTS
from tests.fakes.fake_obs_server import (
    AUTH_FAILED,
    NOT_IDENTIFIED,
    FakeObsServer,
    RecordedRequest,
    Reply,
    auth_string,
    version_data,
)

PASSWORD = "fake-server-pw"


async def open_client(server: FakeObsServer, *, subs: int = 0, password: str | None = PASSWORD) -> ClientConnection:
    """Connect and identify as obsws-python does; returns the identified connection."""
    ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None, ping_interval=None)
    hello = json.loads(await ws.recv())
    assert hello["op"] == 0
    d: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": subs}
    if "authentication" in hello["d"] and password is not None:
        auth = hello["d"]["authentication"]
        d["authentication"] = auth_string(password, auth["salt"], auth["challenge"])
    await ws.send(json.dumps({"op": 1, "d": d}))
    return ws


async def identified(server: FakeObsServer, **kwargs: Any) -> ClientConnection:
    ws = await open_client(server, **kwargs)
    assert json.loads(await ws.recv()) == {"op": 2, "d": {"negotiatedRpcVersion": 1}}
    return ws


async def ask(ws: ClientConnection, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """One request with the fixed id ``r1``, as obsws-python's random ids repeat too."""
    request: dict[str, Any] = {"requestType": request_type, "requestId": "r1"}
    if data:
        request["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": request}))
    frame = json.loads(await ws.recv())
    assert frame["op"] == 7 and frame["d"]["requestId"] == "r1"
    return frame["d"]


@pytest.fixture
async def server():
    async with FakeObsServer(password=PASSWORD) as fake:
        yield fake


# --- protocol ------------------------------------------------------------------------------------


async def test_hello_carries_a_challenge_and_the_right_password_identifies(server):
    ws = await identified(server)

    assert server.client_count == 1
    assert server.auth_failures == 0
    assert [identify["eventSubscriptions"] for identify in server.identifies] == [0]
    await ws.close()


async def test_a_wrong_password_is_refused_with_4009(server):
    ws = await open_client(server, password="wrong")

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()

    assert raised.value.rcvd is not None and raised.value.rcvd.code == AUTH_FAILED
    assert server.auth_failures == 1
    assert server.client_count == 0


async def test_without_a_password_hello_asks_for_no_authentication():
    async with FakeObsServer() as server:
        ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None)
        hello = json.loads(await ws.recv())
        assert "authentication" not in hello["d"]
        assert (hello["d"]["obsStudioVersion"], hello["d"]["rpcVersion"]) == ("32.2.2", 1)
        await ws.close()


async def test_a_request_before_identify_closes_with_4007(server):
    ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None)
    await ws.recv()
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "GetVersion", "requestId": "x"}}))

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()

    assert raised.value.rcvd is not None and raised.value.rcvd.code == NOT_IDENTIFIED
    assert server.errors


# --- live replies ---------------------------------------------------------------------------------


async def test_requests_are_recorded_and_answered_from_the_reply_table(server):
    server.set_reply("GetRecordStatus", Reply({"outputActive": True}), Reply(code=604, comment="nope"), Reply(code=601))
    ws = await identified(server)

    version = await ask(ws, "GetVersion")
    first = await ask(ws, "GetRecordStatus")
    second = await ask(ws, "GetRecordStatus")
    third = await ask(ws, "GetRecordStatus")
    fourth = await ask(ws, "GetRecordStatus")
    other = await ask(ws, "SetCurrentProfile", {"profileName": "Anki Miner Game"})

    assert version["responseData"] == version_data() and version["responseData"]["availableRequests"] == list(
        REQUIRED_REQUESTS
    )
    assert (first["requestStatus"], first["responseData"]) == ({"result": True, "code": 100}, {"outputActive": True})
    assert second["requestStatus"] == {"result": False, "code": 604, "comment": "nope"}
    assert third["requestStatus"] == fourth["requestStatus"] == {"result": False, "code": 601}
    assert "responseData" not in third
    assert other == {
        "requestType": "SetCurrentProfile",
        "requestId": "r1",
        "requestStatus": {"result": True, "code": 100},
    }
    assert server.requests[-1] == RecordedRequest(1, "SetCurrentProfile", {"profileName": "Anki Miner Game"})
    await ws.close()


async def test_a_stalled_request_is_never_answered(server):
    server.stall("SetCurrentProfile")
    ws = await identified(server)
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "SetCurrentProfile", "requestId": "s"}}))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await ws.recv()

    assert server.request_types() == ["SetCurrentProfile"]
    await ws.close()


async def test_events_reach_only_clients_subscribed_to_their_intent(server):
    requests = await identified(server, subs=0)
    outputs = await identified(server, subs=64)
    config = await identified(server, subs=2)

    await server.emit("RecordStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
    await server.emit("ExitStarted")
    await server.emit("VendorEvent", {"x": 1}, intent=2)

    assert json.loads(await outputs.recv())["d"] == {
        "eventType": "RecordStateChanged",
        "eventIntent": 64,
        "eventData": {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
    }
    assert json.loads(await config.recv())["d"] == {"eventType": "VendorEvent", "eventIntent": 2, "eventData": {"x": 1}}
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await requests.recv()
    for ws in (requests, outputs, config):
        await ws.close()


async def test_refused_connections_get_http_503(server):
    server.refuse_connections = True

    with pytest.raises(InvalidStatus) as raised:
        await connect(f"ws://127.0.0.1:{server.port}", proxy=None)

    assert raised.value.response.status_code == 503
    assert server.handshakes == 1


@pytest.mark.parametrize(("code", "reason"), [(None, ""), (1001, "Server stopping.")])
async def test_drop_clients_closes_every_connection(server, code, reason):
    ws = await identified(server)

    await server.drop_clients(code, reason)

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()
    received = raised.value.rcvd
    assert (None if received is None else (received.code, received.reason)) == (
        None if code is None else (code, reason)
    )
    assert server.client_count == 0
