"""The main window (spec 16): status row, game row, Arm/Start with elapsed time and cue count, live
list, banners; requests for the dialogs and the wizard; close to tray. The recent sessions and the
hand-off are in ``test_main_window_sessions.py`` (audit 2026-10-01 plan, section 4.10)."""

from collections.abc import Callable

import pytest

from anki_miner_game.gui.main_window import MainWindow, Pending, RecordingControls
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
    assert window.status_row.names() == []  # no lights in Idle (UJ-04)
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.source_status(OBS_SOURCE_ID, SourceStatus.CONNECTED)
    presenter.source_status("ocr", SourceStatus.CONNECTING)
    presenter.source_status("textractor", SourceStatus.RECEIVING)
    qtbot.waitUntil(lambda: window.status_row.names() == ["OBS", "Game text: Textractor"])
    assert window.status_row.status("ocr") is SourceStatus.CONNECTING
    presenter.state_changed(AppState.IDLE, None)
    qtbot.waitUntil(lambda: window.status_row.names() == [])
    assert window.status_row.status("ocr") is None  # the game's sources are forgotten


def test_the_configured_sources_name_the_lights(qtbot, rig):
    window, _, presenter, *_ = rig
    window.set_text_sources([("luna", "LunaTranslator")])
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.source_status("luna", SourceStatus.CONNECTED)
    qtbot.waitUntil(lambda: window.status_row.names() == ["OBS", "Game text: LunaTranslator"])


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


# The core loop's commands and the pending state (D-01, UJ-01, UJ-02, B4-05) ----------------------


class Loop:
    """``RecordingControls`` over a recording ``Control`` and a presenter; ``slug`` is the selected game."""

    def __init__(self, slug: str | None = "steins-gate") -> None:
        self.control, self.presenter, self.clock = Control(), QtPresenter(), Clock()
        self.slug = slug
        self.changes = 0
        self.controls = RecordingControls(
            self.control,
            self.presenter.signals,
            game=lambda: self.slug,
            auto_start_game=lambda slug: slug == "zero-escape",
            now=self.clock,
        )
        self.controls.changed.connect(self._changed)

    def _changed(self) -> None:
        self.changes += 1

    def state(self, state: AppState) -> None:
        self.presenter.state_changed(state, None if state is AppState.IDLE else self.slug)


ARM = UserCommand(CommandKind.ARM, slug="steins-gate")
START = UserCommand(CommandKind.START)


def test_one_click_in_idle_posts_arm_then_start_and_waits_for_the_recording():
    loop = Loop()
    loop.controls.record()
    assert loop.control.posted == [ARM, START]
    assert loop.controls.pending is Pending.START
    loop.state(AppState.ARMED)
    assert loop.controls.pending is Pending.START  # the arm on the way is not the end
    loop.state(AppState.RECORDING)
    assert loop.controls.pending is None


def test_nothing_runs_twice_while_pending():
    loop = Loop()
    loop.controls.record()
    loop.controls.record()
    loop.controls.get_ready()
    loop.controls.done_playing()
    assert loop.control.posted == [ARM, START]
    assert not loop.controls.can_record()


def test_nothing_to_record_without_a_game():
    loop = Loop(slug=None)
    assert not loop.controls.can_record()
    loop.controls.record()
    assert loop.control.posted == []


def test_get_ready_is_offered_only_in_idle_for_a_game_that_starts_at_the_first_line():
    loop = Loop(slug="zero-escape")
    assert loop.controls.offers_get_ready()
    loop.controls.get_ready()
    assert loop.control.posted == [UserCommand(CommandKind.ARM, slug="zero-escape")]
    assert loop.controls.pending is Pending.GET_READY and not loop.controls.offers_get_ready()
    loop.state(AppState.ARMED)
    assert loop.controls.pending is None and not loop.controls.offers_get_ready()
    loop.slug = "steins-gate"
    loop.state(AppState.IDLE)
    assert not loop.controls.offers_get_ready()
    loop.controls.get_ready()
    assert loop.control.posted == [UserCommand(CommandKind.ARM, slug="zero-escape")]


PATHS = [
    # (slug, state before, action, posted, how it ends)
    ("steins-gate", AppState.IDLE, "record", [ARM, START], ("state", AppState.RECORDING)),
    ("steins-gate", AppState.IDLE, "record", [ARM, START], ("banner", "arm")),
    ("steins-gate", AppState.IDLE, "record", [ARM, START], ("banner", "obs")),
    ("steins-gate", AppState.IDLE, "record", [ARM, START], ("banner", "start_failed")),
    ("steins-gate", AppState.IDLE, "record", [ARM, START], ("banner", "internal_error")),
    (
        "zero-escape",
        AppState.IDLE,
        "get_ready",
        [UserCommand(CommandKind.ARM, slug="zero-escape")],
        ("state", AppState.ARMED),
    ),
    ("zero-escape", AppState.IDLE, "get_ready", [UserCommand(CommandKind.ARM, slug="zero-escape")], ("banner", "arm")),
    ("zero-escape", AppState.IDLE, "get_ready", [UserCommand(CommandKind.ARM, slug="zero-escape")], ("banner", "obs")),
    ("steins-gate", AppState.ARMED, "record", [START], ("state", AppState.RECORDING)),
    ("steins-gate", AppState.ARMED, "record", [START], ("banner", "start_failed")),
    ("steins-gate", AppState.ARMED, "record", [START], ("banner", "internal_error")),
    ("steins-gate", AppState.ARMED, "record", [START], ("state", AppState.IDLE)),
    ("steins-gate", AppState.RECORDING, "record", [UserCommand(CommandKind.STOP)], ("state", AppState.FINALISING)),
    ("steins-gate", AppState.RECORDING, "record", [UserCommand(CommandKind.STOP)], ("banner", "stop_failed")),
    ("steins-gate", AppState.ARMED, "done_playing", [UserCommand(CommandKind.DISARM)], ("state", AppState.IDLE)),
    ("steins-gate", AppState.ARMED, "done_playing", [UserCommand(CommandKind.DISARM)], ("banner", "arm")),
]


@pytest.mark.parametrize(("slug", "before", "action", "posted", "end"), PATHS)
def test_every_pending_action_ends_at_its_state_or_a_failure_banner(slug, before, action, posted, end):
    loop = Loop(slug)
    if before is not AppState.IDLE:
        loop.state(before)
    getattr(loop.controls, action)()
    assert loop.control.posted == posted
    assert loop.controls.pending is not None
    kind, value = end
    if kind == "state":
        loop.state(value)
    else:
        loop.presenter.banner(Banner(value, BannerLevel.ERROR, "it failed"))
    assert loop.controls.pending is None


def test_other_banners_leave_the_action_pending():
    loop = Loop()
    loop.controls.record()
    loop.presenter.banner(Banner("low_disk", BannerLevel.WARNING, "Only 1.8 GB free"))
    assert loop.controls.pending is Pending.START
    loop.state(AppState.ARMED)
    loop.presenter.banner(Banner("obs", BannerLevel.ERROR, "OBS went away"))  # the arm is done: not its failure
    assert loop.controls.pending is Pending.START
    loop.presenter.banner(Banner("start_failed", BannerLevel.ERROR, "OBS did not start recording"))
    assert loop.controls.pending is None


def test_each_change_is_announced():
    loop = Loop()
    loop.controls.record()
    loop.state(AppState.ARMED)
    loop.state(AppState.RECORDING)
    assert loop.changes == 3


def test_elapsed_counts_from_the_recording_start():
    loop = Loop()
    loop.state(AppState.ARMED)
    assert loop.controls.elapsed() == 0.0
    loop.state(AppState.RECORDING)
    loop.clock.t += 3725
    assert loop.controls.elapsed() == 3725
    loop.state(AppState.FINALISING)
    assert loop.controls.elapsed() == 0.0
