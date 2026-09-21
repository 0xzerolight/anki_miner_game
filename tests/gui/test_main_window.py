"""The minimal main window (spec 16 items 2, 3 and 6): game, Arm/Disarm, Start/Stop, state, elapsed, cues, banners."""

from collections.abc import Callable

import pytest

from anki_miner_game.gui.main_window import MainWindow
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    AppState,
    Banner,
    BannerLevel,
    CommandKind,
    SessionEvent,
    SessionInput,
    UserCommand,
)

GAMES = [("steins-gate", "Steins;Gate"), ("zero-escape", "Zero Escape")]
LINE = GameLine(text="はい", raw="はい", t_mono=1.0, source_id="textractor")


class Control:
    """``SessionControl`` that records what the window posts."""

    def __init__(self) -> None:
        self.posted: list[SessionInput] = []

    def post(self, msg: SessionInput) -> None:
        self.posted.append(msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        raise AssertionError("the window listens to the presenter")

    @property
    def state(self) -> AppState:
        return AppState.IDLE


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig(qtbot):
    control, presenter, clock = Control(), QtPresenter(), Clock()
    quits: list[int] = []
    window = MainWindow(
        control, presenter.signals, GAMES, selected="zero-escape", on_quit=lambda: quits.append(1), now=clock
    )
    qtbot.addWidget(window)
    return window, control, presenter, clock, quits


def test_arm_arms_the_selected_game(rig):
    window, control, *_ = rig
    assert window.game.currentData() == "zero-escape"
    assert (window.state_label.text(), window.record_button.isEnabled()) == ("Idle", False)
    window.arm_button.click()
    assert control.posted == [UserCommand(CommandKind.ARM, slug="zero-escape")]


def test_arm_needs_a_game(qtbot):
    window = MainWindow(Control(), QtPresenter().signals, [], on_quit=lambda: None)
    qtbot.addWidget(window)
    assert not window.arm_button.isEnabled()


def test_while_armed_arm_becomes_disarm_and_start_starts(qtbot, rig):
    window, control, presenter, *_ = rig
    presenter.state_changed(AppState.ARMED, "steins-gate")
    qtbot.waitUntil(lambda: window.state_label.text() == "Armed")
    assert window.game.currentData() == "steins-gate"  # follows a CLI --arm
    assert not window.game.isEnabled()
    window.arm_button.click()
    window.record_button.click()
    assert control.posted == [UserCommand(CommandKind.DISARM), UserCommand(CommandKind.START)]
    assert (window.arm_button.text(), window.record_button.text()) == ("Disarm", "Start")


def test_while_recording_stop_stops_and_the_session_is_counted(qtbot, rig):
    window, control, presenter, clock, _ = rig
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.line_accepted(LINE, None, False)  # before the recording: not a cue
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    presenter.line_accepted(LINE, 1200, False)
    presenter.line_accepted(LINE, 1200, True)  # typewriter merge: the same cue
    presenter.line_accepted(LINE, 4000, False)
    qtbot.waitUntil(lambda: window.cues_label.text() == "2 cues")
    assert window.state_label.text() == "Recording"
    assert not window.arm_button.isEnabled()
    clock.t += 3725
    qtbot.waitUntil(lambda: window.elapsed_label.text() == "1:02:05", timeout=3000)
    window.record_button.click()
    assert control.posted == [UserCommand(CommandKind.STOP)]
    clock.t += 1
    presenter.state_changed(AppState.FINALISING, "steins-gate")
    qtbot.waitUntil(lambda: window.state_label.text() == "Finalising")
    assert not window.record_button.isEnabled()
    clock.t += 60
    presenter.state_changed(AppState.ARMED, "steins-gate")
    qtbot.waitUntil(lambda: window.state_label.text() == "Armed")
    assert (window.elapsed_label.text(), window.cues_label.text()) == ("1:02:06", "2 cues")  # the last session


def test_a_new_recording_counts_from_zero(qtbot, rig):
    window, _, presenter, *_ = rig
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    presenter.line_accepted(LINE, 10, False)
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    qtbot.waitUntil(lambda: window.state_label.text() == "Recording")
    assert (window.cues_label.text(), window.elapsed_label.text()) == ("0 cues", "0:00:00")


def test_banners_are_shown_replaced_and_cleared_by_key(qtbot, rig):
    window, _, presenter, *_ = rig
    presenter.banner(Banner("feed", BannerLevel.WARNING, "port 6678 is in use"))
    presenter.banner(Banner("obs", BannerLevel.ERROR, "OBS is not running"))
    presenter.banner(Banner("feed", BannerLevel.WARNING, "port 6679 is in use"))
    qtbot.waitUntil(lambda: window.banner_texts() == ["port 6679 is in use", "OBS is not running"])
    presenter.banner_cleared("feed")
    qtbot.waitUntil(lambda: window.banner_texts() == ["OBS is not running"])


def test_closing_the_window_asks_the_app_to_quit(rig):
    window, *_, quits = rig
    window.show()
    window.close()
    assert quits == [1]
