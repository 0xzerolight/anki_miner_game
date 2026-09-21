import dataclasses

import pytest

from anki_miner_game.models.cue import Cue, Region
from anki_miner_game.models.lines import GameLine, TimedLine


def test_game_line_fields_and_frozen():
    line = GameLine(text="こんにちは", raw="【太郎】こんにちは", t_mono=12.5, source_id="textractor")
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.text = "x"  # type: ignore[misc]
    changed = dataclasses.replace(line, text="やあ")
    assert changed.text == "やあ"
    assert changed.raw == line.raw
    assert line.text == "こんにちは"


def test_timed_line_fields():
    assert TimedLine(offset_ms=5230, text="…", source_id="agent") == TimedLine(5230, "…", "agent")


def test_cue_accepts_a_valid_span():
    cue = Cue(index=1, start_ms=0, end_ms=1, text="a", source_id="luna")
    assert (cue.start_ms, cue.end_ms) == (0, 1)


@pytest.mark.parametrize(
    ("index", "start", "end"),
    [(0, 0, 10), (1, -1, 10), (1, 10, 10), (1, 11, 10)],
)
def test_cue_rejects_a_broken_invariant(index, start, end):
    with pytest.raises(ValueError, match="cue"):
        Cue(index=index, start_ms=start, end_ms=end, text="a", source_id="luna")


def test_cue_replace_revalidates():
    cue = Cue(index=1, start_ms=100, end_ms=200, text="a", source_id="luna")
    with pytest.raises(ValueError):
        dataclasses.replace(cue, end_ms=100)


@pytest.mark.parametrize(("start", "end"), [(-1, 5), (5, 5), (6, 5)])
def test_region_rejects_an_empty_or_negative_span(start, end):
    with pytest.raises(ValueError, match="region"):
        Region(start_ms=start, end_ms=end)


def test_region_valid():
    assert Region(5310, 8920) == Region(start_ms=5310, end_ms=8920)
