import dataclasses

import pytest

from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import COUNT_NAMES, Counts
from anki_miner_game.models.pipeline import DROP_COUNTER, Accepted, Dropped, DropReason, Replaced


def test_every_drop_reason_maps_to_a_counts_field():
    assert set(DROP_COUNTER) == set(DropReason)
    assert set(DROP_COUNTER.values()) <= COUNT_NAMES
    for reason in DropReason:
        assert Counts().incremented(DROP_COUNTER[reason]) != Counts()


def test_empty_lines_count_under_no_letters():
    assert DROP_COUNTER[DropReason.EMPTY] == "no_letters"
    assert DROP_COUNTER[DropReason.NO_LETTERS] == "no_letters"
    assert DROP_COUNTER[DropReason.JUNK] == "junk"
    assert DROP_COUNTER[DropReason.DUPLICATE] == "duplicate"


def test_drop_counter_is_read_only():
    with pytest.raises(TypeError):
        DROP_COUNTER[DropReason.JUNK] = "skip"  # type: ignore[index]


def test_results_are_frozen_values():
    line = GameLine(text="え", raw="え", t_mono=1.0, source_id="agent")
    assert Accepted(line) == Accepted(line=line)
    assert Replaced(line).line is line
    assert Dropped(DropReason.JUNK).reason is DropReason.JUNK
    with pytest.raises(dataclasses.FrozenInstanceError):
        Dropped(DropReason.JUNK).reason = DropReason.EMPTY  # type: ignore[misc]
