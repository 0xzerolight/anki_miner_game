"""The session actor (spec 4.2, 6, 7, 10.2, 12 actor side, 17) and the finalise worker (spec 10.3).

One queue of ``LineReceived``, ``ObsEvent``, ``UserCommand`` and ``Tick`` messages, consumed on the
I/O loop one at a time; every state change happens here. The actor arms a game (OBS on the app's
profile and scene collection, inputs provisioned, text sources started), turns a recording that
starts while armed into a session (manifest and journal in ``_incoming/``, lines journalled at their
record-clock offsets), ends it on ``STOPPED`` or without one (OBS exit, OBS gone after a lost
connection), and finalises it on the one ``FinaliseWorker``. Reconcile (spec 6.3) runs on every
``_Connected`` before later events are handled.

Rules this module keeps (wave-1 amendments 3 and 8, wave 2a contracts, M0 findings):

- The pipeline's "previous accepted line" is the previous journalled line: ``TextPipeline.reset()``
  after an accepted line is dropped as ``paused``, at ``STARTED`` unless held lines are journalled,
  and after a split. A ``Replaced`` is journalled as a ``ReplaceRecord`` only when its base is the
  journal's last ``LineRecord``; otherwise as a ``LineRecord`` at ``clock.offset_ms(line.t_mono)``.
- A ``START`` carrying ``UserCommand.line`` (auto mode) holds that line and every line accepted
  until ``STARTED``; on ``STARTED`` they are journalled in order (the clamp puts every line from
  before the zero at offset 0) with no pipeline reset. A failed start drops them.
- Finalise runs on ``FinaliseWorker``, one call at a time, never on the default executor pool.
- Text sources' ``start``/``stop``/``wait_closed`` run on the actor's thread; their status listener
  runs on the source's thread and is handed to the loop with ``call_soon_threadsafe``.
- A profile or collection switch is done on its ``...Changed`` event, never on the answer (R2 item
  5), and a switch to what is already current is never sent (R2 item 4: no event would come).
"""

import asyncio
import contextlib
import functools
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from anki_miner_game import paths
from anki_miner_game.interfaces.addons import VadJobs
from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, Provisioner, Recorder
from anki_miner_game.interfaces.text_source import TextSource
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import Counts
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    START_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineReceived,
    ObsEvent,
    SessionEvent,
    SessionInput,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsError,
    ObsEventName,
    ObsRequestError,
    ObsUnsupportedError,
)
from anki_miner_game.models.profile import GameProfile, validate
from anki_miner_game.session.finalise import FinaliseResult, finalise
from anki_miner_game.session.restore import ObsRestore, delete_restore, load_restore, restore_path, save_restore
from anki_miner_game.store import StoreError
from anki_miner_game.text.pipeline import TextPipeline

log = logging.getLogger(__name__)

# Record clock (spec 7), measured by R1 (docs/m0/clock.md): the zero is the receipt time of
# RecordStateChanged STARTED and the capture latency is 10 ms. Linux numbers; the Windows values
# stay provisional until H5 (master plan D2).
ZERO_EVENT: Final = "STARTED"
CAPTURE_LATENCY_MS: Final = 10

# Measured by R2 (docs/m0/obs-behaviour.md, summary items).
RESTART_QUESTION_S: Final = 3.0
"""R2 items 3 and 5: ``SetCurrentProfile`` answers within a millisecond of its ``...Changed`` event, or
not at all while OBS's modal restart question is open. No answer this long after the event is that
question."""

# Fixed by the spec.
SWITCH_TIMEOUT_S: Final = 15.0
"""Spec 6.2 step 3: one profile or scene collection switch, request to ``...Changed`` event."""
OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""Spec 17: Arm launches OBS and waits up to 30 s for it to answer."""
FREE_SPACE_WARN_BYTES: Final = 5 * 10**9
"""Spec 17: under 5 GB free at Arm, arm anyway with a warning."""

# This module's own timers.
TICK_S: Final = 1.0
"""How often ``run`` posts a ``Tick``; the resolution of every timer below."""
RESTORE_RETRY_S: Final = 10.0
"""While idle with ``obs_restore.json`` still present: how often to try the restore (or, with no
connection, to look for a running OBS) again."""

# obs-websocket request status codes (obs-websocket@1ef34bf4 src/requesthandler/types/RequestStatus.h).
INVALID_RESOURCE_STATE: Final = 604
"""``GetReplayBufferStatus`` / ``GetVirtualCamStatus`` when that output is not available at all (R2 item 6)."""

OUTPUT_CHECKS: Final = (
    ("stream", "GetStreamStatus"),
    ("recording", "GetRecordStatus"),
    ("replay buffer", "GetReplayBufferStatus"),
    ("virtual camera", "GetVirtualCamStatus"),
)
"""Spec 6.2 step 1: arming (and the restore) refuses while any of these is active."""

_LIVE: Final = frozenset({SourceStatus.CONNECTED, SourceStatus.RECEIVING})


class BannerKey(StrEnum):
    """``Banner.key`` of every banner the actor raises; ``START_FAILED_BANNER_KEY`` is the one more."""

    OBS = "obs"
    ARM = "arm"
    LOW_DISK = "low_disk"
    OBS_RESTART = "obs_restart"
    OBS_QUESTION = "obs_question"
    RESTORE = "obs_restore"
    NO_SOURCE = "no_source"
    STOP_FAILED = "stop_failed"
    FOREIGN_RECORDING = "foreign_recording"
    SESSION_FILES = "session_files"
    SPLIT = "split_unsupported"
    CLOCK = "clock_degraded"
    OBS_EXITED = "obs_exited"
    FINALISE = "finalise"
    NO_CUES = "no_cues"
    INTERNAL = "internal_error"


class FinaliseWorker:
    """Runs ``session.finalise.finalise`` on one dedicated thread, one call at a time (spec 10.3).

    Finalise's NN bump is check-then-act, so every caller under one output root shares this one
    worker: the actor, and the composition's launch-time work (wave-1 amendment 8). Never the
    default ``run_in_executor`` pool.
    """

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finalise")
        self._sleep = sleep

    async def run(self, manifest_path: Path, cfg: AppConfig) -> FinaliseResult:
        """``finalise(manifest_path, cfg)`` on the worker thread; its ``FinaliseError`` passes through."""
        call = functools.partial(finalise, manifest_path, cfg, sleep=self._sleep)
        return await asyncio.get_running_loop().run_in_executor(self._executor, call)

    def shutdown(self) -> None:
        """Wait for a running call, then stop the thread."""
        self._executor.shutdown(wait=True)


@dataclass(frozen=True)
class _Switch:
    what: str
    request: str
    event: str
    field: str


_PROFILE: Final = _Switch("profile", "SetCurrentProfile", ObsEventName.CURRENT_PROFILE_CHANGED, "profileName")
_COLLECTION: Final = _Switch(
    "scene collection",
    "SetCurrentSceneCollection",
    ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED,
    "sceneCollectionName",
)


class _SwitchTimeoutError(Exception):
    """No ``...Changed`` event within ``SWITCH_TIMEOUT_S`` of a profile or scene collection switch."""

    def __init__(self, text: str, *, unanswered: bool) -> None:
        super().__init__(text)
        self.unanswered = unanswered
        """OBS never answered the request either; the gateway drops the link when it is cancelled (T12)."""


class _RestartQuestionError(Exception):
    """OBS switched (``...Changed`` came) but holds the answer behind its modal restart question (R2 item 3).

    The switch itself is done (R2 item 5). The unanswered request is cancelled, so the gateway drops
    the link and reconnects (T12); nothing else can be sent before it.
    """

    def __init__(self) -> None:
        super().__init__("OBS is asking to restart; answer it in OBS's window")


@dataclass
class _Armed:
    profile: GameProfile
    cfg: AppConfig


@dataclass(frozen=True)
class _Shutdown:
    done: "asyncio.Future[None]"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _disk_free(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def _writable(folder: Path) -> bool:
    """Create ``folder`` when missing and prove a file can be made in it (spec 17)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        return False
    return True


class SessionActor:
    """The ``SessionControl`` of the app: one queue, consumed on the I/O loop by ``run``.

    ``post`` is safe from any thread; subscribers run on the loop and must return quickly. ``run``
    does the launch duties, then handles one message at a time until ``shutdown``; ``join`` waits
    until everything posted so far has been handled (tests and the integration suite use it).
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        gateway: ObsGateway,
        discovery: ObsDiscovery,
        provisioner: Provisioner,
        recorder: Recorder,
        finaliser: FinaliseWorker,
        get_config: Callable[[], AppConfig],
        get_profile: Callable[[str], GameProfile | None],
        source_factory: Callable[[AppConfig, GameProfile], Sequence[TextSource]],
        vad_jobs: VadJobs | None = None,
        now: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
        disk_free: Callable[[Path], int] = _disk_free,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``get_profile`` runs on the loop: pass a lookup over the profiles already loaded.

        ``source_factory(cfg, profile)`` builds the text sources of an armed game (spec 8.1).
        ``sleep`` paces only the ``Tick`` timer of ``run``.
        """
        self._loop = loop
        self._gateway = gateway
        self._discovery = discovery
        self._provisioner = provisioner
        self._recorder = recorder
        self._finaliser = finaliser
        self._get_config = get_config
        self._get_profile = get_profile
        self._source_factory = source_factory
        self._vad_jobs = vad_jobs
        self._now = now
        self._utc_now = utc_now
        self._disk_free = disk_free
        self._sleep = sleep

        self._queue: asyncio.Queue[SessionInput | _Shutdown] = asyncio.Queue()
        self._launched = asyncio.Event()
        self._subscribers: list[Callable[[SessionEvent], None]] = []
        self._waiters: list[tuple[Callable[[ObsEvent], bool], asyncio.Future[ObsEvent]]] = []
        self._banners: set[str] = set()

        self._state = AppState.IDLE
        self._armed: _Armed | None = None
        self._pipeline: TextPipeline | None = None
        self._sources: list[TextSource] = []
        self._source_status: dict[str, SourceStatus] = {}
        self._counts = Counts()
        self._held: list[GameLine] | None = None
        """Auto-start: lines accepted between a ``START`` carrying a line and ``STARTED``."""
        self._start_deadline: float | None = None
        """A ``StartRecord`` was answered; ``STARTED`` is due before this ``now()``."""

        self._connected = False
        self._obs_versions = ("unknown", "unknown")
        self._next_restore = 0.0
        """The earliest ``now()`` for the next restore of ``obs_restore.json``: after a failed try, or
        after a switch OBS left unanswered (its question may still be open)."""
        self._next_connect = 0.0
        """While idle, not connected and with ``obs_restore.json``: the next look for a running OBS."""

        gateway.subscribe(self.post)

    # --- SessionControl -------------------------------------------------------------------------

    def post(self, msg: SessionInput) -> None:
        """Enqueue ``msg``; safe from any thread. A message posted after the loop closed is dropped."""
        with contextlib.suppress(RuntimeError):  # the loop is closed: the app is quitting
            self._loop.call_soon_threadsafe(self._enqueue, msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        self._subscribers.append(cb)

    @property
    def state(self) -> AppState:
        return self._state

    # --- running --------------------------------------------------------------------------------

    async def run(self) -> None:
        """Launch duties (``_launch``), then messages one at a time until ``shutdown``.

        Posts a ``Tick`` every ``TICK_S`` meanwhile.
        """
        ticker = asyncio.create_task(self._tick_forever())
        try:
            await self._guarded(self._launch())
            self._launched.set()
            while True:
                msg = await self._queue.get()
                try:
                    if isinstance(msg, _Shutdown):
                        await self._guarded(self._shutdown())
                        msg.done.set_result(None)
                        return
                    await self._guarded(self._handle(msg))
                finally:
                    self._queue.task_done()
        finally:
            ticker.cancel()

    async def join(self) -> None:
        """Return once ``run`` has done the launch duties and handled every message already queued."""
        await self._launched.wait()
        await self._queue.join()

    async def shutdown(self) -> None:
        """Quit at the next message boundary; call it on the I/O loop while ``run`` runs.

        Stops every text source and awaits ``wait_closed``. While armed it disarms (OBS goes back
        to the user's profile).
        """
        done: asyncio.Future[None] = self._loop.create_future()
        self._queue.put_nowait(_Shutdown(done))
        await done

    async def _launch(self) -> None:
        """With OBS running, connect; the ``_Connected`` event does the rest."""
        if await asyncio.to_thread(self._discovery.is_running):
            await self._ensure_connected()

    async def _handle(self, msg: SessionInput) -> None:
        match msg:
            case ObsEvent():
                await self._on_obs_event(msg)
            case UserCommand():
                await self._on_command(msg)
            case Tick():
                await self._on_tick(msg.t_mono)

    def _enqueue(self, msg: SessionInput) -> None:
        """On the loop: wake a handler waiting for this event, then queue it like any message."""
        if isinstance(msg, ObsEvent):
            for predicate, waiter in self._waiters:
                if not waiter.done() and predicate(msg):
                    waiter.set_result(msg)
        self._queue.put_nowait(msg)

    async def _guarded(self, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception:  # the actor must keep consuming, or the whole session freezes
            log.exception("session actor: unhandled error")
            self._banner(BannerKey.INTERNAL, BannerLevel.ERROR, "Something went wrong in the session; see the log.")

    async def _tick_forever(self) -> None:
        while True:
            await self._sleep(TICK_S)
            self.post(Tick(self._now()))

    async def _shutdown(self) -> None:
        if self._state is AppState.ARMED:
            await self._to_idle()
        else:
            await self._stop_sources()

    async def _on_tick(self, t: float) -> None:
        if self._state is AppState.IDLE and restore_path().exists():
            if self._connected:
                if t >= self._next_restore:
                    self._next_restore = t + RESTORE_RETRY_S
                    await self._restore_obs()
            elif t >= self._next_connect:  # OBS opened after a launch without it (T12 retries only a lost link)
                self._next_connect = t + RESTORE_RETRY_S
                if await asyncio.to_thread(self._discovery.is_running):
                    await self._ensure_connected()  # its _Connected restores

    # --- events out -----------------------------------------------------------------------------

    def _publish(self, event: SessionEvent) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:  # a subscriber's bug must not stop the session
                log.exception("session subscriber failed on %s", type(event).__name__)

    def _set_state(self, state: AppState) -> None:
        self._state = state
        slug = None if state is AppState.IDLE or self._armed is None else self._armed.profile.slug
        self._publish(StateChanged(state, slug))

    def _banner(self, key: str, level: BannerLevel, text: str) -> None:
        self._banners.add(key)
        self._publish(BannerRaised(Banner(key, level, text)))

    def _clear(self, *keys: str) -> None:
        for key in keys:
            if key in self._banners:
                self._banners.discard(key)
                self._publish(BannerCleared(key))

    def _obs_status(self, status: SourceStatus) -> None:
        self._publish(SourceStatusChanged(OBS_SOURCE_ID, status))

    # --- OBS events -----------------------------------------------------------------------------

    async def _on_obs_event(self, ev: ObsEvent) -> None:
        match ev.name:
            case ObsEventName.CONNECTED:
                self._connected = True
                self._obs_status(SourceStatus.CONNECTED)
                self._clear(BannerKey.OBS)
                if self._state is AppState.IDLE and self._now() >= self._next_restore:
                    await self._restore_obs()
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)

    # --- commands -------------------------------------------------------------------------------

    async def _on_command(self, cmd: UserCommand) -> None:
        match cmd.kind:
            case CommandKind.ARM:
                if cmd.slug is not None:
                    await self._arm(cmd.slug)
            case CommandKind.DISARM:
                await self._disarm()

    # --- arming (spec 6.2) ----------------------------------------------------------------------

    async def _arm(self, slug: str) -> None:
        if self._state is AppState.RECORDING:
            self._banner(BannerKey.ARM, BannerLevel.INFO, "Stop the recording before arming another game.")
            return
        profile = self._get_profile(slug)
        if profile is None:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"There is no game profile {slug!r}.")
            return
        problems = validate(profile)
        if problems:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"{profile.title} cannot be armed: {'; '.join(problems)}.")
            return
        cfg = self._get_config()
        incoming = paths.incoming_dir(cfg)
        if not _writable(incoming):
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"The output folder {incoming} cannot be written to.")
            return
        if not await self._ensure_connected():
            return
        try:
            active = await self._active_outputs()
        except ObsError as exc:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"Cannot read OBS's outputs: {exc}")
            return
        if active:
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"OBS has an active {', '.join(active)}; stop it first.")
            return
        await self._stop_sources()  # arming another game while armed
        try:
            await self._switch_to_app()
            result = await self._provisioner.ensure_profile(cfg)
            await self._provisioner.ensure_collection(profile)
        except (_SwitchTimeoutError, _RestartQuestionError, ObsError, StoreError) as exc:
            # A request OBS left unanswered was cancelled, so the gateway is dropping the link, and a
            # switch now would meet OBS's open question: the restore waits for the idle retry.
            unanswered = isinstance(exc, _RestartQuestionError) or (
                isinstance(exc, _SwitchTimeoutError) and exc.unanswered
            )
            await self._to_idle(restore=not unanswered)
            self._banner(BannerKey.ARM, BannerLevel.ERROR, f"Could not prepare OBS for {profile.title}: {exc}.")
            return
        if result.needs_restart:
            self._banner(
                BannerKey.OBS_RESTART,
                BannerLevel.WARNING,
                "Some of the app's OBS settings (container, output mode or recording encoder) take effect "
                "only after you disarm and arm again.",
            )
        self._armed = _Armed(profile=profile, cfg=cfg)
        self._pipeline = TextPipeline(profile.filters)
        self._held = None
        self._start_deadline = None
        self._counts = Counts()
        self._start_sources(cfg, profile)
        self._clear(BannerKey.ARM, BannerKey.OBS_QUESTION)
        free = self._disk_free(incoming)
        if free < FREE_SPACE_WARN_BYTES:
            self._banner(
                BannerKey.LOW_DISK,
                BannerLevel.WARNING,
                f"Only {free / 1e9:.1f} GB free in {incoming}; OBS stops recording when the disk is full.",
            )
        else:
            self._clear(BannerKey.LOW_DISK)
        self._set_state(AppState.ARMED)

    async def _ensure_connected(self) -> bool:
        """Connect, launching OBS first when it is not running (spec 11.1, 17); a banner on failure."""
        if self._connected:
            return True
        self._obs_status(SourceStatus.CONNECTING)
        try:
            if not await asyncio.to_thread(self._discovery.is_running):
                await asyncio.to_thread(self._discovery.ensure_server_enabled)
                await asyncio.to_thread(self._discovery.launch)
                if not await self._discovery.wait_ready(OBS_LAUNCH_TIMEOUT_S):
                    raise ObsConnectError(
                        f"OBS did not answer within {OBS_LAUNCH_TIMEOUT_S:g} s; an OBS dialog may be waiting"
                    )
            info = await self._gateway.connect()
        except ObsUnsupportedError as exc:
            self._obs_failed(
                f"OBS {exc.obs_version} lacks {', '.join(exc.missing)}; update OBS to version 30.0 or newer."
            )
            return False
        except ObsAuthError:
            self._obs_failed("OBS rejected the websocket password; enter it in the app's settings.")
            return False
        except ObsConfigError as exc:
            self._obs_failed(f"OBS's websocket settings cannot be read ({exc}); run the setup wizard again.")
            return False
        except ObsError as exc:
            self._obs_failed(f"Cannot connect to OBS: {exc}")
            return False
        self._obs_versions = (info.obs_version, info.websocket_version)
        self._connected = True
        self._obs_status(SourceStatus.CONNECTED)
        self._clear(BannerKey.OBS)
        return True

    def _obs_failed(self, text: str) -> None:
        self._obs_status(SourceStatus.DISCONNECTED)
        self._banner(BannerKey.OBS, BannerLevel.ERROR, text)

    async def _active_outputs(self) -> list[str]:
        """Spec 6.2 step 1. An output OBS reports as not available (604) cannot be active (R2 item 6)."""
        active: list[str] = []
        for label, request in OUTPUT_CHECKS:
            try:
                status = await self._gateway.request(request)
            except ObsRequestError as exc:
                if exc.code == INVALID_RESOURCE_STATE:
                    continue
                raise
            if status.get("outputActive"):
                active.append(label)
        return active

    async def _switch_to_app(self) -> None:
        """Spec 6.2 steps 2-3: remember the user's names, then switch profile, then collection.

        An existing ``obs_restore.json`` is kept: it holds the names from before an earlier arm the
        app never disarmed. A switch to what is already current is skipped (R2 item 4). A profile or
        collection that does not exist yet is created (and made current) by the provisioner in step 4.
        """
        profiles = await self._gateway.request("GetProfileList")
        collections = await self._gateway.request("GetSceneCollectionList")
        current_profile = str(profiles.get("currentProfileName", ""))
        current_collection = str(collections.get("currentSceneCollectionName", ""))
        path = restore_path()
        try:
            saved = load_restore(path)
        except StoreError as exc:
            log.warning("replacing an unusable %s: %s", path.name, exc)
            saved = None
        if saved is None and (current_profile, current_collection) != (OBS_PROFILE_NAME, OBS_COLLECTION_NAME):
            save_restore(path, ObsRestore(profile=current_profile, collection=current_collection))
        if current_profile != OBS_PROFILE_NAME and OBS_PROFILE_NAME in (profiles.get("profiles") or []):
            await self._switch(_PROFILE, OBS_PROFILE_NAME)
        if current_collection != OBS_COLLECTION_NAME and OBS_COLLECTION_NAME in (
            collections.get("sceneCollections") or []
        ):
            await self._switch(_COLLECTION, OBS_COLLECTION_NAME)

    @contextlib.contextmanager
    def _expecting(self, predicate: Callable[[ObsEvent], bool]) -> Iterator[asyncio.Future[ObsEvent]]:
        """A future the first matching event resolves as it is queued (``_enqueue``), while a handler waits."""
        waiter: asyncio.Future[ObsEvent] = self._loop.create_future()
        entry = (predicate, waiter)
        self._waiters.append(entry)
        try:
            yield waiter
        finally:
            self._waiters.remove(entry)
            waiter.cancel()

    async def _switch(self, kind: _Switch, name: str) -> None:
        """One step of spec 6.2 step 3: ``Set...`` sent, done on its ``...Changed`` event (R2 item 5).

        The waiter is registered before the request: the event can arrive before or after the answer
        (source findings 7, R2 item 5). The event is awaited up to ``SWITCH_TIMEOUT_S``; the answer
        then up to ``RESTART_QUESTION_S`` more, because the gateway sends one request at a time and
        nothing else can go out before it. Raises ``ObsRequestError`` when OBS refuses,
        ``_SwitchTimeoutError`` without the event and ``_RestartQuestionError`` when the event came
        and the answer did not (R2 item 3). An unanswered request is cancelled on the way out.
        """
        with self._expecting(lambda ev: ev.name == kind.event and ev.data.get(kind.field) == name) as changed:
            request = asyncio.create_task(self._gateway.request(kind.request, **{kind.field: name}))
            try:
                try:
                    async with asyncio.timeout(SWITCH_TIMEOUT_S):
                        await asyncio.wait((request, changed), return_when=asyncio.FIRST_COMPLETED)
                        if request.done():
                            request.result()  # OBS refused: ObsRequestError
                            await changed  # the answer came first (R2 item 5)
                except TimeoutError:
                    raise _SwitchTimeoutError(
                        f"OBS did not switch to the {kind.what} {name!r} within {SWITCH_TIMEOUT_S:g} s",
                        unanswered=not request.done(),
                    ) from None
                if not request.done():
                    await asyncio.wait((request,), timeout=RESTART_QUESTION_S)
                if not request.done():
                    raise _RestartQuestionError()
                if (late := request.exception()) is not None:  # after its event the step is done anyway
                    log.warning("OBS switched to the %s %r but answered: %s", kind.what, name, late)
                self._clear(BannerKey.OBS_QUESTION)  # an answered switch: no question is open
            finally:
                request.cancel()  # on the wire: the gateway drops the link, as the answer would come late

    async def _restore_obs(self) -> bool:
        """Switch OBS back to ``obs_restore.json`` and delete it (spec 6.2); ``False`` when it has to wait.

        It waits while not connected, while any output is active, or after OBS refused; the next
        ``_Connected`` and every ``RESTORE_RETRY_S`` while idle try again, a failed try a full
        ``RESTORE_RETRY_S`` later. A name OBS no longer has is skipped. A profile switch OBS made but
        holds behind its restart question is done (R2 item 5); a banner points at OBS's window.
        """
        path = restore_path()
        try:
            saved = load_restore(path)
        except StoreError as exc:
            log.warning("deleting an unusable %s: %s", path.name, exc)
            delete_restore(path)
            return True
        if saved is None:
            return True
        if not self._connected:
            return False
        try:
            if await self._active_outputs():
                return False
            collections = await self._gateway.request("GetSceneCollectionList")
            if saved.collection != collections.get("currentSceneCollectionName") and saved.collection in (
                collections.get("sceneCollections") or []
            ):
                await self._switch(_COLLECTION, saved.collection)
            profiles = await self._gateway.request("GetProfileList")
            if saved.profile != profiles.get("currentProfileName") and saved.profile in (
                profiles.get("profiles") or []
            ):
                try:
                    await self._switch(_PROFILE, saved.profile)
                except _RestartQuestionError:
                    self._banner(
                        BannerKey.OBS_QUESTION,
                        BannerLevel.WARNING,
                        f"OBS switched back to your profile {saved.profile!r} and is asking whether to "
                        "restart: answer it in OBS's window.",
                    )
        except (_SwitchTimeoutError, _RestartQuestionError, ObsError) as exc:
            self._next_restore = self._now() + RESTORE_RETRY_S
            self._banner(
                BannerKey.RESTORE,
                BannerLevel.WARNING,
                f"Could not switch OBS back to your profile {saved.profile!r} ({exc}); the app tries again.",
            )
            return False
        delete_restore(path)
        self._clear(BannerKey.RESTORE)
        return True

    async def _disarm(self) -> None:
        if self._state is AppState.RECORDING:
            self._banner(BannerKey.ARM, BannerLevel.INFO, "Stop the recording before disarming.")
            return
        if self._state is AppState.ARMED:
            await self._to_idle()

    async def _to_idle(self, *, restore: bool = True) -> None:
        """Disarm, or undo a failed arm: sources stopped and closed, state ``idle``, OBS restored.

        ``restore=False`` after a switch OBS left unanswered: the gateway is dropping the link, and a
        switch now would meet OBS's open question, so the restore waits ``RESTORE_RETRY_S``.
        """
        await self._stop_sources()
        self._armed = None
        self._pipeline = None
        self._held = None
        self._start_deadline = None
        self._clear(BannerKey.NO_SOURCE, BannerKey.LOW_DISK, BannerKey.OBS_RESTART, START_FAILED_BANNER_KEY)
        if self._state is not AppState.IDLE:
            self._set_state(AppState.IDLE)
        if restore:
            await self._restore_obs()
        else:
            self._next_restore = self._now() + RESTORE_RETRY_S

    # --- text sources (spec 8.1) ----------------------------------------------------------------

    def _start_sources(self, cfg: AppConfig, profile: GameProfile) -> None:
        self._sources = list(self._source_factory(cfg, profile))
        self._source_status = {}
        for source in self._sources:
            source.set_status_listener(self._source_status_listener)
            self._source_status[source.id] = source.status
            self._publish(SourceStatusChanged(source.id, source.status))
            source.start(self._sink)

    async def _stop_sources(self) -> None:
        """Stop every source, then await ``wait_closed`` for each (owocr's tree dead, spec 14)."""
        sources, self._sources = self._sources, []
        for source in sources:
            source.stop()
        results = await asyncio.gather(*(source.wait_closed() for source in sources), return_exceptions=True)
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("text source %s did not close cleanly: %r", source.id, result)

    def _sink(self, raw: str, t_mono: float, source_id: str) -> None:
        self.post(LineReceived(raw=raw, t_mono=t_mono, source_id=source_id))

    def _source_status_listener(self, source_id: str, status: SourceStatus) -> None:
        """Runs on the source's thread (the Qt main thread for the clipboard): hand it to the loop."""
        with contextlib.suppress(RuntimeError):  # the loop is closed: the app is quitting
            self._loop.call_soon_threadsafe(self._on_source_status, source_id, status)

    def _on_source_status(self, source_id: str, status: SourceStatus) -> None:
        self._source_status[source_id] = status
        self._publish(SourceStatusChanged(source_id, status))
        if status in _LIVE:
            self._clear(BannerKey.NO_SOURCE)
