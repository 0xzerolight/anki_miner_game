"""Shared pieces of the OBS gateway tests: credentials, clocks, event capture."""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsCredentials, ObsEventName
from tests.fakes.fake_obs_server import FakeObsServer

PASSWORD = "gateway-test-pw"
CONNECTED = ObsEventName.CONNECTED.value
LOST = ObsEventName.CONNECTION_LOST.value


class Credentials:
    """``ObsClient``'s credentials callable for ``server``: counts calls, hands out ``passwords`` in turn (the last repeats)."""

    def __init__(self, server: FakeObsServer, *passwords: str | None) -> None:
        self.server = server
        self.passwords = list(passwords) if passwords else [PASSWORD]
        self.calls = 0

    def __call__(self) -> ObsCredentials:
        password = self.passwords[min(self.calls, len(self.passwords) - 1)]
        self.calls += 1
        return ObsCredentials("127.0.0.1", self.server.port, password)


class FakeClock:
    """Injected ``now`` and ``sleep``: ``sleep`` records its argument and moves ``now`` on by it.

    ``advance=False`` keeps ``now`` still (a wait that never times out). ``park_from`` parks every
    sleep of at least that many seconds (a 207 retry, the reconnect backoff) until ``release()``.
    """

    def __init__(self, *, advance: bool = True, park_from: float | None = None) -> None:
        self.t = 1000.0
        self.advance = advance
        self.park_from = park_from
        self.sleeps: list[float] = []
        self._released = asyncio.Event()

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.park_from is not None and seconds >= self.park_from:
            await self._released.wait()
        if self.advance:
            self.t += seconds
        await asyncio.sleep(0.001)

    def release(self) -> None:
        self.park_from = None
        self._released.set()

    def backoffs(self) -> list[float]:
        return [s for s in self.sleeps if s >= 1.0]


class ThreadClock:
    """``now`` that records the thread of every call and returns the call's number."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def now(self) -> float:
        self.threads.append(threading.current_thread())
        return float(len(self.threads))


@dataclass
class Events:
    """A subscribed handler: every ``ObsEvent`` it was given, in order."""

    got: list[ObsEvent] = field(default_factory=list)

    def __call__(self, event: ObsEvent) -> None:
        self.got.append(event)

    def names(self) -> list[str]:
        return [event.name for event in self.got]

    def count(self, name: str) -> int:
        return self.names().count(name)


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
