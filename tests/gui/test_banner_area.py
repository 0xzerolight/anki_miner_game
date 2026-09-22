"""The inline banner area (spec 16 item 6): recoverable failures are banners, keyed, never modal dialogs."""

import pytest
from PyQt6.QtCore import Qt

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
