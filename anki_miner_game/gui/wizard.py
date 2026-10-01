"""Setup wizard (spec 16): OBS, game text, optional extras.

Step 1 (``ObsSetup``) finds OBS, says how to turn its websocket server on while OBS runs with it off,
has the shared ``ObsStarter`` turn it on, launch OBS and connect (D-03), checks that no output is
active (spec 6.2 step 1), reads the user's profile and scene collection names, provisions the app's
profile and collection while the user's profile is still current (so ``ensure_profile`` copies its
audio values and re-activates the app's profile itself, spec 11.3), and then switches OBS back to the
user's names through ``ObsGateway``. Each switch back is done on its ``...Changed`` event, never on
the answer, and skipped when its target is already current (``docs/m0/obs-behaviour.md`` items 4-5).
The app never restarts OBS; ``needs_restart`` is text.

The GUI reaches the rest of the app only through ``interfaces``: coroutines go to the I/O loop
through the injected ``run``, whose futures complete there, and every result comes back to the Qt
main thread through a queued signal. Failures are shown on the page, never in a modal dialog.
"""

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from typing import Any, Final

from PyQt6.QtCore import QDir, QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from anki_miner_game.interfaces.addons import AddonService
from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, ObsStarter, Provisioner
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.interfaces.text_source import TextSource
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import AppConfig, TextSourceConfig
from anki_miner_game.models.constants import INCOMING_DIRNAME, OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import AppState, ObsEvent, SourceStatus
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsNotReadyError,
    ObsRequestError,
    ObsServerOffError,
    ObsStartStage,
    ObsUnsupportedError,
    ProvisionResult,
)
from anki_miner_game.models.profile import GameProfile

log = logging.getLogger(__name__)

OBS_DOWNLOAD_URL: Final = "https://obsproject.com/download"
OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""How long a launched OBS gets to answer ``GetVersion`` (spec 11.1, 17): the app's ``ObsStarter`` waits
this long; the wizard names it when OBS did not answer."""
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


def native_path(path: str) -> str:
    """``path`` as the user's system writes it: ``~`` expanded, native separators (UJ-33)."""
    return QDir.toNativeSeparators(os.path.expanduser(path))


def obs_changes_text(cfg: AppConfig) -> str:
    """What the app changes in OBS (spec 11.1, 11.3; R2 item 14): the OBS page's "What exactly changes in OBS"."""
    incoming = native_path(os.path.join(os.path.expanduser(cfg.output_root), INCOMING_DIRNAME))
    return "\n".join(
        [
            "- Turns on OBS's WebSocket server when it is off, only while OBS is closed. Its port and "
            "password stay as they are; a password is created only when OBS requires one and has none.",
            f'- Adds a profile "{OBS_PROFILE_NAME}" that records .mkv files into {incoming}, at most '
            f"{cfg.recording.max_height} lines high at {cfg.recording.fps} fps, without file splitting or "
            "automatic remux, with your profile's audio sample rate and channels.",
            "- Creating that profile turns off OBS's offer to run its auto-configuration wizard for new profiles.",
            f'- Adds a scene collection "{OBS_COLLECTION_NAME}" whose scene "Game" holds the game capture '
            "and audio.",
            "- Your own profiles and scene collections keep their settings. OBS uses the app's profile and "
            "collection only while a game is ready or recording, and after this step it is switched back to "
            "yours. The app never restarts OBS.",
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
    """A game is ready or recording; provisioning now would change its scene."""
    FAILED = "failed"


@dataclass(frozen=True)
class ObsCheck:
    """The outcome of one run of step 1."""

    status: ObsStatus
    text: str
    notes: tuple[str, ...] = ()
    """Warnings shown under the text: ``needs_restart``, a switch back that did not happen."""


_STAGE_TEXT: Final = {
    ObsStartStage.ENABLING_SERVER: "Turning on OBS's WebSocket server…",
    ObsStartStage.LAUNCHING: "Starting OBS…",
    ObsStartStage.CONNECTING: "Connecting to OBS…",
}
"""What step 1 says while the starter runs (spec 16: the page shows each stage)."""


def _server_off() -> ObsCheck:
    return ObsCheck(
        ObsStatus.SERVER_OFF,
        "OBS's WebSocket server is off. In OBS, tick Tools -> WebSocket Server Settings -> "
        "Enable WebSocket server and press OK, then press Fix. Or close OBS and press Fix: "
        "the app turns it on.",
    )


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
_BUSY: Final = ObsCheck(
    ObsStatus.BUSY, "A game is ready or recording. Press Done playing first, then run this step again."
)


@dataclass
class _Waiter:
    event: str
    field: str
    target: str
    future: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class ObsSetup:
    """Wizard step 1 (spec 16): runs on the I/O loop; every run starts again from the top.

    ``starter`` is the app's one start-OBS sequence (D-03): it turns the websocket server on while OBS
    is closed, launches OBS and connects. ``session`` (optional) is asked for its state: while a game
    is armed or recording the step touches nothing. The user's profile and collection names are
    remembered across runs, so a run after a switch back that failed still returns to them rather than
    to the app's names. Subscribes to ``gateway`` once, at the first run; the handler only wakes a
    switch waiting for that event. A run holds ``obs_lock``, which the session actor takes to arm and to
    restore OBS, from start to end and asks for the state again once it has it; its own lock when
    ``None``.
    """

    def __init__(
        self,
        discovery: ObsDiscovery,
        gateway: ObsGateway,
        provisioner: Provisioner,
        *,
        starter: ObsStarter,
        session: SessionControl | None = None,
        switch_timeout_s: float = SWITCH_TIMEOUT_S,
        obs_lock: asyncio.Lock | None = None,
    ) -> None:
        self._discovery = discovery
        self._gateway = gateway
        self._provisioner = provisioner
        self._starter = starter
        self._session = session
        self._switch_timeout_s = switch_timeout_s
        self._obs_lock = obs_lock if obs_lock is not None else asyncio.Lock()
        self._home: dict[_Switch, str] = {}
        self._subscribed = False
        self._lock = threading.Lock()
        self._waiters: list[_Waiter] = []

    async def run(self, cfg: AppConfig, report: Callable[[str], None] = lambda _stage: None) -> ObsCheck:
        """One pass of step 1; ``report(stage)`` hears what it is doing, on the loop's thread."""
        if self._busy():
            return _BUSY
        if self._obs_lock.locked():
            report("Waiting for the app to finish switching OBS…")
        async with self._obs_lock:
            if self._busy():  # armed while this waited for the lock
                return _BUSY
            return await self._run(cfg, report)

    def _busy(self) -> bool:
        return self._session is not None and self._session.state is not AppState.IDLE

    async def _run(self, cfg: AppConfig, report: Callable[[str], None]) -> ObsCheck:
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
            server_on = await self._server_on()
            if not server_on and await asyncio.to_thread(self._discovery.is_running):
                return _server_off()  # spec 11.1 step 3: the app turns the server on only while OBS is closed

            def stage(stage: ObsStartStage) -> None:
                if not (stage is ObsStartStage.ENABLING_SERVER and server_on):  # nothing to turn on
                    report(_STAGE_TEXT[stage])

            info = await self._starter.start(stage)
        except ObsServerOffError:  # OBS started while the server was being turned on
            return _server_off()
        except ObsNotReadyError:
            return ObsCheck(
                ObsStatus.NOT_READY,
                f"OBS did not answer within {OBS_LAUNCH_TIMEOUT_S:g} s. If OBS shows a dialog "
                '(such as "OBS Studio Crash Detected"), answer it, then check again.',
            )
        except ObsAuthError:
            return ObsCheck(
                ObsStatus.AUTH_FAILED,
                "OBS rejected the WebSocket password. Enter the password shown in OBS under "
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

    async def _server_on(self) -> bool:
        """Whether OBS's config has its WebSocket server on (``False`` without a config yet)."""
        ws = await asyncio.to_thread(self._discovery.read_ws_config)
        return ws is not None and ws.server_enabled

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
        try:
            names = {switch: await self._current(switch) for switch in (_PROFILE, _COLLECTION)}
        except ObsError as exc:
            return ObsCheck(ObsStatus.FAILED, f"Cannot read OBS's current profile and scene collection: {exc}")
        for switch, current in names.items():
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


# The wizard ---------------------------------------------------------------------------------------

Runner = Callable[[Coroutine[Any, Any, Any]], concurrent.futures.Future[Any]]
"""Runs a coroutine on the app's I/O loop and returns its future, as
``asyncio.run_coroutine_threadsafe(coro, loop)`` does; the future completes on the loop's thread."""


class WizardStep(IntEnum):
    """The wizard's pages in order (spec 16 as amended by UJ-17); ``SetupWizard(start=...)`` opens one."""

    OBS = 0
    SOURCES = 1
    ADDONS = 2


SOURCE_STATUS_TEXT: Final = {
    SourceStatus.DISCONNECTED: "not connected",
    SourceStatus.CONNECTING: "connecting",
    SourceStatus.CONNECTED: "connected",
    SourceStatus.RECEIVING: "receiving",
}
WAITING_FOR_A_LINE: Final = "waiting for a line"

CLIPBOARD_TEXT: Final = "The clipboard: a game's profile can also take lines copied to the clipboard."
WAYLAND_CLIPBOARD_TEXT: Final = (
    "On Wayland the app sees the clipboard only while one of its windows has focus, so lines copied "
    "while you play are missed there. Use a websocket hooker (Textractor, Agent or LunaTranslator) "
    "instead."
)

ADDON_STATUS_TEXT: Final = {
    AddonStatus.MISSING: "Not installed",
    AddonStatus.INSTALLING: "Installing…",
    AddonStatus.READY: "Installed",
    AddonStatus.BROKEN: "Damaged; install it again to repair it",
}
PROGRESS_STEPS: Final = 1000


def is_wayland_session() -> bool:
    """A Linux desktop running Wayland, where Qt sees the clipboard only while focused (spec 8.1)."""
    return sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _plain_label(text: str = "") -> QLabel:
    """A wrapping label that never reads its text as markup: OBS messages and hooked lines are data."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class _MainThread(QObject):
    """Runs each callable posted from any thread on the thread this object lives in (the Qt main thread)."""

    _call = pyqtSignal(object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._call.connect(self._run)

    def post(self, fn: Callable[[], None]) -> None:
        with contextlib.suppress(RuntimeError):  # the wizard is gone; nobody is left to show the result
            self._call.emit(fn)

    @pyqtSlot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("the setup wizard failed to show a result")


class SetupWizard(QWizard):
    """The setup wizard (spec 16 as amended): (1) OBS, (2) game text, (3) optional extras. A first run
    walks all three, numbered; ``single_step=True`` shows only the ``start`` page with Finish on it (a
    banner's Set up OBS…, Settings' Set up OBS…/Test…/Install…, a game profile's Install…).

    ``save_config(cfg)`` stores ``cfg`` and makes it the app's current config: the OBS gateway reads
    a typed password override through it at its next connect. It is called when a password is typed
    after OBS refused one. ``source_factory`` builds a text
    source for one configured source; the wizard starts the enabled ones while step 2 is shown and
    stops them when it is left. ``run`` puts coroutines on the I/O loop. ``wayland`` defaults to the
    current session. ``open_url`` opens the OBS-download link (a frozen Linux build passes one that
    drops the bundle's ``LD_LIBRARY_PATH``, spec: ``app.open_url``); defaults to
    ``QDesktopServices.openUrl``.
    """

    def __init__(
        self,
        *,
        obs: ObsSetup,
        config: AppConfig,
        save_config: Callable[[AppConfig], object],
        run: Runner,
        source_factory: Callable[[TextSourceConfig], TextSource],
        vad_addon: AddonService,
        ocr_addon: AddonService,
        start: WizardStep = WizardStep.OBS,
        single_step: bool = False,
        wayland: bool | None = None,
        open_url: Callable[[QUrl], object] = QDesktopServices.openUrl,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.single_step = single_step
        self.setWindowTitle("Anki Miner Game setup")
        self.setTitleFormat(Qt.TextFormat.PlainText)
        self.setSubTitleFormat(Qt.TextFormat.PlainText)
        self._config = config
        self._save_config = save_config
        self._run = run
        self.open_url = open_url
        self.main_thread = _MainThread(self)
        self.obs_page = ObsPage(self, obs)
        self.sources_page = SourcesPage(self, source_factory, is_wayland_session() if wayland is None else wayland)
        self.addons_page = AddonsPage(self, vad_addon, ocr_addon)
        steps = (
            (WizardStep.OBS, self.obs_page),
            (WizardStep.SOURCES, self.sources_page),
            (WizardStep.ADDONS, self.addons_page),
        )
        for number, (step, page) in enumerate(steps, start=1):
            if not single_step:
                page.setTitle(f"Step {number} of {len(steps)}: {page.title()}")
            self.setPage(step, page)
        self.setStartId(start)
        self.currentIdChanged.connect(self._page_changed)

    @property
    def config(self) -> AppConfig:
        return self._config

    def store(self, cfg: AppConfig) -> str | None:
        """Save ``cfg`` and use it from now on; the text to show when it cannot be saved."""
        try:
            self._save_config(cfg)
        except Exception as exc:
            log.warning("the settings could not be saved: %s", exc)
            return f"The settings could not be saved: {exc}"
        self._config = cfg
        return None

    def submit(
        self, coro: Coroutine[Any, Any, Any], done: Callable[[concurrent.futures.Future[Any]], None] | None = None
    ) -> None:
        """Run ``coro`` on the I/O loop; ``done(future)`` runs on the Qt main thread when it ends."""
        try:
            future = self._run(coro)
        except RuntimeError as exc:  # the I/O loop has stopped (the app is quitting)
            coro.close()
            future = concurrent.futures.Future()
            future.set_exception(exc)
        if done is not None:
            future.add_done_callback(lambda fut: self.main_thread.post(lambda: done(fut)))

    def _page_changed(self, page_id: int) -> None:
        """Sources run only while step 2 shows; closing the wizard moves to page ``-1``."""
        if page_id == WizardStep.SOURCES:
            self.sources_page.start_sources()
        else:
            self.sources_page.stop_sources()


class ObsPage(QWizardPage):
    """Step 1: shows what the app changes in OBS, then runs ``ObsSetup`` when the user asks."""

    def __init__(self, wizard: SetupWizard, obs: ObsSetup) -> None:
        super().__init__()
        self._wizard = wizard
        self._obs = obs
        self._check: ObsCheck | None = None
        self._running = False
        self.setTitle("OBS")
        self.setSubTitle("Find OBS, turn on its websocket server and create the app's profile and scene collection.")
        self.changes = _plain_label(obs_changes_text(wizard.config))
        self.button = QPushButton("Set up OBS")
        self.button.clicked.connect(self._start)
        self.status = _plain_label()
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.link = QLabel(f'<a href="{OBS_DOWNLOAD_URL}">{OBS_DOWNLOAD_URL}</a>')
        self.link.setTextFormat(Qt.TextFormat.RichText)
        self.link.setOpenExternalLinks(False)
        self.link.linkActivated.connect(lambda href: wizard.open_url(QUrl(href)))
        self.link.hide()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("OBS websocket password")
        self.password.hide()
        self.notes = _plain_label()
        self.notes.hide()
        row = QHBoxLayout()
        row.addWidget(self.button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        for widget in (self.changes, self.status, self.link, self.password, self.notes):
            layout.addWidget(widget)
        layout.insertLayout(1, row)
        layout.addStretch(1)

    def initializePage(self) -> None:
        self.changes.setText(obs_changes_text(self._wizard.config))

    def isComplete(self) -> bool:
        return not self._running and self._check is not None and self._check.status is ObsStatus.READY

    def nextId(self) -> int:
        """A single-step run ends on this page (UJ-17)."""
        return -1 if self._wizard.single_step else super().nextId()

    def _start(self) -> None:
        if self._running:
            return
        typed = self.password.text()
        if self._check is not None and self._check.status is ObsStatus.AUTH_FAILED and typed:
            cfg = self._wizard.config
            error = self._wizard.store(replace(cfg, obs=replace(cfg.obs, password_override=typed)))
            if error is not None:
                self.status.setText(error)
                return
        self._running = True
        self.button.setEnabled(False)
        for widget in (self.link, self.password, self.notes):
            widget.hide()
        self.status.setText("Starting…")
        self.completeChanged.emit()
        post = self._wizard.main_thread.post

        def report(stage: str) -> None:
            post(lambda: self.status.setText(stage))

        self._wizard.submit(self._obs.run(self._wizard.config, report), self._finished)

    def _finished(self, future: concurrent.futures.Future[Any]) -> None:
        try:
            check: ObsCheck = future.result()
        except Exception:
            log.exception("setting up OBS failed")
            check = ObsCheck(ObsStatus.FAILED, "Setting up OBS failed unexpectedly; see the log.")
        self._running = False
        self._check = check
        self.status.setText(check.text)
        self.link.setVisible(check.status is ObsStatus.NOT_INSTALLED)
        self.password.setVisible(check.status is ObsStatus.AUTH_FAILED)
        self.notes.setText("\n".join(check.notes))
        self.notes.setVisible(bool(check.notes))
        self.button.setText(
            "Fix"
            if check.status is ObsStatus.SERVER_OFF
            else "Run again" if check.status is ObsStatus.READY else "Check again"
        )
        self.button.setEnabled(True)
        self.completeChanged.emit()


@dataclass
class _SourceRow:
    status: QLabel
    line: QLabel


class SourcesPage(QWizardPage):
    """Step 2: each enabled text source, "waiting for a line" until one arrives (spec 16)."""

    def __init__(self, wizard: SetupWizard, factory: Callable[[TextSourceConfig], TextSource], wayland: bool) -> None:
        super().__init__()
        self._wizard = wizard
        self._factory = factory
        self._sources: list[TextSource] = []
        self._generation = 0
        self.rows: dict[str, _SourceRow] = {}
        self.setTitle("Text sources")
        self.setSubTitle(
            "Start your text hooker and play until a line shows. Each source below says "
            f'"{WAITING_FOR_A_LINE}" until one arrives.'
        )
        self._grid = QGridLayout()
        self.empty = _plain_label("No text source is enabled. Add one in Settings.")
        self.empty.hide()
        self.clipboard = _plain_label(f"{CLIPBOARD_TEXT} {WAYLAND_CLIPBOARD_TEXT}" if wayland else CLIPBOARD_TEXT)
        layout = QVBoxLayout(self)
        layout.addLayout(self._grid)
        layout.addWidget(self.empty)
        layout.addWidget(self.clipboard)
        layout.addStretch(1)

    def start_sources(self) -> None:
        """Build the rows and start one source per enabled config on the I/O loop."""
        self.stop_sources()
        self._clear_rows()
        self._generation += 1
        generation = self._generation
        enabled = [cfg for cfg in self._wizard.config.text_sources if cfg.enabled]
        self.empty.setVisible(not enabled)
        post = self._wizard.main_thread.post
        started: list[tuple[TextSource, Callable[[str, SourceStatus], None], Callable[[str, float, str], None]]] = []
        for index, cfg in enumerate(enabled):
            row = _SourceRow(
                _plain_label(SOURCE_STATUS_TEXT[SourceStatus.DISCONNECTED]), _plain_label(WAITING_FOR_A_LINE)
            )
            self.rows[cfg.id] = row
            self._grid.addWidget(_plain_label(cfg.name), index, 0)
            self._grid.addWidget(_plain_label(cfg.uri), index, 1)
            self._grid.addWidget(row.status, index, 2)
            self._grid.addWidget(row.line, index, 3)
            self._grid.setColumnStretch(3, 1)

            def on_status(_id: str, status: SourceStatus, row: _SourceRow = row) -> None:
                post(lambda: self._show(generation, row.status, SOURCE_STATUS_TEXT[status]))

            def on_line(raw: str, _t_mono: float, _id: str, row: _SourceRow = row) -> None:
                post(lambda: self._show(generation, row.line, raw))

            started.append((self._factory(cfg), on_status, on_line))
        self._sources = [source for source, _, _ in started]
        if started:
            self._wizard.submit(_start_sources(started), _log_failure("starting the text sources"))

    def nextId(self) -> int:
        """A single-step run ends on this page (UJ-17)."""
        return -1 if self._wizard.single_step else super().nextId()

    def stop_sources(self) -> None:
        """Stop what ``start_sources`` started, on the I/O loop, and wait for each to close."""
        sources, self._sources = self._sources, []
        self._generation += 1
        if sources:
            self._wizard.submit(_stop_sources(sources), _log_failure("stopping the text sources"))

    def _show(self, generation: int, label: QLabel, text: str) -> None:
        """Only the sources started last update the rows; older rows are gone or no longer listened to."""
        if generation == self._generation:
            label.setText(text)

    def _clear_rows(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.rows = {}


async def _start_sources(
    started: list[tuple[TextSource, Callable[[str, SourceStatus], None], Callable[[str, float, str], None]]],
) -> None:
    for source, on_status, on_line in started:
        source.set_status_listener(on_status)
        source.start(on_line)


async def _stop_sources(sources: list[TextSource]) -> None:
    for source in sources:
        source.stop()
    for source in sources:
        await source.wait_closed()


def _log_failure(what: str) -> Callable[[concurrent.futures.Future[Any]], None]:
    def check(future: concurrent.futures.Future[Any]) -> None:
        if not future.cancelled() and future.exception() is not None:
            log.error("the setup wizard: %s failed", what, exc_info=future.exception())

    return check


def saved_in_text(cfg: AppConfig) -> str:
    """The last page's line (UJ-17): where sessions go, as the system writes the path (UJ-33)."""
    return f"Sessions are saved in {native_path(cfg.output_root)} (change it in Settings)."


class _AddonRow:
    """One add-on on step 4: size, platform note, status, Install, progress, and why an install failed."""

    def __init__(self, wizard: SetupWizard, service: AddonService, title: str, description: str) -> None:
        self._wizard = wizard
        self._service = service
        self._installing = False
        self.title = QLabel(f"<b>{title}</b>")
        self.description = _plain_label(description)
        self.size = _plain_label(f"Download: about {round(service.size_bytes / 1_000_000)} MB")
        self.note = _plain_label(service.note or "")
        self.note.setVisible(bool(service.note))
        self.status = _plain_label()
        self.button = QPushButton("Install")
        self.button.clicked.connect(self.install)
        self.progress = QProgressBar()
        self.progress.hide()
        self.error = _plain_label()
        self.error.hide()

    def widgets(self) -> list[QWidget]:
        return [self.title, self.description, self.size, self.note, self.status, self.button, self.progress, self.error]

    def refresh(self) -> None:
        status = AddonStatus.INSTALLING if self._installing else self._service.status()
        self.status.setText(ADDON_STATUS_TEXT[status])
        self.button.setText("Repair" if status is AddonStatus.BROKEN else "Install")
        self.button.setVisible(status is not AddonStatus.READY)
        self.button.setEnabled(status in (AddonStatus.MISSING, AddonStatus.BROKEN))

    def install(self) -> None:
        if self._installing:
            return
        self._installing = True
        self.error.hide()
        self.progress.setRange(0, PROGRESS_STEPS)
        self.progress.setValue(0)
        self.progress.show()
        self.refresh()
        post = self._wizard.main_thread.post

        def progress(done: int, total: int) -> None:
            post(lambda: self._show_progress(done, total))

        self._wizard.submit(self._service.install(progress), self._finished)

    def _show_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)  # busy: the size is not known
            return
        self.progress.setRange(0, PROGRESS_STEPS)
        self.progress.setValue(min(PROGRESS_STEPS, done * PROGRESS_STEPS // total))

    def _finished(self, future: concurrent.futures.Future[Any]) -> None:
        self._installing = False
        self.progress.hide()
        exc = future.exception() if not future.cancelled() else None
        if isinstance(exc, RuntimeError):
            self.error.setText(str(exc))
            self.error.show()
        elif exc is not None:
            log.error("an add-on install failed", exc_info=exc)
            self.error.setText("The install failed; see the log.")
            self.error.show()
        self.refresh()


class AddonsPage(QWizardPage):
    """Step 4: the optional add-ons with their sizes, installed through ``AddonService`` (spec 13.1, 14)."""

    def __init__(self, wizard: SetupWizard, vad: AddonService, ocr: AddonService) -> None:
        super().__init__()
        self._wizard = wizard
        self.setTitle("Optional add-ons")
        self.setSubTitle("Each one downloads only when you install it. You can also install them later from Settings.")
        self.rows = [
            _AddonRow(
                wizard,
                vad,
                "Voice detection (VAD)",
                "After a session, ends each subtitle where the voice ends, so cards carry less music.",
            ),
            _AddonRow(
                wizard,
                ocr,
                "OCR (owocr)",
                "Reads a game's text from the screen, for games no text hooker can read.",
            ),
        ]
        layout = QVBoxLayout(self)
        for row in self.rows:
            for widget in row.widgets():
                layout.addWidget(widget)
        self.saved_in = _plain_label(saved_in_text(wizard.config))
        self.saved_in.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.saved_in.setVisible(not wizard.single_step)
        layout.addWidget(self.saved_in)
        layout.addStretch(1)

    def initializePage(self) -> None:
        self.saved_in.setText(saved_in_text(self._wizard.config))
        for row in self.rows:
            row.refresh()

    def nextId(self) -> int:
        """A single-step run ends on this page (UJ-17)."""
        return -1 if self._wizard.single_step else super().nextId()
