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
  before the zero at offset 0) with no pipeline reset. A failed start drops them. They went out as
  ``LineAccepted`` without an offset when accepted, so each is published once more with its offset,
  between ``StateChanged(RECORDING)`` and ``RecordingStarted``: the window counts the journalled
  lines from these events, and the text feed does not send them a second time.
- Finalise runs on ``FinaliseWorker``, one call at a time, never on the default executor pool.
- Text sources' ``start``/``stop``/``wait_closed`` run on the actor's thread; their status listener
  runs on the source's thread and is handed to the loop with ``call_soon_threadsafe``.
- A profile or collection switch is done on its ``...Changed`` event, never on the answer (R2 item
  5), and a switch to what is already current is never sent (R2 item 4: no event would come).
- The app never pauses a recording: pause edges come only from OBS's ``PAUSED`` / ``RESUMED``
  events (M0 ruling on R1 finding 1; provisioning gives the app's profile its own recording encoder).
- Drift samples are taken on the ``EventClock`` only, and the ``OutputDurationClock`` adds the lag
  the latest one measured (spec 7 as amended).
- Arming and the restore hold ``obs_lock`` while they read and switch OBS's profile and scene
  collection. The composition shares it with the window picker's idle listing and the wizard's OBS
  step, which switch them too: an arm never saves the app's collection a listing made current as the
  user's, and a restore never lands in the middle of their provisioning.
"""

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, Final

from anki_miner_game import __version__, paths
from anki_miner_game.interfaces.addons import VadJobs
from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, Provisioner, Recorder
from anki_miner_game.interfaces.text_source import TextSource
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import (
    ClockKind,
    ClockRecord,
    Counts,
    DriftSample,
    Flag,
    GameRef,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    START_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    LineReceived,
    ObsEvent,
    RecordingStarted,
    RecordingStopped,
    SessionEvent,
    SessionFinalised,
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
    OutputState,
    ProvisionResult,
)
from anki_miner_game.models.pipeline import DROP_COUNTER, Accepted, Dropped
from anki_miner_game.models.profile import GameProfile, validate
from anki_miner_game.session.clock import EventClock, OutputDurationClock
from anki_miner_game.session.finalise import FinaliseError, FinaliseResult, finalise
from anki_miner_game.session.journal import (
    Journal,
    JournalRecord,
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
)
from anki_miner_game.session.manifest import (
    MANIFEST_SUFFIX,
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    reserve_index,
    write_manifest_atomic,
)
from anki_miner_game.session.restore import ObsRestore, delete_restore, load_restore, restore_path, save_restore
from anki_miner_game.store import StoreError, create_temp_file
from anki_miner_game.text.pipeline import TextPipeline

log = logging.getLogger(__name__)

# Record clock (spec 7), measured by R1 (docs/m0/clock.md): the zero is the receipt time of
# RecordStateChanged STARTED and the capture latency is 10 ms. Linux numbers; the Windows values
# stay provisional until H5 (master plan D2).
ZERO_EVENT: Final = "STARTED"
CAPTURE_LATENCY_MS: Final = 10

# Measured by R2 (docs/m0/obs-behaviour.md, summary items).
START_TIMEOUT_S: Final = 10.0
"""R2 item 11: a failed start answers ``StartRecord`` 100, shows its reason in an OBS modal and sends
no ``STARTED``; no ``STARTED`` within 10 s and ``GetRecordStatus`` inactive is the failure. R1 saw
``STARTED`` 7-184 ms after the request."""
RECORD_OUTPUT_NAMES: Final = ("simple_file_output", "adv_file_output")
"""R2 item 8: the file output of ``[Output] Mode`` (Simple, Advanced); its ``GetOutputSettings``
``path`` is the file being recorded, the first file even after a split (item 10). They are tried in
turn rather than picked by reading ``[Output] Mode``: a running output keeps its handler through a
profile switch (source findings section 8), so the current profile's mode need not be the
recording's, and the absent output answers 600."""
RESTART_QUESTION_S: Final = 3.0
"""R2 items 3 and 5: ``SetCurrentProfile`` answers within a millisecond of its ``...Changed`` event, or
not at all while OBS's modal restart question is open. No answer this long after the event is that
question."""
QUIT_STOP_TIMEOUT_S: Final = 5.0
"""Quit while recording: how long to wait for ``STOPPED`` after ``StopRecord`` (R2 item 9: 0.6-1.3 s)."""
RECORD_INACTIVE_WAIT_S: Final = 1.0
"""Quit, after ``STOPPED``: how long to wait for ``GetRecordStatus`` to say inactive before the restore
(R2 item 9: it still says active for about 170 ms)."""
RECORD_INACTIVE_POLL_S: Final = 0.05

# Fixed by the spec.
SWITCH_TIMEOUT_S: Final = 15.0
"""Spec 6.2 step 3: one profile or scene collection switch, request to ``...Changed`` event."""
OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""Spec 17: Arm launches OBS and waits up to 30 s for it to answer."""
FREE_SPACE_WARN_BYTES: Final = 5 * 10**9
"""Spec 17: under 5 GB free at Arm, arm anyway with a warning."""
REANCHOR_S: Final = 10.0
"""Spec 7: the ``OutputDurationClock`` re-anchors every 10 s."""
DRIFT_SAMPLE_AFTER_S: Final = 10.0
"""Spec 7 as amended: ``outputDuration`` is sampled at ``STARTED``, this long after it, at each
``RESUMED`` and at stop."""

# This module's own timers.
TICK_S: Final = 1.0
"""How often ``run`` posts a ``Tick``; the resolution of every timer below."""
OBS_GONE_CHECK_S: Final = 5.0
"""While recording with the connection lost, and after a launch that found OBS not running: how
often to ask whether OBS still runs (spec 6.4)."""
OBS_GONE_ANSWERS: Final = 2
"""How many "not running" answers in a row make OBS gone; ``is_running`` answers ``True`` when it
cannot tell, so one ``False`` is a confident answer, and two keep a process-list blip from ending,
or finalising as an orphan, a recording OBS still writes."""
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
    the link and reconnects (T12); nothing else can be sent before it. ``text`` replaces the message
    when provisioning made the switch and gave up on it (``SessionActor._ensure_profile``).
    """

    def __init__(self, text: str = "OBS is asking to restart; answer it in OBS's window") -> None:
        super().__init__(text)


@dataclass
class _Armed:
    profile: GameProfile
    cfg: AppConfig


@dataclass
class _Session:
    manifest_path: Path
    manifest: SessionManifest
    """As last written, apart from the fields ``_write_manifest`` fills from the live state."""
    files: IncomingFiles
    journal: Journal
    clock: EventClock | OutputDurationClock
    """``EventClock`` from ``STARTED``; ``OutputDurationClock`` after reconcile rows 3 and 4."""
    samples: list[DriftSample] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    tail: GameLine | None = None
    """The line behind the journal's last ``LineRecord``, with its latest text."""
    tail_offset: int | None = None
    stop_journalled: bool = False
    """The journal holds the session's stop (``STOPPING``, a quit or a split); later lines are shown,
    not journalled."""
    next_sample: float | None = None
    """``now()`` of the drift sample due ``DRIFT_SAMPLE_AFTER_S`` after ``STARTED``."""
    lag_ms: int | None = None
    """On the ``OutputDurationClock``: the encoder lag added to ``outputDuration`` (``_lag_ms``)."""
    next_anchor: float | None = None
    """On the ``OutputDurationClock`` (``clock_degraded``): when to re-anchor next."""


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
        fd, probe = create_temp_file(folder)
        os.close(fd)
        probe.unlink()
    except OSError:
        return False
    return True


def _same_line(a: GameLine, b: GameLine) -> bool:
    """A typewriter merge keeps its base line's ``t_mono`` and ``source_id`` (``TextPipeline``)."""
    return a.t_mono == b.t_mono and a.source_id == b.source_id


def _with_flag(flags: tuple[Flag, ...], flag: Flag) -> tuple[Flag, ...]:
    return flags if flag in flags else (*flags, flag)


def _file_name(output_path: str) -> str:
    return PureWindowsPath(output_path).name  # both separators, like session.manifest.incoming_files


def _lag_ms(samples: Sequence[DriftSample]) -> int | None:
    """Spec 7 as amended: ``at_ms - output_duration_ms`` of the latest sample whose duration is above 0.

    ``None`` without one (the sample at ``STARTED`` reads 0: no frame has reached the output yet).
    """
    for sample in reversed(samples):
        if sample.output_duration_ms > 0:
            return sample.at_ms - sample.output_duration_ms
    return None


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
        obs_lock: asyncio.Lock | None = None,
    ) -> None:
        """``get_profile`` runs on the loop: pass a lookup over the profiles already loaded.

        ``source_factory(cfg, profile)`` builds the text sources of an armed game (spec 8.1).
        ``sleep`` paces only the ``Tick`` timer of ``run``. ``obs_lock`` is the lock shared with
        everything else that switches OBS's profile or collection (module docstring); its own when
        ``None``.
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
        self._obs_lock = obs_lock if obs_lock is not None else asyncio.Lock()
        self._holding_obs = False
        """The handler running now holds ``_obs_lock``: an arm, whose undo restores under the same hold."""

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
        self._session: _Session | None = None
        self._counts = Counts()
        self._held: list[GameLine] | None = None
        """Auto-start: lines accepted between a ``START`` carrying a line and ``STARTED``."""
        self._start_deadline: float | None = None
        """A ``StartRecord`` was answered; ``STARTED`` is due before this ``now()``."""

        self._connected = False
        self._obs_versions = ("unknown", "unknown")
        self._lost_ms: int | None = None
        """While recording: the clock reading when the connection dropped, the stop if OBS turns out gone."""
        self._next_gone_check = 0.0
        self._gone_answers = 0
        """Consecutive ``is_running() == False`` answers since the connection dropped while recording,
        or since a launch found OBS not running (``_confirming_absence``)."""
        self._confirming_absence = False
        """The launch found OBS not running: the tick asks again every ``OBS_GONE_CHECK_S``, the orphans
        are finalised after ``OBS_GONE_ANSWERS`` ``False`` answers in a row, and a ``True`` connects
        instead. Any connection ends it (its reconcile handles the orphans)."""
        self._next_restore = 0.0
        """The earliest ``now()`` for the next restore of ``obs_restore.json``: after a failed try, or
        after a switch OBS left unanswered (its question may still be open)."""
        self._next_connect = 0.0
        """While idle, not connected and with ``obs_restore.json``: the next look for a running OBS."""
        self._launch_swept = False
        """The first orphan sweep after launch ran; only it retries ``finalise_pending`` (spec 10.3, 17)."""

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
        to the user's profile). While recording it first stops OBS and finalises the session, then
        disarms; if OBS cannot be stopped, the journal is closed and the next launch resumes the
        session (reconcile row 4) or finalises it (last row). While idle and connected with
        ``obs_restore.json`` still present, it restores once. At most about ``QUIT_STOP_TIMEOUT_S``
        plus one finalise (up to 10 s of rename retries on Windows) plus ``RECORD_INACTIVE_WAIT_S``
        plus the restore's two switches (up to ``SWITCH_TIMEOUT_S`` each, plus
        ``RESTART_QUESTION_S`` when OBS asks to restart).
        """
        done: asyncio.Future[None] = self._loop.create_future()
        self._queue.put_nowait(_Shutdown(done))
        await done

    async def _launch(self) -> None:
        """Launch duties (spec 6.2, 6.3, 10.3, 17 "Unclean previous exit").

        OBS running: connect; the ``_Connected`` event reconciles, restores ``obs_restore.json``
        and finalises orphans. OBS not running: the tick confirms it the way it confirms "OBS gone"
        (``OBS_GONE_ANSWERS`` answers in a row) before it finalises every session left in
        ``_incoming/``, since a wrong answer would rename a video OBS still writes; the restore
        waits for the next connection.
        """
        if await asyncio.to_thread(self._discovery.is_running):
            await self._ensure_connected()
        else:
            self._confirming_absence = True
            self._gone_answers = 1
            self._next_gone_check = self._now() + OBS_GONE_CHECK_S

    async def _handle(self, msg: SessionInput) -> None:
        match msg:
            case LineReceived():
                self._on_line(msg)
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
        if self._state is AppState.RECORDING:
            await self._stop_for_quit()
        if self._state is AppState.ARMED:
            await self._await_record_inactive()  # a recording that just stopped, here or before the quit
            await self._to_idle()
            return
        await self._stop_sources()
        s = self._session
        if s is not None:  # OBS did not stop: the next launch resumes the session (row 4) or finalises it (row 6)
            await self._write_manifest(s)
            s.journal.close()
        elif self._state is AppState.IDLE and self._connected and restore_path().exists():
            # A disarm right after STOPPED found the recording still active and left the restore to the
            # idle tick (R2 item 9); a quit before that tick restores once, when OBS says inactive.
            await self._await_record_inactive()
            await self._restore_obs()

    async def _on_tick(self, t: float) -> None:
        if self._start_deadline is not None and t >= self._start_deadline:
            await self._start_timed_out()
        if self._confirming_absence and not self._connected and t >= self._next_gone_check:
            self._next_gone_check = t + OBS_GONE_CHECK_S
            if await asyncio.to_thread(self._discovery.is_running):
                self._confirming_absence = False
                await self._ensure_connected()  # its _Connected reconciles, finalises orphans and restores
                return
            self._gone_answers += 1
            if self._gone_answers >= OBS_GONE_ANSWERS:
                self._confirming_absence = False
                await self._sweep_orphans(exclude=None)  # OBS confirmed absent: nothing can be recording
        s = self._session
        if s is not None and not self._connected and self._lost_ms is not None and t >= self._next_gone_check:
            self._next_gone_check = t + OBS_GONE_CHECK_S
            if await asyncio.to_thread(self._discovery.is_running):
                self._gone_answers = 0
            else:
                self._gone_answers += 1
                if self._gone_answers >= OBS_GONE_ANSWERS:
                    await self._obs_gone(self._lost_ms)  # spec 6.4: OBS gone after a lost connection
                    return
        if s is not None and self._connected and s.next_anchor is not None and t >= s.next_anchor:
            s.next_anchor = t + REANCHOR_S
            await self._reanchor(s)
        if s is not None and s.next_sample is not None and t >= s.next_sample:
            s.next_sample = None
            await self._sample_drift()
        if self._state is AppState.IDLE and restore_path().exists():
            if self._connected:
                if t >= self._next_restore:
                    self._next_restore = t + RESTORE_RETRY_S
                    await self._restore_obs()
            elif t >= self._next_connect:  # OBS opened after a launch without it (T12 retries only a lost link)
                self._next_connect = t + RESTORE_RETRY_S
                if await asyncio.to_thread(self._discovery.is_running):
                    # its _Connected restores; a failure here (S5-2) keeps whatever banner is already
                    # shown rather than replacing it with this quiet background attempt's own text
                    await self._ensure_connected(banner_on_failure=False)

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
                self._confirming_absence = False
                self._obs_status(SourceStatus.CONNECTED)
                self._clear(BannerKey.OBS)
                await self._reconcile()
                self._lost_ms = None
            case ObsEventName.CONNECTION_LOST:
                self._connected = False
                self._obs_status(SourceStatus.DISCONNECTED)
                if self._session is not None:  # spec 17: lines keep being journalled on the EventClock
                    # Taken now, before any later line raises the clock's floor: every line journalled
                    # after the loss lies past this stop, so no cue runs past a crashed OBS's video.
                    self._lost_ms = self._session.clock.reading_ms(ev.t_mono)
                    self._next_gone_check = ev.t_mono + OBS_GONE_CHECK_S
                    self._gone_answers = 0
            case ObsEventName.EXIT_STARTED:  # R2 item 12: a clean exit; no STOPPED follows
                if self._session is not None:
                    await self._obs_gone(self._session.clock.reading_ms(ev.t_mono))
            case ObsEventName.RECORD_FILE_CHANGED:
                await self._on_split(ev)
            case ObsEventName.RECORD_STATE_CHANGED:  # keyed on outputState: PAUSED has outputActive false
                match ev.data.get("outputState"):
                    case OutputState.STARTED:
                        await self._on_started(ev)
                    case OutputState.STOPPING:
                        self._on_stopping(ev)
                    case OutputState.STOPPED:
                        await self._on_stopped(ev)
                    case OutputState.PAUSED:
                        await self._on_pause_edge(ev, paused=True)
                    case OutputState.RESUMED:
                        await self._on_pause_edge(ev, paused=False)

    # --- commands -------------------------------------------------------------------------------

    async def _on_command(self, cmd: UserCommand) -> None:
        match cmd.kind:
            case CommandKind.ARM:
                if cmd.slug is not None:
                    await self._arm(cmd.slug)
            case CommandKind.DISARM:
                await self._disarm()
            case CommandKind.START:
                await self._start(cmd.line)
            case CommandKind.STOP:
                await self._stop()
            case CommandKind.TOGGLE:
                if self._state is AppState.RECORDING:
                    await self._stop()
                elif self._state is AppState.ARMED:
                    await self._start(None)

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
        async with self._owning_obs():
            await self._arm_obs(profile, cfg, incoming)

    async def _arm_obs(self, profile: GameProfile, cfg: AppConfig, incoming: Path) -> None:
        """Spec 6.2 steps 1-3 and the arm itself, under ``_obs_lock`` until the state is ``armed``."""
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
            collections = await self._save_user_names()
            result = await self._ensure_profile(cfg)
            await self._switch_to_app_collection(collections)
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

    @contextlib.asynccontextmanager
    async def _owning_obs(self) -> AsyncIterator[None]:
        """Hold ``_obs_lock`` while OBS's profile and collection are read and switched.

        Inside a hold (an arm's undo restores) it holds nothing more: the actor handles one message at
        a time, so the flag cannot belong to another handler.
        """
        if self._holding_obs:
            yield
            return
        async with self._obs_lock:
            self._holding_obs = True
            try:
                yield
            finally:
                self._holding_obs = False

    async def _ensure_connected(self, *, banner_on_failure: bool = True) -> bool:
        """Connect, launching OBS first when it is not running (spec 11.1, 17); a banner on failure.

        ``banner_on_failure=False`` (the idle restore retry, S5-2): a failure keeps whatever banner
        is already shown instead of replacing it with this attempt's own, usually less specific, text.
        """
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
                f"OBS {exc.obs_version} lacks {', '.join(exc.missing)}; update OBS to version 30.0 or newer.",
                banner=banner_on_failure,
            )
            return False
        except ObsAuthError:
            self._obs_failed(
                "OBS rejected the websocket password; enter it in the app's settings.", banner=banner_on_failure
            )
            return False
        except ObsConfigError as exc:
            self._obs_failed(
                f"OBS's websocket settings cannot be read ({exc}); run the setup wizard again.",
                banner=banner_on_failure,
            )
            return False
        except ObsError as exc:
            self._obs_failed(f"Cannot connect to OBS: {exc}", banner=banner_on_failure)
            return False
        self._obs_versions = (info.obs_version, info.websocket_version)
        self._connected = True
        self._obs_status(SourceStatus.CONNECTED)
        self._clear(BannerKey.OBS)
        return True

    def _obs_failed(self, text: str, *, banner: bool = True) -> None:
        self._obs_status(SourceStatus.DISCONNECTED)
        if banner:
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

    async def _save_user_names(self) -> dict[str, Any]:
        """Spec 6.2 step 2: write the current profile and collection names to ``obs_restore.json``.

        An existing ``obs_restore.json`` is kept: it holds the names from before an earlier arm the
        app never disarmed. Returns ``GetSceneCollectionList``'s answer for the collection switch.
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
        return collections

    async def _ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        """Spec 6.2 step 3's profile switch and spec 11.3's profile rows, both made by ``ensure_profile``.

        It is called while the user's profile is still current, so it copies that profile's audio
        sample rate and channels into the app's before it leaves it (spec 11.3). Were the app's profile
        made current first, there would be nothing to copy from, and every switch between two profiles
        whose values differ stops at OBS's restart question (R2 item 3): arming would fail at each try.

        The switch into the app's profile still asks once when the values differ now. When its
        ``CurrentProfileChanged`` has come and provisioning has not finished ``RESTART_QUESTION_S``
        later, a banner points at OBS's window while provisioning waits for the answer up to its own
        switch timeout. A failure after that raises ``_RestartQuestionError`` with the provisioner's
        text: the question may still be open, so the restore waits.
        """

        def to_app(ev: ObsEvent) -> bool:
            return ev.name == ObsEventName.CURRENT_PROFILE_CHANGED and ev.data.get("profileName") == OBS_PROFILE_NAME

        with self._expecting(to_app) as changed:
            provisioning = asyncio.ensure_future(self._provisioner.ensure_profile(cfg))
            try:
                await asyncio.wait((provisioning, changed), return_when=asyncio.FIRST_COMPLETED)
                if not provisioning.done():
                    await asyncio.wait((provisioning,), timeout=RESTART_QUESTION_S)
                asked = not provisioning.done()
                if asked:
                    self._banner(
                        BannerKey.OBS_QUESTION,
                        BannerLevel.WARNING,
                        "OBS is asking to restart: answer it in OBS's window (No keeps OBS running and "
                        "lets arming go on).",
                    )
                try:
                    result = await provisioning
                except ObsError as exc:
                    if asked:
                        raise _RestartQuestionError(str(exc)) from exc
                    raise
            finally:
                provisioning.cancel()  # only while this arm is itself cancelled
        self._clear(BannerKey.OBS_QUESTION)
        return result

    async def _switch_to_app_collection(self, collections: dict[str, Any]) -> None:
        """Spec 6.2 step 3's collection switch; ``collections`` is ``GetSceneCollectionList``'s answer.

        Skipped when the app's collection is current (R2 item 4) or does not exist yet: then
        ``ensure_collection`` creates it and makes it current.
        """
        if collections.get("currentSceneCollectionName") != OBS_COLLECTION_NAME and OBS_COLLECTION_NAME in (
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
        async with self._owning_obs():
            return await self._switch_back(path, saved)

    async def _switch_back(self, path: Path, saved: ObsRestore) -> bool:
        """``_restore_obs`` once it holds ``_obs_lock``."""
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

    # --- recording ------------------------------------------------------------------------------

    def _count(self, name: str) -> None:
        self._counts = self._counts.incremented(name)

    async def _start(self, line: GameLine | None) -> None:
        """``StartRecord`` while armed (spec 11.4); the session begins at ``STARTED``, whoever starts it."""
        if self._state is not AppState.ARMED or self._start_deadline is not None:
            return
        if line is not None:  # auto mode: the session's counts start with the line that started it
            self._held = [line]
            self._counts = Counts(received=1, accepted=1)
        try:
            await self._recorder.start()
        except ObsError as exc:
            self._start_failed(f"OBS did not start recording: {exc}")
            return
        self._start_deadline = self._now() + START_TIMEOUT_S

    def _start_failed(self, text: str) -> None:
        """Spec 17: banner, state stays ``armed``; held lines are dropped."""
        self._start_deadline = None
        self._held = None
        self._banner(START_FAILED_BANNER_KEY, BannerLevel.ERROR, text)

    async def _start_timed_out(self) -> None:
        """No ``STARTED`` within ``START_TIMEOUT_S``: a failed start unless OBS reports it recording (R2 item 11).

        OBS shows why in a modal of its own and says nothing on the websocket, so the banner points
        at OBS's window.
        """
        try:
            active = bool((await self._gateway.request("GetRecordStatus")).get("outputActive"))
        except ObsError:
            active = False
        if active:  # still starting: its STARTED is on the way
            self._start_deadline = self._now() + START_TIMEOUT_S
            return
        self._start_failed(f"OBS did not start recording within {START_TIMEOUT_S:g} s; OBS's window says why.")

    async def _stop(self) -> None:
        if self._state is not AppState.RECORDING:
            return
        await self._sample_drift()  # spec 7: outputDuration sampled at stop
        try:
            await self._recorder.stop()
        except ObsError as exc:
            self._banner(BannerKey.STOP_FAILED, BannerLevel.ERROR, f"OBS did not stop recording: {exc}")

    async def _stop_for_quit(self) -> None:
        """Quit while recording: stop OBS and finish the session before the app goes.

        The stop is journalled at the reading when ``StopRecord`` is answered: the video ends there
        within a frame (R2 item 9). Finalise needs ``STOPPED``, awaited up to ``QUIT_STOP_TIMEOUT_S``;
        without it the session stays in ``_incoming/`` for the next launch, stop included.
        """
        s = self._session
        if s is None or not self._connected:
            return
        await self._sample_drift()
        stopping = self._expecting(
            lambda ev: ev.name == ObsEventName.RECORD_STATE_CHANGED
            and ev.data.get("outputState") == OutputState.STOPPED
        )
        with stopping as stopped:
            try:
                await self._recorder.stop()
            except ObsError as exc:
                log.warning("quit while recording: OBS did not stop (%s); the next launch finishes the session", exc)
                return
            stop_ms = s.clock.reading_ms(self._now())
            if not s.stop_journalled:
                self._append(s, StopRecord(offset_ms=stop_ms))
                s.stop_journalled = True
            try:
                async with asyncio.timeout(QUIT_STOP_TIMEOUT_S):
                    await stopped
            except TimeoutError:
                log.warning(
                    "quit while recording: no STOPPED within %g s; the next launch finishes the session",
                    QUIT_STOP_TIMEOUT_S,
                )
                return
        await self._end_session(stop_ms)

    async def _await_record_inactive(self) -> None:
        """Up to ``RECORD_INACTIVE_WAIT_S`` until ``GetRecordStatus`` says inactive (R2 item 9).

        After ``STOPPED`` it still says active for about 170 ms, and a restore in that window would
        take it for a running recording and leave OBS on the app's profile. The quit's restore runs
        once: E1 quit 114 ms after a ``STOPPED`` and OBS stayed on the app's profile.
        """
        for _ in range(round(RECORD_INACTIVE_WAIT_S / RECORD_INACTIVE_POLL_S)):
            try:
                status = await self._gateway.request("GetRecordStatus")
            except ObsError:
                return
            if not status.get("outputActive"):
                return
            await asyncio.sleep(RECORD_INACTIVE_POLL_S)

    def _on_line(self, msg: LineReceived) -> None:
        pipeline = self._pipeline
        if pipeline is None:  # idle: a late frame from a source that was stopped
            return
        self._count("received")
        result = pipeline.process(msg)
        if isinstance(result, Dropped):
            self._count(DROP_COUNTER[result.reason])
        elif isinstance(result, Accepted):
            self._count("accepted")
            self._accept(result.line)
        else:
            self._replace(result.line)

    def _accept(self, line: GameLine) -> None:
        s = self._session
        if s is not None and not s.stop_journalled:
            self._publish(LineAccepted(line, self._journal_line(s, line)))
            return
        if self._held is not None:
            self._held.append(line)
        self._publish(LineAccepted(line, None))

    def _replace(self, line: GameLine) -> None:
        """A typewriter merge (spec 8.2 step 9); see the module docstring for how it is journalled."""
        s = self._session
        if s is not None and not s.stop_journalled:
            if s.tail is not None and _same_line(s.tail, line):
                self._append(s, ReplaceRecord(text=line.text))
                s.tail = line
                offset = s.tail_offset
            else:
                offset = self._journal_line(s, line)
            self._publish(LineAccepted(line, offset, replaces_previous=True))
            return
        if self._held is not None:
            if self._held and _same_line(self._held[-1], line):
                self._held[-1] = line
            else:
                self._held.append(line)
        self._publish(LineAccepted(line, None, replaces_previous=True))

    def _journal_line(self, s: _Session, line: GameLine) -> int | None:
        """A ``LineRecord`` at the line's offset; ``None`` (dropped, counted ``paused``) while paused."""
        offset = s.clock.offset_ms(line.t_mono)
        if offset is None:
            self._count("paused")
            if self._pipeline is not None:
                self._pipeline.reset()  # this line is not journalled, so it is no "previous line"
            return None
        self._append(s, LineRecord(offset_ms=offset, text=line.text, source=line.source_id))
        s.tail, s.tail_offset = line, offset
        if line.source_id not in s.sources_used:
            s.sources_used.append(line.source_id)
        return offset

    def _append(self, s: _Session, record: JournalRecord) -> None:
        try:
            s.journal.append(record)
        except OSError as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot write the session journal ({exc.strerror or exc}); lines are being lost.",
            )

    async def _on_started(self, ev: ObsEvent) -> None:
        """Ownership rule (spec 6.2): only a recording that starts while armed, into ``_incoming/``, is a session."""
        armed = self._armed
        if self._state is not AppState.ARMED or armed is None or self._pipeline is None:
            return
        self._start_deadline = None
        held, self._held = self._held, None
        output_path = ev.data.get("outputPath")
        if not isinstance(output_path, str) or not self._in_incoming(output_path):
            self._banner(
                BannerKey.FOREIGN_RECORDING,
                BannerLevel.WARNING,
                f"OBS is recording to {output_path or 'an unknown file'}, outside the app's folder, so this "
                "recording gets no subtitle. Arm the game again to put OBS back on the app's profile.",
            )
            return
        incoming = paths.incoming_dir(armed.cfg)
        profile = armed.profile
        try:
            files = incoming_files(incoming, output_path)
            manifest = SessionManifest(
                app_version=__version__,
                game=GameRef(slug=profile.slug, title=profile.title),
                index=reserve_index(game_folder(incoming, profile.title), incoming, profile.slug),
                state=ManifestState.RECORDING,
                started_at=self._utc_stamp(),
                obs=ObsRecord(
                    version=self._obs_versions[0],
                    websocket=self._obs_versions[1],
                    profile=OBS_PROFILE_NAME,
                    collection=OBS_COLLECTION_NAME,
                    output_path=output_path,
                ),
                clock=ClockRecord(kind=ClockKind.EVENT, zero_event=ZERO_EVENT, capture_latency_ms=CAPTURE_LATENCY_MS),
                text_mode=profile.text_mode,
            )
            await asyncio.to_thread(write_manifest_atomic, files.manifest, manifest)  # it fsyncs: off the loop
            journal = Journal(files.journal)
        except (StoreError, OSError, ValueError) as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot write the session files in {incoming} ({exc}), so this recording gets no subtitle.",
            )
            return
        clock = EventClock(CAPTURE_LATENCY_MS, now=self._now)
        clock.start(ev.t_mono)
        s = self._session = _Session(
            files.manifest, manifest, files, journal, clock, next_sample=ev.t_mono + DRIFT_SAMPLE_AFTER_S
        )
        self._clear(START_FAILED_BANNER_KEY, BannerKey.FOREIGN_RECORDING, BannerKey.SESSION_FILES)
        journalled: list[LineAccepted] = []
        if held:
            for line in held:
                if (offset := self._journal_line(s, line)) is not None:
                    journalled.append(LineAccepted(line, offset))
        else:
            self._counts = Counts()
            self._pipeline.reset()
        self._set_state(AppState.RECORDING)
        for event in journalled:  # shown when accepted; now with their offsets (module docstring)
            self._publish(event)
        self._publish(RecordingStarted(files.video.stem))
        if not any(status in _LIVE for status in self._source_status.values()):
            self._banner(
                BannerKey.NO_SOURCE,
                BannerLevel.WARNING,
                "No text source is connected: the recording gets no lines until one connects.",
            )
        await self._sample_drift()

    async def _on_pause_edge(self, ev: ObsEvent, *, paused: bool) -> None:
        s = self._session
        if s is None or s.stop_journalled:
            return
        if s.clock.paused == paused:  # its partner edge was lost (R2 missed_pause), so the pause length is unknown
            if isinstance(s.clock, EventClock):
                try:
                    status, mid = await self._record_status()
                except ObsError:  # nothing to anchor on
                    return
                if status.get("outputActive"):
                    await self._degrade(s, status, mid)
            return  # on the OutputDurationClock the re-anchor follows OBS's pause flag
        record: JournalRecord
        if paused:
            s.clock.pause(ev.t_mono)
            record = PauseRecord(offset_ms=s.clock.reading_ms(ev.t_mono))
        else:
            s.clock.resume(ev.t_mono)
            record = ResumeRecord(offset_ms=s.clock.reading_ms(ev.t_mono))
        self._append(s, record)
        await self._write_manifest(s)
        if not paused:
            await self._sample_drift()  # spec 7: at each resume

    def _on_stopping(self, ev: ObsEvent) -> None:
        """R2 item 9: the video ends at ``STOPPING``; ``STOPPED`` follows 0.6-1.3 s later.

        The stop is journalled here, so a line accepted before ``STOPPED`` is shown but not
        journalled, and the last cue ends with the video.
        """
        s = self._session
        if s is None or s.stop_journalled:
            return
        self._append(s, StopRecord(offset_ms=s.clock.reading_ms(ev.t_mono)))
        s.stop_journalled = True

    async def _on_stopped(self, ev: ObsEvent) -> None:
        s = self._session
        if s is not None:
            await self._end_session(s.clock.reading_ms(ev.t_mono))  # the stop, unless STOPPING journalled it
            return
        if self._start_deadline is not None:
            self._start_failed("OBS stopped the recording as it started; OBS's window says why.")
        await self._sweep_orphans(exclude=None)  # nothing records now: every _incoming/ session is an orphan

    async def _sample_drift(self) -> None:
        """Spec 7 as amended: ``outputDuration`` beside the clock's reading, written to the manifest.

        Only on the ``EventClock`` with the recording running unpaused and its stop not yet
        journalled: the samples give the ``OutputDurationClock`` its lag.
        """
        s = self._session
        if s is None or not self._connected or s.stop_journalled or not isinstance(s.clock, EventClock):
            return
        if s.clock.paused:
            return
        try:
            status, mid = await self._record_status()
        except ObsError:
            return
        if s is not self._session or not status.get("outputActive") or status.get("outputPaused"):
            return
        s.samples.append(s.clock.drift_sample(int(status.get("outputDuration") or 0), mid))
        await self._write_manifest(s)

    async def _record_status(self) -> tuple[dict[str, Any], float]:
        """``GetRecordStatus`` and the monotonic midpoint of its round trip."""
        before = self._now()
        status = await self._gateway.request("GetRecordStatus")
        return status, (before + self._now()) / 2

    async def _end_session(self, stop_ms: int, flag: Flag | None = None) -> None:
        """Journal the stop at ``stop_ms`` (unless it is there already), then finalise and return to ``armed``."""
        s, armed = self._session, self._armed
        if s is None or armed is None:
            return
        if not s.stop_journalled:
            self._append(s, StopRecord(offset_ms=stop_ms))
        s.journal.close()
        flags = s.manifest.flags if flag is None else _with_flag(s.manifest.flags, flag)
        await self._write_manifest(s, stopped_at=self._utc_stamp(), flags=flags)
        self._session = None
        self._lost_ms = None
        self._gone_answers = 0
        self._clear(BannerKey.NO_SOURCE, BannerKey.STOP_FAILED)
        self._set_state(AppState.FINALISING)
        self._publish(RecordingStopped(s.files.video.stem))
        await self._finalise(s.manifest_path, armed.cfg)
        if self._pipeline is not None:
            self._pipeline.reset()
        self._counts = Counts()
        self._set_state(AppState.ARMED)

    async def _finalise(self, manifest_path: Path, cfg: AppConfig) -> None:
        try:
            result = await self._finaliser.run(manifest_path, cfg)
        except FinaliseError as exc:
            log.warning("could not finish the session %s: %s", manifest_path.name, exc)
            self._banner(
                BannerKey.FINALISE,
                BannerLevel.ERROR,
                f"Could not finish the session {manifest_path.name} ({exc}); the app tries again at next launch.",
            )
            return
        self._publish(SessionFinalised(result.manifest_path))
        manifest = result.manifest
        if manifest.state is ManifestState.FINALISE_PENDING:
            log.warning("could not move the session %s: the video is still in use", result.manifest_path.name)
            self._banner(
                BannerKey.FINALISE,
                BannerLevel.WARNING,
                f"{manifest.game.title}: the video is still in use, so the session could not be moved; "
                "the app tries again at next launch.",
            )
        if Flag.NO_CUES in manifest.flags:
            self._banner(
                BannerKey.NO_CUES,
                BannerLevel.WARNING,
                f"{manifest.game.title}: no lines were recorded, so the video was kept without a subtitle.",
            )
        if result.queue_vad and self._vad_jobs is not None:
            self._vad_jobs.queue(result.manifest_path)

    async def _write_manifest(self, s: _Session, **changes: Any) -> None:
        """Write the session's manifest with ``changes`` and the live counts, sources and drift samples.

        Off the loop (it fsyncs, and the loop stamps text frames at receipt), awaited, so writes keep
        their order.
        """
        manifest = replace(s.manifest, **changes)
        s.manifest = replace(
            manifest,
            counts=self._counts,
            sources_used=tuple(s.sources_used),
            clock=replace(manifest.clock, drift_samples=tuple(s.samples)),
        )
        try:
            await asyncio.to_thread(write_manifest_atomic, s.manifest_path, s.manifest)
        except StoreError as exc:
            self._banner(BannerKey.SESSION_FILES, BannerLevel.ERROR, f"Cannot update the session manifest ({exc}).")

    def _in_incoming(self, output_path: str) -> bool:
        """Whether OBS's ``output_path`` lies directly in the configured ``_incoming/`` (symlinks resolved)."""
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        try:
            return Path(output_path).parent.samefile(paths.incoming_dir(cfg))
        except OSError:
            return False

    def _utc_stamp(self) -> str:
        return self._utc_now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ending without STOPPED (spec 6.4, 7) ---------------------------------------------------

    async def _on_split(self, ev: ObsEvent) -> None:
        """Spec 7: finalise against the first file; the split's stop is the first stop in the journal.

        Only ``RecordFileChanged`` names the new file; ``STOPPED`` later still names the first one
        (R2 item 10), and the manifest keeps the first file's path.
        """
        s = self._session
        if s is None or s.stop_journalled:
            return
        self._append(s, StopRecord(offset_ms=s.clock.reading_ms(ev.t_mono)))
        s.stop_journalled = True
        if self._pipeline is not None:
            self._pipeline.reset()
        await self._write_manifest(s, flags=_with_flag(s.manifest.flags, Flag.SPLIT_UNSUPPORTED))
        self._banner(
            BannerKey.SPLIT,
            BannerLevel.WARNING,
            "OBS split the recording into a second file: lines from the split on get no subtitle.",
        )

    async def _obs_gone(self, stop_ms: int) -> None:
        """Spec 6.4: ``ExitStarted``, or OBS gone after a lost connection."""
        await self._end_session(stop_ms, Flag.OBS_EXITED)
        self._banner(
            BannerKey.OBS_EXITED,
            BannerLevel.WARNING,
            "OBS closed during the recording; the session was saved up to that moment.",
        )

    # --- reconcile (spec 6.3) and orphans (spec 10.3) -------------------------------------------

    async def _reconcile(self) -> None:
        """Every row of spec 6.3, run on each ``_Connected`` before any later event is handled."""
        try:
            version = await self._gateway.request("GetVersion")
            status, mid = await self._record_status()
            active = bool(status.get("outputActive"))
            live_path = await self._live_output_path() if active else None
        except ObsError as exc:  # the link dropped again: the next _Connected reconciles
            log.info("reconcile postponed: %s", exc)
            return
        self._obs_versions = (
            str(version.get("obsVersion") or "unknown"),
            str(version.get("obsWebSocketVersion") or "unknown"),
        )
        s = self._session
        if s is not None:
            ours = active and (live_path is None or _file_name(live_path) == _file_name(s.manifest.obs.output_path))
            if not ours:  # row 1 (or it stopped and a new recording started while disconnected)
                await self._end_session(self._lost_ms if self._lost_ms is not None else s.clock.reading_ms(mid))
            elif bool(status.get("outputPaused")) != s.clock.paused:  # row 3: a pause edge was missed
                await self._degrade(s, status, mid)
            # row 2: continue
        elif active:
            found = self._manifest_for(live_path) if live_path is not None else None
            if found is not None:  # row 4: the app restarted mid-session
                await self._resume(found, status, mid)
            elif live_path is None:  # row 5, and which _incoming/ session is live is unknown: sweep nothing
                await self._restore_if_due()
                return
            # row 5: not ours; arming refuses while it runs (spec 6.2 step 1)
        live = self._session.manifest_path if self._session is not None else None
        await self._sweep_orphans(exclude=live)  # row 6
        await self._restore_if_due()

    async def _restore_if_due(self) -> None:
        """While idle: the restore, unless a failed try or an unanswered switch put it off (``_next_restore``)."""
        if self._state is AppState.IDLE and self._now() >= self._next_restore:
            await self._restore_obs()

    async def _live_output_path(self) -> str | None:
        """The file the active recording writes: ``GetOutputSettings`` ``path`` (R2 item 8).

        ``None`` when no output in ``RECORD_OUTPUT_NAMES`` answers with a path. A split does not
        change it: the path stays the first file's (R2 item 10). File size against ``outputBytes``
        cannot tell (R2 section 5).
        """
        for name in RECORD_OUTPUT_NAMES:
            try:
                data = await self._gateway.request("GetOutputSettings", outputName=name)
            except ObsRequestError:
                continue
            settings = data.get("outputSettings")
            path = settings.get("path") if isinstance(settings, dict) else None
            if isinstance(path, str) and path:
                return path
        return None

    def _manifest_for(self, output_path: str) -> Path | None:
        """Spec 6.3: the ``_incoming/`` manifest of ``output_path`` in state ``recording``, if any."""
        if not self._in_incoming(output_path):
            return None
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        try:
            path = incoming_files(paths.incoming_dir(cfg), output_path).manifest
            manifest = load_manifest(path)
        except (FileNotFoundError, StoreError, ValueError):
            return None
        return path if manifest.state is ManifestState.RECORDING else None

    async def _resume(self, manifest_path: Path, status: dict[str, Any], mid: float) -> None:
        """Row 4: continue the session's journal on the ``OutputDurationClock``, flag ``clock_degraded``.

        The lag comes from the drift samples the earlier run wrote to the manifest.
        """
        try:
            manifest = load_manifest(manifest_path)
            files = incoming_files(manifest_path.parent, manifest.obs.output_path)
            journal = Journal(files.journal)
        except (FileNotFoundError, StoreError, OSError, ValueError) as exc:
            self._banner(
                BannerKey.SESSION_FILES,
                BannerLevel.ERROR,
                f"Cannot resume the session {manifest_path.name} ({exc}).",
            )
            return
        await self._stop_sources()
        cfg = self._get_config()
        profile = self._get_profile(manifest.game.slug) or GameProfile(
            slug=manifest.game.slug, title=manifest.game.title, text_mode=manifest.text_mode
        )
        lag = _lag_ms(manifest.clock.drift_samples)
        s = self._session = _Session(
            manifest_path,
            manifest,
            files,
            journal,
            self._degraded_clock(status, mid, lag),
            samples=list(manifest.clock.drift_samples),
            sources_used=list(manifest.sources_used),
            lag_ms=lag,
        )
        self._armed = _Armed(profile=profile, cfg=cfg)
        self._pipeline = TextPipeline(profile.filters)
        self._held = None
        self._start_deadline = None
        self._counts = manifest.counts
        await self._mark_degraded(s, mid)
        self._start_sources(cfg, profile)
        self._set_state(AppState.RECORDING)
        self._publish(RecordingStarted(files.video.stem))

    def _degraded_clock(self, status: dict[str, Any], mid: float, lag_ms: int | None) -> OutputDurationClock:
        """Spec 7 fallback, anchored on ``outputDuration + lag`` of this ``GetRecordStatus``; paused when OBS says so."""
        clock = OutputDurationClock(now=self._now)
        clock.anchor(mid, int(status.get("outputDuration") or 0) + (lag_ms or 0))
        if status.get("outputPaused"):
            clock.pause(mid)
        return clock

    async def _degrade(self, s: _Session, status: dict[str, Any], mid: float) -> None:
        """Row 3, or a pause edge whose partner was lost: the ``OutputDurationClock`` from ``mid`` on.

        The lag is the session's own (its drift samples); the edge is journalled at the new
        clock's reading.
        """
        s.lag_ms = _lag_ms(s.samples)
        s.clock = self._degraded_clock(status, mid, s.lag_ms)
        s.next_sample = None
        reading = s.clock.reading_ms(mid)
        self._append(s, PauseRecord(offset_ms=reading) if s.clock.paused else ResumeRecord(offset_ms=reading))
        await self._mark_degraded(s, mid)

    async def _mark_degraded(self, s: _Session, mid: float) -> None:
        s.next_anchor = mid + REANCHOR_S
        await self._write_manifest(
            s,
            clock=replace(s.manifest.clock, kind=ClockKind.OUTPUT_DURATION, degraded=True),
            flags=_with_flag(s.manifest.flags, Flag.CLOCK_DEGRADED),
        )
        if s.lag_ms is None:  # spec 7: no sample to take the encoder's lag from
            self._banner(
                BannerKey.CLOCK,
                BannerLevel.WARNING,
                "OBS's recording changed while the app was not watching, and this session has no timing "
                "sample to correct by: subtitles for the rest of it may be off by a few seconds.",
            )

    async def _reanchor(self, s: _Session) -> None:
        """Spec 7: re-anchor the ``OutputDurationClock`` every ``REANCHOR_S``, following OBS's pause flag."""
        try:
            status, mid = await self._record_status()
        except ObsError:
            return
        clock = s.clock
        if s is not self._session or not status.get("outputActive") or not isinstance(clock, OutputDurationClock):
            return
        paused = bool(status.get("outputPaused"))
        if paused and not clock.paused:
            clock.pause(mid)
        elif clock.paused and not paused:
            clock.resume(mid)
        clock.anchor(mid, int(status.get("outputDuration") or 0) + (s.lag_ms or 0))

    async def _sweep_orphans(self, exclude: Path | None) -> None:
        """Finalise every session left in ``_incoming/`` (spec 6.3 last row, 10.3) except ``exclude``.

        Called only when no recording can be writing one of them: after reconcile, after a
        ``STOPPED`` with no session, and once a launch without OBS confirmed it absent. A
        ``finalise_pending`` one (its video stayed locked) is retried by the first sweep after
        launch only (spec 10.3, 17), since each locked video costs up to 9.8 s of rename retries
        inside a handler.
        """
        states = {ManifestState.RECORDING}
        if not self._launch_swept:
            states.add(ManifestState.FINALISE_PENDING)
            self._launch_swept = True
        cfg = self._armed.cfg if self._armed is not None else self._get_config()
        for path in sorted(paths.incoming_dir(cfg).glob(f"*{MANIFEST_SUFFIX}")):
            if path == exclude:
                continue
            try:
                manifest = load_manifest(path)
            except (FileNotFoundError, StoreError) as exc:
                log.warning("skipping %s: %s", path.name, exc)
                continue
            if manifest.state in states:
                await self._finalise(path, cfg)
