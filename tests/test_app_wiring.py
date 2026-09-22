"""The GUI wiring (T26; spec 8.1, 12, 13, 14, 16): the dialogs, the wizard, the tray, the hotkey, the
text sources of an armed game and the VAD jobs, through the real composition with OBS faked (the
actor's fakes, ``tests/app_rig.py``)."""

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QDialog, QSystemTrayIcon, QWidget

from anki_miner_game import app as app_mod
from anki_miner_game import paths, store
from anki_miner_game.addons.ocr_addon import NOT_INSTALLED
from anki_miner_game.app import App, game_sources, open_url
from anki_miner_game.gui.game_profile_dialog import GameProfileDialog
from anki_miner_game.gui.hotkey_win import ERROR_HOTKEY_ALREADY_REGISTERED, MOD_NOREPEAT, GlobalHotkey, parse_hotkey
from anki_miner_game.gui.settings_dialog import SettingsDialog
from anki_miner_game.gui.wizard import SetupWizard, WizardStep
from anki_miner_game.models.config import AppConfig, FeedSettings, TextSourceConfig
from anki_miner_game.models.manifest import ManifestState, VadState
from anki_miner_game.models.messages import AppState, CommandKind, SourceStatus, UserCommand
from anki_miner_game.models.obs import OutputState
from anki_miner_game.models.profile import AudioMode, AutoSettings, GameProfile, OcrSettings, TextMode
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.naming import slugify
from anki_miner_game.store import StoreWriteError
from anki_miner_game.text.sources.clipboard_source import CLIPBOARD_SOURCE_ID, ClipboardSource
from anki_miner_game.text.sources.ocr_source import OCR_SOURCE_ID
from anki_miner_game.text.sources.websocket_source import WebsocketSource
from tests.app_rig import PROFILE, SLUG, TITLE, WAIT_MS, Rig
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
    rig.wait(lambda: "Clipboard" in app.window.status_row.names())


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


def test_the_tray_arms_the_selected_game_opens_the_feed_shows_the_window_and_quits(rig, monkeypatch, qtbot):
    opened: list[QUrl] = []
    monkeypatch.setattr(app_mod, "open_url", lambda url: opened.append(url) or True)
    app = rig.start()
    tray = app.tray
    assert app.window.minimise_to_tray is QSystemTrayIcon.isSystemTrayAvailable()
    tray.menu.aboutToShow.emit()
    tray.arm_action.trigger()
    rig.wait(lambda: rig.state() is AppState.ARMED and last_slug(rig) == SLUG)
    tray.feed_action.trigger()
    assert app.feed is not None and opened == [QUrl(app.feed.page_url)]
    app.window.hide()
    tray.show_action.trigger()
    assert app.window.isVisible()
    with qtbot.waitSignal(app.stopped, timeout=WAIT_MS):
        tray.quit_action.trigger()
    assert rig.obs.profile == "Untitled"  # the quit disarmed


# The hotkey (spec 16, Windows) ---------------------------------------------------------------------


class HotkeyApi:
    def __init__(self, answer: int = 0) -> None:
        self.answer = answer
        self.registered: list[tuple[int, int]] = []

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> int:
        self.registered.append((modifiers, vk))
        return self.answer

    def unregister(self, hotkey_id: int) -> None:
        pass


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


def test_a_hotkey_another_program_holds_is_a_banner_and_new_settings_register_again(rig):
    api = HotkeyApi(ERROR_HOTKEY_ALREADY_REGISTERED)
    with_hotkey(rig, api)
    app = rig.start()
    rig.wait(lambda: "already in use" in rig.banners().get("hotkey", ""))
    api.answer = 0
    app.window.settings_action.trigger()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.config_saved.emit(replace(app.config, hotkey="Ctrl+Alt+F10"))
    assert api.registered[-1] == registered("Ctrl+Alt+F10")
    rig.wait(lambda: "hotkey" not in rig.banners())


# Game profiles (spec 5, 16) ------------------------------------------------------------------------


def test_a_new_game_is_saved_listed_selected_and_can_be_armed(rig):
    app = rig.start()
    app.window.new_game_button.click()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None
    dialog.title_edit.setText("Chaos;Head")
    dialog.audio_combo.setCurrentIndex(dialog.audio_combo.findData(AudioMode.DESKTOP))  # Windows: no window pinned
    dialog.accept()
    slug = slugify("Chaos;Head")
    assert store.load_profiles().profiles[slug].title == "Chaos;Head"
    assert app.window.game.currentData() == slug
    app.post(UserCommand(CommandKind.ARM, slug=slug))
    rig.wait(lambda: rig.state() is AppState.ARMED and last_slug(rig) == slug)


def test_an_edit_applies_to_the_next_arm_through_auto_mode(rig):
    """The profile the actor and auto mode look up is the one just saved: auto-start on the first line."""
    app = rig.start()
    app.window.edit_game_button.click()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None
    saved = replace(rig.profile, auto=AutoSettings(enabled=True, start_on_first_line=True))
    dialog.profile_saved.emit(saved)
    assert store.load_profiles().profiles[SLUG] == saved
    rig.arm()
    rig.sources[0].line("はじまり")
    rig.wait(lambda: "StartRecord" in rig.gateway.names())


def test_a_profile_that_cannot_be_saved_is_a_banner(rig, monkeypatch):
    def refuse(profile: GameProfile) -> Path:
        raise StoreWriteError(paths.profile_path(profile.slug), "disk full")

    app = rig.start()
    monkeypatch.setattr(store, "save_profile", refuse)
    app.window.new_game_button.click()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None
    dialog.profile_saved.emit(GameProfile(slug="new", title="New"))
    assert "disk full" in rig.banners()["profile_save"]
    assert app.window.game.findData("new") < 0


# Settings (spec 5, 16) -----------------------------------------------------------------------------


def test_saved_settings_are_stored_and_used_at_once(rig, tmp_path):
    place(tmp_path / "elsewhere", manifest(7))
    app = rig.start()
    app.window.settings_action.trigger()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.output_edit.setText(str(tmp_path / "elsewhere"))
    dialog.add_source_button.click()
    dialog.feed_check.setChecked(False)  # the dialog's ports start at 1: the rig's feed binds port 0
    dialog.ws_port_spin.setValue(2)
    dialog.accept()
    assert store.load_config() == app.config
    assert app.config.output_root == str(tmp_path / "elsewhere")
    assert app.window.status_row.names()[-1] == "New source"
    assert [cells[0] for cells in app.window.recent.cells()] == ["Steins;Gate - 07"]


def test_the_feed_follows_its_settings(rig):
    app = rig.start()
    assert app.feed is not None
    app.window.settings_action.trigger()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.config_saved.emit(replace(app.config, feed=FeedSettings(enabled=False, ws_port=0, http_port=0)))
    rig.wait(lambda: app.feed is None)
    dialog.config_saved.emit(replace(app.config, feed=FeedSettings(enabled=True, ws_port=0, http_port=0)))
    rig.wait(lambda: app.feed is not None)


def test_settings_that_cannot_be_saved_are_a_banner(rig, monkeypatch):
    def refuse(cfg: AppConfig) -> Path:
        raise StoreWriteError(paths.config_path(), "read-only")

    app = rig.start()
    monkeypatch.setattr(store, "save_config", refuse)
    before = app.config
    app.window.settings_action.trigger()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.config_saved.emit(replace(app.config, output_root="/elsewhere"))
    assert "read-only" in rig.banners()["config"]
    assert app.config == before


# The setup wizard (spec 16) ------------------------------------------------------------------------


def test_the_first_launch_opens_the_setup_wizard_at_obs(rig):
    app = rig.start(write_config=False)
    wizard = shown(app, SetupWizard)
    assert wizard is not None and wizard.currentId() == WizardStep.OBS


def test_a_launch_with_settings_opens_no_wizard(rig):
    app = rig.start()
    assert shown(app, SetupWizard) is None


def test_the_menu_opens_the_wizard_and_every_wizard_shares_the_apps_obs_step(rig):
    app = rig.start()
    app.window.setup_action.trigger()
    first = shown(app, SetupWizard)
    assert first is not None and first.currentId() == WizardStep.OBS
    first.close()
    app.window.setup_action.trigger()
    second = shown(app, SetupWizard)
    assert second is not None and second is not first
    assert second.obs_page._obs is first.obs_page._obs  # one ObsSetup per app: one gateway subscription


def test_a_password_the_wizard_saves_is_the_config_at_once(rig):
    """The gateway reads a typed password override through the app's config at its next connect."""
    app = rig.start()
    app.window.setup_action.trigger()
    wizard = shown(app, SetupWizard)
    assert wizard is not None
    assert wizard.store(replace(app.config, obs=replace(app.config.obs, password_override="typed"))) is None
    assert app.config.obs.password_override == "typed"
    assert store.load_config().obs.password_override == "typed"


def test_a_step_run_again_from_settings_opens_over_them_and_shows_what_it_saved(rig, tmp_path):
    app = rig.start()
    app.window.settings_action.trigger()
    settings = shown(app, SettingsDialog)
    assert settings is not None
    settings.setup_buttons[WizardStep.FOLDER].click()
    wizard = shown(app, SetupWizard)
    assert wizard is not None and wizard.currentId() == WizardStep.FOLDER
    assert wizard.parent() is settings
    folder = tmp_path / "chosen"
    wizard.folder_page.path.setText(str(folder))
    assert wizard.folder_page.validatePage()
    assert app.config.output_root == str(folder)
    assert settings.output_edit.text() == str(folder)


def test_the_obs_step_the_picker_and_the_actor_share_one_obs_lock(rig):
    app = rig.start()
    running = app._running
    assert running is not None
    lock = running.actor._obs_lock
    assert running.picker._obs_lock is lock and running.obs_setup._obs_lock is lock


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


# Quitting ------------------------------------------------------------------------------------------


def test_a_quit_closes_the_open_dialogs(rig):
    app = rig.start()
    app.window.settings_action.trigger()
    settings = shown(app, SettingsDialog)
    assert settings is not None
    settings.setup_buttons[WizardStep.SOURCES].click()
    assert shown(app, SetupWizard) is not None
    rig.close()
    assert [dialog for dialog in app.window.findChildren(QDialog) if dialog.isVisible()] == []


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
