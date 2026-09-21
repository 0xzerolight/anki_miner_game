"""First-run wizard (spec 16): OBS, text sources, output folder, optional add-ons.

Step 1 (``ObsSetup``) finds OBS, turns its websocket server on or says how to, launches OBS, connects,
checks that no output is active (spec 6.2 step 1), reads the user's profile and scene collection
names, provisions the app's profile and collection while the user's profile is still current (so
``ensure_profile`` copies its audio values and re-activates the app's profile itself, spec 11.3), and
then switches OBS back to the user's names through ``ObsGateway``. Each switch back is done on its
``...Changed`` event, never on the answer, and skipped when its target is already current
(``docs/m0/obs-behaviour.md`` items 4-5). The app never restarts OBS; ``needs_restart`` is text.

The GUI reaches the rest of the app only through ``interfaces``: coroutines go to the I/O loop
through the injected ``run``, whose futures complete there, and every result comes back to the Qt
main thread through a queued signal. Failures are shown on the page, never in a modal dialog.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import Any, Final

from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, Provisioner
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import INCOMING_DIRNAME, OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import AppState, ObsEvent
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsRequestError,
    ObsUnsupportedError,
    ProvisionResult,
)
from anki_miner_game.models.profile import GameProfile

log = logging.getLogger(__name__)

OBS_DOWNLOAD_URL: Final = "https://obsproject.com/download"
OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""How long a launched OBS gets to answer ``GetVersion`` (spec 11.1, 17)."""
SWITCH_TIMEOUT_S: Final = 15.0
"""How long one switch back may take before the wizard gives up on it (spec 6.2 step 3)."""
NOT_AVAILABLE: Final = 604
"""obs-websocket ``InvalidResourceState``: the replay buffer or virtual camera is not configured or
not installed, so it cannot be active (``docs/m0/obs-behaviour.md`` item 6)."""

OUTPUT_CHECKS: Final = (
    ("streaming", "GetStreamStatus"),
    ("recording", "GetRecordStatus"),
    ("running its replay buffer", "GetReplayBufferStatus"),
    ("running its virtual camera", "GetVirtualCamStatus"),
)
"""Spec 6.2 step 1: ``ensure_profile`` and ``ensure_collection`` need every output inactive."""

SETUP_PROFILE: Final = GameProfile(slug="setup", title="Setup")
"""The game profile the wizard provisions the collection with: default capture, no pinned window.
Arming provisions the collection again for the armed game (spec 11.3)."""

NEEDS_RESTART_TEXT: Final = (
    "OBS was already on the app's profile, so it applies the new recording format only once that "
    "profile is activated again: in OBS, choose another profile and then the app's one again "
    "(Profile menu), or restart OBS yourself. The app never restarts OBS."
)


def obs_changes_text(cfg: AppConfig) -> str:
    """What the app changes in OBS, shown before step 1 runs (spec 11.1, 11.3; R2 item 14)."""
    incoming = PurePath(cfg.output_root) / INCOMING_DIRNAME
    return "\n".join(
        [
            "What the app changes in OBS:",
            "- Turns on OBS's websocket server when it is off, only while OBS is closed. Its port and "
            "password stay as they are; a password is created only when OBS requires one and has none.",
            f'- Adds a profile "{OBS_PROFILE_NAME}" that records .mkv files into {incoming}, at most '
            f"{cfg.recording.max_height} lines high at {cfg.recording.fps} fps, without file splitting or "
            "automatic remux, with your profile's audio sample rate and channels.",
            "- Creating that profile turns off OBS's offer to run its auto-configuration wizard for new " "profiles.",
            f'- Adds a scene collection "{OBS_COLLECTION_NAME}" whose scene "Game" holds the game capture '
            "and audio.",
            "- Your own profiles and scene collections keep their settings. OBS uses the app's profile and "
            "collection only while a game is armed, and after this step it is switched back to yours. "
            "The app never restarts OBS.",
        ]
    )


class ObsStatus(StrEnum):
    READY = "ready"
    NOT_INSTALLED = "not_installed"
    SERVER_OFF = "server_off"
    """The websocket server is off while OBS runs: the user turns it on, or closes OBS (spec 11.1 step 3)."""
    NOT_READY = "not_ready"
    AUTH_FAILED = "auth_failed"
    UNSUPPORTED = "unsupported"
    OUTPUT_ACTIVE = "output_active"
    BUSY = "busy"
    """A game is armed or recording; provisioning now would change the armed game's scene."""
    FAILED = "failed"


@dataclass(frozen=True)
class ObsCheck:
    """The outcome of one run of step 1."""

    status: ObsStatus
    text: str
    notes: tuple[str, ...] = ()
    """Warnings shown under the text: ``needs_restart``, a switch back that did not happen."""


@dataclass(frozen=True)
class _Switch:
    what: str
    menu: str
    list_request: str
    current_key: str
    request: str
    field: str
    event: str


_PROFILE: Final = _Switch(
    "profile",
    "Profile",
    "GetProfileList",
    "currentProfileName",
    "SetCurrentProfile",
    "profileName",
    ObsEventName.CURRENT_PROFILE_CHANGED,
)
_COLLECTION: Final = _Switch(
    "scene collection",
    "Scene Collection",
    "GetSceneCollectionList",
    "currentSceneCollectionName",
    "SetCurrentSceneCollection",
    "sceneCollectionName",
    ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED,
)
_APP_NAMES: Final = {_PROFILE: OBS_PROFILE_NAME, _COLLECTION: OBS_COLLECTION_NAME}


@dataclass
class _Waiter:
    event: str
    field: str
    target: str
    future: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class ObsSetup:
    """Wizard step 1 (spec 16): runs on the I/O loop; every run starts again from the top.

    ``session`` (optional) is asked for its state: while a game is armed or recording the step
    touches nothing. The user's profile and collection names are remembered across runs, so a run
    after a switch back that failed still returns to them rather than to the app's names.
    Subscribes to ``gateway`` once, at the first run; the handler only wakes a switch waiting for
    that event.
    """

    def __init__(
        self,
        discovery: ObsDiscovery,
        gateway: ObsGateway,
        provisioner: Provisioner,
        *,
        session: SessionControl | None = None,
        launch_timeout_s: float = OBS_LAUNCH_TIMEOUT_S,
        switch_timeout_s: float = SWITCH_TIMEOUT_S,
    ) -> None:
        self._discovery = discovery
        self._gateway = gateway
        self._provisioner = provisioner
        self._session = session
        self._launch_timeout_s = launch_timeout_s
        self._switch_timeout_s = switch_timeout_s
        self._home: dict[_Switch, str] = {}
        self._subscribed = False
        self._lock = threading.Lock()
        self._waiters: list[_Waiter] = []

    async def run(self, cfg: AppConfig, report: Callable[[str], None] = lambda _stage: None) -> ObsCheck:
        """One pass of step 1; ``report(stage)`` hears what it is doing, on the loop's thread."""
        if self._session is not None and self._session.state is not AppState.IDLE:
            return ObsCheck(ObsStatus.BUSY, "A game is armed. Disarm it first, then run this step again.")
        if not self._subscribed:
            self._gateway.subscribe(self._on_event)
            self._subscribed = True
        report("Looking for OBS…")
        if await asyncio.to_thread(self._discovery.find_install) is None:
            return ObsCheck(
                ObsStatus.NOT_INSTALLED,
                f"OBS Studio is not installed. Install it from {OBS_DOWNLOAD_URL}, then check again.",
            )
        try:
            blocked = await self._start_obs(report)
            if blocked is not None:
                return blocked
            report("Connecting to OBS…")
            info = await self._gateway.connect()
        except ObsAuthError:
            return ObsCheck(
                ObsStatus.AUTH_FAILED,
                "OBS rejected the websocket password. Enter the password shown in OBS under "
                "Tools -> WebSocket Server Settings -> Show Connect Info, then try again.",
            )
        except ObsUnsupportedError as exc:
            return ObsCheck(
                ObsStatus.UNSUPPORTED,
                f"OBS {exc.obs_version} lacks {', '.join(exc.missing)}. Update OBS to version 30.0 or "
                "newer, then check again.",
            )
        except ObsError as exc:
            return ObsCheck(ObsStatus.FAILED, f"Cannot connect to OBS: {exc}")
        try:
            active = await self._active_outputs()
        except ObsError as exc:
            return ObsCheck(ObsStatus.FAILED, f"Cannot read OBS's outputs: {exc}")
        if active:
            return ObsCheck(
                ObsStatus.OUTPUT_ACTIVE, f"OBS is {' and '.join(active)}. Stop it in OBS, then check again."
            )
        return await self._provision(cfg, info, report)

    async def _start_obs(self, report: Callable[[str], None]) -> ObsCheck | None:
        """Websocket server on and OBS running and ready (spec 11.1 steps 2-3); a check when blocked."""
        ws = await asyncio.to_thread(self._discovery.read_ws_config)
        running = await asyncio.to_thread(self._discovery.is_running)
        if ws is None or not ws.server_enabled:
            off = ObsCheck(
                ObsStatus.SERVER_OFF,
                "OBS's websocket server is off. In OBS, tick Tools -> WebSocket Server Settings -> "
                "Enable WebSocket server and press OK, then press Fix. Or close OBS and press Fix: "
                "the app turns it on.",
            )
            if running:
                return off
            report("Turning on OBS's websocket server…")
            if not await asyncio.to_thread(self._discovery.ensure_server_enabled):
                return off  # OBS started meanwhile
        if not running:
            report("Starting OBS…")
            await asyncio.to_thread(self._discovery.launch)
            if not await self._discovery.wait_ready(self._launch_timeout_s):
                return ObsCheck(
                    ObsStatus.NOT_READY,
                    f"OBS did not answer within {self._launch_timeout_s:g} s. If OBS shows a dialog "
                    '(such as "OBS Studio Crash Detected"), answer it, then check again.',
                )
        return None

    async def _active_outputs(self) -> list[str]:
        active: list[str] = []
        for label, request in OUTPUT_CHECKS:
            try:
                status = await self._gateway.request(request)
            except ObsRequestError as exc:
                if exc.code == NOT_AVAILABLE:
                    continue
                raise
            if status.get("outputActive"):
                active.append(label)
        return active

    async def _provision(self, cfg: AppConfig, info: ObsInfo, report: Callable[[str], None]) -> ObsCheck:
        for switch in (_PROFILE, _COLLECTION):
            current = await self._current(switch)
            if current is not None and current != _APP_NAMES[switch]:
                self._home[switch] = current
        report("Setting up the app's profile and scene collection in OBS…")
        result: ProvisionResult | None = None
        failure: str | None = None
        try:
            result = await self._provisioner.ensure_profile(cfg)
            await self._provisioner.ensure_collection(SETUP_PROFILE)
        except ObsError as exc:
            failure = f"Setting up OBS failed: {exc}"
        except Exception:
            log.exception("setting up OBS failed")
            failure = "Setting up OBS failed unexpectedly; see the log."
        report("Switching OBS back to your profile and scene collection…")
        notes = [note for switch in (_PROFILE, _COLLECTION) if (note := await self._switch_back(switch))]
        if failure is not None:
            return ObsCheck(ObsStatus.FAILED, failure, tuple(notes))
        if result is not None and result.needs_restart:
            notes.insert(0, NEEDS_RESTART_TEXT)
        return ObsCheck(
            ObsStatus.READY,
            f"OBS {info.obs_version} is set up: the app's profile and scene collection are ready.",
            tuple(notes),
        )

    async def _current(self, switch: _Switch) -> str | None:
        name = (await self._gateway.request(switch.list_request)).get(switch.current_key)
        return name if isinstance(name, str) and name else None

    async def _switch_back(self, switch: _Switch) -> str | None:
        """Return OBS to the user's profile or collection; a note for the user when that failed."""
        target = self._home.get(switch)
        if target is None:
            return None
        try:
            await self._switch(switch, target)
        except (ObsError, TimeoutError) as exc:
            reason = f"no answer within {self._switch_timeout_s:g} s" if isinstance(exc, TimeoutError) else str(exc)
            log.warning("switching OBS back to the %s %r failed: %s", switch.what, target, reason)
            return (
                f'OBS could not be switched back to your {switch.what} "{target}" ({reason}). '
                f"Choose it in OBS's {switch.menu} menu."
            )
        return None

    async def _switch(self, switch: _Switch, target: str) -> None:
        """``Set...`` and wait for its ``...Changed`` event; nothing when ``target`` is current (R2 items 4-5)."""
        if await self._current(switch) == target:
            return
        loop = asyncio.get_running_loop()
        waiter = _Waiter(switch.event, switch.field, target, loop.create_future(), loop)
        with self._lock:
            self._waiters.append(waiter)
        send = asyncio.ensure_future(self._gateway.request(switch.request, **{switch.field: target}))
        first: set[asyncio.Future[Any]] = {waiter.future, send}
        try:
            async with asyncio.timeout(self._switch_timeout_s):
                done, _ = await asyncio.wait(first, return_when=asyncio.FIRST_COMPLETED)
                if waiter.future not in done:
                    send.result()  # raises when OBS refused the switch
                    await waiter.future  # answered first: the switch is done only on its event
        except TimeoutError:
            if not send.done():
                send.cancel()  # the gateway drops a connection whose request was cancelled
            raise
        finally:
            with self._lock:
                self._waiters.remove(waiter)
            if not send.done():
                send.add_done_callback(_consume)  # the answer may never come (R2 item 3)

    def _on_event(self, event: ObsEvent) -> None:
        """On obsws-python's thread: wake a switch waiting for exactly this event, nothing else.

        Matched here, at arrival, so an event that came before its waiter existed never counts.
        """
        with self._lock:
            matched = [
                waiter
                for waiter in self._waiters
                if event.name == waiter.event and event.data.get(waiter.field) == waiter.target
            ]
        for waiter in matched:
            with contextlib.suppress(RuntimeError):  # the loop has closed
                waiter.loop.call_soon_threadsafe(_resolve, waiter.future)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _consume(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.debug("a switch answered late: %s", task.exception())
