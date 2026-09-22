"""An ``App`` over the session actor's fake OBS, for the composition and launch tests."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anki_miner_game import store
from anki_miner_game.app import App, HotkeyFactory, ObsServices, SourceFactory
from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.config import AppConfig, FeedSettings
from anki_miner_game.models.messages import AppState, CommandKind, SourceStatus, UserCommand
from anki_miner_game.models.profile import GameProfile
from tests.session.actor_harness import FakeClock, FakeDiscovery, FakeGateway, FakeObs, FakeProvisioner

SLUG = "steins-gate"
TITLE = "Steins;Gate"
PROFILE = GameProfile(slug=SLUG, title=TITLE)
WAIT_MS = 10_000


class Source:
    """A text source whose ``wait_closed`` takes a while, as owocr's tree kill does."""

    def __init__(self, source_id: str = "textractor") -> None:
        self._id = source_id
        self.sink: LineSink | None = None
        self.listener: StatusListener | None = None
        self.stopped = False
        self.closed_while_the_loop_ran = False

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.DISCONNECTED if self.stopped else SourceStatus.CONNECTED

    def start(self, sink: LineSink) -> None:
        self.sink = sink

    def stop(self) -> None:
        self.stopped = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0.2)
        self.closed_while_the_loop_ran = asyncio.get_running_loop().is_running()

    def set_status_listener(self, cb: StatusListener) -> None:
        self.listener = cb

    def line(self, raw: str) -> None:
        assert self.sink is not None, "not started"
        self.sink(raw, 1.0, self._id)


class Rig:
    """An ``App`` over the actor's fake OBS, with settings in the isolated home."""

    def __init__(self, qtbot: Any, tmp_path: Path) -> None:
        self.qtbot = qtbot
        self.output_root = tmp_path / "out"
        self.cfg = AppConfig(output_root=str(self.output_root), feed=FeedSettings(ws_port=0, http_port=0))
        self.obs = FakeObs()
        self.gateway = FakeGateway(self.obs, FakeClock())
        self.discovery = FakeDiscovery()
        self.provisioner = FakeProvisioner(self.gateway)
        self.sources = [Source()]
        self.profile = PROFILE
        """The one game profile saved before the app starts."""
        self.source_factory: SourceFactory | None = lambda _cfg, _game: self.sources
        """``None``: the app's own text sources (``app.game_sources``)."""
        self.hotkey: HotkeyFactory = lambda _parent: None
        """No global hotkey unless a test gives one (a real one would register system-wide on Windows)."""
        self.events: list[tuple[str, tuple[Any, ...]]] = []
        self.app: App | None = None

    def start(self, *, name: str | None = None, write_config: bool = True) -> App:
        if write_config:
            store.save_config(self.cfg)
        store.save_profile(self.profile)
        services = ObsServices(self.discovery, self.gateway, self.provisioner)
        app = App(
            obs=lambda _config: services,
            source_factory=self.source_factory,
            server_name=name,
            hotkey=self.hotkey,
        )
        signals = app.presenter.signals
        for signal_name in (
            "state_changed",
            "source_status",
            "banner",
            "banner_cleared",
            "session_finished",
            "vad_finished",
        ):
            getattr(signals, signal_name).connect(
                lambda *args, signal_name=signal_name: self.events.append((signal_name, args))
            )
        self.app = app
        app.start()
        self.qtbot.addWidget(app.window)
        self.qtbot.addWidget(app.tray.menu)
        return app

    def close(self) -> None:
        if self.app is not None:
            self.app.close()

    def banners(self) -> dict[str, str]:
        shown: dict[str, str] = {}
        for name, args in self.events:
            if name == "banner":
                shown[args[0].key] = args[0].text
            elif name == "banner_cleared":
                shown.pop(args[0], None)
        return shown

    def state(self) -> AppState | None:
        states = [args[0] for name, args in self.events if name == "state_changed"]
        return states[-1] if states else None

    def wait(self, condition: Callable[[], object]) -> None:
        self.qtbot.waitUntil(lambda: bool(condition()), timeout=WAIT_MS)

    def arm(self) -> None:
        assert self.app is not None
        self.app.post(UserCommand(CommandKind.ARM, slug=SLUG))
        self.wait(lambda: self.state() is AppState.ARMED)
