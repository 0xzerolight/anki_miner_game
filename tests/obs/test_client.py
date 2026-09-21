"""The OBS gateway against a live FakeObsServer (spec 3.3, 11.2, 17 auth row; docs/m0/source-findings.md 5, 8, 11)."""

import asyncio
import logging
import threading

import pytest

from anki_miner_game.interfaces.obs import ObsGateway
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsCredentials,
    ObsRequestError,
    ObsUnsupportedError,
)
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, NOT_READY_RETRY_S, NOT_READY_TIMEOUT_S, ObsClient
from tests.fakes.fake_obs_server import NOT_READY_COMMENT, NOT_READY_REPLY, Reply, version_data
from tests.obs.helpers import CONNECTED, LOST, PASSWORD, Credentials, FakeClock, ThreadClock, wait_until
from tests.test_contracts import _assert_conforms

RECORDING = {
    "outputActive": True,
    "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED",
    "outputPath": "/videos/_incoming/2026-09-21 18-40-39.mkv",
}


def test_the_client_conforms_to_the_obs_gateway_protocol():
    _assert_conforms(ObsGateway, ObsClient)


# --- connect -------------------------------------------------------------------------------------


async def test_connect_returns_the_version_and_announces_the_connection(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)

    info = await gateway.connect()

    assert (info.obs_version, info.websocket_version) == ("32.2.2", "5.7.4")
    assert info.available_requests == frozenset(REQUIRED_REQUESTS)
    assert [(e.name, dict(e.data), e.t_mono) for e in events.got] == [(CONNECTED, {}, clock.t)]
    assert [identify["eventSubscriptions"] for identify in obs_server.identifies] == [0, EVENT_SUBSCRIPTIONS]
    assert EVENT_SUBSCRIPTIONS == 1 | 2 | 64
    assert obs_server.request_types() == ["GetVersion"]


async def test_connect_when_connected_returns_the_same_info_without_reconnecting(obs_server, make_gateway):
    gateway, events = make_gateway()
    first = await gateway.connect()

    assert await gateway.connect() is first
    assert obs_server.handshakes == 2
    assert events.names() == [CONNECTED]


async def test_connect_names_the_missing_request_and_closes(obs_server, make_gateway):
    obs_server.version = version_data(
        obs_version="29.1.3", available_requests=[r for r in REQUIRED_REQUESTS if r != "SetRecordDirectory"]
    )
    gateway, events = make_gateway()

    with pytest.raises(ObsUnsupportedError) as raised:
        await gateway.connect()

    assert (raised.value.obs_version, raised.value.missing) == ("29.1.3", ("SetRecordDirectory",))
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_connect_retries_not_ready_until_obs_has_loaded(obs_server, make_gateway):
    obs_server.set_reply("GetVersion", NOT_READY_REPLY, NOT_READY_REPLY, Reply(version_data()))
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)

    await gateway.connect()

    assert obs_server.request_types() == ["GetVersion"] * 3
    assert clock.sleeps == [NOT_READY_RETRY_S] * 2
    assert events.names() == [CONNECTED]


async def test_connect_raises_not_ready_after_the_timeout(obs_server, make_gateway):
    obs_server.set_reply("GetVersion", NOT_READY_REPLY)
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    start = clock.t

    with pytest.raises(ObsRequestError) as raised:
        await gateway.connect()

    assert (raised.value.request, raised.value.code, raised.value.comment) == ("GetVersion", 207, NOT_READY_COMMENT)
    assert clock.t - start >= NOT_READY_TIMEOUT_S
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_connect_fails_when_obs_is_not_listening(obs_server, make_gateway):
    obs_server.refuse_connections = True
    gateway, _ = make_gateway()

    with pytest.raises(ObsConnectError) as raised:
        await gateway.connect()

    assert type(raised.value) is ObsConnectError
    assert f"127.0.0.1:{obs_server.port}" in str(raised.value)


async def test_a_config_error_from_the_credentials_passes_through(make_gateway):
    error = ObsConfigError("OBS's websocket config.json is missing")

    def credentials() -> ObsCredentials:
        raise error

    gateway, _ = make_gateway(credentials=credentials)

    with pytest.raises(ObsConfigError) as raised:
        await gateway.connect()

    assert raised.value is error


# --- authentication (spec 17) --------------------------------------------------------------------


async def test_a_refused_password_is_read_again_once_and_the_new_one_is_used(obs_server, make_gateway):
    credentials = Credentials(obs_server, "stale-password", PASSWORD)
    gateway, events = make_gateway(credentials=credentials)

    await gateway.connect()

    assert credentials.calls == 2
    assert obs_server.auth_failures == 1
    assert events.names() == [CONNECTED]


@pytest.mark.parametrize("password", ["wrong-password", None], ids=["wrong", "missing"])
async def test_a_password_refused_twice_raises_obs_auth_error(obs_server, make_gateway, password):
    credentials = Credentials(obs_server, password)
    gateway, events = make_gateway(credentials=credentials)

    with pytest.raises(ObsAuthError):
        await gateway.connect()

    assert credentials.calls == 2
    assert obs_server.auth_failures == (2 if password else 0)
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_the_password_is_never_logged(obs_server, make_gateway, caplog):
    caplog.set_level(logging.DEBUG)
    gateway, _ = make_gateway(credentials=Credentials(obs_server, "wrong-password", PASSWORD))

    await gateway.connect()
    obs_server.set_reply("StartRecord", Reply(code=500, comment="Recording failed"))
    with pytest.raises(ObsRequestError):
        await gateway.request("StartRecord")
    await gateway.close()

    assert logging.getLogger("obsws_python").getEffectiveLevel() >= logging.WARNING
    assert caplog.records, "the gateway logs its own connection lines"
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()
        assert "wrong-password" not in record.getMessage()
    assert PASSWORD not in repr(gateway)


# --- requests ------------------------------------------------------------------------------------


async def test_request_sends_the_fields_and_returns_the_response_data(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", Reply({"outputActive": False, "outputDuration": 0}))
    gateway, _ = make_gateway()
    await gateway.connect()

    assert await gateway.request("GetRecordStatus") == {"outputActive": False, "outputDuration": 0}
    assert await gateway.request("SetCurrentProfile", profileName="Anki Miner Game") == {}

    assert [(r.request_type, dict(r.request_data)) for r in obs_server.requests[1:]] == [
        ("GetRecordStatus", {}),
        ("SetCurrentProfile", {"profileName": "Anki Miner Game"}),
    ]


@pytest.mark.parametrize(
    ("request_type", "reply", "comment"),
    [
        (
            "GetReplayBufferStatus",
            Reply(code=604, comment="Replay buffer is not available."),
            "Replay buffer is not available.",
        ),
        (
            "GetVirtualCamStatus",
            Reply(code=604, comment="VirtualCam is not available."),
            "VirtualCam is not available.",
        ),
        ("CreateSceneCollection", Reply(code=601), ""),
    ],
    ids=["replay-buffer-604", "virtual-camera-604", "without-comment"],
)
async def test_a_failed_request_raises_obs_request_error(obs_server, make_gateway, request_type, reply, comment):
    """R2 item 6: 604 from a status request is an ordinary failure carrying the code (the actor reads it as inactive)."""
    obs_server.set_reply(request_type, reply)
    gateway, events = make_gateway()
    await gateway.connect()

    with pytest.raises(ObsRequestError) as raised:
        await gateway.request(request_type)

    assert (raised.value.request, raised.value.code, raised.value.comment) == (request_type, reply.code, comment)
    assert events.names() == [CONNECTED]
    assert await gateway.request("GetRecordStatus") == {}


async def test_request_retries_not_ready_then_returns(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", NOT_READY_REPLY, NOT_READY_REPLY, Reply({"outputActive": True}))
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()

    assert await gateway.request("GetRecordStatus") == {"outputActive": True}
    assert obs_server.request_types().count("GetRecordStatus") == 3
    assert clock.sleeps == [NOT_READY_RETRY_S] * 2


async def test_request_raises_not_ready_after_the_timeout(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", NOT_READY_REPLY)
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()
    start = clock.t

    with pytest.raises(ObsRequestError) as raised:
        await gateway.request("GetRecordStatus")

    assert (raised.value.code, raised.value.comment) == (207, NOT_READY_COMMENT)
    assert clock.t - start >= NOT_READY_TIMEOUT_S


async def test_request_before_connect_raises_obs_connect_error(make_gateway):
    gateway, _ = make_gateway()

    with pytest.raises(ObsConnectError):
        await gateway.request("GetRecordStatus")


async def test_concurrent_requests_each_get_their_own_answer(obs_server, make_gateway):
    """obsws-python never matches a reply to its request, so requests must not overlap on the wire."""
    names = ["GetRecordStatus", "GetStreamStatus", "GetReplayBufferStatus", "GetVirtualCamStatus", "GetProfileList"]
    for name in names:
        obs_server.set_reply(name, Reply({"asked": name}))
    gateway, _ = make_gateway()
    await gateway.connect()

    answers = await asyncio.gather(*(gateway.request(name) for name in names * 4))

    assert [answer["asked"] for answer in answers] == names * 4


async def test_a_stalled_request_leaves_the_loop_free_and_close_ends_it(obs_server, make_gateway):
    obs_server.stall("SetCurrentProfile")
    gateway, events = make_gateway()
    await gateway.connect()
    pending = asyncio.create_task(gateway.request("SetCurrentProfile", profileName="Anki Miner Game"))

    ticks = 0
    while ticks < 20:
        await asyncio.sleep(0.005)
        ticks += 1
    assert not pending.done()

    await gateway.close()

    with pytest.raises(ObsConnectError):
        await pending
    assert events.names() == [CONNECTED]


# --- scene collection changes --------------------------------------------------------------------


async def test_collection_changing_follows_the_changing_and_changed_events(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()

    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)
    await obs_server.emit("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"})
    await wait_until(lambda: not gateway.collection_changing)

    assert events.names() == [CONNECTED, "CurrentSceneCollectionChanging", "CurrentSceneCollectionChanged"]


async def test_requests_wait_while_the_collection_changes(obs_server, make_gateway):
    gateway, _ = make_gateway(clock=FakeClock(advance=False))
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)

    pending = asyncio.create_task(gateway.request("GetRecordStatus"))
    await asyncio.sleep(0.1)
    assert obs_server.request_types() == ["GetVersion"]

    await obs_server.emit("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"})
    assert await asyncio.wait_for(pending, 5.0) == {}
    assert obs_server.request_types() == ["GetVersion", "GetRecordStatus"]


async def test_a_lost_changed_event_does_not_hold_requests_forever(obs_server, make_gateway):
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)
    start = clock.t

    assert await gateway.request("GetRecordStatus") == {}
    assert clock.t - start >= NOT_READY_TIMEOUT_S


async def test_a_lost_connection_ends_the_collection_change(obs_server, make_gateway):
    gateway, events = make_gateway(clock=FakeClock(park_from=1.0))
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)

    await obs_server.drop_clients()
    await wait_until(lambda: LOST in events.names())

    assert not gateway.collection_changing


# --- events --------------------------------------------------------------------------------------


async def test_events_arrive_as_obs_sent_them_stamped_with_the_injected_clock(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()
    clock.t = 5000.25

    await obs_server.emit("RecordStateChanged", RECORDING)
    await obs_server.emit("ExitStarted")
    await wait_until(lambda: len(events.got) == 3)

    assert [(e.name, dict(e.data), e.t_mono) for e in events.got[1:]] == [
        ("RecordStateChanged", RECORDING, 5000.25),
        ("ExitStarted", {}, 5000.25),
    ]


async def test_events_are_stamped_on_the_library_event_thread(obs_server, make_gateway):
    clock = ThreadClock()
    gateway, events = make_gateway(now=clock.now)
    await gateway.connect()

    await obs_server.emit("RecordStateChanged", RECORDING)
    await wait_until(lambda: len(events.got) == 2)

    stamped_by = clock.threads[int(events.got[1].t_mono) - 1]
    assert stamped_by is not threading.current_thread()
    assert not stamped_by.name.startswith("obs-gateway")


async def test_an_event_that_arrives_during_connect_follows_connected(obs_server, make_gateway):
    """Spec 6.3: the actor reconciles on ``_Connected`` before it trusts an event of that connection."""
    obs_server.set_reply("GetVersion", NOT_READY_REPLY, Reply(version_data()))
    clock = FakeClock(park_from=NOT_READY_RETRY_S)
    loop_thread = threading.current_thread()
    stamped_on: list[threading.Thread] = []

    def now() -> float:
        stamped_on.append(threading.current_thread())
        return clock.t

    gateway, events = make_gateway(clock=clock, now=now)
    start = clock.t
    connecting = asyncio.create_task(gateway.connect())
    await wait_until(lambda: clock.sleeps == [NOT_READY_RETRY_S])  # GetVersion got 207; connect is parked

    await obs_server.emit("RecordStateChanged", RECORDING)
    await wait_until(lambda: any(thread is not loop_thread for thread in stamped_on))  # the gateway has it
    assert events.got == []

    clock.release()
    await connecting

    assert [(e.name, dict(e.data), e.t_mono) for e in events.got] == [
        (CONNECTED, {}, start + NOT_READY_RETRY_S),
        ("RecordStateChanged", RECORDING, start),
    ]


async def test_only_the_subscribed_categories_reach_the_handlers(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()

    await obs_server.emit("InputCreated", {"inputName": "Game capture"})
    await obs_server.emit("SceneCreated", {"sceneName": "Game"})
    await obs_server.emit("CurrentProfileChanged", {"profileName": "Anki Miner Game"})
    await wait_until(lambda: len(events.got) == 2)

    assert events.names() == [CONNECTED, "CurrentProfileChanged"]


async def test_every_handler_gets_every_event_and_a_failing_one_stops_nothing(obs_server, make_gateway):
    gateway, events = make_gateway()
    later: list[str] = []

    def broken(event):
        raise RuntimeError("handler bug")

    gateway.subscribe(broken)
    gateway.subscribe(lambda event: later.append(event.name))
    await gateway.connect()

    await obs_server.emit("RecordStateChanged", RECORDING)
    await obs_server.emit("RecordFileChanged", {"newOutputPath": "/videos/_incoming/b.mkv"})
    await wait_until(lambda: len(later) == 3)

    assert events.names() == later == [CONNECTED, "RecordStateChanged", "RecordFileChanged"]


# --- close ---------------------------------------------------------------------------------------


async def test_close_disconnects_without_a_lost_event(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()

    await gateway.close()
    await obs_server.wait_for_client_count(0)
    await asyncio.sleep(0.1)

    assert events.names() == [CONNECTED]
    assert obs_server.handshakes == 2
    with pytest.raises(ObsConnectError):
        await gateway.request("GetRecordStatus")


async def test_close_releases_both_sockets(obs_server, make_gateway, ws_sockets):
    """websocket-client's own ``close()`` keeps the socket once the server has sent a close frame."""
    gateway, _ = make_gateway()
    await gateway.connect()
    assert len(ws_sockets) == 2

    await gateway.close()

    assert [sock.fileno() for sock in ws_sockets] == [-1, -1]


async def test_close_while_connect_is_under_way_leaves_nothing_open(obs_server, make_gateway):
    obs_server.set_reply("GetVersion", NOT_READY_REPLY, Reply(version_data()))
    clock = FakeClock(park_from=NOT_READY_RETRY_S)
    gateway, events = make_gateway(clock=clock)
    connecting = asyncio.create_task(gateway.connect())
    await wait_until(lambda: clock.sleeps == [NOT_READY_RETRY_S])  # both connections open, GetVersion got 207

    await gateway.close()
    clock.release()

    with pytest.raises(ObsConnectError):
        await connecting
    await obs_server.wait_for_client_count(0)
    assert events.got == []
    assert obs_server.request_types() == ["GetVersion"]  # the 207 retries stop at close()


async def test_the_gateway_connects_again_after_close(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()
    await gateway.close()

    await gateway.connect()

    assert events.names() == [CONNECTED, CONNECTED]
    assert await gateway.request("GetRecordStatus") == {}
