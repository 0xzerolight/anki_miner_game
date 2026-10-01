"""The tray (spec 16 as amended by UJ-05, UJ-12): it mirrors the window's buttons through the same
``RecordingControls``; items that cannot run are hidden; the app icon carries a state dot."""

from collections.abc import Callable

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import QSystemTrayIcon

from anki_miner_game.gui import colours
from anki_miner_game.gui.icon import app_icon
from anki_miner_game.gui.main_window import RecordingControls
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.gui.tray import Tray, state_icon
from anki_miner_game.models.messages import AppState, CommandKind, SessionEvent, SessionInput, UserCommand

FEED = "http://127.0.0.1:6679/"
TITLE = "Steins;Gate"


class Control:
    def __init__(self) -> None:
        self.posted: list[SessionInput] = []

    def post(self, msg: SessionInput) -> None:
        self.posted.append(msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        raise AssertionError("the tray listens to the presenter")

    @property
    def state(self) -> AppState:
        return AppState.IDLE


class Rig:
    def __init__(self, qtbot) -> None:
        self.qtbot = qtbot
        self.control = Control()
        self.presenter = QtPresenter()
        self.slug: str | None = "steins-gate"
        self.auto = False
        self.feed: str | None = FEED
        self.messages_ok = True
        self.opened: list[QUrl] = []
        self.shows: list[int] = []
        self.quits: list[int] = []
        self.controls = RecordingControls(
            self.control, self.presenter.signals, game=lambda: self.slug, auto_start_game=lambda _slug: self.auto
        )
        self.tray = Tray(
            self.controls,
            game_title=lambda: TITLE if self.slug else None,
            feed_url=lambda: self.feed,
            open_url=self.opened.append,
            supports_messages=lambda: self.messages_ok,
        )
        qtbot.addWidget(self.tray.menu)
        self.tray.show_requested.connect(lambda: self.shows.append(1))
        self.tray.quit_requested.connect(lambda: self.quits.append(1))

    def state(self, state: AppState) -> None:
        self.presenter.state_changed(state, None if state is AppState.IDLE else "steins-gate")
        self.qtbot.waitUntil(lambda: self.controls.state is state)

    def menu(self) -> list[str]:
        self.tray.menu.aboutToShow.emit()
        return [a.text() for a in self.tray.menu.actions() if a.isVisible() and not a.isSeparator()]


@pytest.fixture
def rig(qtbot) -> Rig:
    return Rig(qtbot)


ALWAYS = ["Open text feed", "Show window", "Quit"]


def test_idle_offers_start_recording_for_the_selected_game(rig):
    assert rig.menu() == ["Start recording: Steins;Gate", *ALWAYS]
    rig.tray.record_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.ARM, slug="steins-gate"), UserCommand(CommandKind.START)]


def test_idle_offers_get_ready_for_a_game_that_starts_at_the_first_line(rig):
    rig.auto = True
    assert rig.menu() == ["Start recording: Steins;Gate", "Get ready: Steins;Gate", *ALWAYS]
    rig.tray.get_ready_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.ARM, slug="steins-gate")]


def test_no_game_no_session_items_and_no_separator(rig):
    rig.slug = None
    assert rig.menu() == ALWAYS
    assert not rig.tray.session_separator.isVisible()


def test_ready_offers_start_recording_and_done_playing(rig):
    rig.state(AppState.ARMED)
    assert rig.menu() == ["Start recording", "Done playing", *ALWAYS]
    rig.tray.done_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.DISARM)]


def test_recording_offers_stop_recording(rig):
    rig.state(AppState.RECORDING)
    assert rig.menu() == ["Stop recording", *ALWAYS]
    rig.tray.record_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.STOP)]


def test_saving_and_pending_offer_no_command(rig):
    rig.tray.record_action.trigger()
    assert rig.menu() == ALWAYS  # a click is pending
    rig.state(AppState.FINALISING)
    assert rig.menu() == ALWAYS


def test_open_text_feed_opens_the_page_and_is_hidden_with_the_feed_off(rig):
    rig.menu()
    rig.tray.feed_action.trigger()
    assert rig.opened == [QUrl(FEED)]
    rig.feed = None
    assert "Open text feed" not in rig.menu()


def test_show_window_and_a_click_on_the_icon_ask_for_the_window(rig):
    rig.tray.show_action.trigger()
    rig.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    rig.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Context)  # opens the menu, nothing else
    assert rig.shows == [1, 1]


def test_quit_asks_the_app_to_quit(rig):
    rig.tray.quit_action.trigger()
    assert rig.quits == [1]


def test_the_tooltip_names_the_state_and_the_game(rig):
    assert rig.tray.icon.toolTip() == "Anki Miner Game - Not recording"
    rig.state(AppState.ARMED)
    assert rig.tray.icon.toolTip() == "Anki Miner Game - Ready: Steins;Gate"
    rig.state(AppState.RECORDING)
    assert rig.tray.icon.toolTip() == "Anki Miner Game - Recording Steins;Gate 0:00:00"


def test_the_icon_follows_the_state(rig):
    idle = rig.tray.icon.icon().cacheKey()
    rig.state(AppState.RECORDING)
    assert rig.tray.icon.icon().cacheKey() != idle


@pytest.mark.parametrize("state", list(AppState))
def test_the_state_dot_sits_on_the_app_icon(state):
    image = state_icon(state, app_icon()).pixmap(32, 32).toImage()
    assert image.pixelColor(25, 25) == QColor(colours.STATE_COLOUR[state])  # the dot, lower right
    assert image.pixelColor(8, 8).alpha() > 0  # the app icon under it


def test_without_the_app_icon_the_dot_is_the_icon(qtbot):
    image = state_icon(AppState.ARMED, QIcon()).pixmap(32, 32).toImage()
    assert image.pixelColor(16, 16) == QColor(colours.STATE_COLOUR[AppState.ARMED])


def test_the_first_hide_to_the_tray_says_so_once(rig, monkeypatch):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(rig.tray.icon, "showMessage", lambda title, text, *rest: shown.append((title, text)))
    rig.state(AppState.RECORDING)
    rig.tray.tell_still_running()
    rig.tray.tell_still_running()
    assert shown == [
        (
            "Anki Miner Game is still running",
            "Steins;Gate is recording. Click this icon to open the window; right-click it to quit.",
        )
    ]


def test_no_notice_where_the_desktop_shows_none(rig, monkeypatch):
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(rig.tray.icon, "showMessage", lambda title, text, *rest: shown.append((title, text)))
    rig.messages_ok = False
    rig.state(AppState.ARMED)
    rig.tray.tell_still_running()
    assert shown == []
