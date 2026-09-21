"""Fixtures for the OBS gateway tests: a live FakeObsServer, gateways closed at teardown, socket tracking."""

import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import websocket

from anki_miner_game.models.obs import ObsCredentials
from anki_miner_game.obs.client import ObsClient
from tests.fakes.fake_obs_server import FakeObsServer
from tests.obs.helpers import PASSWORD, Credentials, Events, FakeClock

MakeGateway = Callable[..., tuple[ObsClient, Events]]


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """websocket-client routes even 127.0.0.1 through ``http_proxy`` unless ``no_proxy`` lists it."""
    for var in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def obs_server() -> AsyncIterator[FakeObsServer]:
    async with FakeObsServer(password=PASSWORD) as server:
        yield server


@pytest.fixture
async def make_gateway(obs_server: FakeObsServer) -> AsyncIterator[MakeGateway]:
    """``make_gateway(credentials=None, clock=None, now=None)`` -> a subscribed gateway; all are closed at teardown."""
    made: list[ObsClient] = []

    def make(
        credentials: Callable[[], ObsCredentials] | None = None,
        clock: FakeClock | None = None,
        now: Callable[[], float] | None = None,
    ) -> tuple[ObsClient, Events]:
        clock = clock or FakeClock()
        gateway = ObsClient(credentials or Credentials(obs_server), now=now or clock.now, sleep=clock.sleep)
        events = Events()
        gateway.subscribe(events)
        made.append(gateway)
        return gateway, events

    yield make
    for gateway in made:
        await gateway.close()


@pytest.fixture
def ws_sockets(monkeypatch) -> list[socket.socket]:
    """Every socket websocket-client (under obsws-python) connected during the test, in order."""
    opened: list[socket.socket] = []
    real_connect = websocket.WebSocket.connect

    def connect(self: websocket.WebSocket, url: str, **options: Any) -> None:
        real_connect(self, url, **options)
        opened.append(self.sock)

    monkeypatch.setattr(websocket.WebSocket, "connect", connect)
    return opened
