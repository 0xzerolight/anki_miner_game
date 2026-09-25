"""Fakes and a running actor for the session actor tests (spec 18: injected ``now``, fake OBS).

``FakeObs`` is a small model of what OBS reports; ``FakeGateway`` answers the actor's requests from
it and, like the real gateway, hands events to the subscribed handler (the actor's ``post``).
``Harness`` builds a ``SessionActor`` over these fakes with an isolated output root, runs it on the
test's loop, and records every ``SessionEvent``. Ticks come only from the tests (``tick``).
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    AppState,
    BannerCleared,
    BannerRaised,
    CommandKind,
    LineAccepted,
    ObsEvent,
    SessionEvent,
    SessionFinalised,
    SourceStatus,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsConnectError,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsRequestError,
    OutputState,
    ProvisionResult,
    WsConfig,
)
from anki_miner_game.models.profile import AudioMode, AudioSettings, FilterSettings, GameProfile
from anki_miner_game.obs import provision
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.session.session import FinaliseWorker, SessionActor

SLUG = "steins-gate"
TITLE = "Steins;Gate"
OBS_STEM = "2026-10-02 18-04-11"
T0 = 1000.0
"""The fake monotonic clock's start."""
UTC_NOW = datetime(2026, 10, 2, 18, 4, 11, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeObs:
    """What OBS reports. ``None`` for replay buffer or virtual camera: not available (604)."""

    profile: str = "Untitled"
    collection: str = "Untitled"
    profiles: list[str] = field(default_factory=lambda: ["Untitled", OBS_PROFILE_NAME])
    collections: list[str] = field(default_factory=lambda: ["Untitled", OBS_COLLECTION_NAME])
    stream_active: bool = False
    replay_active: bool | None = None
    vcam_active: bool | None = None
    record_active: bool = False
    record_paused: bool = False
    stale_active_reads: int = 0
    """How many ``GetRecordStatus`` answers after the recording stopped still say active (R2 item 9:
    about 170 ms after ``STOPPED``)."""
    output_duration: int = 0
    output_path: str | None = None
    """The file output's ``path``; ``None`` makes ``GetOutputSettings`` fail for every name."""
    output_name: str = "simple_file_output"
    """The file output of ``[Output] Mode`` (``adv_file_output`` in Advanced mode, R2 item 8)."""
    version: str = "32.2.2"
    websocket: str = "5.7.4"
    lost_switch_events: int = 0
    """How many of the next ``...Changed`` events after a ``SetCurrent...`` never arrive (the switch times out)."""
    late_switch_events: bool = False
    """``...Changed`` arrives after the ``SetCurrent...`` answer instead of before it (R2 item 5 saw both)."""
    restart_question: asyncio.Event | None = None
    """Set: ``SetCurrentProfile`` switches and sends its event, then answers only once this is set
    (OBS's modal restart question, R2 item 3)."""
    stops_on_request: bool = False
    """``StopRecord`` makes OBS send ``STOPPING`` and ``STOPPED`` (otherwise the tests send them)."""


class FakeGateway:
    """``ObsGateway`` over ``FakeObs``. ``fail(name, error)`` makes the next ``name`` request raise.

    Like T12's gateway, a request cancelled after it went out drops the link (``_ConnectionLost``)
    and the gateway reconnects on its own (``_Connected``), since OBS's late answer would be read as
    the next request's.
    """

    def __init__(self, obs: FakeObs, clock: FakeClock) -> None:
        self.obs = obs
        self.clock = clock
        self.handlers: list[Callable[[ObsEvent], None]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, list[Exception]] = {}
        self.connect_error: Exception | None = None
        self.connects = 0
        self.connected = False
        self.drops = 0

    # ObsGateway
    async def connect(self) -> ObsInfo:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.emit(ObsEventName.CONNECTED)
        return ObsInfo(self.obs.version, self.obs.websocket, frozenset(REQUIRED_REQUESTS))

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None:
        self.handlers.append(handler)

    @property
    def collection_changing(self) -> bool:
        return False

    async def close(self) -> None:
        self.connected = False

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        self.sent.append((name, fields))
        pending = self.errors.get(name)
        if pending:
            raise pending.pop(0)
        if not self.connected:
            raise ObsConnectError("not connected")
        answer = self._answer(name, fields)
        if name == "SetCurrentProfile" and self.obs.restart_question is not None:
            try:
                await self.obs.restart_question.wait()
            except asyncio.CancelledError:
                self._drop_link()
                raise
        return answer

    # test side
    def emit(self, name: str, data: dict[str, Any] | None = None, t: float | None = None) -> None:
        event = ObsEvent(name, data or {}, self.clock() if t is None else t)
        for handler in self.handlers:
            handler(event)

    def record_event(self, state: OutputState, path: str | None = None, t: float | None = None) -> None:
        self.emit(ObsEventName.RECORD_STATE_CHANGED, {"outputState": state, "outputPath": path}, t)

    def names(self) -> list[str]:
        return [name for name, _ in self.sent]

    def fail(self, name: str, error: Exception) -> None:
        self.errors.setdefault(name, []).append(error)

    def _drop_link(self) -> None:
        self.drops += 1
        self.connected = False
        self.emit(ObsEventName.CONNECTION_LOST)
        asyncio.get_running_loop().call_soon(self._reconnected)

    def _reconnected(self) -> None:
        self.connected = True
        self.emit(ObsEventName.CONNECTED)

    def _answer(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        obs = self.obs
        match name:
            case "GetVersion":
                return {"obsVersion": obs.version, "obsWebSocketVersion": obs.websocket}
            case "GetStreamStatus":
                return {"outputActive": obs.stream_active}
            case "GetRecordStatus":
                active = obs.record_active
                if not active and obs.stale_active_reads > 0:
                    obs.stale_active_reads -= 1
                    active = True
                return {
                    "outputActive": active,
                    "outputPaused": obs.record_paused,
                    "outputDuration": obs.output_duration,
                }
            case "GetReplayBufferStatus" | "GetVirtualCamStatus":
                active = obs.replay_active if name == "GetReplayBufferStatus" else obs.vcam_active
                if active is None:
                    raise ObsRequestError(name, 604, "not available")
                return {"outputActive": active}
            case "GetProfileList":
                return {"currentProfileName": obs.profile, "profiles": list(obs.profiles)}
            case "GetSceneCollectionList":
                return {"currentSceneCollectionName": obs.collection, "sceneCollections": list(obs.collections)}
            case "SetCurrentProfile":
                obs.profile = fields["profileName"]
                self._switched(ObsEventName.CURRENT_PROFILE_CHANGED, {"profileName": obs.profile})
                return {}
            case "SetCurrentSceneCollection":
                obs.collection = fields["sceneCollectionName"]
                self._switched(ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED, {"sceneCollectionName": obs.collection})
                return {}
            case "GetOutputSettings":
                if fields["outputName"] != obs.output_name or obs.output_path is None:
                    raise ObsRequestError(name, 600, "No output was found")
                return {"outputSettings": {"path": obs.output_path, "muxer_settings": ""}}
            case "StartRecord":
                return {}
            case "StopRecord":
                if obs.stops_on_request:
                    obs.record_active = obs.record_paused = False
                    self.record_event(OutputState.STOPPING)
                    self.record_event(OutputState.STOPPED, obs.output_path)
                return {}
        raise AssertionError(f"unexpected request {name}")

    def _switched(self, event: str, data: dict[str, Any]) -> None:
        if self.obs.lost_switch_events:
            self.obs.lost_switch_events -= 1
        elif self.obs.late_switch_events:
            asyncio.get_running_loop().call_later(0.01, self.emit, event, data)
        else:
            self.emit(event, data)


class FakeDiscovery:
    def __init__(self) -> None:
        self.running = True
        self.answers: list[bool] = []
        """Answers ``is_running`` gives first, one per call, before it falls back to ``running``."""
        self.ready = True
        self.launches = 0
        self.enabled = 0
        self.running_checks = 0
        self.ws_config: WsConfig | None = WsConfig(server_enabled=True, port=4455, password=None, auth_required=False)
        """``read_ws_config``'s answer; websocket on by default, as a working OBS install has it."""

    def is_running(self) -> bool:
        self.running_checks += 1
        if self.answers:
            return self.answers.pop(0)
        return self.running

    def read_ws_config(self) -> WsConfig | None:
        return self.ws_config

    def ensure_server_enabled(self) -> bool:
        self.enabled += 1
        return True

    def launch(self) -> None:
        self.launches += 1
        self.running = True

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        return self.ready


class FakeProvisioner:
    """``Provisioner`` over ``FakeGateway``. ``ensure_profile`` makes the app's profile current itself, as
    ``obs/provision.py`` does: ``SetCurrentProfile`` through the gateway, done on its answer and its
    ``CurrentProfileChanged`` within ``switch_timeout_s``, else ``ObsError`` (a request still unanswered
    then is cancelled, so the gateway drops the link). It skips the switch when the app's profile is
    current or missing (the real one creates it), and raises ``error`` after it."""

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.profiles: list[AppConfig] = []
        self.started_on: list[str] = []
        """OBS's current profile at each ``ensure_profile``: the one whose audio values it copies (spec 11.3)."""
        self.collections: list[GameProfile] = []
        self.error: Exception | None = None
        self.needs_restart = False
        self.switch_timeout_s = provision.SWITCH_TIMEOUT_S

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        self.profiles.append(cfg)
        obs = self.gateway.obs
        self.started_on.append(obs.profile)
        if obs.profile != OBS_PROFILE_NAME and OBS_PROFILE_NAME in obs.profiles:
            await self._switch_to_app()
        if self.error is not None:
            raise self.error
        return ProvisionResult(changed=False, needs_restart=self.needs_restart)

    async def _switch_to_app(self) -> None:
        changed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def on_event(ev: ObsEvent) -> None:
            if (
                ev.name == ObsEventName.CURRENT_PROFILE_CHANGED
                and ev.data.get("profileName") == OBS_PROFILE_NAME
                and not changed.done()
            ):
                changed.set_result(None)

        self.gateway.handlers.append(on_event)
        try:
            async with asyncio.timeout(self.switch_timeout_s):
                await self.gateway.request("SetCurrentProfile", profileName=OBS_PROFILE_NAME)
                await changed
        except TimeoutError:
            raise ObsError(
                f"OBS did not finish switching to {OBS_PROFILE_NAME!r} within {self.switch_timeout_s:g} s; "
                "an OBS dialog may be waiting for an answer"
            ) from None
        finally:
            self.gateway.handlers.remove(on_event)

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult:
        self.collections.append(profile)
        return ProvisionResult(changed=False, needs_restart=False)

    async def list_windows(self) -> list[Any]:
        return []

    async def capture_method(self, profile: GameProfile) -> str:
        return "xcomposite_input"


class FakeSource:
    """A text source; ``line`` calls the sink as the source's own thread would."""

    def __init__(self, source_id: str, status: SourceStatus = SourceStatus.CONNECTED) -> None:
        self._id = source_id
        self._status = status
        self.sink: LineSink | None = None
        self.listener: StatusListener | None = None
        self.starts = 0
        self.stops = 0
        self.closed = 0
        self.start_thread: int | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return self._status

    def start(self, sink: LineSink) -> None:
        self.starts += 1
        self.start_thread = threading.get_ident()
        self.sink = sink

    def stop(self) -> None:
        self.stops += 1
        self.set_status(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)
        self.closed += 1

    def set_status_listener(self, cb: StatusListener) -> None:
        self.listener = cb

    def set_status(self, status: SourceStatus) -> None:
        if status is self._status:
            return
        self._status = status
        if self.listener is not None:
            self.listener(self._id, status)

    def line(self, raw: str, t_mono: float) -> None:
        assert self.sink is not None, "source not started"
        self.sink(raw, t_mono, self._id)


class FakeVadJobs:
    def __init__(self) -> None:
        self.queued: list[Path] = []

    def queue(self, manifest_path: Path) -> None:
        self.queued.append(manifest_path)

    def rerun(self, manifest_path: Path) -> None:
        raise AssertionError("the actor never re-runs")

    def restore(self, manifest_path: Path) -> None:
        raise AssertionError("the actor never restores")


def profile(*, typewriter: bool = False, slug: str = SLUG, title: str = TITLE) -> GameProfile:
    """Desktop audio (the Linux default, fixed so Windows CI arms it too)."""
    return GameProfile(
        slug=slug,
        title=title,
        audio=AudioSettings(mode=AudioMode.DESKTOP),
        filters=FilterSettings(typewriter_merge=typewriter),
    )


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        gateway: Any = None,
        provisioner: Any = None,
        obs_lock: asyncio.Lock | None = None,
    ) -> None:
        """``sleep`` paces the actor's ``Tick`` timer; by default it never fires (tests call ``tick``).

        ``gateway`` and ``provisioner`` replace ``FakeGateway`` and ``FakeProvisioner`` (both or neither),
        for a test that runs the real provisioner against T14's stateful fake OBS. ``obs_lock`` is the
        lock the composition shares with the window picker and the wizard.
        """
        self.output_root = tmp_path / "out"
        self.cfg = AppConfig(output_root=str(self.output_root))
        self.incoming = self.output_root / "_incoming"
        self.clock = FakeClock()
        self.obs = FakeObs()
        self.gateway = gateway or FakeGateway(self.obs, self.clock)
        self.discovery = FakeDiscovery()
        self.provisioner = provisioner or FakeProvisioner(self.gateway)
        self.vad = FakeVadJobs()
        self.profiles = {SLUG: profile()}
        self.sources = [FakeSource("textractor")]
        self.free_bytes = 100 * 10**9
        self.events: list[SessionEvent] = []
        self.finaliser = FinaliseWorker(sleep=lambda _s: None)
        self.actor = SessionActor(
            loop=asyncio.get_running_loop(),
            gateway=self.gateway,
            discovery=self.discovery,
            provisioner=self.provisioner,
            recorder=ObsRecorder(self.gateway),
            finaliser=self.finaliser,
            get_config=lambda: self.cfg,
            get_profile=self.profiles.get,
            source_factory=lambda _cfg, _game: self.sources,
            vad_jobs=self.vad,
            now=self.clock,
            utc_now=lambda: UTC_NOW,
            disk_free=lambda _folder: self.free_bytes,
            sleep=sleep or _never,
            obs_lock=obs_lock,
        )
        self.actor.subscribe(self.events.append)
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.actor.run())
        await self.settle()

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            await self.actor.shutdown()
            await self.task
        self.finaliser.shutdown()

    async def settle(self) -> None:
        """Let every posted message, and every message those post, be handled."""
        for _ in range(8):
            await asyncio.sleep(0)
            await self.actor.join()

    async def send(self, kind: CommandKind, slug: str | None = None, line: GameLine | None = None) -> None:
        self.actor.post(UserCommand(kind, slug=slug, line=line))
        await self.settle()

    async def tick(self, t: float) -> None:
        self.clock.t = t
        self.actor.post(Tick(t))
        await self.settle()

    async def arm(self, slug: str = SLUG) -> None:
        await self.send(CommandKind.ARM, slug)

    async def emit(self, name: str, data: dict[str, Any] | None = None, t: float | None = None) -> None:
        """OBS sends an event at ``t`` (default: now); the fake clock moves to ``t``."""
        if t is not None:
            self.clock.t = t
        self.gateway.emit(name, data, t)
        await self.settle()

    def video_path(self, stem: str = OBS_STEM) -> Path:
        return self.incoming / f"{stem}.mkv"

    async def started(self, t: float = T0 + 1.0, stem: str = OBS_STEM) -> Path:
        """OBS starts recording into ``_incoming/``: the video appears, then ``STARTED`` arrives at ``t``."""
        video = self.video_path(stem)
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
        self.obs.record_active = True
        self.obs.output_path = str(video)
        self.clock.t = t
        self.gateway.record_event(OutputState.STARTED, str(video), t)
        await self.settle()
        return video

    async def stopping(self, t: float) -> None:
        """OBS begins to stop at ``t``: the video ends here (R2 item 9)."""
        self.clock.t = t
        self.gateway.record_event(OutputState.STOPPING, None, t)
        await self.settle()

    async def stopped(self, t: float, stem: str = OBS_STEM) -> None:
        """``STOPPED`` at ``t``; without ``stopping`` first it stands for a ``STOPPING`` the app missed."""
        self.obs.record_active = False
        self.obs.record_paused = False
        self.clock.t = t
        self.gateway.record_event(OutputState.STOPPED, str(self.video_path(stem)), t)
        await self.settle()

    async def line(self, raw: str, t: float, source: int = 0) -> None:
        self.clock.t = t
        self.sources[source].line(raw, t)
        await self.settle()

    def states(self) -> list[tuple[AppState, str | None]]:
        return [(e.state, e.slug) for e in self.events if isinstance(e, StateChanged)]

    def banners(self) -> dict[str, str]:
        """Banners currently shown, by key."""
        shown: dict[str, str] = {}
        for e in self.events:
            if isinstance(e, BannerRaised):
                shown[e.banner.key] = e.banner.text
            elif isinstance(e, BannerCleared):
                shown.pop(e.key, None)
        return shown

    def accepted(self) -> list[LineAccepted]:
        return [e for e in self.events if isinstance(e, LineAccepted)]

    def finalised(self) -> list[Path]:
        return [e.manifest_path for e in self.events if isinstance(e, SessionFinalised)]

    def game_dir(self) -> Path:
        return self.output_root / TITLE
