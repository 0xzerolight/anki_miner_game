"""The main window (spec 16): status row, game row, Arm/Start with elapsed time and cue count, live
list, banners; requests for the dialogs and the wizard; close to tray. The recent sessions and the
hand-off are in ``test_main_window_sessions.py`` (audit 2026-10-01 plan, section 4.10)."""

from collections.abc import Callable

import pytest

from anki_miner_game.gui.main_window import MainWindow
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    Banner,
    BannerLevel,
    CommandKind,
    SessionEvent,
    SessionInput,
    SourceStatus,
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


SOURCES = [("textractor", "Textractor"), ("agent", "Agent")]


@pytest.fixture
def rig(qtbot):
    control, presenter, clock = Control(), QtPresenter(), Clock()
    quits: list[int] = []
    window = MainWindow(
        control,
        presenter.signals,
        GAMES,
        selected="zero-escape",
        on_quit=lambda: quits.append(1),
        now=clock,
        text_sources=SOURCES,
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


# The cue count: the lines the actor journals -----------------------------------------------------


def line(text: str, t_mono: float) -> GameLine:
    return GameLine(text=text, raw=text, t_mono=t_mono, source_id="textractor")


def test_lines_held_for_an_auto_start_count_once_journalled(qtbot, rig):
    window, _, presenter, *_ = rig
    held = line("はじまり", 1.0)
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.line_accepted(held, None, False)  # the line that started the recording
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    presenter.line_accepted(held, 0, False)  # journalled at STARTED, published again with its offset
    presenter.line_accepted(line("つぎ", 3.0), 2000, False)
    qtbot.waitUntil(lambda: window.cues_label.text() == "2 cues")
    assert window.live_list.entries() == [("はじまり", 0), ("つぎ", 2000)]


def test_a_merge_the_actor_journals_as_a_new_line_is_counted(qtbot, rig):
    window, _, presenter, *_ = rig
    early = line("え", 1.2)
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.line_accepted(early, None, False)  # before the recording: never journalled
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    presenter.line_accepted(line("はい", 2.0), 700, False)
    presenter.line_accepted(GameLine("えっと…", "えっと…", early.t_mono, early.source_id), 900, True)
    qtbot.waitUntil(lambda: window.cues_label.text() == "2 cues")
    assert window.live_list.entries() == [("えっと…", 900), ("はい", 700)]


# The status row ----------------------------------------------------------------------------------


def test_the_status_row_follows_the_source_status(qtbot, rig):
    window, _, presenter, *_ = rig
    assert window.status_row.names() == ["OBS", "Textractor", "Agent"]
    presenter.source_status(OBS_SOURCE_ID, SourceStatus.CONNECTED)
    presenter.source_status("ocr", SourceStatus.CONNECTING)
    qtbot.waitUntil(lambda: window.status_row.status("ocr") is SourceStatus.CONNECTING)
    assert window.status_row.status(OBS_SOURCE_ID) is SourceStatus.CONNECTED
    presenter.state_changed(AppState.IDLE, None)  # disarmed: the game's own lights go
    qtbot.waitUntil(lambda: window.status_row.names() == ["OBS", "Textractor", "Agent"])


def test_the_configured_sources_can_change(rig):
    window, *_ = rig
    window.set_text_sources([("luna", "LunaTranslator")])
    assert window.status_row.names() == ["OBS", "LunaTranslator"]


# The game row and the requests for dialogs -------------------------------------------------------


def test_new_game_and_edit_ask_for_the_profile_dialog(qtbot, rig):
    window, *_ = rig
    with qtbot.waitSignal(window.new_game_requested):
        window.new_game_button.click()
    with qtbot.waitSignal(window.edit_game_requested) as edit:
        window.edit_game_button.click()
    assert edit.args == ["zero-escape"]


def test_edit_needs_a_game(qtbot):
    window = MainWindow(Control(), QtPresenter().signals, [], on_quit=lambda: None)
    qtbot.addWidget(window)
    assert window.new_game_button.isEnabled() and not window.edit_game_button.isEnabled()


def test_settings_and_the_setup_wizard_are_asked_for_from_the_menu(qtbot, rig):
    window, *_, quits = rig
    with qtbot.waitSignal(window.settings_requested):
        window.settings_action.trigger()
    with qtbot.waitSignal(window.setup_requested):
        window.setup_action.trigger()
    window.quit_action.trigger()
    assert quits == [1]


def test_the_game_list_can_change_and_keeps_the_selection(rig):
    window, *_ = rig
    window.set_games([("persona-5", "Persona 5"), ("zero-escape", "Zero Escape")])
    assert window.game.currentData() == "zero-escape"
    window.set_games([("persona-5", "Persona 5")])
    assert window.game.currentData() == "persona-5"
    window.set_games(GAMES, selected="steins-gate")
    assert window.game.currentData() == "steins-gate"


# Closing -----------------------------------------------------------------------------------------


def test_closing_the_window_asks_the_app_to_quit(rig):
    window, *_, quits = rig
    window.show()
    window.close()
    assert quits == [1]


@pytest.mark.parametrize("state", [AppState.ARMED, AppState.RECORDING])
def test_closing_while_armed_or_recording_minimises_to_the_tray(qtbot, rig, state):
    window, _, presenter, *_, quits = rig
    window.minimise_to_tray = True
    window.show()
    presenter.state_changed(state, "steins-gate")
    qtbot.waitUntil(lambda: window.state_label.text() != "Idle")
    window.close()
    assert quits == []
    assert not window.isVisible()


def test_closing_while_idle_quits_even_with_a_tray(rig):
    window, *_, quits = rig
    window.minimise_to_tray = True
    window.show()
    window.close()
    assert quits == [1]
