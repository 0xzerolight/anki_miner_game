"""Game profiles and the window picker through the real composition (T26; spec 5, 16), OBS faked
(``tests/app_rig.py``). Split from ``test_app_wiring.py`` by owner (audit 2026-10-01 plan, section 4.10):
the profile dialog opens through the window's requests, never its buttons."""

import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QWidget

from anki_miner_game import app as app_mod
from anki_miner_game import paths, store
from anki_miner_game.app import App
from anki_miner_game.gui import game_profile_dialog
from anki_miner_game.gui.game_profile_dialog import GameProfileDialog, ProfileDialogServices
from anki_miner_game.gui.wizard import SetupWizard, WizardStep
from anki_miner_game.models.messages import AppState, CommandKind, UserCommand
from anki_miner_game.models.obs import WindowItem
from anki_miner_game.models.profile import AudioMode, AudioSettings, AutoSettings, GameProfile
from anki_miner_game.obs.startup import LocalObsStarter
from anki_miner_game.session.naming import slugify
from anki_miner_game.store import StoreWriteError
from tests.app_rig import SLUG, Rig


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


# Game profiles (spec 5, 16) ------------------------------------------------------------------------


def test_a_new_game_is_saved_listed_selected_and_can_be_armed(rig):
    app = rig.start()
    app.window.new_game_requested.emit()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None
    slug = slugify("Chaos;Head")
    dialog.profile_saved.emit(GameProfile(slug=slug, title="Chaos;Head", audio=AudioSettings(mode=AudioMode.DESKTOP)))
    assert store.load_profiles().profiles[slug].title == "Chaos;Head"
    assert app.window.game.currentData() == slug
    app.post(UserCommand(CommandKind.ARM, slug=slug))
    rig.wait(lambda: rig.state() is AppState.ARMED and last_slug(rig) == slug)


def test_an_edit_applies_to_the_next_arm_through_auto_mode(rig):
    """The profile the actor and auto mode look up is the one just saved: auto-start on the first line."""
    app = rig.start()
    app.window.edit_game_requested.emit(SLUG)
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
    app.window.new_game_requested.emit()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None
    dialog.profile_saved.emit(GameProfile(slug="new", title="New"))
    assert "disk full" in rig.banners()["profile_save"]
    assert app.window.game.findData("new") < 0


# OBS shared by the picker, the wizard and the actor --------------------------------------------------


def test_the_obs_step_the_picker_and_the_actor_share_one_obs_lock(rig):
    app = rig.start()
    running = app._running
    assert running is not None
    lock = running.actor._obs_lock
    assert running.picker._obs_lock is lock and running.obs_setup._obs_lock is lock


def test_the_obs_step_and_the_picker_start_obs_through_one_starter_over_the_apps_obs(rig):
    """D-03: one start-OBS sequence, over the app's own discovery and gateway."""
    app = rig.start()
    running = app._running
    assert running is not None
    starter = running.picker._starter
    assert isinstance(starter, LocalObsStarter)
    assert running.obs_setup._starter is starter
    assert starter._discovery is rig.discovery and starter._gateway is rig.gateway


def test_a_new_game_lists_its_window_with_obs_closed_at_launch_and_saves_it(rig, monkeypatch):
    """B4-01, D-03: OBS was not running when the app started (the usual case); opening the Game window
    list starts it, connects, lists, and the game saves with its window."""
    monkeypatch.setattr(game_profile_dialog, "is_wayland_session", lambda: False)
    rig.discovery.running = False
    game = WindowItem("[chaoshead.exe]: Chaos;Head", "Chaos;Head:UnityWndClass:chaoshead.exe", True)

    async def windows() -> list[WindowItem]:
        return [game]

    monkeypatch.setattr(rig.provisioner, "list_windows", windows)
    app = rig.start()
    assert not rig.gateway.connected
    app.window.new_game_requested.emit()
    dialog = shown(app, game_profile_dialog.GameProfileDialog)
    assert dialog is not None
    dialog.title_edit.setText("Chaos;Head")

    dialog.window_combo.showPopup()
    rig.wait(lambda: not dialog.listing_windows)
    dialog.window_combo.hidePopup()

    assert (rig.discovery.launches, rig.gateway.connected) == (1, True)
    index = dialog.window_combo.findData(game.value)
    assert index > 0
    dialog.window_combo.setCurrentIndex(index)
    dialog.window_combo.activated.emit(index)
    dialog.accept()
    saved = store.load_profiles().profiles[slugify("Chaos;Head")]
    assert saved.capture.window == game.value
    assert saved.audio.mode is (AudioMode.APP if sys.platform == "win32" else AudioMode.DESKTOP)


# Install… in a game profile (UJ-22b, master 4.8 G6) ------------------------------------------------


def test_install_in_a_game_profile_opens_the_add_ons_page_alone_over_it(rig, monkeypatch):
    """UJ-22b: the OCR add-on is installed from where it is needed, then the profile reads it again."""
    made: list[ProfileDialogServices] = []
    real = app_mod.ProfileDialogServices

    def capture(**fields: Any) -> ProfileDialogServices:
        made.append(real(**fields))
        return made[-1]

    monkeypatch.setattr(app_mod, "ProfileDialogServices", capture)
    app = rig.start()
    app.window.new_game_requested.emit()
    dialog = shown(app, GameProfileDialog)
    assert dialog is not None and made
    install = made[-1].install_addons
    assert install is not None
    refreshed: list[int] = []
    monkeypatch.setattr(dialog, "refresh_addons", lambda: refreshed.append(1))
    install()
    wizard = shown(app, SetupWizard)
    assert wizard is not None and wizard.parent() is dialog
    assert wizard.single_step and wizard.currentId() == WizardStep.ADDONS
    assert wizard.currentPage().nextId() == -1
    wizard.reject()
    assert refreshed == [1]
