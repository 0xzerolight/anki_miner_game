"""The settings and the setup wizard through the real composition (T26; spec 5, 16), OBS faked
(``tests/app_rig.py``). Split from ``test_app_wiring.py`` by owner (audit 2026-10-01 plan, section 4.10):
dialogs open through the window's requests, never its buttons or menu."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog, QWidget

from anki_miner_game import paths, store
from anki_miner_game.app import App
from anki_miner_game.gui.settings_dialog import SettingsDialog
from anki_miner_game.gui.wizard import SetupWizard, WizardStep
from anki_miner_game.models.config import AppConfig, FeedSettings
from anki_miner_game.store import StoreWriteError
from tests.app_rig import Rig


@pytest.fixture
def rig(qtbot, tmp_path) -> Iterator[Rig]:
    made = Rig(qtbot, tmp_path)
    yield made
    made.close()


def shown[W: QWidget](app: App, kind: type[W]) -> W | None:
    """The last visible ``kind`` window the app opened (dialogs are children of the main window)."""
    found = [widget for widget in app.window.findChildren(kind) if widget.isVisible()]
    return found[-1] if found else None


# Settings (spec 5, 16) -----------------------------------------------------------------------------


def test_saved_settings_are_stored_and_used_at_once(rig, tmp_path):
    app = rig.start()
    app.window.settings_requested.emit()
    dialog = shown(app, SettingsDialog)
    assert dialog is not None
    dialog.output_edit.setText(str(tmp_path / "elsewhere"))
    dialog.add_source_button.click()
    dialog.sources_table.item(dialog.sources_table.rowCount() - 1, 2).setText("localhost:7001")
    dialog.feed_check.setChecked(False)  # the dialog's ports start at 1: the rig's feed binds port 0
    dialog.ws_port_spin.setValue(2)
    dialog.accept()
    assert store.load_config() == app.config
    assert app.config.output_root == str(tmp_path / "elsewhere")
    assert app.config.text_sources[-1].name == "New source"


def test_the_feed_follows_its_settings(rig):
    app = rig.start()
    assert app.feed is not None
    app.window.settings_requested.emit()
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
    app.window.settings_requested.emit()
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


def test_a_setup_request_opens_the_wizard_and_every_wizard_shares_the_apps_obs_step(rig):
    app = rig.start()
    app.window.setup_requested.emit()
    first = shown(app, SetupWizard)
    assert first is not None and first.currentId() == WizardStep.OBS
    first.close()
    app.window.setup_requested.emit()
    second = shown(app, SetupWizard)
    assert second is not None and second is not first
    assert second.obs_page._obs is first.obs_page._obs  # one ObsSetup per app: one gateway subscription


def test_a_password_the_wizard_saves_is_the_config_at_once(rig):
    """The gateway reads a typed password override through the app's config at its next connect."""
    app = rig.start()
    app.window.setup_requested.emit()
    wizard = shown(app, SetupWizard)
    assert wizard is not None
    assert wizard.store(replace(app.config, obs=replace(app.config.obs, password_override="typed"))) is None
    assert app.config.obs.password_override == "typed"
    assert store.load_config().obs.password_override == "typed"


def test_a_step_run_again_from_settings_opens_over_them_and_shows_the_password_it_saved(rig):
    app = rig.start()
    app.window.settings_requested.emit()
    settings = shown(app, SettingsDialog)
    assert settings is not None
    settings.setup_step_requested.emit(WizardStep.OBS)
    wizard = shown(app, SetupWizard)
    assert wizard is not None and wizard.currentId() == WizardStep.OBS
    assert wizard.parent() is settings
    assert wizard.store(replace(app.config, obs=replace(app.config.obs, password_override="typed"))) is None
    assert app.config.obs.password_override == "typed"
    assert settings.password_edit.text() == "typed"


# Quitting ------------------------------------------------------------------------------------------


def test_a_quit_closes_the_open_dialogs(rig):
    app = rig.start()
    app.window.settings_requested.emit()
    settings = shown(app, SettingsDialog)
    assert settings is not None
    settings.setup_step_requested.emit(WizardStep.SOURCES)
    assert shown(app, SetupWizard) is not None
    rig.close()
    assert [dialog for dialog in app.window.findChildren(QDialog) if dialog.isVisible()] == []
