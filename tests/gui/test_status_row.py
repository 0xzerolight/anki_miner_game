"""The status row (spec 16 item 1): OBS, each enabled text source, OCR when in OCR mode; four states per light."""

import pytest

from anki_miner_game.gui.widgets.status_row import StatusRow
from anki_miner_game.models.messages import OBS_SOURCE_ID, SourceStatus

SOURCES = [("textractor", "Textractor"), ("agent", "Agent")]


@pytest.fixture
def row(qtbot) -> StatusRow:
    widget = StatusRow(SOURCES)
    qtbot.addWidget(widget)
    return widget


def test_obs_and_each_enabled_source_start_disconnected(row):
    assert row.names() == ["OBS", "Textractor", "Agent"]
    assert {row.status(i) for i in (OBS_SOURCE_ID, "textractor", "agent")} == {SourceStatus.DISCONNECTED}


@pytest.mark.parametrize("status", list(SourceStatus))
def test_each_light_shows_each_of_the_four_states(row, status):
    row.set_status("agent", status)
    assert row.status("agent") is status
    assert row.light("agent").toolTip() == f"Agent: {status.value}"


def test_the_four_states_look_different(row):
    looks = set()
    for status in SourceStatus:
        row.set_status(OBS_SOURCE_ID, status)
        looks.add(row.light(OBS_SOURCE_ID).dot.styleSheet())
    assert len(looks) == 4


def test_ocr_and_the_clipboard_get_a_light_while_the_armed_game_uses_them(row):
    row.set_status("ocr", SourceStatus.CONNECTING)
    row.set_status("clipboard", SourceStatus.CONNECTED)
    assert row.names() == ["OBS", "Textractor", "Agent", "OCR", "Clipboard"]
    row.clear_armed_only()
    assert row.names() == ["OBS", "Textractor", "Agent"]
    row.set_status("ocr", SourceStatus.DISCONNECTED)  # its stop, reported after the disarm
    assert row.names() == ["OBS", "Textractor", "Agent"]


def test_another_source_id_is_named_by_its_id(row):
    row.set_status("mine", SourceStatus.CONNECTED)
    assert row.names()[-1] == "mine"


def test_the_configured_sources_can_change_and_keep_their_states(row):
    row.set_status("agent", SourceStatus.RECEIVING)
    row.set_text_sources([("agent", "Agent"), ("luna", "LunaTranslator")])
    assert row.names() == ["OBS", "Agent", "LunaTranslator"]
    assert row.status("agent") is SourceStatus.RECEIVING
    assert row.status("textractor") is None


def test_a_configured_source_is_not_duplicated_by_its_status(row):
    row.set_status("textractor", SourceStatus.CONNECTED)
    row.clear_armed_only()
    assert row.names() == ["OBS", "Textractor", "Agent"]
    assert row.status("textractor") is SourceStatus.CONNECTED
