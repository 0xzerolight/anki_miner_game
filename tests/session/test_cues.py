"""Cue rules (spec 9; 18.1 row ``session/cues.py``)."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anki_miner_game.models.config import CueSettings
from anki_miner_game.models.constants import (
    MAX_CUE_SECONDS_MAX,
    MAX_CUE_SECONDS_MIN,
    MIN_CUE_MS,
    SKIP_MS,
    START_SHIFT_MS,
)
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.lines import TimedLine
from anki_miner_game.session.cues import build_cues

DEFAULTS = CueSettings()  # max_cue_seconds 15, end_gap_ms 350
NO_SHIFT = 0
"""The rules hold for any shift; with none, a cue starts at its line's offset, which keeps the cases readable."""
HOOK = START_SHIFT_MS["hook"]
OCR = START_SHIFT_MS["ocr"]


def lines_at(*offsets: int, source_id: str = "textractor") -> list[TimedLine]:
    return [TimedLine(offset_ms=off, text=f"line {n}", source_id=source_id) for n, off in enumerate(offsets)]


def spans(cues: list[Cue]) -> list[tuple[str, int, int]]:
    return [(c.text, c.start_ms, c.end_ms) for c in cues]


# Spec 9 table, computed with the defaults: (line shown for D, cue length, gap to next cue);
# ``None`` = dropped.
SPEC_TABLE = [
    (250, None, None),
    (320, 320, 0),
    (600, 500, 100),
    (850, 500, 350),
    (900, 550, 350),
    (4_000, 3_650, 350),
    (40_000, 15_000, 25_000),
]


@pytest.mark.parametrize(("shown_ms", "length_ms", "gap_ms"), SPEC_TABLE)
def test_spec_table_row(shown_ms, length_ms, gap_ms):
    first, second = 10_000, 10_000 + shown_ms
    cues = build_cues(lines_at(first, second), stop_ms=second + 60_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)

    if length_ms is None:
        assert spans(cues) == [("line 1", second, second + 15_000)]
        return
    head, nxt = cues
    assert (head.text, head.start_ms) == ("line 0", first)
    assert head.end_ms - head.start_ms == length_ms
    assert nxt.start_ms - head.end_ms == gap_ms
    assert nxt.start_ms == second


@pytest.mark.parametrize(("shown_ms", "kept"), [(SKIP_MS - 1, False), (SKIP_MS, True)])
def test_skip_threshold_is_exclusive(shown_ms, kept):
    cues = build_cues(lines_at(10_000, 10_000 + shown_ms), stop_ms=90_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert [c.text for c in cues] == (["line 0", "line 1"] if kept else ["line 1"])
    if kept:
        assert cues[0].end_ms == 10_000 + SKIP_MS  # the gap shrinks to 0 rather than inverting


def test_cues_are_numbered_from_one_and_carry_text_and_source():
    lines = [
        TimedLine(offset_ms=5_230, text="「こんにちは」", source_id="agent"),
        TimedLine(offset_ms=9_600, text="skipped", source_id="agent"),
        TimedLine(offset_ms=9_760, text="…え？", source_id="luna"),
    ]
    cues = build_cues(lines, stop_ms=20_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert cues == [
        Cue(index=1, start_ms=5_230, end_ms=9_410, text="「こんにちは」", source_id="agent"),
        Cue(index=2, start_ms=9_760, end_ms=19_650, text="…え？", source_id="luna"),
    ]


def test_no_lines_gives_no_cues():
    assert build_cues([], stop_ms=60_000, shift_ms=NO_SHIFT, cfg=DEFAULTS) == []


def test_burst_of_click_through_lines_is_dropped_as_a_whole():
    # Four lines 200 ms apart: each is shown for less than SKIP_MS, though the burst spans 600 ms.
    # Measuring D to the next *kept* line would rescue the second one (400 ms to the fourth).
    lines = lines_at(5_000, 10_000, 10_200, 10_400, 10_600, 20_000)
    cues = build_cues(lines, stop_ms=40_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert spans(cues) == [
        ("line 0", 5_000, 10_250),  # ends against the kept "line 4", not the dropped "line 1"
        ("line 4", 10_600, 19_650),
        ("line 5", 20_000, 35_000),
    ]


def test_a_dropped_line_never_shortens_its_neighbour():
    # "line 1" is shown for 100 ms and dropped; "line 0" ends against "line 2", the next kept line.
    lines = lines_at(5_000, 7_000, 7_100)
    cues = build_cues(lines, stop_ms=30_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 5_000, 6_750), ("line 2", 7_100, 22_100)]


@pytest.mark.parametrize(
    ("stop_ms", "expected"),
    [
        (10_299, []),  # shown for 299 ms before the stop: skip
        (10_300, [("line 0", 10_000, 10_300)]),  # never past the stop
        (10_400, [("line 0", 10_000, 10_400)]),  # the gap shrinks against the stop as against a next line
        (14_000, [("line 0", 10_000, 13_650)]),  # end gap before the stop
        (60_000, [("line 0", 10_000, 25_000)]),  # cap
    ],
)
def test_last_line_is_measured_against_the_stop_offset(stop_ms, expected):
    assert spans(build_cues(lines_at(10_000), stop_ms=stop_ms, shift_ms=NO_SHIFT, cfg=DEFAULTS)) == expected


def test_last_kept_line_ends_against_the_stop_when_later_lines_are_dropped():
    # "line 1" is shown for 200 ms before the stop and dropped; "line 0" runs to stop - end gap.
    cues = build_cues(lines_at(10_000, 12_000), stop_ms=12_200, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 10_000, 11_850)]


def test_lines_past_the_stop_are_dropped_and_no_cue_ends_past_it():
    # Spec 9 defines D against the stop only for the last line; a line whose next line lies past the
    # stop is the last line inside the recording, so it is measured against the stop too.
    cues = build_cues(lines_at(1_000, 5_000), stop_ms=900, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert cues == []
    cues = build_cues(lines_at(1_000, 5_000), stop_ms=3_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 1_000, 2_650)]


def test_hook_shift_starts_each_cue_400_ms_before_its_line_arrives():
    # A hooker delivers a line after its voice has started; ends follow the shifted starts.
    cues = build_cues(lines_at(5_000, 9_000), stop_ms=30_000, shift_ms=HOOK, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 4_600, 8_250), ("line 1", 8_600, 23_600)]


def test_hook_shift_clamps_at_zero():
    # The first line of a session, 300 ms after the recording's zero.
    cues = build_cues(lines_at(300, 5_000), stop_ms=30_000, shift_ms=HOOK, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 0, 4_250), ("line 1", 4_600, 19_600)]


def test_hook_shift_clamps_at_previous_start_and_the_earlier_line_is_dropped():
    # An auto start journals the line that started it at offset 0; the next one came 250 ms later.
    # Both starts clamp to 0; the earlier of two lines sharing a start has D = 0 and is dropped.
    cues = build_cues(lines_at(0, 250, 5_000), stop_ms=30_000, shift_ms=HOOK, cfg=DEFAULTS)
    assert spans(cues) == [("line 1", 0, 4_250), ("line 2", 4_600, 19_600)]


def test_ocr_shift_moves_starts_earlier():
    cues = build_cues(lines_at(5_000, 9_000), stop_ms=30_000, shift_ms=OCR, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 4_000, 7_650), ("line 1", 8_000, 23_000)]


def test_ocr_shift_clamps_at_zero():
    cues = build_cues(lines_at(400, 5_000), stop_ms=30_000, shift_ms=OCR, cfg=DEFAULTS)
    assert spans(cues) == [("line 0", 0, 3_650), ("line 1", 4_000, 19_000)]


def test_ocr_shift_clamps_at_previous_start_and_the_earlier_line_is_dropped():
    # Both starts clamp to 0; the earlier of two lines sharing a start has D = 0 and is dropped.
    cues = build_cues(lines_at(200, 700, 5_000), stop_ms=30_000, shift_ms=OCR, cfg=DEFAULTS)
    assert spans(cues) == [("line 1", 0, 3_650), ("line 2", 4_000, 19_000)]


@pytest.mark.parametrize(
    ("shift_ms", "expected"),
    [
        (NO_SHIFT, [("line 1", 5_000, 8_650), ("line 2", 9_000, 24_000)]),
        (HOOK, [("line 1", 4_600, 8_250), ("line 2", 8_600, 23_600)]),
    ],
)
def test_start_never_moves_before_the_previous_start(shift_ms, expected):
    # Offsets are clamped non-decreasing by the record clock; the cue builder does not rely on it.
    cues = build_cues(lines_at(5_000, 4_000, 9_000), stop_ms=30_000, shift_ms=shift_ms, cfg=DEFAULTS)
    assert spans(cues) == expected


def test_settings_change_cap_and_gap():
    cfg = CueSettings(max_cue_seconds=5, end_gap_ms=0)
    cues = build_cues(lines_at(1_000, 3_000, 30_000), stop_ms=60_000, shift_ms=NO_SHIFT, cfg=cfg)
    assert spans(cues) == [("line 0", 1_000, 3_000), ("line 1", 3_000, 8_000), ("line 2", 30_000, 35_000)]


TEXTS = ["え", "えっと…", "「はい」", "line"]


@st.composite
def cue_inputs(draw):
    first = draw(st.integers(min_value=0, max_value=100_000))
    # Small gaps hit the skip and minimum-length boundaries; large ones hit the cap.
    gaps = draw(st.lists(st.one_of(st.integers(0, 1_200), st.integers(0, 90_000)), max_size=40))
    offsets = list(itertools.accumulate(gaps, initial=first))
    texts = draw(st.lists(st.sampled_from(TEXTS), min_size=len(offsets), max_size=len(offsets)))
    lines = [TimedLine(offset_ms=o, text=t, source_id="src") for o, t in zip(offsets, texts, strict=True)]
    stop_ms = draw(st.one_of(st.integers(-1_000, offsets[-1] + 120_000), st.just(offsets[-1])))
    shift_ms = draw(st.one_of(st.sampled_from(sorted(START_SHIFT_MS.values())), st.integers(-5_000, 0)))
    cfg = CueSettings(
        max_cue_seconds=draw(st.integers(MAX_CUE_SECONDS_MIN, MAX_CUE_SECONDS_MAX)),
        end_gap_ms=draw(st.integers(0, 3_000)),
    )
    return lines, stop_ms, shift_ms, cfg


@given(cue_inputs())
def test_invariant_holds_for_arbitrary_non_decreasing_offsets(case):
    lines, stop_ms, shift_ms, cfg = case
    cues = build_cues(lines, stop_ms=stop_ms, shift_ms=shift_ms, cfg=cfg)

    assert [c.index for c in cues] == list(range(1, len(cues) + 1))
    for cue, nxt in itertools.pairwise(cues):
        assert 0 <= cue.start_ms < cue.end_ms <= nxt.start_ms
    for cue in cues:
        assert SKIP_MS <= cue.end_ms - cue.start_ms <= cfg.max_cue_seconds * 1000
        assert cue.end_ms <= stop_ms
    # Cues keep the lines' order and text: every cue is one line, and no line yields two cues.
    remaining = iter(lines)
    for cue in cues:
        assert any(line.text == cue.text and line.offset_ms + shift_ms <= cue.start_ms for line in remaining)


def test_minimum_cue_length_applies_when_the_next_start_allows():
    # D = 700: the formula keeps MIN_CUE_MS rather than the 350 ms end gap when both cannot hold.
    cues = build_cues(lines_at(0, 700), stop_ms=60_000, shift_ms=NO_SHIFT, cfg=DEFAULTS)
    assert cues[0].end_ms == MIN_CUE_MS
