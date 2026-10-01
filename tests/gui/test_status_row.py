"""The status row (spec 16 item 1 as amended by UJ-04): OBS and one Game text light, shown from a click on."""

import pytest

from anki_miner_game.gui import colours
from anki_miner_game.gui.widgets.status_row import StatusRow, dot_style
from anki_miner_game.models.messages import OBS_SOURCE_ID, SourceStatus

SOURCES = [("textractor", "Textractor"), ("agent", "Agent"), ("luna", "LunaTranslator")]


@pytest.fixture
def row(qtbot) -> StatusRow:
    widget = StatusRow(SOURCES)
    qtbot.addWidget(widget)
    return widget


def test_two_lights_obs_and_game_text_waiting(row):
    assert row.names() == ["OBS", "Game text: waiting"]
    assert row.status(OBS_SOURCE_ID) is SourceStatus.DISCONNECTED
    assert row.game_text_status() is SourceStatus.DISCONNECTED
    assert row.game_light.toolTip() == "No text source is running yet."


def test_hidden_rows_have_no_lights(row):
    row.set_shown(False)
    assert row.names() == []
    row.set_shown(True)
    assert row.names() == ["OBS", "Game text: waiting"]


def test_the_game_text_light_names_the_live_sources_and_takes_the_best_status(row):
    row.set_status("textractor", SourceStatus.CONNECTING)
    row.set_status("agent", SourceStatus.CONNECTED)
    row.set_status("luna", SourceStatus.RECEIVING)
    assert row.names() == ["OBS", "Game text: Agent, LunaTranslator"]
    assert row.game_text_status() is SourceStatus.RECEIVING
    assert row.game_light.status is SourceStatus.RECEIVING
    assert row.game_light.toolTip() == "Textractor: Connecting\nAgent: Connected\nLunaTranslator: Receiving"


def test_the_clipboard_and_ocr_fold_into_the_game_text_light(row):
    row.set_status("clipboard", SourceStatus.CONNECTED)
    row.set_status("ocr", SourceStatus.CONNECTING)
    assert row.names() == ["OBS", "Game text: Clipboard"]
    assert row.game_light.toolTip() == "Clipboard: Connected\nOCR: Connecting"


def test_obs_has_its_own_light_and_tooltip(row):
    row.set_status(OBS_SOURCE_ID, SourceStatus.CONNECTED)
    assert row.status(OBS_SOURCE_ID) is SourceStatus.CONNECTED
    assert row.obs_light.toolTip() == "OBS: Connected"
    assert row.game_text_status() is SourceStatus.DISCONNECTED  # OBS is no text source


@pytest.mark.parametrize("status", list(SourceStatus))
def test_each_state_has_its_colour_and_shape(status):
    style = dot_style(status)
    assert colours.SOURCE_COLOUR[status] in style
    assert style.startswith("background-color") is colours.SOURCE_FILLED[status]  # dots; rings are borders


def test_the_four_states_look_different(row):
    looks = set()
    for status in SourceStatus:
        row.set_status(OBS_SOURCE_ID, status)
        looks.add(row.obs_light.dot.styleSheet())
    assert len(looks) == 4


def test_idle_forgets_the_games_sources_and_keeps_obs(row):
    row.set_status(OBS_SOURCE_ID, SourceStatus.CONNECTED)
    row.set_status("agent", SourceStatus.RECEIVING)
    row.clear_sources()
    assert row.status("agent") is None
    assert row.status(OBS_SOURCE_ID) is SourceStatus.CONNECTED
    assert row.names() == ["OBS", "Game text: waiting"]
    row.set_status("ocr", SourceStatus.DISCONNECTED)  # its stop, reported after Done playing
    assert row.status("ocr") is None


def test_new_settings_keep_a_live_source_and_its_state(row):
    """B4-08: saving Settings while an OCR or clipboard game is ready kept no light for it."""
    row.set_status("ocr", SourceStatus.CONNECTING)
    row.set_status("agent", SourceStatus.RECEIVING)
    row.set_text_sources([("agent", "Agent"), ("luna", "LunaTranslator")])
    assert row.status("ocr") is SourceStatus.CONNECTING
    assert row.status("agent") is SourceStatus.RECEIVING
    assert row.names() == ["OBS", "Game text: Agent"]
    assert "OCR: Connecting" in row.game_light.toolTip()


def test_a_source_no_setting_names_is_named_by_its_id(row):
    row.set_status("mine", SourceStatus.CONNECTED)
    assert row.names() == ["OBS", "Game text: mine"]


def test_renaming_a_configured_source_renames_it_in_the_light(row):
    row.set_status("luna", SourceStatus.CONNECTED)
    row.set_text_sources([("luna", "Luna")])
    assert row.names() == ["OBS", "Game text: Luna"]
