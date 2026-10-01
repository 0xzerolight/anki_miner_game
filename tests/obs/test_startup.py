"""The one start-OBS sequence (D-03; spec 11.1, 17): ``LocalObsStarter`` over a fake discovery and gateway."""

import threading

import pytest

from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConnectError,
    ObsInfo,
    ObsNotReadyError,
    ObsServerOffError,
    ObsStartStage,
    ObsUnsupportedError,
)
from anki_miner_game.obs.startup import OBS_LAUNCH_TIMEOUT_S, LocalObsStarter

INFO = ObsInfo("32.2.2", "5.7.4", frozenset(REQUIRED_REQUESTS))


class Discovery:
    """The ``ObsDiscovery`` calls the starter makes, in the order it makes them."""

    def __init__(self, *, running: bool = False, ready: bool | Exception = True) -> None:
        self.running = running
        self.ready = ready
        self.enables = True
        """What ``ensure_server_enabled`` answers."""
        self.starts_meanwhile = False
        """OBS starts while the server is being turned on (``ensure_server_enabled`` then answers False)."""
        self.launch_error: Exception | None = None
        self.calls: list[object] = []

    def is_running(self) -> bool:
        self.calls.append("is_running")
        return self.running

    def ensure_server_enabled(self) -> bool:
        self.calls.append("ensure_server_enabled")
        if self.starts_meanwhile:
            self.running = True
            return False
        return self.enables

    def launch(self) -> None:
        self.calls.append("launch")
        if self.launch_error is not None:
            raise self.launch_error
        self.running = True

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        self.calls.append(("wait_ready", timeout_s))
        if isinstance(self.ready, Exception):
            raise self.ready
        return self.ready


class Gateway:
    def __init__(self, calls: list[object], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    async def connect(self) -> ObsInfo:
        self.calls.append("connect")
        if self.error is not None:
            raise self.error
        return INFO


def starter(discovery: Discovery, error: Exception | None = None, **kwargs: float) -> LocalObsStarter:
    return LocalObsStarter(discovery, Gateway(discovery.calls, error), **kwargs)


async def test_a_running_obs_is_only_connected():
    discovery = Discovery(running=True)
    stages: list[ObsStartStage] = []
    assert await starter(discovery).start(stages.append) == INFO
    assert stages == [ObsStartStage.CONNECTING]
    assert discovery.calls == ["is_running", "connect"]


async def test_a_closed_obs_gets_its_server_on_is_launched_waited_for_up_to_30_s_and_connected():
    discovery = Discovery()
    stages: list[ObsStartStage] = []
    assert await starter(discovery).start(stages.append) == INFO
    assert stages == [ObsStartStage.ENABLING_SERVER, ObsStartStage.LAUNCHING, ObsStartStage.CONNECTING]
    assert OBS_LAUNCH_TIMEOUT_S == 30.0
    assert discovery.calls == ["is_running", "ensure_server_enabled", "launch", ("wait_ready", 30.0), "connect"]


async def test_the_launch_timeout_is_the_starters_own():
    discovery = Discovery()
    await starter(discovery, timeout_s=0.5).start()
    assert ("wait_ready", 0.5) in discovery.calls


async def test_an_obs_that_does_not_answer_in_time_is_not_connected():
    discovery = Discovery(ready=False)
    with pytest.raises(ObsNotReadyError, match="OBS did not answer within 30 s; an OBS dialog may be waiting"):
        await starter(discovery).start()
    assert "connect" not in discovery.calls


async def test_an_obs_started_while_its_server_was_off_is_neither_launched_nor_connected():
    discovery = Discovery()
    discovery.starts_meanwhile = True
    stages: list[ObsStartStage] = []
    with pytest.raises(ObsServerOffError):
        await starter(discovery).start(stages.append)
    assert stages == [ObsStartStage.ENABLING_SERVER]
    assert "launch" not in discovery.calls and "connect" not in discovery.calls


async def test_without_an_install_the_launch_failure_is_raised():
    discovery = Discovery()
    discovery.enables = False  # no install: the server cannot be turned on, and OBS is not running
    discovery.launch_error = ObsConnectError("OBS Studio is not installed")
    with pytest.raises(ObsConnectError, match="not installed"):
        await starter(discovery).start()
    assert "connect" not in discovery.calls


@pytest.mark.parametrize("where", ["wait_ready", "connect"])
async def test_a_refused_password_is_raised_as_it_comes(where):
    refused = ObsAuthError("refused twice")
    discovery = Discovery(ready=refused if where == "wait_ready" else True)
    with pytest.raises(ObsAuthError):
        await starter(discovery, refused if where == "connect" else None).start()


async def test_what_connect_raises_is_raised_unchanged():
    unsupported = ObsUnsupportedError("29.1.3", ("SetInputMute",))
    with pytest.raises(ObsUnsupportedError) as raised:
        await starter(Discovery(running=True), unsupported).start()
    assert raised.value is unsupported


async def test_stages_are_reported_on_the_callers_thread():
    threads: list[int] = []
    await starter(Discovery()).start(lambda _stage: threads.append(threading.get_ident()))
    assert threads == [threading.get_ident()] * 3
