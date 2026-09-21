"""Fakes for the first-run wizard tests (spec 16, 11.1, 11.3).

``WizardObs`` is T14's ``FakeObs``, which sends a switch's ``...Changing`` and ``...Changed`` events
itself, with the rest of the ``ObsGateway`` surface the wizard uses: ``connect`` and the four output
status requests (spec 6.2 step 1). Its switch events reach the handlers from another thread, as
obsws-python's event thread delivers them; ``switch_mode`` picks the order R2 saw (item 5) and the
cases where OBS never answers or never sends the events. ``FakeDiscovery`` models the install, the
websocket ``config.json`` and the OBS process.
"""

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import AppState
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsCredentials,
    ObsInfo,
    ObsInstall,
    ObsRequestError,
    WsConfig,
)
from tests.obs.fake_obs import LINUX_X11_KINDS, FakeObs

INVALID_RESOURCE_STATE = 604

SWITCH_REQUESTS = frozenset({"SetCurrentProfile", "SetCurrentSceneCollection"})


class WizardObs(FakeObs):
    """``FakeObs`` plus ``connect`` and output statuses; switch events come per ``switch_mode``.

    ``switch_mode`` for every switch whose events ``FakeObs`` announces (``SetCurrentProfile``,
    ``SetCurrentSceneCollection``, ``CreateSceneCollection``; ``CreateProfile``'s switch is
    ``FakeObs``'s own):

    - ``"event_first"``: ``...Changing`` and ``...Changed`` reach the handlers before the answer.
    - ``"answer_first"``: the answer comes at once; the events wait until ``release_events()``.
    - ``"no_answer"``: the events come; the answer to a ``Set...`` never does until
      ``answer_pending()`` (R2 item 3).
    - ``"no_event"``: the answer comes, the events never do.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("input_kinds", LINUX_X11_KINDS)
        super().__init__(**kwargs)
        self.info = ObsInfo("32.2.2", "5.7.4", frozenset(REQUIRED_REQUESTS))
        self.connect_error: Exception | None = None
        self.connects = 0
        self.active = {"GetStreamStatus": False, "GetRecordStatus": False}
        self.replay_buffer: bool | None = None
        """``None``: not configured, so ``GetReplayBufferStatus`` answers 604 (R2 item 6)."""
        self.virtual_cam: bool | None = None
        self.fail: dict[str, Exception] = {}
        """Request name -> the error it raises (checked before the request runs)."""
        self.switch_mode = "event_first"
        self._held_events: list[tuple[str, dict[str, Any]]] = []
        self._pending: list[asyncio.Future[dict[str, Any]]] = []

    # ObsGateway surface ----------------------------------------------------------------------

    async def connect(self) -> ObsInfo:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        return self.info

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        if name in self.fail:
            self.calls.append((name, dict(fields)))
            raise self.fail[name]
        before = (self.current_profile, self.current_collection)
        result = await super().request(name, **fields)
        switched = (self.current_profile, self.current_collection) != before
        if name in SWITCH_REQUESTS and switched and self.switch_mode == "no_answer":
            pending: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending.append(pending)
            return await pending
        return result

    def _announce(self, *events: tuple[str, dict[str, Any]]) -> None:
        """``FakeObs``'s hook for the events of a switch it has made: sent as ``switch_mode`` says."""
        if self.switch_mode == "answer_first":
            self._held_events.extend(events)
        elif self.switch_mode != "no_event":
            self.emit_from_thread(events)

    # Test helpers ----------------------------------------------------------------------------

    def emit_from_thread(self, events: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        """Send ``events`` to every handler on another thread, as obsws-python does, and wait for it."""
        thread = threading.Thread(target=self._announce_now, args=(events,))
        thread.start()
        thread.join()

    def event_names(self) -> list[str]:
        return [name for _, name, _ in self.events]

    def release_events(self) -> None:
        held, self._held_events = tuple(self._held_events), []
        self.emit_from_thread(held)

    def answer_pending(self, loop: asyncio.AbstractEventLoop) -> None:
        """Answer every request left unanswered in ``no_answer`` mode (on ``loop``)."""
        for pending in self._pending:
            loop.call_soon_threadsafe(lambda fut=pending: fut.done() or fut.set_result({}))

    # Output statuses (spec 6.2 step 1) --------------------------------------------------------

    def _GetStreamStatus(self) -> dict[str, Any]:  # noqa: N802
        return {"outputActive": self.active["GetStreamStatus"]}

    def _GetRecordStatus(self) -> dict[str, Any]:  # noqa: N802
        return {"outputActive": self.active["GetRecordStatus"], "outputPaused": False}

    def _GetReplayBufferStatus(self) -> dict[str, Any]:  # noqa: N802
        if self.replay_buffer is None:
            raise ObsRequestError("GetReplayBufferStatus", INVALID_RESOURCE_STATE, "Replay buffer is not available.")
        return {"outputActive": self.replay_buffer}

    def _GetVirtualCamStatus(self) -> dict[str, Any]:  # noqa: N802
        if self.virtual_cam is None:
            raise ObsRequestError("GetVirtualCamStatus", INVALID_RESOURCE_STATE, "VirtualCam is not available.")
        return {"outputActive": self.virtual_cam}


class FakeDiscovery:
    """``ObsDiscovery``: an install that is found or not, a websocket config, an OBS that runs or not."""

    def __init__(
        self,
        *,
        installed: bool = True,
        ws: WsConfig | Exception | None = None,
        running: bool = True,
        ready: bool | Exception = True,
    ) -> None:
        self.install = ObsInstall(argv=("obs",), cwd=None, flatpak=False) if installed else None
        self.ws: WsConfig | Exception | None = (
            ws if ws is not None else WsConfig(server_enabled=True, port=4455, password="pw", auth_required=True)
        )
        self.running = running
        self.ready = ready
        self.calls: list[Any] = []

    def find_install(self) -> ObsInstall | None:
        self.calls.append("find_install")
        return self.install

    def config_root(self) -> Path | None:
        return Path("/obs") if self.install else None

    def read_ws_config(self) -> WsConfig | None:
        self.calls.append("read_ws_config")
        if isinstance(self.ws, Exception):
            raise self.ws
        return self.ws

    def ensure_server_enabled(self) -> bool:
        self.calls.append("ensure_server_enabled")
        if isinstance(self.ws, Exception):
            raise self.ws
        if self.ws is not None and self.ws.server_enabled:
            return True
        if self.running or self.install is None:
            return False
        base = self.ws or WsConfig(server_enabled=False, port=4455, password=None, auth_required=True)
        self.ws = replace(base, server_enabled=True, password=base.password or "generated")
        return True

    def is_running(self) -> bool:
        self.calls.append("is_running")
        return self.running

    def launch(self) -> None:
        self.calls.append("launch")
        self.running = True

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        self.calls.append(("wait_ready", timeout_s))
        if isinstance(self.ready, Exception):
            raise self.ready
        return self.ready

    def credentials(self, cfg: AppConfig) -> ObsCredentials:
        return ObsCredentials(host=cfg.obs.host, port=4455, password=cfg.obs.password_override)


class FakeSession:
    """``SessionControl`` whose state the test sets."""

    def __init__(self, state: AppState = AppState.IDLE) -> None:
        self.state = state

    def post(self, msg: object) -> None:
        raise AssertionError("the wizard never posts to the session")

    def subscribe(self, cb: object) -> None:
        raise AssertionError("the wizard never subscribes to the session")
