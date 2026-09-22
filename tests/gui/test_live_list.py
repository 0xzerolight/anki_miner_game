"""The live list (spec 16 item 4) and the cue count taken from what the session actor journals."""

import pytest

from anki_miner_game.gui.widgets.live_list import LIVE_LINES, JournalCounter, LiveList
from anki_miner_game.models.lines import GameLine


def line(text: str, t_mono: float, source_id: str = "textractor") -> GameLine:
    return GameLine(text=text, raw=text, t_mono=t_mono, source_id=source_id)


def merged(base: GameLine, text: str) -> GameLine:
    """A typewriter merge keeps its base line's ``t_mono`` and ``source_id`` (``TextPipeline``)."""
    return GameLine(text=text, raw=text, t_mono=base.t_mono, source_id=base.source_id)


# The count: one per LineRecord the actor journals ------------------------------------------------


def count(*events: tuple[GameLine, int | None, bool]) -> int:
    counter = JournalCounter()
    for event in events:
        counter.add(*event)
    return counter.count


def test_a_line_with_an_offset_is_journalled_and_one_without_is_not():
    assert count((line("まえ", 1.0), None, False), (line("はい", 2.0), 1200, False)) == 1


def test_a_merge_into_the_last_journalled_line_is_a_replace_record_not_a_line():
    base = line("え", 1.0)
    assert count((base, 1010, False), (merged(base, "えっと…"), 1010, True)) == 1


def test_a_merge_whose_base_was_never_journalled_is_a_new_line():
    trigger, early = line("はじまり", 1.0), line("え", 1.2)  # "え" came before the auto start: not held
    assert count((trigger, 0, False), (merged(early, "えっと…"), 510, True)) == 2


def test_lines_held_for_an_auto_start_count_when_published_again_with_their_offsets():
    first, second = line("はじまり", 1.0), line("つづき", 1.4)
    assert count((first, None, False), (second, None, False), (first, 0, False), (second, 0, False)) == 2


def test_a_merge_into_the_last_held_line_after_started_is_a_replace_record():
    held = line("え", 1.0)
    assert count((held, None, False), (held, 0, False), (merged(held, "えっと"), 0, True)) == 1


def test_lines_dropped_while_paused_or_after_the_stop_are_not_counted():
    base = line("はい", 1.0)
    assert count((base, 100, False), (line("ポーズ", 2.0), None, False), (merged(base, "はいはい"), None, True)) == 1


def test_reset_starts_a_new_recording_from_zero():
    counter = JournalCounter()
    base = line("はい", 1.0)
    counter.add(base, 100, False)
    counter.reset()
    counter.add(merged(base, "はいはい"), 100, True)  # the tail was reset: a new recording's first line
    assert counter.count == 1


# The list ----------------------------------------------------------------------------------------


@pytest.fixture
def live(qtbot) -> LiveList:
    widget = LiveList()
    qtbot.addWidget(widget)
    return widget


def test_lines_are_listed_in_order_marked_by_whether_the_recording_holds_them(live):
    live.add(line("まえ", 1.0), None, False)
    live.add(line("はい", 2.0), 1200, False)
    assert live.entries() == [("まえ", None), ("はい", 1200)]
    assert live.item(0).toolTip() == "Not in a recording"
    assert live.item(1).toolTip() == "In the recording at 0:00:01"


def test_only_the_last_200_lines_are_kept(live):
    for i in range(LIVE_LINES + 5):
        live.add(line(f"line {i}", float(i)), None, False)
    assert live.count() == LIVE_LINES
    assert live.entries()[0] == ("line 5", None)


def test_a_merge_replaces_its_base_line(live):
    base = line("え", 1.0)
    live.add(base, None, False)
    live.add(line("ほか", 1.1, "agent"), None, False)
    live.add(merged(base, "えっと…"), None, True)
    assert live.entries() == [("えっと…", None), ("ほか", None)]


def test_a_held_line_published_again_is_marked_recorded_not_listed_twice(live):
    held = line("はじまり", 1.0)
    live.add(held, None, False)
    live.add(line("つぎ", 1.5), None, False)
    live.add(held, 0, False)
    assert live.entries() == [("はじまり", 0), ("つぎ", None)]


def test_held_lines_read_in_one_clock_tick_are_each_marked_once(live):
    """Windows' monotonic clock ticks every 15.6 ms: two lines of one burst can share a ``t_mono``."""
    first, second = line("はじまり", 1.0), line("つづき", 1.0)
    for held in (first, second):
        live.add(held, None, False)
    for held in (first, second):
        live.add(held, 0, False)
    assert live.entries() == [("はじまり", 0), ("つづき", 0)]


def test_a_merge_journalled_as_a_new_line_takes_its_offset(live):
    early = line("え", 1.2)
    live.add(early, None, False)
    live.add(merged(early, "えっと…"), 510, True)
    assert live.entries() == [("えっと…", 510)]


def test_a_merge_whose_base_is_gone_from_the_list_is_added(live):
    base = line("え", 0.5)
    live.add(base, None, False)
    for i in range(LIVE_LINES):
        live.add(line(f"line {i}", 1.0 + i), None, False)
    live.add(merged(base, "えっと…"), None, True)
    assert live.entries()[-1] == ("えっと…", None)
    assert live.count() == LIVE_LINES


def test_the_list_is_read_only(live):
    live.add(line("はい", 1.0), None, False)
    assert live.editTriggers() == live.EditTrigger.NoEditTriggers
