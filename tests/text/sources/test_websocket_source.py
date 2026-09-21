"""WebsocketSource: a hooker's websocket client (spec 3.2, 8.1)."""

import asyncio
import itertools
import json
import socket

import pytest
from websockets.asyncio.client import connect

from anki_miner_game.models.messages import SourceStatus
from anki_miner_game.text.sources import websocket_source
from anki_miner_game.text.sources.websocket_source import WebsocketSource, parse_frame
from tests.fakes.fake_hooker import FakeHookerServer
from tests.test_contracts import _assert_conforms


def test_conforms_to_the_text_source_protocol():
    from anki_miner_game.interfaces.text_source import TextSource

    _assert_conforms(TextSource, WebsocketSource)


# --- frame parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "line"),
    [
        pytest.param("お前は誰だ？", "お前は誰だ？", id="plain text"),
        pytest.param(
            '{"sentence": "行くぞ", "time": "2026-09-21T10:00:00", "source": "Textractor"}',
            "行くぞ",
            id="dict with sentence",
        ),
        pytest.param('{"text": "行くぞ"}', '{"text": "行くぞ"}', id="dict without sentence is the frame"),
        pytest.param('{"sentence": 5}', '{"sentence": 5}', id="non-string sentence is the frame"),
        pytest.param('["行くぞ"]', '["行くぞ"]', id="JSON list is the frame"),
        pytest.param('"行くぞ"', '"行くぞ"', id="JSON string is the frame"),
        pytest.param("123", "123", id="JSON number is the frame"),
        pytest.param("null", "null", id="JSON null is the frame"),
        pytest.param("{broken", "{broken", id="invalid JSON is the frame"),
        pytest.param("", "", id="empty frame passes through for the pipeline to count"),
        pytest.param("[" * 100_000, "[" * 100_000, id="nesting too deep to parse is the frame"),
        pytest.param("行くぞ".encode(), "行くぞ", id="binary frame decoded as UTF-8"),
        pytest.param(b"\xff\xfe", "\ufffd\ufffd", id="undecodable bytes replaced"),
    ],
)
def test_parse_frame(frame, line):
    assert parse_frame(frame) == line


# --- the source against a fake hooker --------------------------------------------------------------

FALLBACK = "/api/ws/text/origin"


class FakeSleep:
    """Injected ``sleep``: records each delay and returns only once the test has released a call."""

    def __init__(self, free: int = 0) -> None:
        self.delays: list[float] = []
        self._tokens = asyncio.Semaphore(free)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self._tokens.acquire()

    def release(self) -> None:
        self._tokens.release()

    async def wait_calls(self, n: int) -> None:
        async with asyncio.timeout(5):
            while len(self.delays) < n:
                await asyncio.sleep(0.005)


class Sink:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[tuple[str, float, str]] = asyncio.Queue()

    def __call__(self, raw: str, t_mono: float, source_id: str) -> None:
        self.lines.put_nowait((raw, t_mono, source_id))

    async def next(self) -> tuple[str, float, str]:
        async with asyncio.timeout(5):
            return await self.lines.get()


class Statuses:
    """Status listener recording each call and whether ``source.status`` already reported it."""

    def __init__(self, source: WebsocketSource) -> None:
        self.source = source
        self.seen: list[SourceStatus] = []
        self.property_agreed: list[bool] = []

    def __call__(self, source_id: str, status: SourceStatus) -> None:
        assert source_id == self.source.id
        self.seen.append(status)
        self.property_agreed.append(self.source.status == status)

    async def wait_for(self, status: SourceStatus, count: int = 1) -> None:
        async with asyncio.timeout(5):
            while self.seen.count(status) < count:
                await asyncio.sleep(0.005)


async def _wait_no_clients(hooker: FakeHookerServer) -> None:
    async with asyncio.timeout(5):
        while hooker.client_count:
            await asyncio.sleep(0.005)


@pytest.fixture
async def make_source():
    made: list[WebsocketSource] = []

    def make(uri: str, **kwargs) -> WebsocketSource:
        source = WebsocketSource("textractor", uri, **kwargs)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()
        await source.wait_closed()


async def test_plain_frame_reaches_the_sink_with_the_source_id(make_source):
    sink = Sink()
    async with FakeHookerServer() as hooker:
        make_source(hooker.uri, now=lambda: 42.0).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("お前は誰だ？")
        assert await sink.next() == ("お前は誰だ？", 42.0, "textractor")


async def test_json_frame_delivers_its_sentence(make_source):
    sink = Sink()
    async with FakeHookerServer() as hooker:
        make_source(hooker.uri, now=lambda: 1.0).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast(json.dumps({"sentence": "行くぞ", "time": "2026-09-21T10:00:00", "source": "GSM"}))
        assert await sink.next() == ("行くぞ", 1.0, "textractor")


async def test_t_mono_is_read_from_the_injected_now_once_per_frame_at_receipt(make_source):
    sink = Sink()
    ticks = itertools.count(100)
    async with FakeHookerServer() as hooker:
        make_source(hooker.uri, now=lambda: float(next(ticks))).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("一")
        await hooker.broadcast("二")
        assert [await sink.next(), await sink.next()] == [("一", 100.0, "textractor"), ("二", 101.0, "textractor")]


async def test_luna_path_is_tried_at_once_when_the_handshake_is_refused(make_source):
    sink = Sink()
    sleep = FakeSleep()
    async with FakeHookerServer(accept_paths={FALLBACK}) as hooker:
        make_source(hooker.uri, sleep=sleep).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("行くぞ")
        assert (await sink.next())[0] == "行くぞ"
        assert hooker.requested_paths == ["/", FALLBACK]
        assert sleep.delays == []


async def test_luna_path_is_tried_once_per_attempt(make_source):
    sleep = FakeSleep(free=1)
    async with FakeHookerServer(accept_paths=()) as hooker:
        make_source(hooker.uri, sleep=sleep).start(Sink())
        await sleep.wait_calls(2)
        assert hooker.requested_paths == ["/", FALLBACK, "/", FALLBACK]


async def test_reconnect_backoff_is_1_2_5_10_then_every_10_s(make_source):
    sleep = FakeSleep(free=5)
    async with FakeHookerServer(accept_paths=()) as hooker:
        make_source(hooker.uri, sleep=sleep).start(Sink())
        await sleep.wait_calls(6)
        assert sleep.delays == [1.0, 2.0, 5.0, 10.0, 10.0, 10.0]


async def test_refused_connection_backs_off_without_the_luna_path(make_source):
    sleep = FakeSleep()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound_not_listening:
        bound_not_listening.bind(("127.0.0.1", 0))
        port = bound_not_listening.getsockname()[1]
        source = make_source(f"127.0.0.1:{port}", sleep=sleep)
        statuses = Statuses(source)
        source.set_status_listener(statuses)
        source.start(Sink())
        await sleep.wait_calls(1)
        assert sleep.delays == [1.0]
        assert statuses.seen == [SourceStatus.CONNECTING, SourceStatus.DISCONNECTED]


async def test_malformed_uri_backs_off_instead_of_ending_the_source(make_source):
    sleep = FakeSleep(free=1)
    make_source("localhost:notaport", sleep=sleep).start(Sink())
    await sleep.wait_calls(2)
    assert sleep.delays == [1.0, 2.0]


async def test_backoff_restarts_after_a_connection(make_source):
    sink = Sink()
    sleep = FakeSleep()
    accepted: set[str] = set()
    async with FakeHookerServer(accept_paths=accepted) as hooker:
        make_source(hooker.uri, sleep=sleep).start(sink)
        await sleep.wait_calls(1)
        sleep.release()
        await sleep.wait_calls(2)
        accepted.add("/")
        sleep.release()
        await hooker.wait_for_clients(1)
        await hooker.drop_clients()
        await sleep.wait_calls(3)
        sleep.release()
        await hooker.wait_for_clients(1)
        await hooker.broadcast("戻った")
        assert (await sink.next())[0] == "戻った"
        assert sleep.delays == [1.0, 2.0, 1.0]


async def test_connects_without_keepalive_pings_and_with_a_short_close_timeout(make_source, monkeypatch):
    # Hookers answer neither pings nor close frames: websockets' default 10 s close timeout would
    # make every stop() + wait_closed() take 10 s.
    calls = []
    real_connect = websocket_source.connect

    def spy(url, **kwargs):
        calls.append(kwargs)
        return real_connect(url, **kwargs)

    monkeypatch.setattr(websocket_source, "connect", spy)
    async with FakeHookerServer() as hooker:
        make_source(hooker.uri).start(Sink())
        await hooker.wait_for_clients(1)
    assert calls
    assert all(kwargs["ping_interval"] is None for kwargs in calls)
    assert all(kwargs["close_timeout"] == websocket_source.CLOSE_TIMEOUT_S <= 1.0 for kwargs in calls)


async def test_ignores_proxy_settings_in_the_environment(make_source, monkeypatch):
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("ws_proxy", "http_proxy", "HTTP_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    sink = Sink()
    async with FakeHookerServer() as hooker:
        make_source(hooker.uri).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("直接")
        assert (await sink.next())[0] == "直接"


async def test_status_transitions(make_source):
    sink = Sink()
    sleep = FakeSleep()
    async with FakeHookerServer() as hooker:
        source = make_source(hooker.uri, sleep=sleep)
        statuses = Statuses(source)
        source.set_status_listener(statuses)
        assert source.status is SourceStatus.DISCONNECTED
        source.start(sink)
        await hooker.wait_for_clients(1)
        await statuses.wait_for(SourceStatus.CONNECTED)
        await hooker.broadcast("一")
        await hooker.broadcast("二")
        await sink.next()
        await sink.next()
        await hooker.drop_clients()
        await sleep.wait_calls(1)
        sleep.release()
        await statuses.wait_for(SourceStatus.CONNECTED, count=2)
        source.stop()
        assert source.status is SourceStatus.DISCONNECTED
        await source.wait_closed()
        await _wait_no_clients(hooker)
    assert statuses.seen == [
        SourceStatus.CONNECTING,
        SourceStatus.CONNECTED,
        SourceStatus.RECEIVING,
        SourceStatus.DISCONNECTED,
        SourceStatus.CONNECTING,
        SourceStatus.CONNECTED,
        SourceStatus.DISCONNECTED,
    ]
    assert all(statuses.property_agreed)


async def test_stop_during_backoff_reports_nothing_new(make_source):
    sleep = FakeSleep()
    async with FakeHookerServer(accept_paths=()) as hooker:
        source = make_source(hooker.uri, sleep=sleep)
        statuses = Statuses(source)
        source.set_status_listener(statuses)
        source.start(Sink())
        await sleep.wait_calls(1)
        source.stop()
        await source.wait_closed()
    assert statuses.seen == [SourceStatus.CONNECTING, SourceStatus.DISCONNECTED]


async def test_a_later_status_listener_replaces_the_earlier_one(make_source):
    first: list[SourceStatus] = []
    second: list[SourceStatus] = []
    async with FakeHookerServer() as hooker:
        source = make_source(hooker.uri)
        source.set_status_listener(lambda _id, status: first.append(status))
        source.set_status_listener(lambda _id, status: second.append(status))
        source.start(Sink())
        await hooker.wait_for_clients(1)
        source.stop()
        await source.wait_closed()
    assert first == []
    assert second[0] is SourceStatus.CONNECTING
    assert second[-1] is SourceStatus.DISCONNECTED


async def test_starts_again_after_stop(make_source):
    sink = Sink()
    async with FakeHookerServer() as hooker:
        source = make_source(hooker.uri)
        source.start(sink)
        await hooker.wait_for_clients(1)
        source.stop()
        await source.wait_closed()
        await _wait_no_clients(hooker)
        source.start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("再開")
        assert (await sink.next())[0] == "再開"


async def test_start_twice_raises(make_source):
    async with FakeHookerServer() as hooker:
        source = make_source(hooker.uri)
        source.start(Sink())
        with pytest.raises(RuntimeError):
            source.start(Sink())


async def test_a_failing_sink_keeps_the_connection(make_source):
    received: list[str] = []

    def sink(raw: str, t_mono: float, source_id: str) -> None:
        received.append(raw)
        if raw == "壊れる":
            raise RuntimeError("sink bug")

    async with FakeHookerServer() as hooker:
        make_source(hooker.uri).start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("壊れる")
        await hooker.broadcast("続く")
        async with asyncio.timeout(5):
            while len(received) < 2:
                await asyncio.sleep(0.005)
        assert received == ["壊れる", "続く"]
        assert hooker.requested_paths == ["/"]


async def test_a_failing_status_listener_keeps_the_source_running(make_source, caplog):
    sink = Sink()
    sleep = FakeSleep()
    seen: list[SourceStatus] = []

    def listener(source_id: str, status: SourceStatus) -> None:
        seen.append(status)
        raise RuntimeError("listener bug")

    async with FakeHookerServer() as hooker:
        source = make_source(hooker.uri, sleep=sleep)
        source.set_status_listener(listener)
        source.start(sink)
        await hooker.wait_for_clients(1)
        await hooker.broadcast("一")
        await hooker.broadcast("二")
        assert [(await sink.next())[0] for _ in range(2)] == ["一", "二"]
        assert source.status is SourceStatus.RECEIVING
        await hooker.drop_clients()
        await sleep.wait_calls(1)
        sleep.release()
        await hooker.wait_for_clients(1)
        await hooker.broadcast("三")
        assert (await sink.next())[0] == "三"
        source.stop()
        await source.wait_closed()
        await _wait_no_clients(hooker)
        assert hooker.requested_paths == ["/", "/"]
    connection = [SourceStatus.CONNECTING, SourceStatus.CONNECTED, SourceStatus.RECEIVING, SourceStatus.DISCONNECTED]
    assert seen == connection * 2
    assert caplog.text.count("status listener failed") == len(seen)


async def test_the_source_and_a_texthooker_page_both_receive_every_frame(make_source):
    """Hookers broadcast, so this app listens beside a texthooker page without stealing lines (spec 3.2)."""
    sink = Sink()
    async with FakeHookerServer() as hooker, connect(f"ws://{hooker.uri}") as page:
        make_source(hooker.uri).start(sink)
        await hooker.wait_for_clients(2)
        for line in ("一", "二", "三"):
            await hooker.broadcast(line)
        assert [(await sink.next())[0] for _ in range(3)] == ["一", "二", "三"]
        async with asyncio.timeout(5):
            assert [await page.recv() for _ in range(3)] == ["一", "二", "三"]
