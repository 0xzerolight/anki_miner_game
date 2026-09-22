"""The harness of the scripted sessions: the real composition against ``ReplayedObs`` and a hooker.

``Scripted`` builds ``App`` as ``launch.main`` does, with the two seams the composition offers tests
(``obs`` and ``source_factory``) and one clock: ``SessionActor``, ``ObsClient`` and the hooker's
``WebsocketSource`` all read ``Clock``, so an event is stamped with its recorded time and a line with
the time the script gives it, and every offset is exact. The fake OBS and the hooker run on their
own loop and thread (``ServerThread``), as separate processes would.

A script is a list of calls at recording times (seconds after the transcript's first record):
``arm``, ``line``, ``start``, ``stop``, ``tick``, ``until``. Each first plays every step of the
recording due before it, in order (``_play``):

- a request the app sends (``ReplayedObs.app_sends``): wait until it has come, then send its
  recorded answer when that answer's step is due;
- a request someone else sent (the user in OBS): a switch is made in ``FakeObs`` without its own
  events; the rest change nothing but what OBS reports next;
- an event: the clock moves to it, the files OBS creates appear (a recording's video, a split's
  second file), OBS sends it, and the step ends once the app's gateway has it;
- ``drop``: the app's connection is cut; ``reopen``: the app's gateway may reconnect (its backoff
  waits for this) and has reconciled; ``obs_closed``: OBS closes the connections with the recorded
  code, refuses new ones and is no longer running.

Before the clock moves the actor is settled (``actor.join``), so a reading taken while it handles
something (a drift sample's round trip) is taken at the time it was due. ``settles=False`` turns
that off for a script in which the actor waits for a later step (a switch played from the
recording), where settling would wait forever.
"""

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar

from anki_miner_game import app as app_mod
from anki_miner_game import paths, store
from anki_miner_game.app import App, ObsServices
from anki_miner_game.interfaces.text_source import LineSink, TextSource
from anki_miner_game.models.config import AppConfig, FeedSettings, TextSourceConfig, VadSettings
from anki_miner_game.models.manifest import SessionManifest
from anki_miner_game.models.messages import (
    AppState,
    BannerCleared,
    BannerRaised,
    CommandKind,
    ObsEvent,
    SessionEvent,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import ObsCredentials, ObsEventName
from anki_miner_game.models.profile import AudioMode, AudioSettings, GameProfile
from anki_miner_game.obs import client as client_mod
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, ObsClient
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.naming import session_stem
from anki_miner_game.session.session import SessionActor
from anki_miner_game.text.sources.websocket_source import WebsocketSource
from tests.fakes.fake_hooker import FakeHookerServer
from tests.fakes.fake_obs_server import FakeObsServer
from tests.integration.obs_replay import SWITCH_EVENTS, SWITCH_REQUESTS, ReplayedObs, Step, load
from tests.obs.fake_obs import LINUX_X11_KINDS, FakeObs
from tests.session.actor_harness import FakeDiscovery

T = TypeVar("T")

SLUG: Final = "steins-gate"
TITLE: Final = "Steins;Gate"
HOOKER: Final = "textractor"
BASE: Final = 1000.0
"""The clock at the transcript's first record."""
LAUNCH_AT: Final = -20.0
"""Where the clock stands while the app launches, before anything the transcript holds."""
UTC_NOW: Final = datetime(2026, 9, 21, 18, 42, 29, tzinfo=UTC)
WAIT_S: Final = 15.0
APP_RECORD_REQUESTS: Final = frozenset({"StartRecord", "StopRecord"})
"""What the app sends of a recording's requests; a pause or a split is made in OBS (spec 7)."""
PLAYER: Final = "linux"
"""The transcripts are Linux X11 ones: provisioning plans for them on every CI runner."""


def profile(**changes: Any) -> GameProfile:
    """The scripted game: hook mode, desktop audio (the Linux default, fixed so Windows CI arms it too)."""
    fields: dict[str, Any] = {"slug": SLUG, "title": TITLE, "audio": AudioSettings(mode=AudioMode.DESKTOP)}
    return GameProfile(**{**fields, **changes})


class Clock:
    """The one ``now`` of the run; ``at(t)`` puts it at recording time ``t``."""

    def __init__(self) -> None:
        self.t = BASE + LAUNCH_AT

    def __call__(self) -> float:
        return self.t

    def at(self, t: float) -> None:
        self.t = BASE + t

    @property
    def recording_time(self) -> float:
        """Where the clock stands in the recording, to the millisecond as the steps are."""
        return round(self.t - BASE, 3)


class ServerThread:
    """An asyncio loop on its own thread for the fake OBS and the hooker."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="fake-obs-and-hooker", daemon=True)
        self.thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 4 * WAIT_S) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(WAIT_S)
        self.loop.close()


class CountedSource(WebsocketSource):
    """The real hooker source; ``counted`` goes up once a frame's line has reached the actor's sink."""

    def __init__(self, source_id: str, uri: str, *, now: Callable[[], float], counted: Callable[[], None]) -> None:
        super().__init__(source_id, uri, now=now)
        self._counted = counted

    def start(self, sink: LineSink) -> None:
        def counting(raw: str, t_mono: float, source_id: str) -> None:
            sink(raw, t_mono, source_id)
            self._counted()

        super().start(counting)


async def wait_for(condition: Callable[[], object], what: str, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.002)


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()


class AppRun:
    """``App`` over an OBS served by ``server`` on ``servers``' loop, with every ``SessionEvent`` recorded."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: Any,
        *,
        clock: Clock,
        servers: ServerThread,
        server: FakeObsServer,
        game: GameProfile,
        hooker: FakeHookerServer | None,
        discovery: FakeDiscovery,
    ) -> None:
        self.clock = clock
        self.servers = servers
        self.server = server
        self.hooker = hooker
        self.discovery = discovery
        self.output_root = tmp_path / "out"
        self.incoming = self.output_root / "_incoming"
        self.cfg = AppConfig(
            output_root=str(self.output_root),
            text_sources=() if hooker is None else (TextSourceConfig(id=HOOKER, name="Textractor", uri=hooker.uri),),
            feed=FeedSettings(ws_port=0, http_port=0),
            vad=VadSettings(enabled=False),
        )
        self.game = game
        self.events: list[SessionEvent] = []
        self.received: list[ObsEvent] = []
        """Every event the app's gateway handed on, in order (``_Connected`` and ``_ConnectionLost`` too)."""
        self.lines = 0
        """Hooker frames that reached the actor."""
        self.reconnect = threading.Event()
        """Set: the gateway's next reconnect attempt may go (``_gateway_sleep``)."""
        self.gateway: ObsClient | None = None
        self.actor: SessionActor | None = None
        self.io_loop: asyncio.AbstractEventLoop | None = None
        self.app: App | None = None
        monkeypatch.setattr(app_mod, "SessionActor", self._build_actor)

    # --- the composition's seams ---------------------------------------------------------------

    def _services(self, _config: Callable[[], AppConfig]) -> ObsServices:
        port = self.server.port
        gateway = ObsClient(lambda: ObsCredentials("127.0.0.1", port, None), now=self.clock, sleep=self._gateway_sleep)
        self.gateway = gateway
        return ObsServices(self.discovery, gateway, ObsProvisioner(gateway, platform=PLAYER))

    def _build_actor(self, **kwargs: Any) -> SessionActor:
        actor = SessionActor(
            **kwargs,
            now=self.clock,
            utc_now=lambda: UTC_NOW,
            disk_free=lambda _folder: 10**12,
            sleep=_never,  # ticks come from the script only
        )
        actor.subscribe(self.events.append)
        assert self.gateway is not None  # _services ran first (App._build)
        self.gateway.subscribe(self.received.append)  # after the actor: once seen here, the actor has it
        self.actor, self.io_loop = actor, kwargs["loop"]
        return actor

    def _sources(self, cfg: AppConfig, game: GameProfile) -> Sequence[TextSource]:
        return [
            CountedSource(source.id, source.uri, now=self.clock, counted=self._count_line)
            for source in cfg.text_sources
            if source.enabled
        ]

    def _count_line(self) -> None:
        self.lines += 1

    async def _gateway_sleep(self, seconds: float) -> None:
        """The reconnect backoff waits for ``reconnect``; the gateway's short waits are real."""
        if seconds not in client_mod.BACKOFF_S:
            await asyncio.sleep(seconds)
            return
        while not self.reconnect.is_set():
            await asyncio.sleep(0.002)
        self.reconnect.clear()

    # --- running ----------------------------------------------------------------------------------

    def launch_app(self, qtbot: Any) -> App:
        store.save_config(self.cfg)
        store.save_profile(self.game)
        app = App(obs=self._services, source_factory=self._sources)
        self.app = app
        app.start()
        qtbot.addWidget(app.window)
        return app

    def post(self, message: UserCommand | Tick) -> None:
        assert self.actor is not None
        self.actor.post(message)

    async def settle(self) -> None:
        """Wait until the actor has handled everything posted so far."""
        assert self.actor is not None and self.io_loop is not None
        future = asyncio.run_coroutine_threadsafe(self.actor.join(), self.io_loop)
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), WAIT_S)
        except TimeoutError:
            future.cancel()
            raise AssertionError(f"the session actor did not settle at t={self.clock.recording_time:.3f}") from None

    async def heard(self, name: str, data: dict[str, Any] | None, since: int) -> None:
        def seen() -> bool:
            return any(e.name == name and (data is None or dict(e.data) == data) for e in self.received[since:])

        await wait_for(seen, f"the app's gateway to receive {name} {data or ''}")

    def close(self) -> None:
        if self.app is not None:
            self.app.close()

    # --- what the session left ------------------------------------------------------------------

    def state(self) -> AppState | None:
        states = [e.state for e in self.events if isinstance(e, StateChanged)]
        return states[-1] if states else None

    def banners(self) -> dict[str, str]:
        shown: dict[str, str] = {}
        for e in list(self.events):
            if isinstance(e, BannerRaised):
                shown[e.banner.key] = e.banner.text
            elif isinstance(e, BannerCleared):
                shown.pop(e.key, None)
        return shown

    def game_files(self) -> list[str]:
        folder = self.output_root / TITLE
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def incoming_files(self) -> list[str]:
        return sorted(path.name for path in self.incoming.iterdir()) if self.incoming.exists() else []

    def srt(self, index: int) -> str:
        return (self.output_root / TITLE / f"{session_stem(TITLE, index)}.srt").read_bytes().decode("utf-8")

    def manifest(self, index: int) -> SessionManifest:
        return load_manifest(self.output_root / TITLE / f"{session_stem(TITLE, index)}.session.json")

    def restore_file(self) -> Path:
        return paths.home() / "obs_restore.json"


class Scripted(AppRun):
    """One R2 recording played to the app (see the module docstring)."""

    def __init__(
        self,
        qtbot: Any,
        tmp_path: Path,
        monkeypatch: Any,
        transcript: str,
        *,
        game: GameProfile | None = None,
        app_sends: frozenset[str] = APP_RECORD_REQUESTS,
        ignored: frozenset[str] = frozenset(),
        settles: bool = True,
        hooker: bool = True,
    ) -> None:
        self.recording = load(transcript)
        self.ignored = ignored
        self.settles = settles
        self.obs = FakeObs(input_kinds=LINUX_X11_KINDS)
        self.qtbot = qtbot
        self._position = 0
        self._closed = False
        clock = Clock()
        servers = ServerThread()

        async def start_servers() -> tuple[ReplayedObs, FakeHookerServer | None]:
            replayed = ReplayedObs(
                self.obs,
                self.recording,
                app_sends=app_sends,
                now=lambda: clock.recording_time,
                incoming=tmp_path / "out" / "_incoming",
                videos=tmp_path / "Videos",
            )
            await replayed.start()
            fake_hooker = FakeHookerServer(now=clock) if hooker else None
            if fake_hooker is not None:
                await fake_hooker.start()
            return replayed, fake_hooker

        self.replayed, fake_hooker = servers.run(start_servers())
        super().__init__(
            tmp_path,
            monkeypatch,
            clock=clock,
            servers=servers,
            server=self.replayed,
            game=game or profile(),
            hooker=fake_hooker,
            discovery=FakeDiscovery(),
        )

    # --- the script -------------------------------------------------------------------------------

    def launch(self) -> None:
        """Start the app on ``FakeObs`` as provisioned and left on ``Untitled`` (the README's "Disarmed")."""
        self.servers.run(self._provision())
        self.launch_app(self.qtbot)
        self.servers.run(self.settle())

    async def _provision(self) -> None:
        self.replayed.muted = True
        try:
            provisioner = ObsProvisioner(self.obs, platform=PLAYER)
            await provisioner.ensure_profile(self.cfg)
            await provisioner.ensure_collection(self.game)
            await self.obs.request("SetCurrentSceneCollection", sceneCollectionName="Untitled")
            await self.obs.request("SetCurrentProfile", profileName="Untitled")
        finally:
            self.replayed.muted = False
        self.obs.reset_calls()

    def arm(self, at: float = -10.0) -> None:
        self.command(CommandKind.ARM, at, slug=self.game.slug)
        self.servers.run(self._armed())

    async def _armed(self) -> None:
        await self.settle()
        assert self.state() is AppState.ARMED, f"not armed: {self.banners()}"
        if self.hooker is not None:
            await wait_for(
                lambda: SourceStatusChanged(HOOKER, SourceStatus.CONNECTED) in self.events, "the hooker source"
            )

    def start(self) -> None:
        self.command(CommandKind.START, self._next_app_request("StartRecord").t)

    def stop(self) -> None:
        self.command(CommandKind.STOP, self._next_app_request("StopRecord").t)

    def disarm(self, at: float) -> None:
        self.command(CommandKind.DISARM, at)

    def command(self, kind: CommandKind, at: float, *, slug: str | None = None) -> None:
        self.until(at, inclusive=False)
        self.servers.run(self._command(kind, at, slug))

    async def _command(self, kind: CommandKind, at: float, slug: str | None) -> None:
        await self._settle_if_safe()
        self.clock.at(at)
        self.post(UserCommand(kind, slug=slug))

    def line(self, t: float, text: str) -> None:
        self.until(t, inclusive=False)
        self.servers.run(self._line(t, text))

    async def _line(self, t: float, text: str) -> None:
        assert self.hooker is not None
        await self._settle_if_safe()
        self.clock.at(t)
        seen = self.lines
        await self.hooker.broadcast(text)
        await wait_for(lambda: self.lines > seen, f"the line {text!r}")

    def tick(self, t: float) -> None:
        self.until(t, inclusive=False)
        self.servers.run(self._tick(t))

    async def _tick(self, t: float) -> None:
        await self._settle_if_safe()
        self.clock.at(t)
        self.post(Tick(self.clock()))
        await self._settle_if_safe()

    def until(self, t: float, *, inclusive: bool = True) -> None:
        """Play every step of the recording due by ``t`` (before it, unless ``inclusive``)."""
        self.servers.run(self._play(t, inclusive))

    def until_end(self) -> None:
        self.until(max((step.t for step in self.recording.steps), default=0.0))
        self.servers.run(self._settle_if_safe())

    def settled(self) -> None:
        self.servers.run(self.settle())

    def wait(self, condition: Callable[[], object], what: str) -> None:
        self.servers.run(wait_for(condition, what))

    def check_replay(self) -> None:
        """Nothing the app sent fell outside what OBS and the recording can answer."""
        assert self.replayed.unscripted == [] and self.replayed.errors == []

    # --- playing ----------------------------------------------------------------------------------

    def _next_app_request(self, name: str) -> Step:
        return next(
            step
            for step in self.recording.steps[self._position :]
            if step.kind == "request" and step.name == name and name in self.replayed.app_sends
        )

    async def _settle_if_safe(self) -> None:
        if self.settles:
            await self.settle()

    async def _play(self, t: float, inclusive: bool) -> None:
        steps = self.recording.steps
        while self._position < len(steps):
            step = steps[self._position]
            if step.t > t or (step.t == t and not inclusive):
                return
            try:
                await self._play_step(step)
            except AssertionError as exc:
                raise AssertionError(f"{self.recording.name} step {step}: {exc}") from exc
            self._position += 1

    async def _play_step(self, step: Step) -> None:
        replayed = self.replayed
        if step.kind in ("request", "response") and step.name in self.ignored:
            return
        if step.kind == "request":
            if step.name in replayed.app_sends:
                try:
                    await asyncio.wait_for(replayed.arrived(step), WAIT_S)
                except TimeoutError:
                    raise AssertionError(f"the app did not send {step.name} {dict(step.data)}") from None
            elif step.name in SWITCH_REQUESTS:  # someone else's: made in FakeObs, the recorded events follow
                await replayed.switch_quietly(step.name, dict(step.data))
        elif step.kind == "response":
            if step.name in replayed.app_sends:
                await replayed.answer(step)
        elif step.kind == "windows":
            self.obs.window_lists["xcomposite_input"] = list(step.data["items"])
        elif step.kind == "event":
            if step.name in SWITCH_EVENTS and self.ignored:
                return
            await self._event(step)
        elif step.kind == "drop":
            await self._settle_if_safe()
            self.clock.at(step.t)
            since = len(self.received)
            await replayed.drop_clients(None)
            await self.heard(ObsEventName.CONNECTION_LOST, None, since)
        elif step.kind == "reopen":
            await self._settle_if_safe()
            self.clock.at(step.t)
            replayed.played_events.append(step)
            since = len(self.received)
            self.reconnect.set()
            await self.heard(ObsEventName.CONNECTED, None, since)
            await self._settle_if_safe()
        elif step.kind == "obs_closed":
            await self._settle_if_safe()
            self.clock.at(step.t)
            self.discovery.running = False
            replayed.refuse_connections = True
            since = len(self.received)
            await replayed.drop_clients(step.code, step.reason)
            await self.heard(ObsEventName.CONNECTION_LOST, None, since)

    async def _event(self, step: Step) -> None:
        await self._settle_if_safe()
        self.clock.at(step.t)
        data = self.replayed.rewrite(dict(step.data))
        started = str(data.get("outputState", "")).endswith("STARTED")
        created = data.get("newOutputPath") or (data.get("outputPath") if started else None)
        if isinstance(created, str):  # OBS creates the file as it starts writing it
            Path(created).parent.mkdir(parents=True, exist_ok=True)
            Path(created).write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
        self.replayed.played_events.append(step)
        since = len(self.received)
        await self.replayed.emit(step.name, data, intent=step.intent)
        if step.intent & EVENT_SUBSCRIPTIONS and self.replayed.client_count:
            await self.heard(step.name, data, since)

    def close(self) -> None:
        """Quit the app (OBS still answering), then stop the fakes; a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        try:
            super().close()
        finally:
            if self.hooker is not None:
                self.servers.run(self.hooker.stop())
            self.servers.run(self.replayed.stop())
            self.servers.stop()
