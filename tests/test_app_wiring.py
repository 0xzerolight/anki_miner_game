"""The GUI wiring (T26; spec 8.1, 12, 13, 14, 16): the window, the tray, the hotkey, the text sources of
an armed game and the VAD jobs, through the real composition with OBS faked (the actor's fakes,
``tests/app_rig.py``). The settings and the wizard are in ``test_app_wiring_setup.py``, game profiles in
``test_app_wiring_profile.py`` (audit 2026-10-01 plan, section 4.10)."""

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QSystemTrayIcon, QWidget

from anki_miner_game import app as app_mod
from anki_miner_game import paths, store
from anki_miner_game.addons.ocr_addon import NOT_INSTALLED
from anki_miner_game.app import App, game_sources, open_url
from anki_miner_game.gui.hotkey_win import ERROR_HOTKEY_ALREADY_REGISTERED, MOD_NOREPEAT, GlobalHotkey, parse_hotkey
from anki_miner_game.gui.settings_dialog import SettingsDialog
from anki_miner_game.models.config import AppConfig, TextSourceConfig
from anki_miner_game.models.manifest import ManifestState, VadState
from anki_miner_game.models.messages import AppState, CommandKind, SourceStatus, UserCommand
from anki_miner_game.models.obs import OutputState
from anki_miner_game.models.profile import GameProfile, OcrSettings, TextMode
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.text.sources.clipboard_source import CLIPBOARD_SOURCE_ID, ClipboardSource
from anki_miner_game.text.sources.ocr_source import OCR_SOURCE_ID
from anki_miner_game.text.sources.websocket_source import WebsocketSource
from tests.app_rig import PROFILE, SLUG, TITLE, WAIT_MS, Rig, Source
from tests.gui.session_fakes import manifest, place
from tests.session.actor_harness import T0


@pytest.fixture
def rig(qtbot, tmp_path) -> Iterator[Rig]:
    made = Rig(qtbot, tmp_path)
    yield made
    made.close()


def shown[W: QWidget](app: App, kind: type[W]) -> W | None:
    """The last visible ``kind`` window the app opened (dialogs are children of the main window)."""
    found = [widget for widget in app.window.findChildren(kind) if widget.isVisible()]
    return found[-1] if found else None


def last_slug(rig: Rig) -> str | None:
    slugs = [args[1] for name, args in rig.events if name == "state_changed"]
    return slugs[-1] if slugs else None


def record(rig: Rig) -> None:
    """OBS starts recording into ``_incoming/`` and stops when asked (the quit's or a Stop's ``StopRecord``)."""
    video = rig.output_root / "_incoming" / "2026-10-02 18-04-11.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
    rig.obs.record_active, rig.obs.output_path = True, str(video)
    rig.obs.stops_on_request = True
    rig.gateway.record_event(OutputState.STARTED, str(video))
    rig.wait(lambda: rig.state() is AppState.RECORDING)


# The text sources of an armed game (spec 8.1, 14) -------------------------------------------------

CFG = AppConfig(text_sources=(TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677"),))


def no_ocr(_settings: OcrSettings) -> Any:
    raise AssertionError("hook mode runs no owocr")


def test_hook_mode_adds_the_clipboard_when_the_game_takes_it(qtbot):
    with_clipboard = game_sources(CFG, GameProfile(slug=SLUG, title=TITLE, clipboard=True), ocr=no_ocr)
    assert [(type(s), s.id) for s in with_clipboard] == [
        (WebsocketSource, "textractor"),
        (ClipboardSource, CLIPBOARD_SOURCE_ID),
    ]
    assert [type(s) for s in game_sources(CFG, GameProfile(slug=SLUG, title=TITLE), ocr=no_ocr)] == [WebsocketSource]


def test_ocr_mode_runs_only_owocr_with_the_games_ocr_settings():
    settings = OcrSettings(language="ja", rects="0,0,10,10")
    asked: list[OcrSettings] = []

    def ocr(given: OcrSettings) -> Any:
        asked.append(given)
        return "owocr"

    profile = GameProfile(slug=SLUG, title=TITLE, text_mode=TextMode.OCR, ocr=settings)
    assert game_sources(CFG, profile, ocr=ocr) == ["owocr"]
    assert asked == [settings]


def test_an_armed_ocr_game_without_the_add_on_says_so_in_a_banner(rig):
    rig.source_factory = None  # the app's own sources: owocr through the OCR add-on, not installed here
    rig.profile = replace(PROFILE, text_mode=TextMode.OCR)
    rig.start()
    rig.arm()
    rig.wait(lambda: rig.banners().get("ocr") == NOT_INSTALLED)
    assert ("source_status", (OCR_SOURCE_ID, SourceStatus.CONNECTED)) not in rig.events


def test_an_armed_game_that_takes_the_clipboard_listens_to_it(rig):
    rig.source_factory = None
    rig.cfg = replace(rig.cfg, text_sources=())  # no hooker: nothing connects to a default port
    rig.profile = replace(PROFILE, clipboard=True)
    app = rig.start()
    rig.arm()
    rig.wait(lambda: ("source_status", (CLIPBOARD_SOURCE_ID, SourceStatus.CONNECTED)) in rig.events)
    rig.wait(lambda: "Game text: Clipboard" in app.window.status_row.names())


# VAD jobs (spec 13; VadJobs docstring) -------------------------------------------------------------


def vad_finished(rig: Rig, path: Path) -> VadState | None:
    states = [args[1] for name, args in rig.events if name == "vad_finished" and args[0] == path]
    return states[-1] if states else None


def test_a_vad_pass_a_quit_interrupted_is_run_again_at_launch(rig):
    interrupted = place(rig.output_root, manifest(3, state=ManifestState.VAD_RUNNING))
    rig.start()
    rig.wait(lambda: vad_finished(rig, interrupted) is VadState.UNAVAILABLE)  # the add-on is not installed here
    assert load_manifest(interrupted).state is ManifestState.READY


def test_a_finalised_session_is_queued_for_its_vad_pass(rig):
    app = rig.start()
    rig.arm()
    record(rig)  # STARTED at the fake OBS clock's T0
    assert rig.sources[0].sink is not None
    rig.sources[0].sink("こんにちは", T0 + 1.0, "textractor")
    rig.gateway.clock.t = T0 + 5.0  # STOPPING and STOPPED come at this reading
    app.post(UserCommand(CommandKind.STOP))
    placed = rig.output_root / TITLE / f"{TITLE} - 01.session.json"
    rig.wait(lambda: vad_finished(rig, placed) is VadState.UNAVAILABLE)
    assert load_manifest(placed).vad is not None


# The tray (spec 16) --------------------------------------------------------------------------------


def test_the_tray_records_the_selected_game_opens_the_feed_shows_the_window_and_quits(rig, monkeypatch, qtbot):
    opened: list[QUrl] = []
    monkeypatch.setattr(app_mod, "open_url", lambda url: opened.append(url) or True)
    app = rig.start()
    tray = app.tray
    assert app.window.minimise_to_tray is QSystemTrayIcon.isSystemTrayAvailable()
    tray.menu.aboutToShow.emit()
    assert tray.record_action.text() == f"Start recording: {TITLE}"
    tray.record_action.trigger()
    rig.wait(lambda: last_slug(rig) == SLUG and "StartRecord" in rig.gateway.names())
    record(rig)
    tray.feed_action.trigger()
    assert app.feed is not None and opened == [QUrl(app.feed.page_url)]
    app.window.hide()
    tray.show_action.trigger()
    assert app.window.isVisible()
    with qtbot.waitSignal(app.stopped, timeout=WAIT_MS):
        tray.quit_action.trigger()
    assert rig.obs.profile == "Untitled"  # the quit stopped, saved and gave OBS back


def test_closing_the_window_while_ready_says_once_that_the_app_is_still_running(rig, monkeypatch):
    app = rig.start()
    told: list[tuple[str, str]] = []
    monkeypatch.setattr(app.tray, "_supports_messages", lambda: True)
    monkeypatch.setattr(app.tray.icon, "showMessage", lambda title, text, *rest: told.append((title, text)))
    app.window.minimise_to_tray = True
    rig.arm()
    rig.wait(lambda: app.window.controls.state is AppState.ARMED)
    app.window.close()
    app.window.show()
    app.window.close()
    assert told == [
        (
            "Anki Miner Game is still running",
            f"{TITLE} is ready. Click this icon to open the window; right-click it to quit.",
        )
    ]


# The hotkey (spec 16, Windows) ---------------------------------------------------------------------


class HotkeyApi:
    def __init__(self, answer: int = 0) -> None:
        self.answer = answer
        self.registered: list[tuple[int, int]] = []
        self.unregistered = 0

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> int:
        self.registered.append((modifiers, vk))
        return self.answer

    def unregister(self, hotkey_id: int) -> None:
        self.unregistered += 1


def with_hotkey(rig: Rig, api: HotkeyApi) -> list[GlobalHotkey]:
    made: list[GlobalHotkey] = []

    def factory(parent: Any) -> GlobalHotkey:
        made.append(GlobalHotkey(parent, api=api))
        return made[-1]

    rig.hotkey = factory
    return made


def registered(text: str) -> tuple[int, int]:
    hotkey = parse_hotkey(text)
    return hotkey.modifiers | MOD_NOREPEAT, hotkey.vk


def test_the_hotkey_is_registered_and_toggles_start_and_stop(rig):
    api = HotkeyApi()
    made = with_hotkey(rig, api)
    rig.start()
    assert api.registered == [registered(rig.cfg.hotkey)]
    rig.arm()
    made[0].activated.emit()
    rig.wait(lambda: "StartRecord" in rig.gateway.names())


def test_closing_the_app_releases_the_hotkey(rig):
    # The QApplication outlives the App here, so its quit never comes: a native filter left
    # installed would outlive its hotkey (an access violation on Windows once freed off-thread).
    api = HotkeyApi()
    with_hotkey(rig, api)
    rig.start()
    rig.close()
    assert api.unregistered == 1


def test_a_hotkey_another_program_holds_is_a_banner_and_new_settings_register_again(rig):
    api = HotkeyApi(ERROR_HOTKEY_ALREADY_REGISTERED)
    with_hotkey(rig, api)
    app = rig.start()
    rig.wait(lambda: "already in use" in rig.banners().get("hotkey", ""))
    api.answer = 0
    app.window.settings_requested.emit()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.config_saved.emit(replace(app.config, hotkey="Ctrl+Alt+F10"))
    assert api.registered[-1] == registered("Ctrl+Alt+F10")
    rig.wait(lambda: "hotkey" not in rig.banners())


# Settings reach the window (spec 5, 16) ------------------------------------------------------------


def test_saved_settings_reach_the_window(rig, tmp_path):
    """A new text source names the Game text light; a new output folder lists its sessions."""
    place(tmp_path / "elsewhere", manifest(7))
    rig.sources = [Source("mine")]
    app = rig.start()
    app.window.settings_requested.emit()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    mine = TextSourceConfig(id="mine", name="My hooker", uri="localhost:7777")
    dialog.config_saved.emit(
        replace(app.config, output_root=str(tmp_path / "elsewhere"), text_sources=(*app.config.text_sources, mine))
    )
    assert [cells[0] for cells in app.window.recent.cells()] == ["Steins;Gate - 07"]
    rig.arm()
    rig.wait(lambda: app.window.status_row.names() == ["OBS", "Game text: My hooker"])


# The last armed game (AppConfig.last_game) ---------------------------------------------------------


def test_the_armed_game_is_selected_at_the_next_launch(rig):
    rig.start()
    rig.arm()
    rig.wait(lambda: store.load_config().last_game == SLUG)


def test_an_unusable_settings_file_is_left_alone_when_a_game_is_armed(rig):
    paths.config_path().write_text("{ not json", encoding="utf-8")
    rig.start(write_config=False)
    rig.arm()
    assert paths.config_path().read_text(encoding="utf-8") == "{ not json"


# Opening folders and the feed page -----------------------------------------------------------------


def test_a_frozen_linux_build_opens_urls_with_the_users_library_path(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib/mine")
    spawned: list[tuple[list[str], dict[str, str]]] = []

    def spawn(argv: list[str], **kwargs: Any) -> None:
        spawned.append((argv, kwargs["env"]))

    assert open_url(QUrl("http://127.0.0.1:6679/"), frozen=True, platform="linux", spawn=spawn)
    assert [argv for argv, _env in spawned] == [["xdg-open", "http://127.0.0.1:6679/"]]
    assert spawned[0][1]["LD_LIBRARY_PATH"] == "/usr/lib/mine"
    assert os.environ["LD_LIBRARY_PATH"] == "/bundle/_internal"  # the app's own path is unchanged


def test_a_frozen_linux_build_that_cannot_start_xdg_open_says_so(monkeypatch):
    def spawn(argv: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError("xdg-open")

    assert open_url(QUrl("http://127.0.0.1:6679/"), frozen=True, platform="linux", spawn=spawn) is False


@pytest.mark.parametrize(("frozen", "platform"), [(False, "linux"), (True, "win32")])
def test_elsewhere_urls_open_through_qt(monkeypatch, frozen, platform):
    opened: list[QUrl] = []

    class Desktop:
        @staticmethod
        def openUrl(url: QUrl) -> bool:  # noqa: N802 - Qt's name
            opened.append(url)
            return True

    monkeypatch.setattr(app_mod, "QDesktopServices", Desktop)

    def spawn(argv: list[str], **kwargs: Any) -> None:
        raise AssertionError("no xdg-open here")

    assert open_url(QUrl("file:///tmp"), frozen=frozen, platform=platform, spawn=spawn)
    assert opened == [QUrl("file:///tmp")]
