"""D-02: settings that hold the old default hotkey exactly move to ``Alt+F9`` at launch; nothing else changes."""

import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from anki_miner_game import paths, store
from anki_miner_game.app import OLD_DEFAULT_HOTKEY
from anki_miner_game.models.config import AppConfig
from anki_miner_game.store import StoreWriteError
from tests.app_rig import Rig


@pytest.fixture
def rig(qtbot, tmp_path) -> Iterator[Rig]:
    made = Rig(qtbot, tmp_path)
    yield made
    made.close()


def test_the_old_default_moves_to_alt_f9_and_is_saved(rig, caplog):
    caplog.set_level(logging.INFO, logger="anki_miner_game.app")
    assert OLD_DEFAULT_HOTKEY == "Ctrl+Shift+F9" and AppConfig().hotkey == "Alt+F9"
    rig.cfg = replace(rig.cfg, hotkey=OLD_DEFAULT_HOTKEY)
    app = rig.start()
    assert app.config.hotkey == "Alt+F9"
    assert store.load_config() == app.config  # every other setting kept
    assert "hotkey Ctrl+Shift+F9 migrated to Alt+F9" in caplog.text


@pytest.mark.parametrize("chord", ["ctrl+shift+f9", " Ctrl+Shift+F9 ", "Ctrl+Alt+F9", "Alt+F10"])
def test_any_other_chord_is_the_users_own_and_stays(rig, chord):
    rig.cfg = replace(rig.cfg, hotkey=chord)
    app = rig.start()
    assert app.config.hotkey == chord
    assert store.load_config().hotkey == chord


def test_a_first_launch_writes_the_new_default(rig):
    app = rig.start(write_config=False)
    assert app.config.hotkey == store.load_config().hotkey == "Alt+F9"


def test_an_unusable_settings_file_is_left_alone(rig):
    paths.config_path().write_text('{"schema": 1, "hotkey": "Ctrl+Shift+F9", ', encoding="utf-8")
    app = rig.start(write_config=False)
    assert paths.config_path().read_text(encoding="utf-8") == '{"schema": 1, "hotkey": "Ctrl+Shift+F9", '
    assert app.config.hotkey == "Alt+F9"  # the defaults this run uses


def test_a_migration_that_cannot_be_saved_still_uses_alt_f9_for_this_run(rig, monkeypatch, caplog):
    store.save_config(replace(rig.cfg, hotkey=OLD_DEFAULT_HOTKEY))
    written = paths.config_path().read_bytes()

    def refuse(cfg: AppConfig) -> Path:
        raise StoreWriteError(paths.config_path(), "read-only")

    monkeypatch.setattr(store, "save_config", refuse)
    app = rig.start(write_config=False)
    assert app.config.hotkey == "Alt+F9"
    assert paths.config_path().read_bytes() == written
    assert "settings not saved" in caplog.text and "read-only" in caplog.text
