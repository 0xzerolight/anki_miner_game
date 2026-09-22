"""The composition (spec 4.1, 4.2, 16, 17): settings at launch, the launch duties, the text feed, the CLI
verbs and quitting, with OBS faked (the actor's fakes) or served by ``FakeObsServer``."""

import asyncio
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from websockets.sync.client import connect

from anki_miner_game import app as app_mod
from anki_miner_game import paths, store
from anki_miner_game.app import App, ObsServices, forward, hook_sources
from anki_miner_game.feed import FeedServer, http_server
from anki_miner_game.gui.cli_verbs import send, server_name
from anki_miner_game.models.config import AppConfig, FeedSettings, TextSourceConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import (
    ClockRecord,
    GameRef,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    RecordingStarted,
    RecordingStopped,
    SessionFinalised,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.obs import OutputState
from anki_miner_game.models.profile import GameProfile, TextMode
from anki_miner_game.obs.client import ObsClient
from anki_miner_game.obs.discovery import LocalObsDiscovery
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import Journal, LineRecord
from anki_miner_game.session.manifest import load_manifest, write_manifest_atomic
from anki_miner_game.session.restore import ObsRestore, restore_path, save_restore
from anki_miner_game.text.sources.websocket_source import WebsocketSource
from tests.app_rig import SLUG, TITLE, WAIT_MS, Rig
from tests.fakes.fake_obs_server import FakeObsServer
from tests.gui.session_fakes import TITLE as SESSION_TITLE
from tests.gui.session_fakes import manifest, place


@pytest.fixture
def rig(qtbot, tmp_path) -> Iterator[Rig]:
    made = Rig(qtbot, tmp_path)
    yield made
    made.close()


@pytest.fixture
def feed_on_free_ports(monkeypatch):
    """The default settings bind the feed's 6678/6679; tests never bind a default port."""
    monkeypatch.setattr(app_mod, "FeedServer", lambda _ws, _http: FeedServer(0, 0))


# Settings at launch -------------------------------------------------------------------------------


def test_a_first_launch_writes_the_default_settings(rig, feed_on_free_ports):
    assert not paths.config_path().exists()
    rig.start(write_config=False)
    assert paths.config_path().is_file()
    assert store.load_config() == AppConfig()


def test_settings_that_cannot_be_read_are_kept_and_the_defaults_used(rig, feed_on_free_ports):
    paths.config_path().write_text("{ not json", encoding="utf-8")
    app = rig.start(write_config=False)
    rig.wait(lambda: "config" in rig.banners())
    assert "defaults" in rig.banners()["config"]
    assert paths.config_path().read_text(encoding="utf-8") == "{ not json"
    assert app.config == AppConfig()


def test_a_game_profile_that_cannot_be_read_is_reported(rig):
    paths.games_dir().mkdir(parents=True, exist_ok=True)
    (paths.games_dir() / "broken.json").write_text("[]", encoding="utf-8")
    app = rig.start()
    rig.wait(lambda: "profiles" in rig.banners())
    assert "broken.json" in rig.banners()["profiles"]
    assert [app.window.game.itemData(i) for i in range(app.window.game.count())] == [SLUG]


def test_the_window_shows_the_enabled_sources_and_the_sessions_in_the_output_folder(rig):
    rig.cfg = replace(
        rig.cfg,
        text_sources=(
            TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677"),
            TextSourceConfig(id="agent", name="Agent", uri="localhost:9001", enabled=False),
        ),
    )
    place(rig.output_root, manifest(3))
    app = rig.start()
    assert app.window.status_row.names() == ["OBS", "Textractor"]
    assert [cells[0] for cells in app.window.recent.cells()] == [f"{SESSION_TITLE} - 03"]


# Launch duties (spec 6.2, 6.3, 10.3, 17 "Unclean previous exit") ----------------------------------


def leave_orphan(incoming: Path, stem: str) -> None:
    """What a crashed app leaves in ``_incoming/``: video, manifest ``recording`` and journal."""
    video = incoming / f"{stem}.mkv"
    incoming.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
    manifest = SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug=SLUG, title=TITLE),
        index=1,
        state=ManifestState.RECORDING,
        started_at="2026-10-01T20:00:00Z",
        obs=ObsRecord(
            version="32.2.2",
            websocket="5.7.4",
            profile=OBS_PROFILE_NAME,
            collection=OBS_COLLECTION_NAME,
            output_path=str(video),
        ),
        clock=ClockRecord(),
        text_mode=TextMode.HOOK,
    )
    write_manifest_atomic(incoming / f"{stem}.session.json", manifest)
    journal = Journal(incoming / f"{stem}.lines.jsonl")
    journal.append(LineRecord(offset_ms=2_000, text="まえ", source="textractor"))
    journal.close()


def test_launch_restores_obs_and_leaves_orphans_to_reconcile_on_the_one_finalise_worker(rig, monkeypatch):
    save_restore(restore_path(), ObsRestore(profile="Untitled", collection="Untitled"))
    rig.obs.profile, rig.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME  # the crash left OBS on ours
    leave_orphan(rig.output_root / "_incoming", "2026-10-01 20-00-00")
    threads: list[str] = []
    real_finalise = session_mod.finalise

    def finalise(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real_finalise(*args, **kwargs)

    monkeypatch.setattr(session_mod, "finalise", finalise)
    rig.start()
    placed = rig.output_root / TITLE / f"{TITLE} - 01.session.json"
    rig.wait(lambda: ("session_finished", (placed,)) in rig.events)
    rig.wait(lambda: not restore_path().exists())
    assert (rig.obs.profile, rig.obs.collection) == ("Untitled", "Untitled")
    assert load_manifest(placed).state is ManifestState.READY
    assert len(threads) == 1 and threads[0].startswith("finalise")  # FinaliseWorker, not a shared pool


# The text feed (spec 15, 17 "Feed port in use") ---------------------------------------------------


def test_a_feed_port_in_use_turns_the_feed_off_with_a_banner_naming_it(rig):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        rig.cfg = AppConfig(output_root=rig.cfg.output_root, feed=FeedSettings(ws_port=port, http_port=0))
        app = rig.start()
        rig.wait(lambda: "feed" in rig.banners())
    assert str(port) in rig.banners()["feed"]
    assert app.feed is None


def test_a_feed_that_cannot_start_otherwise_is_off_with_its_error(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(http_server, "PAGE_PATH", tmp_path / "missing" / "page.html")
    app = rig.start()
    rig.wait(lambda: "feed" in rig.banners())
    assert "page.html" in rig.banners()["feed"]
    assert app.feed is None


def test_accepted_lines_are_broadcast_on_the_feed(rig):
    app = rig.start()
    assert app.feed is not None
    rig.arm()
    with connect(f"ws://127.0.0.1:{app.feed.ws_port}", proxy=None) as client:
        rig.wait(lambda: app.feed is not None and app.feed.client_count == 1)
        rig.sources[0].line("こんにちは")
        assert client.recv(timeout=5) == "こんにちは"


def test_a_line_held_for_an_auto_start_reaches_the_feed_once_and_the_cue_count(rig):
    """The actor publishes held lines again with their offsets at STARTED; the feed has sent them already."""
    app = rig.start()
    assert app.feed is not None
    rig.arm()
    with connect(f"ws://127.0.0.1:{app.feed.ws_port}", proxy=None) as client:
        rig.wait(lambda: app.feed is not None and app.feed.client_count == 1)
        rig.sources[0].line("はじまり")
        assert client.recv(timeout=5) == "はじまり"
        app.post(UserCommand(CommandKind.START, line=GameLine("はじまり", "はじまり", 1.0, "textractor")))
        video = rig.output_root / "_incoming" / "2026-10-02 18-04-11.mkv"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
        rig.obs.record_active, rig.obs.output_path = True, str(video)
        rig.obs.stops_on_request = True  # the quit's StopRecord ends the recording
        rig.gateway.record_event(OutputState.STARTED, str(video))
        rig.wait(lambda: rig.state() is AppState.RECORDING)
        rig.sources[0].line("つぎ")
        assert client.recv(timeout=5) == "つぎ"
    rig.wait(lambda: app.window.cues_label.text() == "2 cues")  # both journalled (the window's count)
    assert [text for text, _offset in app.window.live_list.entries()] == ["はじまり", "つぎ"]


# CLI verbs and quitting (spec 16) -----------------------------------------------------------------


def send_from_a_worker(rig: Rig, name: str, command: UserCommand | None) -> bool | None:
    answers: list[bool | None] = []
    worker = threading.Thread(target=lambda: answers.append(send(name, command)))
    worker.start()
    rig.wait(lambda: not worker.is_alive())
    return answers[0]


def test_verbs_reach_the_session_and_a_bare_launch_shows_the_window(rig):
    name = server_name(paths.home())
    app = rig.start(name=name)
    app.window.hide()
    assert send_from_a_worker(rig, name, UserCommand(CommandKind.ARM, slug=SLUG)) is True
    rig.wait(lambda: rig.state() is AppState.ARMED)
    assert send_from_a_worker(rig, name, None) is True
    rig.wait(app.window.isVisible)


def test_quit_disarms_and_waits_for_every_source_to_close_before_the_loop_stops(rig):
    rig.start()
    rig.arm()
    assert rig.obs.profile == OBS_PROFILE_NAME
    rig.close()
    assert rig.sources[0].stopped and rig.sources[0].closed_while_the_loop_ran
    assert rig.obs.profile == "Untitled"  # disarm restored the user's OBS
    assert not restore_path().exists()


def test_closing_the_window_stops_the_app_in_the_background(rig):
    app = rig.start()
    rig.arm()
    with rig.qtbot.waitSignal(app.stopped, timeout=WAIT_MS):
        app.window.close()
        assert not app.window.isVisible()  # hidden at once; the disarm runs on the I/O loop
    assert rig.sources[0].closed_while_the_loop_ran
    assert rig.obs.profile == "Untitled"


# The OBS password (spec 11.1 item 4, global constraints) ------------------------------------------

PASSWORD = "never-in-the-log-7f3a"


class ServerThread:
    """``FakeObsServer`` on its own loop and thread, as a separate OBS process would be."""

    def __init__(self, server: FakeObsServer) -> None:
        self.server = server
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="fake-obs", daemon=True)

    def __enter__(self) -> FakeObsServer:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(5)
        return self.server

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def test_the_obs_password_never_reaches_the_log(qtbot, tmp_path, monkeypatch, caplog):
    for var in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    caplog.set_level(logging.DEBUG)
    proc = tmp_path / "proc" / "4242"
    proc.mkdir(parents=True)
    (proc / "comm").write_text("obs\n", encoding="utf-8")  # an OBS of this user runs
    cfg = AppConfig(
        output_root=str(tmp_path / "out"),
        feed=FeedSettings(ws_port=0, http_port=0),
    )
    store.save_config(cfg)
    with ServerThread(FakeObsServer(password=PASSWORD)) as obs:
        ws_config = tmp_path / "home-config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(ws_config))
        path = ws_config / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {"server_enabled": True, "server_port": obs.port, "auth_required": True, "server_password": PASSWORD}
            ),
            encoding="utf-8",
        )

        def local(config: Callable[[], AppConfig]) -> ObsServices:
            discovery = LocalObsDiscovery(
                config,
                platform="linux",
                which=lambda name: "/usr/bin/obs" if name == "obs" else None,
                proc_root=proc.parent,
            )
            gateway = ObsClient(lambda: discovery.credentials(config()))
            return ObsServices(discovery, gateway, ObsProvisioner(gateway))

        app = App(obs=local)
        lights: list[SourceStatus] = []
        app.presenter.signals.source_status.connect(
            lambda source_id, status: lights.append(status) if source_id == OBS_SOURCE_ID else None
        )
        try:
            app.start()
            qtbot.addWidget(app.window)
            qtbot.waitUntil(lambda: SourceStatus.CONNECTED in lights, timeout=WAIT_MS)
            assert obs.identifies  # it logged in with the password
        finally:
            app.close()
    logged = caplog.text + "".join(repr(record.args) for record in caplog.records)
    assert caplog.records
    assert PASSWORD not in logged


# Pieces -------------------------------------------------------------------------------------------


def test_hook_mode_listens_to_the_enabled_sources_the_game_uses():
    cfg = AppConfig(
        text_sources=(
            TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677"),
            TextSourceConfig(id="agent", name="Agent", uri="localhost:9001", enabled=False),
            TextSourceConfig(id="luna", name="LunaTranslator", uri="localhost:2333"),
        )
    )
    every = hook_sources(cfg, GameProfile(slug=SLUG, title=TITLE))
    assert [(type(s), s.id) for s in every] == [(WebsocketSource, "textractor"), (WebsocketSource, "luna")]
    chosen = hook_sources(cfg, GameProfile(slug=SLUG, title=TITLE, source_ids=("luna", "agent")))
    assert [s.id for s in chosen] == ["luna"]
    assert hook_sources(cfg, GameProfile(slug=SLUG, title=TITLE, text_mode=TextMode.OCR)) == []


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *args: self.calls.append((name, args))


LINE = GameLine(text="はい", raw="はい", t_mono=1.0, source_id="textractor")
BANNER = Banner("obs", BannerLevel.ERROR, "OBS is not running")
MANIFEST = Path("/out/Game/Game - 01.session.json")


@pytest.mark.parametrize(
    ("event", "call"),
    [
        (StateChanged(AppState.ARMED, SLUG), ("state_changed", (AppState.ARMED, SLUG))),
        (SourceStatusChanged("luna", SourceStatus.RECEIVING), ("source_status", ("luna", SourceStatus.RECEIVING))),
        (LineAccepted(LINE, 1200, True), ("line_accepted", (LINE, 1200, True))),
        (BannerRaised(BANNER), ("banner", (BANNER,))),
        (BannerCleared("obs"), ("banner_cleared", ("obs",))),
        (SessionFinalised(MANIFEST), ("session_finished", (MANIFEST,))),
    ],
)
def test_each_session_event_is_one_presenter_call(event, call):
    presenter = Recorder()
    forward(presenter, event)  # type: ignore[arg-type]
    assert presenter.calls == [call]


@pytest.mark.parametrize("event", [RecordingStarted("stem"), RecordingStopped("stem")])
def test_recording_edges_are_for_session_subscribers_only(event):
    presenter = Recorder()
    forward(presenter, event)  # type: ignore[arg-type]
    assert presenter.calls == []
