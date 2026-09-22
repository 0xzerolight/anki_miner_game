"""The OBS gateway replaying sessions recorded from a real OBS (spec 18.2; M0 spike R2).

One test per transcript in ``tests/fixtures/obs_transcripts/``: the test sends each recorded request
through the gateway once its connection is up, checks the answer against the recording, and checks
that the handlers saw ``_Connected``, the recorded events the gateway subscribes to, and
``_ConnectionLost`` exactly where the recording has them.
"""

import asyncio
from pathlib import Path

import pytest

from anki_miner_game.models.obs import ObsRequestError
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, ObsClient
from tests.fakes.fake_obs_server import FakeObsServer, Transcript, load_transcript
from tests.fakes.obs_transcript_sample import RECORDING_PATH, write_sample_transcript
from tests.obs.helpers import CONNECTED, LOST, Credentials, Events, FakeClock, wait_until

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "obs_transcripts"
TRANSCRIPTS = sorted(FIXTURES.glob("*.jsonl"))
REPLAY_TIMEOUT_S = 20.0


async def replay(transcript: Transcript) -> tuple[Events, FakeObsServer]:
    """Drive a gateway through ``transcript`` against a replaying FakeObsServer; return what it heard."""
    idle = asyncio.Event()  # no request in flight: the server may play a recorded close now
    idle.set()
    async with FakeObsServer(transcript=transcript, before_cut=idle.wait) as server:
        clock = FakeClock()
        gateway = ObsClient(Credentials(server, None), now=clock.now, sleep=clock.sleep)
        events = Events()
        gateway.subscribe(events)
        try:
            async with asyncio.timeout(REPLAY_TIMEOUT_S):
                await gateway.connect()
                for exchange in transcript.exchanges():
                    await wait_until(lambda g=exchange.generation: events.count(CONNECTED) > g, REPLAY_TIMEOUT_S)
                    sent = gateway.request(exchange.request_type, **exchange.request_data)
                    idle.clear()
                    try:
                        if exchange.response["requestStatus"]["result"]:
                            assert await sent == dict(exchange.response.get("responseData") or {}), exchange
                        else:
                            with pytest.raises(ObsRequestError) as raised:
                                await sent
                            status = exchange.response["requestStatus"]
                            assert (raised.value.code, raised.value.comment) == (
                                status["code"],
                                status.get("comment", ""),
                            )
                    finally:
                        idle.set()
                await server.replay_finished.wait()
                expected = transcript.expected_events(EVENT_SUBSCRIPTIONS, CONNECTED, LOST)
                await wait_until(lambda: len(events.got) >= len(expected), REPLAY_TIMEOUT_S)
        except TimeoutError:
            pytest.fail(f"replay stuck at {server.replay_position}; heard {events.names()}")
        finally:
            await gateway.close()
        assert server.unscripted == [] and server.errors == []
        return events, server


async def test_the_gateway_replays_the_sample_transcript(tmp_path):
    transcript = load_transcript(write_sample_transcript(tmp_path))

    events, server = await replay(transcript)

    assert [(e.name, dict(e.data)) for e in events.got] == [
        (CONNECTED, {}),
        ("CurrentProfileChanged", {"profileName": "Anki Miner Game"}),
        (
            "RecordStateChanged",
            {"outputActive": False, "outputPath": None, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"},
        ),
        (
            "RecordStateChanged",
            {"outputActive": True, "outputPath": RECORDING_PATH, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
        ),
        ("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"}),
        ("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"}),
        (LOST, {}),
        (CONNECTED, {}),
        ("ExitStarted", {}),
        (LOST, {}),
    ]
    assert server.request_types().count("GetVersion") == 2  # the gateway's own, answered live, one per connection
    assert server.refuse_connections


def test_the_recorded_transcripts_are_present():
    assert TRANSCRIPTS, f"no R2 transcripts in {FIXTURES}: merge main after the M0 gate"


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=[path.stem for path in TRANSCRIPTS])
async def test_the_gateway_replays_a_recorded_obs_session(path):
    transcript = load_transcript(path)

    events, _ = await replay(transcript)

    assert [(e.name, dict(e.data)) for e in events.got] == transcript.expected_events(
        EVENT_SUBSCRIPTIONS, CONNECTED, LOST
    )
