"""Auto mode (spec 12, window rule as amended by docs/m0/wave-1-amendments.md item 10)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from anki_miner_game.lifecycle.auto import POLL_S, AutoMode, window_open
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    START_FAILED_BANNER_KEY,
    STOP_FAILED_BANNER_KEY,
    AppState,
    Banner,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    SessionEvent,
    SessionInput,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.obs import ObsConnectError, ObsEventName
from anki_miner_game.models.profile import AutoSettings, CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.session import session as session_mod
from tests.session.actor_harness import T0, Harness
from tests.session.actor_harness import profile as actor_profile

WIN_VALUE = "Steins#3AGate 60 FPS:UnityWndClass:SteinsGate.exe"
X11_VALUE = "0x3a00007\r\nSteins;Gate\r\nsteinsgate"


@dataclass(frozen=True)
class Item:
    value: str
    enabled: bool
    name: str = ""


class FakeControl:
    def __init__(self, state: AppState = AppState.IDLE) -> None:
        self.posted: list[SessionInput] = []
        self.subscribers: list[Callable[[SessionEvent], None]] = []
        self._state = state

    def post(self, msg: SessionInput) -> None:
        self.posted.append(msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        self.subscribers.append(cb)

    @property
    def state(self) -> AppState:
        return self._state

    def emit(self, event: SessionEvent) -> None:
        if isinstance(event, StateChanged):
            self._state = event.state
        for cb in self.subscribers:
            cb(event)

    def commands(self) -> list[CommandKind]:
        return [m.kind for m in self.posted if isinstance(m, UserCommand)]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Windows:
    def __init__(self, items: list[Item]) -> None:
        self.items = items
        self.calls = 0
        self.error: Exception | None = None
        self.on_call: Callable[[], None] | None = None

    async def __call__(self) -> list[Item]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return list(self.items)


def make_profile(
    *,
    enabled: bool = True,
    start: bool = True,
    idle_min: int = 10,
    window_close: bool = True,
    window: str | None = WIN_VALUE,
    kind: CaptureKind = CaptureKind.GAME,
) -> GameProfile:
    return GameProfile(
        slug="steins-gate",
        title="Steins;Gate",
        capture=CaptureSettings(kind=kind, window=window),
        auto=AutoSettings(
            enabled=enabled,
            start_on_first_line=start,
            stop_idle_minutes=idle_min,
            stop_on_window_close=window_close,
        ),
    )


SLUG = "steins-gate"


class Rig:
    def __init__(self, profile: GameProfile | None, items: list[Item] | None = None) -> None:
        self.profiles: dict[str, GameProfile] = {} if profile is None else {profile.slug: profile}
        self.lookups: list[str] = []
        self.control = FakeControl()
        self.clock = Clock()
        self.windows = Windows(items if items is not None else [Item(WIN_VALUE, True)])
        self.auto = AutoMode(self.control, self.lookup, self.windows, now=self.clock)

    def lookup(self, slug: str) -> GameProfile | None:
        self.lookups.append(slug)
        return self.profiles.get(slug)

    def state(self, state: AppState, slug: str = SLUG) -> None:
        self.control.emit(StateChanged(state, None if state is AppState.IDLE else slug))

    def line(self, offset_ms: int | None = None) -> None:
        self.control.emit(LineAccepted(GameLine("こんにちは", "こんにちは", self.clock.t, "hook"), offset_ms))

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds

    def check(self) -> None:
        asyncio.run(self.auto.check())


def test_subscribes_to_the_control() -> None:
    rig = Rig(make_profile())
    assert len(rig.control.subscribers) == 1


# Auto-start.


def test_first_line_while_armed_sends_start_once() -> None:
    rig = Rig(make_profile())
    rig.state(AppState.ARMED)
    rig.line()
    rig.line()
    assert rig.control.commands() == [CommandKind.START]


def test_the_start_carries_the_line_that_triggered_it() -> None:
    """The actor journals that line at offset 0 on ``STARTED`` (spec 12); a manual START carries none."""
    rig = Rig(make_profile())
    rig.state(AppState.ARMED)
    first = GameLine("始まり", "始まり", rig.clock.t, "hook")
    rig.control.emit(LineAccepted(first, None))
    rig.line()
    assert rig.control.posted == [UserCommand(CommandKind.START, line=first)]


def test_lines_while_idle_or_recording_send_nothing() -> None:
    rig = Rig(make_profile())
    rig.line()
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.line(100)
    assert rig.control.commands() == []


def test_a_repeated_armed_state_does_not_start_twice() -> None:
    rig = Rig(make_profile())
    rig.state(AppState.ARMED)
    rig.line()
    rig.state(AppState.ARMED)
    rig.line()
    assert rig.control.commands() == [CommandKind.START]


def test_a_failed_start_lets_the_next_line_start_again() -> None:
    """Spec 17: a failed ``StartRecord`` keeps the state at ``armed``, so no ``StateChanged`` resets the flag."""
    rig = Rig(make_profile())
    rig.state(AppState.ARMED)
    rig.line()
    rig.control.emit(BannerRaised(Banner("no-source", BannerLevel.WARNING, "No text source")))
    rig.line()
    assert rig.control.commands() == [CommandKind.START]
    rig.control.emit(BannerRaised(Banner(START_FAILED_BANNER_KEY, BannerLevel.ERROR, "StartRecord failed")))
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.START]


def test_a_start_failure_banner_outside_armed_changes_nothing() -> None:
    rig = Rig(make_profile(idle_min=0, window=None))
    rig.state(AppState.RECORDING)
    rig.control.emit(BannerRaised(Banner(START_FAILED_BANNER_KEY, BannerLevel.ERROR, "StartRecord failed")))
    rig.line(100)
    assert rig.control.commands() == []


def test_start_on_first_line_off_sends_nothing() -> None:
    rig = Rig(make_profile(start=False))
    rig.state(AppState.ARMED)
    rig.line()
    assert rig.control.commands() == []


@pytest.mark.parametrize("profile", [make_profile(enabled=False), None])
def test_auto_mode_off_or_no_profile_never_starts(profile: GameProfile | None) -> None:
    rig = Rig(profile)
    rig.state(AppState.ARMED)
    rig.line()
    assert rig.control.commands() == []


def test_profile_is_read_when_the_state_changes() -> None:
    rig = Rig(make_profile(enabled=False))
    rig.state(AppState.ARMED)
    rig.profiles[SLUG] = make_profile()
    rig.line()
    assert rig.control.commands() == []
    rig.state(AppState.IDLE)
    rig.state(AppState.ARMED)
    rig.line()
    assert rig.control.commands() == [CommandKind.START]


def test_the_profile_is_looked_up_by_the_armed_slug_once_per_change() -> None:
    rig = Rig(make_profile())
    rig.state(AppState.ARMED)
    rig.line()
    rig.line()
    rig.state(AppState.RECORDING)
    rig.state(AppState.IDLE)
    assert rig.lookups == [SLUG, SLUG]
    assert rig.control.commands() == [CommandKind.START]


def test_nothing_is_known_before_the_first_state_change() -> None:
    """``SessionControl`` has no armed slug, so a control already armed at construction starts nothing."""
    control = FakeControl(AppState.ARMED)
    profile = make_profile()
    AutoMode(control, {profile.slug: profile}.get, Windows([]), now=Clock())
    control.emit(LineAccepted(GameLine("a", "a", 0.0, "hook"), None))
    assert control.posted == []
    control.emit(StateChanged(AppState.ARMED, SLUG))
    control.emit(LineAccepted(GameLine("b", "b", 1.0, "hook"), None))
    assert [m.kind for m in control.posted if isinstance(m, UserCommand)] == [CommandKind.START]


def test_arming_another_game_starts_a_new_armed_period_with_its_profile() -> None:
    rig = Rig(make_profile())
    rig.profiles["chaos-head"] = replace(make_profile(), slug="chaos-head")
    rig.profiles["robotics-notes"] = replace(make_profile(start=False), slug="robotics-notes")
    rig.state(AppState.ARMED)
    rig.line()
    rig.state(AppState.ARMED, "chaos-head")
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.START]
    rig.state(AppState.ARMED, "robotics-notes")
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.START]


def test_arming_another_game_drops_the_previous_games_window() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.profiles["chaos-head"] = replace(make_profile(idle_min=0, window=None), slug="chaos-head")
    rig.state(AppState.ARMED)
    rig.state(AppState.ARMED, "chaos-head")
    rig.state(AppState.RECORDING, "chaos-head")
    rig.check()
    rig.check()
    assert rig.windows.calls == 0
    assert rig.control.commands() == []


# Auto-stop, idle.


def test_idle_stop_after_the_configured_minutes_since_the_last_line() -> None:
    rig = Rig(make_profile(idle_min=10, window=None))
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.advance(300)
    rig.line(300_000)
    rig.advance(599)
    rig.check()
    assert rig.control.commands() == []
    rig.advance(1)
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]
    rig.advance(60)
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_idle_counts_from_the_recording_start_without_lines() -> None:
    rig = Rig(make_profile(idle_min=1, window=None))
    rig.state(AppState.ARMED)
    rig.advance(3600)
    rig.state(AppState.RECORDING)
    rig.advance(59)
    rig.check()
    assert rig.control.commands() == []
    rig.advance(1)
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_idle_zero_disables_the_idle_stop() -> None:
    rig = Rig(make_profile(idle_min=0, window=None))
    rig.state(AppState.RECORDING)
    rig.advance(10 * 3600)
    rig.check()
    assert rig.control.commands() == []


def test_idle_stop_needs_auto_mode_on() -> None:
    rig = Rig(make_profile(enabled=False, idle_min=1, window=None))
    rig.state(AppState.RECORDING)
    rig.advance(3600)
    rig.check()
    assert rig.control.commands() == []


def test_idle_is_not_checked_outside_recording() -> None:
    rig = Rig(make_profile(idle_min=1, window=None))
    rig.state(AppState.ARMED)
    rig.advance(3600)
    rig.check()
    rig.state(AppState.FINALISING)
    rig.advance(3600)
    rig.check()
    assert rig.control.commands() == []


def test_stop_is_sent_again_in_a_later_session() -> None:
    rig = Rig(make_profile(idle_min=1, window=None, start=False))
    rig.state(AppState.RECORDING)
    rig.advance(60)
    rig.check()
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.advance(60)
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP, CommandKind.STOP]


# Auto-stop, window closed.


def test_two_consecutive_misses_stop_the_session() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    assert rig.control.commands() == []
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_a_hit_between_misses_resets_the_count() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.windows.items = [Item(WIN_VALUE, True)]
    rig.check()
    rig.windows.items = [Item(WIN_VALUE, False)]
    rig.check()
    assert rig.control.commands() == []
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_misses_do_not_carry_into_the_next_session() -> None:
    rig = Rig(make_profile(idle_min=0, start=False), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.check()
    assert rig.control.commands() == []


def test_a_query_error_is_neither_hit_nor_miss() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.windows.error = ObsConnectError("gone")
    rig.check()
    assert rig.control.commands() == []
    rig.windows.error = None
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_an_empty_list_is_neither_hit_nor_miss() -> None:
    rig = Rig(make_profile(idle_min=0), [])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.check()
    assert rig.control.commands() == []


@pytest.mark.parametrize(
    "profile",
    [
        make_profile(enabled=False, idle_min=0),
        make_profile(window_close=False, idle_min=0),
        make_profile(window=None, idle_min=0),
        make_profile(kind=CaptureKind.PIPEWIRE, idle_min=0),
    ],
    ids=["auto-off", "window-close-off", "no-window", "pipewire"],
)
def test_window_is_not_polled(profile: GameProfile) -> None:
    rig = Rig(profile, [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.check()
    assert rig.windows.calls == 0
    assert rig.control.commands() == []


def test_window_is_polled_only_while_recording() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.ARMED)
    rig.check()
    rig.check()
    assert rig.windows.calls == 0


def test_state_change_during_the_query_sends_nothing() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    rig.check()
    rig.windows.on_call = lambda: rig.state(AppState.FINALISING)
    rig.check()
    assert rig.control.commands() == []


# The window rule itself.


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([Item(WIN_VALUE, True)], True),
        ([Item(WIN_VALUE, False)], False),
        # Retitled window: the stored item reads disabled, a live item has the same class and exe.
        ([Item(WIN_VALUE, False), Item("Steins;Gate 30 FPS:UnityWndClass:SteinsGate.exe", True)], True),
        ([Item(WIN_VALUE, False), Item("x:unitywndclass:STEINSGATE.EXE", True)], True),
        ([Item(WIN_VALUE, False), Item("x:UnityWndClass:Other.exe", True)], False),
        ([Item(WIN_VALUE, False), Item("x:OtherClass:SteinsGate.exe", True)], False),
        ([Item("", True), Item(WIN_VALUE, False)], False),
    ],
)
def test_window_open_windows(items: list[Item], expected: bool) -> None:
    assert window_open(items, WIN_VALUE) is expected


def test_window_open_windows_decodes_escapes() -> None:
    stored = "t:Cls#3AA#22:G#3Ame.exe"
    assert window_open([Item("other:cls:a#:g:me.EXE", True)], stored) is False
    assert window_open([Item("other:CLS#3Aa#22:g#3Ame.EXE", True)], stored) is True


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([Item(X11_VALUE, True)], True),
        ([Item(X11_VALUE, False)], False),
        # Retitled: item 0 disabled, the same xid listed again, enabled, under its new name.
        ([Item(X11_VALUE, False), Item("0x3a00007\r\nSteins;Gate 60 FPS\r\nsteinsgate", True)], True),
        # Reopened window: new xid, same name and class; capture no longer follows it.
        ([Item(X11_VALUE, False), Item("0x4100002\r\nSteins;Gate\r\nsteinsgate", True)], False),
        ([Item(X11_VALUE, False), Item("0x3a00007\r\nSteins;Gate\r\nsteinsgate", False)], False),
    ],
)
def test_window_open_x11(items: list[Item], expected: bool) -> None:
    assert window_open(items, X11_VALUE) is expected


def test_window_open_x11_item_zero_enabled_counts_even_with_another_value() -> None:
    assert window_open([Item("0x1\r\nOther\r\nother", True)], X11_VALUE) is True


def test_window_open_unknown_format_is_none() -> None:
    assert window_open([Item("anything", False)], "no separators here") is None


def test_window_open_empty_list_is_none() -> None:
    assert window_open([], WIN_VALUE) is None
    assert window_open([], X11_VALUE) is None


# The polling loop.


def test_run_checks_on_the_poll_interval() -> None:
    rig = Rig(make_profile(idle_min=0), [Item(WIN_VALUE, False)])
    rig.state(AppState.RECORDING)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        rig.advance(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.auto.run(sleep=fake_sleep))
    assert slept == [POLL_S] * 3
    assert rig.windows.calls == 2
    assert rig.control.commands() == [CommandKind.STOP]


def test_poll_interval_is_five_seconds() -> None:
    assert POLL_S == 5.0


def test_profile_replace_keeps_auto_off_by_default() -> None:
    profile = replace(make_profile(), auto=AutoSettings())
    rig = Rig(profile)
    rig.state(AppState.ARMED)
    rig.line()
    rig.state(AppState.RECORDING)
    rig.advance(3600)
    rig.check()
    rig.check()
    assert rig.control.commands() == []
    assert rig.windows.calls == 0


# Auto-stop retry (B1-08).


def test_a_failed_auto_stop_is_posted_again_at_the_next_check() -> None:
    rig = Rig(make_profile(idle_min=1, window=None, start=False))
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.advance(60)
    rig.check()
    rig.control.emit(BannerRaised(Banner(STOP_FAILED_BANNER_KEY, BannerLevel.ERROR, "OBS did not stop recording")))
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP, CommandKind.STOP]


def test_a_stop_that_worked_is_not_posted_again() -> None:
    rig = Rig(make_profile(idle_min=1, window=None, start=False))
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)
    rig.advance(60)
    rig.check()
    rig.state(AppState.FINALISING)
    rig.check()
    assert rig.control.commands() == [CommandKind.STOP]


def test_a_stop_failure_banner_outside_recording_changes_nothing() -> None:
    rig = Rig(make_profile(idle_min=1, window=None, start=False))
    rig.state(AppState.ARMED)
    rig.control.emit(BannerRaised(Banner(STOP_FAILED_BANNER_KEY, BannerLevel.ERROR, "x")))
    rig.check()
    assert rig.control.commands() == []


# Manual stops pause auto-start (D-06, B5-04).

OBS_EXITED = Banner("obs_exited", BannerLevel.WARNING, "OBS closed during the recording; the session was saved.")


def auto_session(rig: Rig) -> None:
    """Ready, a line starts the recording (auto mode's START), OBS reports it recording."""
    rig.state(AppState.ARMED)
    rig.line()
    rig.state(AppState.RECORDING)


def test_a_manual_stop_pauses_auto_start() -> None:
    rig = Rig(make_profile(window=None))
    auto_session(rig)
    rig.state(AppState.FINALISING)  # the user pressed Stop: auto mode posted nothing
    rig.state(AppState.ARMED)
    assert not rig.auto.auto_start_pending()
    rig.line()
    assert rig.control.commands() == [CommandKind.START]


def test_an_auto_idle_stop_keeps_auto_start() -> None:
    rig = Rig(make_profile(idle_min=1, window=None))
    auto_session(rig)
    rig.advance(60)
    rig.check()  # auto mode's own STOP
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    assert rig.auto.auto_start_pending()
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.STOP, CommandKind.START]


def test_a_manual_start_after_a_pause_and_then_an_auto_stop_keeps_auto_start() -> None:
    rig = Rig(make_profile(idle_min=1, window=None))
    auto_session(rig)
    rig.state(AppState.FINALISING)  # manual stop: paused
    rig.state(AppState.ARMED)
    rig.state(AppState.RECORDING)  # the user pressed Start recording: the pause ends
    rig.advance(60)
    rig.check()
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.STOP, CommandKind.START]


def test_done_playing_or_another_game_ends_the_pause() -> None:
    rig = Rig(make_profile(window=None))
    rig.profiles["chaos-head"] = replace(make_profile(window=None), slug="chaos-head")
    auto_session(rig)
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.state(AppState.ARMED, "chaos-head")  # another game readied
    assert rig.auto.auto_start_pending()
    rig.state(AppState.RECORDING, "chaos-head")
    rig.state(AppState.FINALISING, "chaos-head")
    rig.state(AppState.ARMED, "chaos-head")  # paused again
    rig.state(AppState.IDLE)
    rig.state(AppState.ARMED, "chaos-head")  # Done playing, then Get ready
    assert rig.auto.auto_start_pending()


def test_obs_exiting_during_an_auto_recording_is_no_manual_stop() -> None:
    """Review Focus 1: the actor ends the session (FINALISING, ARMED), then raises obs_exited."""
    rig = Rig(make_profile(window=None))
    auto_session(rig)
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.control.emit(BannerRaised(OBS_EXITED))
    assert rig.auto.auto_start_pending()
    rig.line()
    assert rig.control.commands() == [CommandKind.START, CommandKind.START]


def test_an_obs_exit_banner_from_an_earlier_recording_does_not_lift_a_later_pause() -> None:
    rig = Rig(make_profile(window=None))
    auto_session(rig)
    rig.state(AppState.FINALISING)
    rig.state(AppState.ARMED)
    rig.control.emit(BannerRaised(OBS_EXITED))
    rig.line()  # starts again
    rig.state(AppState.RECORDING)
    rig.state(AppState.FINALISING)  # this time a manual stop
    rig.state(AppState.ARMED)
    assert not rig.auto.auto_start_pending()


@pytest.mark.parametrize(
    ("profile", "state", "pending"),
    [
        (make_profile(), AppState.IDLE, False),
        (make_profile(), AppState.ARMED, True),
        (make_profile(start=False), AppState.ARMED, False),
        (make_profile(enabled=False), AppState.ARMED, False),
        (make_profile(), AppState.RECORDING, False),
        (make_profile(), AppState.FINALISING, False),
    ],
)
def test_auto_start_pending_only_while_ready_with_start_at_the_first_line(
    profile: GameProfile, state: AppState, pending: bool
) -> None:
    rig = Rig(profile)
    rig.state(state)
    assert rig.auto.auto_start_pending() is pending


async def _no_windows() -> list[Item]:
    return []


async def test_obs_exiting_mid_auto_recording_then_a_line_relaunches_obs_and_records(tmp_path, monkeypatch):
    """Review Focus 1 over the real actor: the next line's START relaunches OBS (D-04) and records."""
    monkeypatch.setattr(session_mod, "QUIT_STOP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(session_mod, "QUIT_START_WAIT_S", 0.05)
    h = Harness(tmp_path)
    h.profiles[SLUG] = replace(actor_profile(), auto=AutoSettings(enabled=True, start_on_first_line=True))
    auto = AutoMode(h.actor, h.profiles.get, _no_windows, now=h.clock)
    await h.start()
    try:
        await h.arm()
        await h.line("はじまり", T0 + 0.2)
        await h.started(T0 + 1.0)
        await h.emit(ObsEventName.EXIT_STARTED, {}, T0 + 5.0)
        h.gateway.connected = False
        h.discovery.running = False
        await h.emit(ObsEventName.CONNECTION_LOST, {}, T0 + 5.3)
        assert h.actor.state is AppState.ARMED
        assert auto.auto_start_pending()
        await h.line("つぎ", T0 + 10.0)
        assert h.discovery.launches == 1
        assert h.gateway.names().count("StartRecord") == 2
        await h.started(T0 + 11.0, stem="2026-10-02 18-05-00")
        assert h.actor.state is AppState.RECORDING
        h.obs.stops_on_request = True
    finally:
        await h.stop()
