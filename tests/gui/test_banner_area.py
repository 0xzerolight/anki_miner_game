"""The inline banner area (spec 16 item 6): recoverable failures are banners, keyed, never modal dialogs."""

import pytest
from PyQt6.QtCore import Qt

from anki_miner_game.gui import colours
from anki_miner_game.gui.widgets.banner_area import BannerArea
from anki_miner_game.models.messages import Banner, BannerLevel


@pytest.fixture
def area(qtbot) -> BannerArea:
    widget = BannerArea()
    qtbot.addWidget(widget)
    return widget


def test_a_banner_with_a_shown_key_replaces_it_in_place(area):
    area.show_banner(Banner("feed", BannerLevel.WARNING, "port 6678 is in use"))
    area.show_banner(Banner("obs", BannerLevel.ERROR, "OBS is not running"))
    area.show_banner(Banner("feed", BannerLevel.ERROR, "port 6679 is in use"))
    assert area.texts() == ["port 6679 is in use", "OBS is not running"]
    assert area.level("feed") is BannerLevel.ERROR


def test_a_cleared_key_goes_and_an_unknown_one_is_ignored(area):
    area.show_banner(Banner("obs", BannerLevel.ERROR, "OBS is not running"))
    area.clear("feed")
    area.clear("obs")
    assert area.texts() == []


def test_the_user_can_dismiss_a_banner(area):
    area.show_banner(Banner("config", BannerLevel.WARNING, "Settings cannot be saved"))
    area.close_button("config").click()
    assert area.texts() == []


def test_banner_text_is_never_read_as_markup(area):
    area.show_banner(Banner("obs", BannerLevel.INFO, "<b>OBS</b> & co"))
    assert area.label("obs").textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize("level", list(BannerLevel))
def test_the_level_is_an_icon_and_an_edge_and_the_text_keeps_the_palette_colour(area, level):
    area.show_banner(Banner("obs", level, "OBS is not running"))
    assert area.label("obs").styleSheet() == ""  # the theme's text colour, light or dark (UJ-11)
    assert not area.icon("obs").pixmap().isNull()
    assert f"border-left: 3px solid {colours.BANNER_COLOUR[level]}" in area.frame("obs").styleSheet()


def test_a_new_level_replaces_the_edge(area):
    area.show_banner(Banner("obs", BannerLevel.INFO, "a"))
    area.show_banner(Banner("obs", BannerLevel.ERROR, "b"))
    assert colours.BANNER_COLOUR[BannerLevel.ERROR] in area.frame("obs").styleSheet()


def test_a_banner_with_an_action_carries_its_button(qtbot):
    area = BannerArea(actions={"obs": "Set up OBS…"})
    qtbot.addWidget(area)
    area.show_banner(Banner("obs", BannerLevel.ERROR, "OBS's WebSocket server is off."))
    area.show_banner(Banner("feed", BannerLevel.WARNING, "port 6678 is in use"))
    button = area.action_button("obs")
    assert button is not None and button.text() == "Set up OBS…" and not button.isHidden()
    assert area.action_button("feed") is None
    with qtbot.waitSignal(area.action_clicked) as clicked:
        button.click()
    assert clicked.args == ["obs"]


def test_action_buttons_can_be_hidden_and_a_later_banner_follows(qtbot):
    area = BannerArea(actions={"obs": "Set up OBS…"})
    qtbot.addWidget(area)
    area.show_banner(Banner("obs", BannerLevel.ERROR, "a"))
    area.set_actions_shown(False)
    assert area.action_button("obs").isHidden()
    area.clear("obs")
    area.show_banner(Banner("obs", BannerLevel.ERROR, "b"))
    assert area.action_button("obs").isHidden()
    area.set_actions_shown(True)
    assert not area.action_button("obs").isHidden()
