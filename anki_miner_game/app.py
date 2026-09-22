"""The composition root (spec 4.1, 4.2): builds the app's parts and wires them; no DI container.

Threads (spec 4.2): widgets, the presenter's signals and the CLI server live on the Qt main thread;
the session actor, OBS, the text sources, auto mode and the text feed's websocket live on the I/O
loop (``runtime.io_thread.IoThread``). The actor is built on the loop and publishes every
``SessionEvent`` there; ``forward`` turns each into one ``Presenter`` call, whose signals reach the
window on the main thread, and every accepted line also goes to the text feed (spec 8.2, 15).

Launch (spec 17 "Unclean previous exit"): the actor itself restores ``obs_restore.json`` and
finalises orphans once reconcile allows it (``SessionActor._launch``), on the one
``FinaliseWorker`` built here; nothing else finalises. The settings file is written with the
defaults when there is none; one that cannot be read is left alone and the defaults run with a
banner. A feed port in use turns the feed off for the run with a banner (spec 17).

Quit (``request_quit`` from the window, or ``close`` after the Qt loop ends): auto mode stops, the
actor shuts down (a recording is stopped and finalised, OBS restored, and every started text source
stopped with its ``wait_closed`` awaited), then the finalise worker, the OBS connection and the
feed stop, and only then the I/O loop.

This first composition wires hook-mode websocket sources and auto mode; the clipboard, OCR, the VAD
pass, the tray, the hotkey, the dialogs and the wizard are wired by T26.
"""

import asyncio
import concurrent.futures
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from PyQt6.QtCore import QObject, pyqtSignal

from anki_miner_game import paths, store
from anki_miner_game.feed import FeedPortInUseError, FeedServer
from anki_miner_game.gui.cli_verbs import CliServer
from anki_miner_game.gui.main_window import MainWindow
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway, Provisioner
from anki_miner_game.interfaces.presenter import Presenter
from anki_miner_game.interfaces.text_source import TextSource
from anki_miner_game.lifecycle.auto import AutoMode
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import (
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    LineAccepted,
    RecordingStarted,
    SessionEvent,
    SessionFinalised,
    SourceStatusChanged,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.profile import GameProfile, TextMode
from anki_miner_game.obs.client import ObsClient
from anki_miner_game.obs.discovery import LocalObsDiscovery
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.runtime.io_thread import IoThread
from anki_miner_game.session.session import FinaliseWorker, SessionActor
from anki_miner_game.text.sources.websocket_source import WebsocketSource

log = logging.getLogger(__name__)

FEED_BANNER_KEY: Final = "feed"
CONFIG_BANNER_KEY: Final = "config"
PROFILES_BANNER_KEY: Final = "profiles"
SHUTDOWN_TIMEOUT_S: Final = 120.0
"""``close`` waits this long for the quit; the actor's own worst case is about 40 s
(``SessionActor.shutdown``)."""


@dataclass(frozen=True)
class ObsServices:
    """The OBS side of the app, built once on the I/O loop (one provisioner per app)."""

    discovery: ObsDiscovery
    gateway: ObsGateway
    provisioner: Provisioner


def local_obs(config: Callable[[], AppConfig]) -> ObsServices:
    """The user's OBS: discovery on this machine, the obs-websocket gateway, provisioning over it.

    The gateway reads the credentials from OBS's config at every connect (``ObsClient``).
    """
    discovery = LocalObsDiscovery(config)
    gateway = ObsClient(lambda: discovery.credentials(config()))
    return ObsServices(discovery, gateway, ObsProvisioner(gateway))


SourceFactory = Callable[[AppConfig, GameProfile], Sequence[TextSource]]


def hook_sources(cfg: AppConfig, profile: GameProfile) -> list[TextSource]:
    """Hook mode: one websocket source per enabled hooker the game uses (``source_ids``, all when ``None``).

    Nothing in OCR mode; the clipboard and OCR sources are T26's.
    """
    if profile.text_mode is not TextMode.HOOK:
        return []
    wanted = profile.source_ids
    return [
        WebsocketSource(source.id, source.uri)
        for source in cfg.text_sources
        if source.enabled and (wanted is None or source.id in wanted)
    ]


def forward(presenter: Presenter, event: SessionEvent) -> None:
    """One ``SessionEvent`` as its one ``Presenter`` call (``Presenter`` docstring)."""
    match event:
        case StateChanged():
            presenter.state_changed(event.state, event.slug)
        case SourceStatusChanged():
            presenter.source_status(event.source_id, event.status)
        case LineAccepted():
            presenter.line_accepted(event.line, event.offset_ms, event.replaces_previous)
        case BannerRaised():
            presenter.banner(event.banner)
        case BannerCleared():
            presenter.banner_cleared(event.key)
        case SessionFinalised():
            presenter.session_finished(event.manifest_path)
        case _:  # RecordingStarted, RecordingStopped: for SessionControl subscribers such as auto mode
            pass


@dataclass
class _Running:
    services: ObsServices
    finaliser: FinaliseWorker
    actor: SessionActor
    auto: AutoMode
    tasks: tuple["asyncio.Task[None]", ...] = ()


class App(QObject):
    """The running app. Build and call ``start`` and ``close`` on the Qt main thread.

    ``obs`` and ``source_factory`` are what the tests replace. With ``server_name`` the app listens
    for CLI verbs (``gui.cli_verbs``); the caller has made sure no other instance does.
    """

    stopped = pyqtSignal()
    """The quit ``request_quit`` began has finished; the caller ends the Qt loop."""

    def __init__(
        self,
        *,
        obs: Callable[[Callable[[], AppConfig]], ObsServices] = local_obs,
        source_factory: SourceFactory = hook_sources,
        server_name: str | None = None,
    ) -> None:
        super().__init__()
        self.presenter = QtPresenter()
        self._obs = obs
        self._source_factory = source_factory
        self._server_name = server_name
        self._config = AppConfig()
        self._profiles: Mapping[str, GameProfile] = MappingProxyType({})
        self._io = IoThread()
        self._running: _Running | None = None
        self._window: MainWindow | None = None
        self._cli: CliServer | None = None
        self._feed: FeedServer | None = None
        self._stopping: concurrent.futures.Future[None] | None = None
        self._closed = False
        self._journalling_held = False
        """Between ``StateChanged(RECORDING)`` and ``RecordingStarted``; read and written on the loop."""

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def window(self) -> MainWindow:
        if self._window is None:
            raise RuntimeError("the app has not started")
        return self._window

    @property
    def feed(self) -> FeedServer | None:
        """The running text feed; ``None`` while it is off (disabled, or it could not start)."""
        return self._feed

    def post(self, command: UserCommand) -> None:
        """Hand a command to the session actor; safe from any thread once started."""
        if self._running is None:
            raise RuntimeError("the app has not started")
        self._running.actor.post(command)

    # --- starting -------------------------------------------------------------------------------

    def start(self) -> None:
        banners = self._load_settings()
        self._io.start_loop()
        actor = self._io.submit(self._build()).result()
        games = sorted(((slug, p.title) for slug, p in self._profiles.items()), key=lambda game: game[1].casefold())
        self._window = MainWindow(
            actor,
            self.presenter.signals,
            games,
            on_quit=self.request_quit,
            selected=self._config.last_game,
            text_sources=[(source.id, source.name) for source in self._config.text_sources if source.enabled],
            output_root=lambda: paths.output_root(self._config),
        )
        for banner in banners:
            self.presenter.banner(banner)
        self._io.submit(self._run()).result()
        if self._server_name is not None:
            self._cli = CliServer(self._server_name, on_command=actor.post, on_show=self._show_window, parent=self)
            self._cli.listen()
        self._window.show()

    def _load_settings(self) -> list[Banner]:
        banners: list[Banner] = []
        first_run = not paths.config_path().exists()
        try:
            self._config = store.load_config()
        except store.StoreError as exc:
            log.warning("settings file unusable: %s", exc)
            banners.append(
                Banner(
                    CONFIG_BANNER_KEY,
                    BannerLevel.ERROR,
                    f"The settings file cannot be used ({exc}); this run uses the defaults and leaves the file alone.",
                )
            )
        else:
            if first_run:
                try:
                    log.info("default settings written to %s", store.save_config(self._config))
                except store.StoreError as exc:
                    log.warning("default settings not written: %s", exc)
                    banners.append(Banner(CONFIG_BANNER_KEY, BannerLevel.WARNING, f"Settings cannot be saved: {exc}"))
        loaded = store.load_profiles()
        self._profiles = MappingProxyType(dict(loaded.profiles))
        if loaded.errors:
            for error in loaded.errors:
                log.warning("game profile left out: %s", error)
            names = ", ".join(error.path.name for error in loaded.errors)
            banners.append(
                Banner(
                    PROFILES_BANNER_KEY,
                    BannerLevel.WARNING,
                    f"Some game profiles cannot be read and are left out: {names}.",
                )
            )
        return banners

    def _profile_for(self, slug: str) -> GameProfile | None:
        """Runs on the I/O loop: a lookup over the profiles already loaded, never the disk."""
        return self._profiles.get(slug)

    async def _build(self) -> SessionActor:
        """On the loop: the actor and everything it drives, built on the thread it runs on."""
        services = self._obs(lambda: self._config)
        finaliser = FinaliseWorker()
        actor = SessionActor(
            loop=asyncio.get_running_loop(),
            gateway=services.gateway,
            discovery=services.discovery,
            provisioner=services.provisioner,
            recorder=ObsRecorder(services.gateway),
            finaliser=finaliser,
            get_config=lambda: self._config,
            get_profile=self._profile_for,
            source_factory=self._source_factory,
        )
        auto = AutoMode(actor, self._profile_for, services.provisioner.list_windows)  # before any StateChanged
        actor.subscribe(lambda event: forward(self.presenter, event))
        actor.subscribe(self._broadcast)
        self._running = _Running(services, finaliser, actor, auto)
        return actor

    async def _run(self) -> None:
        """On the loop: the feed first (its banner is the first thing shown), then the actor and auto mode."""
        running = self._running
        assert running is not None
        await self._start_feed()
        running.tasks = (
            asyncio.create_task(running.actor.run(), name="session-actor"),
            asyncio.create_task(running.auto.run(), name="auto-mode"),
        )
        for task in running.tasks:
            task.add_done_callback(_report_end)

    async def _start_feed(self) -> None:
        settings = self._config.feed
        if not settings.enabled:
            return
        feed = FeedServer(settings.ws_port, settings.http_port)
        try:
            await feed.start()
        except FeedPortInUseError as exc:
            text = f"The text feed is off for this run: port {exc.port} is in use."
        except OSError as exc:  # page.html missing from the bundle, a bind refused for another reason
            text = f"The text feed is off for this run: {exc}"
        else:
            self._feed = feed
            return
        log.warning("%s", text)
        self.presenter.banner(Banner(FEED_BANNER_KEY, BannerLevel.WARNING, text))

    def _broadcast(self, event: SessionEvent) -> None:
        """On the loop: every accepted line, a typewriter merge's longer text too, goes to the feed once.

        Lines held for an auto start come again with their offsets between ``StateChanged(RECORDING)``
        and ``RecordingStarted`` (``session.session`` docstring); the feed sent them when accepted.
        """
        if isinstance(event, StateChanged):
            self._journalling_held = event.state is AppState.RECORDING
        elif isinstance(event, RecordingStarted):
            self._journalling_held = False
        elif isinstance(event, LineAccepted) and self._feed is not None and not self._journalling_held:
            self._feed.broadcast(event.line.text)

    def _show_window(self) -> None:
        window = self.window
        window.showNormal()
        window.raise_()
        window.activateWindow()

    # --- quitting -------------------------------------------------------------------------------

    def request_quit(self) -> None:
        """Hide the window and quit in the background; ``stopped`` follows. Main thread."""
        if self._stopping is not None:
            return
        if self._window is not None:
            self._window.hide()
        self._stopping = self._io.submit(self._stop())
        self._stopping.add_done_callback(lambda _done: self.stopped.emit())

    def close(self) -> None:
        """Finish the quit (starting it when ``request_quit`` did not) and stop the I/O loop. Main thread."""
        if self._closed:
            return
        self._closed = True
        if self._cli is not None:
            self._cli.close()
        if self._window is not None:
            self._window.hide()
        if not self._io.isRunning():
            return
        if self._stopping is None:
            self._stopping = self._io.submit(self._stop())
        try:
            self._stopping.result(timeout=SHUTDOWN_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            log.error("quitting took longer than %s s; stopping the I/O loop anyway", SHUTDOWN_TIMEOUT_S)
        except Exception:
            log.exception("quitting failed")
        self._io.stop_loop()

    async def _stop(self) -> None:
        running = self._running
        if running is None:
            return
        actor_task, auto_task = running.tasks or (None, None)
        if auto_task is not None:
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)
        if actor_task is not None and not actor_task.done():
            await _step("the session", running.actor.shutdown())
            await asyncio.gather(actor_task, return_exceptions=True)
        await _step("the finalise worker", asyncio.to_thread(running.finaliser.shutdown))
        await _step("the OBS connection", running.services.gateway.close())
        feed, self._feed = self._feed, None
        if feed is not None:
            await _step("the text feed", feed.stop())
        log.info("stopped")


async def _step(what: str, work: Awaitable[None]) -> None:
    """One step of the quit; its failure is logged and the next step still runs."""
    try:
        await work
    except Exception:
        log.exception("stopping %s failed", what)


def _report_end(task: "asyncio.Task[None]") -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("%s ended with an error", task.get_name(), exc_info=exc)
