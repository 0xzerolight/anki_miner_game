"""FakeHookerServer: a hooker's broadcast server, scripted on an injected clock (spec 3.2, 18.2)."""

import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.fakes.fake_hooker import FakeHookerServer


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def _recv(ws, timeout: float = 5.0):
    async with asyncio.timeout(timeout):
        return await ws.recv()


async def test_binds_loopback_on_an_os_assigned_port():
    async with FakeHookerServer() as server:
        assert server.port > 0
        assert server.uri == f"127.0.0.1:{server.port}"


async def test_broadcast_reaches_every_connected_client():
    async with FakeHookerServer() as server, connect(f"ws://{server.uri}") as a, connect(f"ws://{server.uri}") as b:
        await server.wait_for_clients(2)
        await server.broadcast("一行目")
        await server.broadcast(b"\x00binary")
        assert [await _recv(a), await _recv(a)] == ["一行目", b"\x00binary"]
        assert [await _recv(b), await _recv(b)] == ["一行目", b"\x00binary"]


async def test_pump_sends_the_frames_due_on_the_injected_clock_in_script_order():
    clock = FakeClock()
    script = [(0.0, "first"), (1.5, "second"), (1.5, "third"), (4.0, "fourth")]
    async with FakeHookerServer(script, now=clock) as server, connect(f"ws://{server.uri}") as ws:
        await server.wait_for_clients(1)
        assert await server.pump() == ["first"]
        assert await server.pump() == []
        clock.t += 1.5
        assert await server.pump() == ["second", "third"]
        clock.t += 10.0
        assert await server.pump() == ["fourth"]
        assert [await _recv(ws) for _ in range(4)] == ["first", "second", "third", "fourth"]


async def test_script_times_count_from_start():
    clock = FakeClock(50.0)
    server = FakeHookerServer([(2.0, "late")], now=clock)
    clock.t = 100.0  # time passing before start does not count
    async with server:
        clock.t = 101.0
        assert await server.pump() == []
        clock.t = 102.0
        assert await server.pump() == ["late"]


async def test_unlisted_paths_get_404_and_every_requested_path_is_recorded():
    async with FakeHookerServer(accept_paths={"/api/ws/text/origin"}) as server:
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(f"ws://{server.uri}"):
                pass
        assert excinfo.value.response.status_code == 404
        async with connect(f"ws://{server.uri}/api/ws/text/origin"):
            await server.wait_for_clients(1)
        assert server.requested_paths == ["/", "/api/ws/text/origin"]


async def test_drop_clients_closes_every_connection():
    async with FakeHookerServer() as server, connect(f"ws://{server.uri}") as ws:
        await server.wait_for_clients(1)
        await server.drop_clients()
        async with asyncio.timeout(5):
            await ws.wait_closed()
        assert server.client_count == 0


async def test_wait_for_clients_times_out():
    async with FakeHookerServer() as server:
        with pytest.raises(TimeoutError):
            await server.wait_for_clients(1, timeout=0.05)
