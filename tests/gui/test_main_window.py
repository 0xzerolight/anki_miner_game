"""The main window (spec 16 as amended by D-01 and UJ-01..UJ-14): lights, the game row, one primary
button with Get ready and Done playing, one status text, banners with Set up OBS…, the hand-off, the
Lines list with its hints, recent sessions; requests for the dialogs and the wizard; close to tray."""

from collections.abc import Callable
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QSize

from anki_miner_game.gui import colours
from anki_miner_game.gui.main_window import MainWindow, Pending, RecordingControls
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.gui.widgets.layout import screen_bounded
from anki_miner_game.models.addons import AddonStatus
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
from tests.gui.session_fakes import Jobs, manifest, place

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


@pytest.fixture
def make(qtbot):
    """A window with keyword arguments of the test's choice; ``selected`` defaults to Zero Escape."""

    def build(games=GAMES, **kwargs) -> SimpleNamespace:
        control, presenter, clock, quits = Control(), QtPresenter(), Clock(), []
        kwargs.setdefault("selected", "zero-escape")
        window = MainWindow(
            control,
            presenter.signals,
            games,
            on_quit=lambda: quits.append(1),
            now=clock,
            text_sources=SOURCES,
            **kwargs,
        )
        qtbot.addWidget(window)
        return SimpleNamespace(window=window, control=control, presenter=presenter, clock=clock, quits=quits)

    return build


def test_banners_are_shown_replaced_and_cleared_by_key(qtbot, rig):
    window, _, presenter, *_ = rig
    presenter.banner(Banner("feed", BannerLevel.WARNING, "port 6678 is in use"))
    presenter.banner(Banner("obs", BannerLevel.ERROR, "OBS is not running"))
    presenter.banner(Banner("feed", BannerLevel.WARNING, "port 6679 is in use"))
    qtbot.waitUntil(lambda: window.banner_texts() == ["port 6679 is in use", "OBS is not running"])
    presenter.banner_cleared("feed")
    qtbot.waitUntil(lambda: window.banner_texts() == ["OBS is not running"])


# The line count: the lines the actor journals ----------------------------------------------------


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
    qtbot.waitUntil(lambda: window.status_label.text() == "Recording 0:00:00 · 2 lines")
    assert window.live_list.entries() == [("はじまり", 0), ("つぎ", 2000)]


def test_a_merge_the_actor_journals_as_a_new_line_is_counted(qtbot, rig):
    window, _, presenter, *_ = rig
    early = line("え", 1.2)
    presenter.state_changed(AppState.ARMED, "steins-gate")
    presenter.line_accepted(early, None, False)  # before the recording: never journalled
    presenter.state_changed(AppState.RECORDING, "steins-gate")
    presenter.line_accepted(line("はい", 2.0), 700, False)
    presenter.line_accepted(GameLine("えっと…", "えっと…", early.t_mono, early.source_id), 900, True)
    qtbot.waitUntil(lambda: window.status_label.text() == "Recording 0:00:00 · 2 lines")
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
def test_closing_while_ready_or_recording_minimises_to_the_tray_and_says_so(qtbot, rig, state):
    window, _, presenter, *_, quits = rig
    window.minimise_to_tray = True
    window.show()
    presenter.state_changed(state, "steins-gate")
    with qtbot.waitSignal(window.hidden_to_tray):
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


# One click records; the window follows RecordingControls (D-01, UJ-01..UJ-03, B4-05) --------------


def test_idle_shows_start_recording_no_status_and_no_lights(make):
    window = make().window
    assert (window.primary_button.text(), window.primary_button.isEnabled()) == ("Start recording", True)
    assert window.get_ready_button.isHidden() and window.done_button.isHidden()
    assert window.status_label.text() == "" and window.status_dot.isHidden()
    assert window.status_row.names() == []


def test_one_click_in_idle_shows_starting_until_the_recording_runs(make):
    w = make()
    window = w.window
    window.primary_button.click()
    assert w.control.posted == [UserCommand(CommandKind.ARM, slug="zero-escape"), UserCommand(CommandKind.START)]
    assert (window.primary_button.text(), window.primary_button.isEnabled()) == ("Starting…", False)
    assert not window.game.isEnabled() and not window.edit_game_button.isEnabled()
    assert window.status_row.names() == ["OBS", "Game text: waiting"]  # lights from the click on
    window.primary_button.click()
    assert len(w.control.posted) == 2  # no second command while pending (B4-05)
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert window.primary_button.text() == "Starting…"  # not the Ready on the way
    assert not window.done_button.isHidden() and not window.done_button.isEnabled()
    w.presenter.state_changed(AppState.RECORDING, "zero-escape")
    assert (window.primary_button.text(), window.primary_button.isEnabled()) == ("Stop recording", True)
    assert window.done_button.isHidden()


@pytest.mark.parametrize("key", ["arm", "obs", "start_failed", "internal_error"])
def test_a_failed_one_click_gives_the_button_back(make, key):
    w = make()
    w.window.primary_button.click()
    w.presenter.banner(Banner(key, BannerLevel.ERROR, "it failed"))
    assert (w.window.primary_button.text(), w.window.primary_button.isEnabled()) == ("Start recording", True)
    assert w.window.game.isEnabled() and w.window.edit_game_button.isEnabled()
    assert w.window.status_row.names() == []


def test_ready_starts_and_done_playing_ends_the_game(make):
    w = make()
    window = w.window
    w.presenter.state_changed(AppState.ARMED, "steins-gate")
    assert window.game.currentData() == "steins-gate"  # follows a CLI --arm
    assert (window.primary_button.text(), window.done_button.text()) == ("Start recording", "Done playing")
    assert window.status_label.text() == "Ready"
    window.done_button.click()
    assert w.control.posted == [UserCommand(CommandKind.DISARM)]
    assert not window.done_button.isEnabled() and not window.primary_button.isEnabled()
    w.presenter.state_changed(AppState.IDLE, None)
    assert window.primary_button.isEnabled() and window.done_button.isHidden()


def test_recording_shows_time_and_lines_and_stops_with_one_click(qtbot, make):
    w = make()
    window = w.window
    w.presenter.state_changed(AppState.ARMED, "steins-gate")
    w.presenter.line_accepted(LINE, None, False)  # before the recording: not counted
    w.presenter.state_changed(AppState.RECORDING, "steins-gate")
    w.presenter.line_accepted(LINE, 1200, False)
    w.presenter.line_accepted(LINE, 1200, True)  # typewriter merge: the same line
    w.presenter.line_accepted(LINE, 4000, False)
    assert window.status_label.text() == "Recording 0:00:00 · 2 lines"
    assert not window.status_dot.isHidden() and colours.RED in window.status_dot.styleSheet()
    w.clock.t += 3725
    qtbot.waitUntil(lambda: window.status_label.text() == "Recording 1:02:05 · 2 lines", timeout=3000)
    window.primary_button.click()
    assert w.control.posted == [UserCommand(CommandKind.STOP)]
    assert (window.primary_button.text(), window.primary_button.isEnabled()) == ("Stopping…", False)
    w.presenter.state_changed(AppState.FINALISING, "steins-gate")
    assert window.status_label.text() == "Saving the session…"
    assert not window.primary_button.isEnabled()
    w.presenter.state_changed(AppState.ARMED, "steins-gate")
    assert window.status_label.text() == "Ready" and window.status_dot.isHidden()
    assert window.primary_button.text() == "Start recording"  # after Stop the app stays ready


def test_a_new_recording_counts_from_zero(make):
    w = make()
    w.presenter.state_changed(AppState.RECORDING, "steins-gate")
    w.presenter.line_accepted(LINE, 10, False)
    w.presenter.state_changed(AppState.ARMED, "steins-gate")
    w.presenter.state_changed(AppState.RECORDING, "steins-gate")
    assert w.window.status_label.text() == "Recording 0:00:00 · 0 lines"


def test_ready_promises_an_automatic_start_only_while_it_is_pending(make):
    """D-06: after a manual Stop auto mode pauses, and the status must not claim otherwise."""
    pending = [True]
    w = make(auto_start_pending=lambda: pending[0])
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert w.window.status_label.text() == "Ready: recording starts at the first line"
    pending[0] = False
    w.presenter.state_changed(AppState.RECORDING, "zero-escape")
    w.presenter.state_changed(AppState.FINALISING, "zero-escape")
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert w.window.status_label.text() == "Ready"


def test_a_banner_after_ready_reads_auto_start_again(make):
    """Review Focus 1: the ``obs_exited`` banner comes after ``StateChanged(ARMED)`` and lifts the pause
    with no new state change; the status must follow it."""
    flag = [False]
    w = make(auto_start_pending=lambda: flag[0])
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert w.window.status_label.text() == "Ready"
    flag[0] = True
    w.presenter.banner(Banner("obs_exited", BannerLevel.WARNING, "OBS closed during the recording."))
    assert w.window.status_label.text() == "Ready: recording starts at the first line"


def test_get_ready_only_for_a_game_that_starts_at_the_first_line(make):
    w = make(auto_start_game=lambda slug: slug == "zero-escape")
    window = w.window
    assert not window.get_ready_button.isHidden() and window.get_ready_button.text() == "Get ready"
    window.game.setCurrentIndex(window.game.findData("steins-gate"))
    assert window.get_ready_button.isHidden()
    window.game.setCurrentIndex(window.game.findData("zero-escape"))
    window.get_ready_button.click()
    assert w.control.posted == [UserCommand(CommandKind.ARM, slug="zero-escape")]
    assert window.primary_button.text() == "Getting OBS ready…" and window.get_ready_button.isHidden()
    assert window.controls.pending is Pending.GET_READY
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert window.primary_button.text() == "Start recording" and not window.done_button.isHidden()


@pytest.mark.parametrize(
    ("state", "enabled"),
    [(AppState.IDLE, True), (AppState.ARMED, False), (AppState.RECORDING, False), (AppState.FINALISING, False)],
)
def test_edit_only_in_idle_and_new_game_always(make, state, enabled):
    """B5-01 / D-05: a profile saved while ready was ignored until the next Get ready."""
    w = make()
    w.presenter.state_changed(state, None if state is AppState.IDLE else "steins-gate")
    assert w.window.edit_game_button.isEnabled() is enabled
    assert w.window.new_game_button.isEnabled()


def test_the_window_shares_its_controls(make):
    window = make().window
    assert isinstance(window.controls, RecordingControls)
    assert window.controls.game() == "zero-escape"
    assert window.selected_title() == "Zero Escape"


# Layout: first run, Settings…, banners, hints, sizes (UJ-06, UJ-07, UJ-08b, UJ-10a, UJ-13, UJ-14) --


def test_first_run_offers_only_settings_and_add_your_game(qtbot):
    window = MainWindow(Control(), QtPresenter().signals, [], on_quit=lambda: None)
    qtbot.addWidget(window)
    assert not window.first_run.isHidden()
    assert window.add_game_button.text() == "Add your game…"
    assert window.first_run_hint.text() == (
        "Add the game you want to play. You do this once per game; after that, pick it here and press "
        "Start recording."
    )
    for part in (window.game_row, window.control_row, window.lines_label, window.live_list):
        assert part.isHidden()
    assert window.recent.isHidden() and window.recent_label.isHidden()
    assert window.selected_title() is None
    with qtbot.waitSignal(window.new_game_requested):
        window.add_game_button.click()
    window.set_games(GAMES)
    assert window.first_run.isHidden() and not window.game_row.isHidden() and not window.live_list.isHidden()


def test_settings_is_a_flat_button_and_there_is_no_menu_bar(qtbot, make):
    window = make().window
    assert window.menuWidget() is None
    assert window.settings_button.text() == "Settings…" and window.settings_button.isFlat()
    with qtbot.waitSignal(window.settings_requested):
        window.settings_button.click()


def test_an_obs_banner_offers_set_up_obs_only_while_idle(qtbot, make):
    w = make()
    w.presenter.banner(Banner("obs", BannerLevel.ERROR, "OBS's WebSocket server is off."))
    w.presenter.banner(Banner("feed", BannerLevel.WARNING, "port 6678 is in use"))
    button = w.window.banners.action_button("obs")
    assert button is not None and button.text() == "Set up OBS…" and not button.isHidden()
    assert w.window.banners.action_button("feed") is None
    with qtbot.waitSignal(w.window.setup_requested):
        button.click()
    w.presenter.state_changed(AppState.ARMED, "steins-gate")
    assert button.isHidden()
    w.presenter.state_changed(AppState.IDLE, None)
    w.window.primary_button.click()
    assert button.isHidden()  # not while a click is pending either


def test_the_empty_lines_list_says_what_comes_next(make):
    w = make()
    live = w.window.live_list
    assert live.placeholder() == (
        "Start the game and your text hooker, then press Start recording. Lines from the game show here."
    )
    w.window.primary_button.click()
    w.presenter.source_status(OBS_SOURCE_ID, SourceStatus.CONNECTING)
    assert live.placeholder() == "Starting OBS. The first time can take up to 30 s."
    w.presenter.source_status(OBS_SOURCE_ID, SourceStatus.CONNECTED)
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert live.placeholder() == "Waiting for the first line from your text hooker…"
    w.presenter.state_changed(AppState.RECORDING, "zero-escape")
    w.presenter.state_changed(AppState.FINALISING, "zero-escape")
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert live.placeholder() == "Press Start recording for the next session, or Done playing when you stop."
    w.presenter.state_changed(AppState.IDLE, None)
    assert live.placeholder().startswith("Start the game and your text hooker")


def test_the_hand_off_goes_when_the_next_recording_starts(make):
    w = make()
    w.window.handoff.show()
    w.presenter.state_changed(AppState.ARMED, "zero-escape")
    assert not w.window.handoff.isHidden()
    w.presenter.state_changed(AppState.RECORDING, "zero-escape")
    assert w.window.handoff.isHidden()


def test_recent_sessions_show_only_once_there_is_one(qtbot, tmp_path):
    root = tmp_path / "out"
    window = MainWindow(
        Control(), QtPresenter().signals, GAMES, on_quit=lambda: None, output_root=lambda: root, vad_jobs=Jobs()
    )
    qtbot.addWidget(window)
    assert window.recent.isHidden() and window.recent_label.isHidden()
    place(root, manifest(1))
    window.reload_sessions()
    assert not window.recent.isHidden() and not window.recent_label.isHidden()


def test_the_window_fits_the_screen_and_lines_and_sessions_share_the_height(make):
    window = make().window
    assert window.size() == screen_bounded(window, QSize(560, 680))
    column = window.centralWidget().layout()
    assert column.stretch(column.indexOf(window.live_list)) == 1
    assert column.stretch(column.indexOf(window.recent)) == 1


# The voice-trimming add-on reaches the recent sessions (UJ-09, master 4.8 G2) ----------------------


class _IdleControl:
    """``SessionControl`` for a window that only lists sessions."""

    state = AppState.IDLE

    def post(self, msg: object) -> None:
        raise AssertionError("nothing is posted here")

    def subscribe(self, cb: object) -> None:
        raise AssertionError("the window listens to the presenter")


class _ReadyAddon:
    """``AddonService`` of an installed add-on; the recent sessions read only ``status``."""

    size_bytes = 96_000_000
    note = None

    def status(self) -> AddonStatus:
        return AddonStatus.READY

    async def install(self, progress: object) -> None:
        raise AssertionError("nothing is installed here")


def test_the_window_hands_the_voice_trimming_add_on_to_the_recent_sessions(qtbot, tmp_path):
    root = tmp_path / "out"
    path = place(root, manifest(3))

    def menu_texts(addon: _ReadyAddon | None) -> list[str]:
        window = MainWindow(
            _IdleControl(),
            QtPresenter().signals,
            [("steins-gate", "Steins;Gate")],
            on_quit=lambda: None,
            output_root=lambda: root,
            vad_addon=addon,
        )
        qtbot.addWidget(window)
        return [action.text() for action in window.recent.menu_for(path).actions()]

    assert "Trim again" in menu_texts(_ReadyAddon())
    assert "Trim again" not in menu_texts(None)
