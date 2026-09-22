"""The tray (spec 16): Arm, Start/Stop, Open text feed, Show window, Quit."""

from collections.abc import Callable

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QSystemTrayIcon

from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.gui.tray import Tray
from anki_miner_game.models.messages import AppState, CommandKind, SessionEvent, SessionInput, UserCommand

FEED = "http://127.0.0.1:6679/"


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
        self.game: str | None = "steins-gate"
        self.feed: str | None = FEED
        self.opened: list[QUrl] = []
        self.shows: list[int] = []
        self.quits: list[int] = []
        self.tray = Tray(
            self.control,
            self.presenter.signals,
            game=lambda: self.game,
            feed_url=lambda: self.feed,
            open_url=self.opened.append,
        )
        qtbot.addWidget(self.tray.menu)
        self.tray.show_requested.connect(lambda: self.shows.append(1))
        self.tray.quit_requested.connect(lambda: self.quits.append(1))

    def state(self, state: AppState) -> None:
        self.presenter.state_changed(state, None if state is AppState.IDLE else "steins-gate")
        self.qtbot.waitUntil(lambda: self.tray.state is state)

    def menu(self) -> list[tuple[str, bool]]:
        self.tray.menu.aboutToShow.emit()
        return [(a.text(), a.isEnabled()) for a in self.tray.menu.actions() if not a.isSeparator()]


@pytest.fixture
def rig(qtbot) -> Rig:
    return Rig(qtbot)


def test_the_menu_while_idle(rig):
    assert rig.menu() == [
        ("Arm", True),
        ("Start", False),
        ("Open text feed", True),
        ("Show window", True),
        ("Quit", True),
    ]
    rig.tray.arm_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.ARM, slug="steins-gate")]


def test_arm_needs_a_game(rig):
    rig.game = None
    assert ("Arm", False) in rig.menu()


def test_while_armed_disarm_and_start(rig):
    rig.state(AppState.ARMED)
    assert rig.menu()[:2] == [("Disarm", True), ("Start", True)]
    rig.tray.arm_action.trigger()
    rig.tray.record_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.DISARM), UserCommand(CommandKind.START)]


def test_while_recording_stop_and_no_disarm(rig):
    rig.state(AppState.RECORDING)
    assert rig.menu()[:2] == [("Disarm", False), ("Stop", True)]
    rig.tray.record_action.trigger()
    assert rig.control.posted == [UserCommand(CommandKind.STOP)]
    rig.state(AppState.FINALISING)
    assert rig.menu()[:2] == [("Disarm", False), ("Start", False)]


def test_open_text_feed_opens_the_page_and_is_off_with_the_feed(rig):
    rig.menu()
    rig.tray.feed_action.trigger()
    assert rig.opened == [QUrl(FEED)]
    rig.feed = None
    assert ("Open text feed", False) in rig.menu()


def test_show_window_and_a_click_on_the_icon_ask_for_the_window(rig):
    rig.tray.show_action.trigger()
    rig.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    rig.tray.icon.activated.emit(QSystemTrayIcon.ActivationReason.Context)  # opens the menu, nothing else
    assert rig.shows == [1, 1]


def test_quit_asks_the_app_to_quit(rig):
    rig.tray.quit_action.trigger()
    assert rig.quits == [1]


def test_the_icon_and_tooltip_follow_the_state(rig):
    idle = rig.tray.icon.icon().cacheKey()
    rig.state(AppState.RECORDING)
    assert rig.tray.icon.toolTip() == "Anki Miner Game: recording"
    assert rig.tray.icon.icon().cacheKey() != idle
