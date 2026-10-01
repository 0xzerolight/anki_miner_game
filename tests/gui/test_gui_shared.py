"""The words, colours, banner keys and icon the windows share (UJ-32, UJ-04, UJ-11, UJ-12, D-01)."""

import ast
import re
from pathlib import Path

import pytest
from PyQt6.QtGui import QColor

from anki_miner_game.gui import banner_keys, colours, icon, strings
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.messages import (
    START_FAILED_BANNER_KEY,
    STOP_FAILED_BANNER_KEY,
    AppState,
    BannerLevel,
    SourceStatus,
)
from anki_miner_game.session.session import BannerKey

REPO = Path(__file__).resolve().parents[2]

# The glossary (UJ-32, D-01) -----------------------------------------------------------------------


def shown_words() -> list[str]:
    """Every user-facing text in ``strings``: its ``str`` constants and its tables' values."""
    found: list[str] = []
    for name, value in vars(strings).items():
        if name.startswith("_") or not name.isupper():
            continue
        if isinstance(value, str):
            found.append(value)
        elif hasattr(value, "values"):
            found.extend(text for text in value.values() if isinstance(text, str))
    return found


def test_the_glossary_never_shows_arm_cue_or_a_lower_case_websocket():
    words = shown_words()
    assert len(words) > 30
    for text in words:
        assert not re.search(r"\b(arm|armed|arming|disarm|cues?)\b", text, re.IGNORECASE), text
        assert "websocket" not in text and "Websocket" not in text, text


def test_buttons_that_open_a_window_end_in_an_ellipsis():
    for text in (strings.ADD_YOUR_GAME, strings.NEW_GAME, strings.EDIT_GAME, strings.SETTINGS, strings.SET_UP_OBS):
        assert text.endswith("…"), text
    for text in (strings.START_RECORDING, strings.STOP_RECORDING, strings.DONE_PLAYING, strings.GET_READY):
        assert not text.endswith("…"), text


def test_the_d01_button_labels():
    assert (strings.START_RECORDING, strings.GET_READY, strings.STOP_RECORDING, strings.DONE_PLAYING) == (
        "Start recording",
        "Get ready",
        "Stop recording",
        "Done playing",
    )
    assert (strings.GETTING_OBS_READY, strings.STARTING, strings.STOPPING) == (
        "Getting OBS ready…",
        "Starting…",
        "Stopping…",
    )


@pytest.mark.parametrize(
    ("state", "pending", "text"),
    [
        (AppState.IDLE, False, ""),
        (AppState.IDLE, True, ""),
        (AppState.ARMED, False, "Ready"),
        (AppState.ARMED, True, "Ready: recording starts at the first line"),
        (AppState.RECORDING, False, "Recording 0:22:05 · 7 lines"),
        (AppState.RECORDING, True, "Recording 0:22:05 · 7 lines"),
        (AppState.FINALISING, False, "Saving the session…"),
    ],
)
def test_the_one_status_text(state, pending, text):
    assert strings.status_text(state, auto_start_pending=pending, elapsed="0:22:05", lines=7) == text


def test_one_line_is_singular():
    assert strings.lines_text(1) == "1 line"
    assert strings.lines_text(0) == "0 lines" and strings.lines_text(7) == "7 lines"
    assert strings.status_text(AppState.RECORDING, auto_start_pending=False, elapsed="0:00:03", lines=1) == (
        "Recording 0:00:03 · 1 line"
    )


@pytest.mark.parametrize(
    ("state", "text"),
    [
        (AppState.IDLE, "Anki Miner Game - Not recording"),
        (AppState.ARMED, "Anki Miner Game - Ready: Steins;Gate"),
        (AppState.RECORDING, "Anki Miner Game - Recording Steins;Gate 0:22:05"),
        (AppState.FINALISING, "Anki Miner Game - Saving the session…"),
    ],
)
def test_the_tray_tooltip(state, text):
    assert strings.tray_tooltip(state, "Steins;Gate", "0:22:05") == text


def test_every_source_and_add_on_status_has_its_words_and_no_enum_value_shows():
    assert set(strings.SOURCE_STATUS_TEXT) == set(SourceStatus)
    assert set(strings.ADDON_STATUS_TEXT) == set(AddonStatus)
    assert [strings.SOURCE_STATUS_TEXT[s] for s in SourceStatus] == [
        "Not connected",
        "Connecting",
        "Connected",
        "Receiving",
    ]
    assert [strings.ADDON_STATUS_TEXT[s] for s in AddonStatus] == [
        "Not installed",
        "Installing…",
        "Installed",
        "Damaged",
    ]


def test_install_buttons():
    assert strings.size_mb(96_400_000) == 96
    assert strings.install_now_text(AddonStatus.MISSING, 96_400_000) == "Install (96 MB)"
    assert strings.install_now_text(AddonStatus.BROKEN, 100_000_000) == "Repair (100 MB)"
    assert strings.install_elsewhere_text(AddonStatus.MISSING) == "Install…"
    assert strings.install_elsewhere_text(AddonStatus.BROKEN) == "Repair…"


# Colours (UJ-04, UJ-11, UJ-12) ---------------------------------------------------------------------


def test_the_colour_tokens_are_valid_colours_and_every_table_covers_its_enum():
    for token in (colours.GREY, colours.AMBER, colours.GREEN, colours.BLUE, colours.RED):
        assert QColor(token).isValid() and re.fullmatch(r"#[0-9a-f]{6}", token), token
    assert set(colours.SOURCE_COLOUR) == set(colours.SOURCE_FILLED) == set(SourceStatus)
    assert set(colours.STATE_COLOUR) == set(AppState)
    assert set(colours.BANNER_COLOUR) == set(BannerLevel)
    assert colours.GREY == "#9e9e9e"


def test_lights_are_rings_until_connected_and_the_tray_dot_is_red_while_recording():
    assert [colours.SOURCE_FILLED[s] for s in SourceStatus] == [False, False, True, True]
    assert [colours.SOURCE_COLOUR[s] for s in SourceStatus] == [
        colours.GREY,
        colours.AMBER,
        colours.GREEN,
        colours.BLUE,
    ]
    assert colours.STATE_COLOUR[AppState.RECORDING] == colours.RED
    assert colours.BANNER_COLOUR[BannerLevel.ERROR] == colours.RED


# Banner keys: the GUI's copies of the actor's (UJ-02, UJ-10) ------------------------------------


def test_the_guis_banner_keys_are_the_actors():
    assert banner_keys.OBS_BANNER_KEY == BannerKey.OBS
    assert banner_keys.ARM_BANNER_KEY == BannerKey.ARM
    assert banner_keys.INTERNAL_BANNER_KEY == BannerKey.INTERNAL
    assert banner_keys.FOREIGN_RECORDING_BANNER_KEY == BannerKey.FOREIGN_RECORDING
    assert banner_keys.SESSION_FILES_BANNER_KEY == BannerKey.SESSION_FILES
    assert STOP_FAILED_BANNER_KEY == BannerKey.STOP_FAILED == "stop_failed"
    assert START_FAILED_BANNER_KEY == "start_failed"


def test_the_failure_sets_of_each_pending_action():
    assert sorted(banner_keys.ARM_FAILED_KEYS) == ["arm", "internal_error", "obs"]
    assert sorted(banner_keys.START_FAILED_KEYS) == [
        "foreign_recording",
        "internal_error",
        "session_files",
        "start_failed",
    ]
    assert sorted(banner_keys.STOP_FAILED_KEYS) == ["internal_error", "stop_failed"]
    assert sorted(banner_keys.DONE_FAILED_KEYS) == ["arm", "internal_error"]
    assert sorted(banner_keys.SET_UP_OBS_KEYS) == ["obs"]


# The app icon (UJ-12) ------------------------------------------------------------------------------


def test_the_app_icon_is_the_packaged_256_px_png(qapp):
    assert icon.APP_ICON_PATH.read_bytes() == (REPO / "packaging" / "icons" / "anki-miner-game-256.png").read_bytes()
    assert not icon.app_icon().isNull()
    assert icon.DESKTOP_FILE_NAME == "anki-miner-game"
    assert (REPO / "packaging" / "deb" / f"{icon.DESKTOP_FILE_NAME}.desktop").is_file()


def test_a_missing_icon_file_is_a_null_icon(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(icon, "APP_ICON_PATH", tmp_path / "missing.png")
    assert icon.app_icon().isNull()


# The dependency rule (spec 4.1) --------------------------------------------------------------------

SHARED = ("strings.py", "colours.py", "banner_keys.py", "icon.py", "widgets/layout.py")


@pytest.mark.parametrize("module", SHARED)
def test_the_shared_gui_modules_import_only_models_interfaces_and_gui(module):
    tree = ast.parse((REPO / "anki_miner_game" / "gui" / module).read_text(encoding="utf-8"))
    imported = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imported += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    allowed = ("anki_miner_game.models.", "anki_miner_game.interfaces.", "anki_miner_game.gui")
    assert [name for name in imported if name.startswith("anki_miner_game") and not name.startswith(allowed)] == []
