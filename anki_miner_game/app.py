"""The composition root (spec 4.1, 4.2): builds the app's parts and wires them; no DI container.

Threads (spec 4.2): widgets, the tray, the dialogs, the hotkey, the presenter's signals and the CLI
server live on the Qt main thread; the session actor, OBS, the text sources, auto mode and the text
feed's websocket live on the I/O loop (``runtime.io_thread.IoThread``); VAD passes run on the
trimmer's job thread. The actor is built on the loop and publishes every ``SessionEvent`` there;
``forward`` turns each into one ``Presenter`` call, whose signals reach the window and the tray on
the main thread, and every accepted line also goes to the text feed (spec 8.2, 15). Dialogs and the
wizard hand their coroutines to the loop through ``IoThread.submit``.

Launch (spec 17 "Unclean previous exit"): the actor itself restores ``obs_restore.json`` and
finalises orphans once reconcile allows it (``SessionActor._launch``), on the one
``FinaliseWorker`` built here; nothing else finalises. The window lists the recent sessions before
the actor runs, so VAD passes a quit or a crash interrupted go to ``VadJobs.rerun`` before any
finalise queues a new one. The settings file is written with the defaults when there is none, and
the setup wizard opens; one that cannot be read is left alone and the defaults run with a banner.
A feed port in use turns the feed off with a banner (spec 17).

OBS: one ``CapturePicker`` and one ``ObsSetup`` per app, since each subscribes to the gateway, which
has no unsubscribe. They share one ``asyncio.Lock`` with the actor: arming, the restore, the
picker's idle listing and wizard step 1 each hold it while they switch OBS's profile or collection.

Saving: a profile saved in its dialog goes to disk and into the profiles the actor and auto mode
look up (``_profile_for``, no disk read on the loop); settings saved in their dialog or by the
wizard go to disk and become the config everything reads, the gateway's password override
included. The last armed game is remembered in the settings.

Quit (``request_quit`` from the window or the tray, or ``close`` after the Qt loop ends): the dialogs
close, the actor shuts down (a recording is stopped and finalised, OBS restored, and every started
text source stopped with its ``wait_closed`` awaited), then auto mode, the finalise worker, the VAD
jobs, the OBS connection and the feed stop, and only then the I/O loop.
"""

import asyncio
import concurrent.futures
import logging
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog

from anki_miner_game import paths, store
from anki_miner_game.addons.ocr_addon import OcrAddon
from anki_miner_game.addons.vad_addon import VadAddon
from anki_miner_game.feed import FeedPortInUseError, FeedServer
from anki_miner_game.gui.cli_verbs import CliServer
from anki_miner_game.gui.game_profile_dialog import CapturePicker, GameProfileDialog, ProfileDialogServices
from anki_miner_game.gui.hotkey_win import GlobalHotkey, HotkeyError
from anki_miner_game.gui.main_window import MainWindow
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.gui.settings_dialog import SettingsDialog
from anki_miner_game.gui.tray import Tray
from anki_miner_game.gui.wizard import ObsSetup, SetupWizard, WizardStep
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
    CommandKind,
    LineAccepted,
    RecordingStarted,
    SessionEvent,
    SessionFinalised,
    SourceStatusChanged,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.profile import GameProfile, OcrSettings, TextMode
from anki_miner_game.obs.client import ObsClient
from anki_miner_game.obs.discovery import LocalObsDiscovery
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.runtime.child_env import child_environ
from anki_miner_game.runtime.io_thread import IoThread
from anki_miner_game.session.naming import slugify
from anki_miner_game.session.session import FinaliseWorker, SessionActor
from anki_miner_game.text.sources.clipboard_source import ClipboardSource
from anki_miner_game.text.sources.ocr_source import OcrSource
from anki_miner_game.text.sources.websocket_source import WebsocketSource
from anki_miner_game.vad.trimmer import VadTrimmer

log = logging.getLogger(__name__)

FEED_BANNER_KEY: Final = "feed"
CONFIG_BANNER_KEY: Final = "config"
PROFILES_BANNER_KEY: Final = "profiles"
PROFILE_SAVE_BANNER_KEY: Final = "profile_save"
HOTKEY_BANNER_KEY: Final = "hotkey"
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
HotkeyFactory = Callable[[QObject], GlobalHotkey | None]
"""Builds the global Start/Stop hotkey with the app as its parent; ``None`` where there is none."""


def platform_hotkey(parent: QObject) -> GlobalHotkey | None:
    """Windows has a global hotkey (spec 16); Linux binds the CLI verbs in the desktop instead."""
    return GlobalHotkey(parent) if sys.platform == "win32" else None


def hook_sources(cfg: AppConfig, profile: GameProfile) -> list[TextSource]:
    """Hook mode: one websocket source per enabled hooker the game uses (``source_ids``, all when ``None``).

    Nothing in OCR mode.
    """
    if profile.text_mode is not TextMode.HOOK:
        return []
    wanted = profile.source_ids
    return [
        WebsocketSource(source.id, source.uri)
        for source in cfg.text_sources
        if source.enabled and (wanted is None or source.id in wanted)
    ]


def game_sources(cfg: AppConfig, profile: GameProfile, *, ocr: Callable[[OcrSettings], TextSource]) -> list[TextSource]:
    """The text sources of an armed game (spec 8.1): its hookers, plus the clipboard when the profile
    takes it; in OCR mode only ``ocr(profile.ocr)``, a supervised owocr. Runs on the I/O loop."""
    if profile.text_mode is TextMode.OCR:
        return [ocr(profile.ocr)]
    sources = hook_sources(cfg, profile)
    if profile.clipboard:
        sources.append(ClipboardSource())
    return sources


def open_url(
    url: QUrl,
    *,
    frozen: bool | None = None,
    platform: str | None = None,
    spawn: Callable[..., object] = subprocess.Popen,
) -> bool:
    """Open a folder or the text feed's page in the user's own program.

    ``QDesktopServices.openUrl``, except in a frozen Linux build, where the program would inherit the
    bundle's ``LD_LIBRARY_PATH``: there ``xdg-open`` runs with ``runtime.child_env.child_environ()``.
    ``frozen`` defaults to ``sys.frozen`` and ``platform`` to ``sys.platform``.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen or (platform or sys.platform) in ("win32", "darwin"):
        return QDesktopServices.openUrl(url)
    try:
        spawn(
            ["xdg-open", url.toString()],
            env=child_environ(frozen=frozen, platform=platform),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("cannot open %s: %s", url.toString(), exc)
        return False
    return True


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
    picker: CapturePicker
    obs_setup: ObsSetup
    tasks: tuple["asyncio.Task[None]", ...] = ()


class App(QObject):
    """The running app. Build and call ``start`` and ``close`` on the Qt main thread.

    ``obs``, ``source_factory`` and ``hotkey`` are what the tests replace (``source_factory``
    defaults to ``game_sources``). With ``server_name`` the app listens for CLI verbs
    (``gui.cli_verbs``); the caller has made sure no other instance does.
    """

    stopped = pyqtSignal()
    """The quit ``request_quit`` began has finished; the caller ends the Qt loop."""

    def __init__(
        self,
        *,
        obs: Callable[[Callable[[], AppConfig]], ObsServices] = local_obs,
        source_factory: SourceFactory | None = None,
        server_name: str | None = None,
        hotkey: HotkeyFactory = platform_hotkey,
    ) -> None:
        super().__init__()
        self.presenter = QtPresenter()
        self._obs = obs
        self._source_factory: SourceFactory = source_factory or self._game_sources
        self._server_name = server_name
        self._hotkey_factory = hotkey
        self._config = AppConfig()
        self._first_run = False
        self._settings_unusable = False
        """The settings file could not be read at launch: left alone until the user saves settings."""
        self._profiles: Mapping[str, GameProfile] = MappingProxyType({})
        self._io = IoThread()
        self._running: _Running | None = None
        self._window: MainWindow | None = None
        self._tray: Tray | None = None
        self._hotkey: GlobalHotkey | None = None
        self._cli: CliServer | None = None
        self._feed: FeedServer | None = None
        self._stopping: concurrent.futures.Future[None] | None = None
        self._closed = False
        self._journalling_held = False
        """Between ``StateChanged(RECORDING)`` and ``RecordingStarted``; read and written on the loop."""
        home = paths.home()
        self._vad_addon = VadAddon(home)
        self._ocr_addon = OcrAddon(home)
        self._vad_jobs = VadTrimmer(self._vad_addon, self.presenter, lambda: self._config)

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def window(self) -> MainWindow:
        if self._window is None:
            raise RuntimeError("the app has not started")
        return self._window

    @property
    def tray(self) -> Tray:
        if self._tray is None:
            raise RuntimeError("the app has not started")
        return self._tray

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
        window = self._window = MainWindow(
            actor,
            self.presenter.signals,
            self._games(),
            on_quit=self.request_quit,
            selected=self._config.last_game,
            text_sources=_enabled_sources(self._config),
            output_root=lambda: paths.output_root(self._config),
            vad_jobs=self._vad_jobs,  # the recent sessions hand interrupted passes on before the actor runs
            open_url=open_url,
        )
        window.new_game_requested.connect(lambda: self._open_profile_dialog(None))
        window.edit_game_requested.connect(self._open_profile_dialog)
        window.settings_requested.connect(self._open_settings)
        window.setup_requested.connect(lambda: self._open_wizard(WizardStep.OBS))
        self.presenter.signals.state_changed.connect(self._remember_game)
        tray = self._tray = Tray(
            actor, self.presenter.signals, game=self._selected_game, feed_url=self._feed_url, open_url=open_url
        )
        tray.show_requested.connect(self._show_window)
        tray.quit_requested.connect(self.request_quit)
        window.minimise_to_tray = tray.show()
        self._hotkey = self._hotkey_factory(self)
        if self._hotkey is not None:
            self._hotkey.activated.connect(lambda: actor.post(UserCommand(CommandKind.TOGGLE)))
            self._register_hotkey()
        for banner in banners:
            self.presenter.banner(banner)
        self._io.submit(self._run()).result()
        if self._server_name is not None:
            self._cli = CliServer(self._server_name, on_command=actor.post, on_show=self._show_window, parent=self)
            self._cli.listen()
        window.show()
        if self._first_run:
            self._open_wizard(WizardStep.OBS)

    def _load_settings(self) -> list[Banner]:
        banners: list[Banner] = []
        first_run = not paths.config_path().exists()
        try:
            self._config = store.load_config()
        except store.StoreError as exc:
            log.warning("settings file unusable: %s", exc)
            self._settings_unusable = True
            banners.append(
                Banner(
                    CONFIG_BANNER_KEY,
                    BannerLevel.ERROR,
                    f"The settings file cannot be used ({exc}); this run uses the defaults and leaves the file alone.",
                )
            )
        else:
            self._first_run = first_run
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

    def _game_sources(self, cfg: AppConfig, profile: GameProfile) -> list[TextSource]:
        return game_sources(cfg, profile, ocr=self._ocr_source)

    def _ocr_source(self, settings: OcrSettings) -> TextSource:
        """owocr through the OCR add-on; its give-up banner goes to the presenter (spec 14, 17)."""
        return OcrSource(self._ocr_addon, settings, on_banner=lambda event: forward(self.presenter, event))

    async def _build(self) -> SessionActor:
        """On the loop: the actor and everything it drives, built on the thread it runs on."""
        services = self._obs(lambda: self._config)
        finaliser = FinaliseWorker()
        obs_lock = asyncio.Lock()
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
            vad_jobs=self._vad_jobs,
            obs_lock=obs_lock,
        )
        auto = AutoMode(actor, self._profile_for, services.provisioner.list_windows)  # before any StateChanged
        actor.subscribe(lambda event: forward(self.presenter, event))
        actor.subscribe(self._broadcast)
        picker = CapturePicker(services.gateway, services.provisioner, actor, obs_lock=obs_lock)
        obs_setup = ObsSetup(
            services.discovery, services.gateway, services.provisioner, session=actor, obs_lock=obs_lock
        )
        self._running = _Running(services, finaliser, actor, auto, picker, obs_setup)
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

    async def _restart_feed(self) -> None:
        """On the loop: the feed again with the settings just saved (ports, on or off)."""
        if self._stopping is not None:
            return
        feed, self._feed = self._feed, None
        if feed is not None:
            await _step("the text feed", feed.stop())
        self.presenter.banner_cleared(FEED_BANNER_KEY)
        await self._start_feed()

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

    # --- the window, the tray and the hotkey (main thread) --------------------------------------

    def _games(self) -> list[tuple[str, str]]:
        return sorted(((slug, p.title) for slug, p in self._profiles.items()), key=lambda game: game[1].casefold())

    def _selected_game(self) -> str | None:
        slug = self.window.game.currentData()
        return slug if isinstance(slug, str) else None

    def _feed_url(self) -> str | None:
        feed = self._feed
        return None if feed is None else feed.page_url

    def _show_window(self) -> None:
        window = self.window
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def _register_hotkey(self) -> None:
        if self._hotkey is None:
            return
        try:
            self._hotkey.register(self._config.hotkey)
        except HotkeyError as exc:
            log.warning("hotkey not registered: %s", exc)
            self.presenter.banner(Banner(HOTKEY_BANNER_KEY, BannerLevel.WARNING, str(exc)))
        else:
            self.presenter.banner_cleared(HOTKEY_BANNER_KEY)

    def _remember_game(self, state: AppState, slug: str | None) -> None:
        """The armed game is the one the window selects at the next launch (``AppConfig.last_game``).

        Not while the settings file that could not be read is left alone.
        """
        if state is not AppState.ARMED or slug is None or slug == self._config.last_game or self._settings_unusable:
            return
        try:
            self._adopt_config(replace(self._config, last_game=slug))
        except store.StoreError as exc:
            log.warning("the last armed game is not remembered: %s", exc)

    # --- dialogs and the wizard (main thread) ---------------------------------------------------

    def _open_profile_dialog(self, slug: str | None) -> None:
        """New game (``slug`` ``None``) or edit one; the saved profile is stored by ``_save_profile``."""
        running = self._running
        profile = None if slug is None else self._profiles.get(slug)
        if running is None or (slug is not None and profile is None):
            return
        services = ProfileDialogServices(
            run=self._io.submit,
            capture=running.picker,
            ocr_picker=self._ocr_addon,
            ocr_addon=self._ocr_addon,
            slugify=slugify,
        )
        dialog = GameProfileDialog(
            services, self._config.text_sources, profile, taken_slugs=tuple(self._profiles), parent=self.window
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.profile_saved.connect(self._save_profile)
        dialog.open()

    def _save_profile(self, profile: GameProfile) -> None:
        try:
            store.save_profile(profile)
        except (store.StoreError, store.InvalidProfileError) as exc:
            log.warning("game profile %s not saved: %s", profile.slug, exc)
            text = f"{profile.title} could not be saved: {exc}"
            self.presenter.banner(Banner(PROFILE_SAVE_BANNER_KEY, BannerLevel.ERROR, text))
            return
        self.presenter.banner_cleared(PROFILE_SAVE_BANNER_KEY)
        self._profiles = MappingProxyType({**self._profiles, profile.slug: profile})  # what the loop looks up
        idle = self._running is None or self._running.actor.state is AppState.IDLE
        self.window.set_games(self._games(), profile.slug if idle else None)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._config, vad_addon=self._vad_addon, parent=self.window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.config_saved.connect(self._save_settings)
        dialog.setup_step_requested.connect(lambda step: self._open_wizard(step, settings=dialog))
        dialog.open()

    def _save_settings(self, cfg: AppConfig) -> None:
        try:
            self._adopt_config(cfg)
        except store.StoreError as exc:
            log.warning("settings not saved: %s", exc)
            self.presenter.banner(Banner(CONFIG_BANNER_KEY, BannerLevel.ERROR, f"Settings cannot be saved: {exc}"))
            return
        self.presenter.banner_cleared(CONFIG_BANNER_KEY)

    def _open_wizard(self, step: WizardStep, *, settings: SettingsDialog | None = None) -> None:
        """The setup wizard at ``step``; over ``settings`` when a step is re-run from there (spec 16)."""
        running = self._running
        if running is None:
            return

        def save(cfg: AppConfig) -> None:
            self._adopt_config(cfg)
            if settings is not None:
                settings.take_setup(cfg)

        wizard = SetupWizard(
            obs=running.obs_setup,
            config=self._config,
            save_config=save,
            run=self._io.submit,
            source_factory=lambda source: WebsocketSource(source.id, source.uri),
            vad_addon=self._vad_addon,
            ocr_addon=self._ocr_addon,
            start=step,
            open_url=open_url,
            parent=settings if settings is not None else self.window,
        )
        wizard.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        wizard.open()

    def _adopt_config(self, cfg: AppConfig) -> None:
        """Store ``cfg`` and run with it from now on; raises ``store.StoreError`` when it cannot be stored.

        Text sources, OBS and recording settings apply at the next arm and the next connect; the
        window's lights and sessions, the hotkey and the feed follow at once.
        """
        store.save_config(cfg)
        old, self._config = self._config, cfg
        self._settings_unusable = False
        if cfg.text_sources != old.text_sources:
            self.window.set_text_sources(_enabled_sources(cfg))
        if cfg.output_root != old.output_root:
            self.window.reload_sessions()
        if cfg.hotkey != old.hotkey:
            self._register_hotkey()
        if cfg.feed != old.feed:
            self._io.submit(self._restart_feed())

    # --- quitting -------------------------------------------------------------------------------

    def request_quit(self) -> None:
        """Hide the window and quit in the background; ``stopped`` follows. Main thread."""
        if self._stopping is not None:
            return
        self._put_away()
        self._stopping = self._io.submit(self._stop())
        self._stopping.add_done_callback(lambda _done: self.stopped.emit())

    def close(self) -> None:
        """Finish the quit (starting it when ``request_quit`` did not) and stop the I/O loop. Main thread."""
        if self._closed:
            return
        self._closed = True
        if self._cli is not None:
            self._cli.close()
        self._put_away()
        if self._hotkey is not None:
            self._hotkey.unregister()  # here, not only at aboutToQuit: a QApplication can outlive the app
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

    def _put_away(self) -> None:
        """Close the dialogs (the wizard stops the sources it started) and hide the window and the tray."""
        if self._window is not None:
            for dialog in self._window.findChildren(QDialog):
                dialog.close()
            self._window.hide()
        if self._tray is not None:
            self._tray.icon.hide()

    async def _stop(self) -> None:
        running = self._running
        if running is None:
            return
        actor_task, auto_task = running.tasks or (None, None)
        if actor_task is not None and not actor_task.done():
            # Before auto mode: cancelling its window poll mid-request drops the OBS link (T12), and
            # the session could then neither stop the recording nor restore OBS.
            await _step("the session", running.actor.shutdown())
            await asyncio.gather(actor_task, return_exceptions=True)
        if auto_task is not None:
            auto_task.cancel()
            await asyncio.gather(auto_task, return_exceptions=True)
        await _step("the finalise worker", asyncio.to_thread(running.finaliser.shutdown))
        await _step("the VAD jobs", asyncio.to_thread(self._vad_jobs.close))  # a pending pass stays queued
        await _step("the OBS connection", running.services.gateway.close())
        feed, self._feed = self._feed, None
        if feed is not None:
            await _step("the text feed", feed.stop())
        log.info("stopped")


def _enabled_sources(cfg: AppConfig) -> list[tuple[str, str]]:
    return [(source.id, source.name) for source in cfg.text_sources if source.enabled]


async def _step(what: str, work: Awaitable[None]) -> None:
    """One step of the quit; its failure is logged and the next step still runs."""
    try:
        await work
    except Exception:
        log.exception("stopping %s failed", what)


def _report_end(task: "asyncio.Task[None]") -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("%s ended with an error", task.get_name(), exc_info=exc)
