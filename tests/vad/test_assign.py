"""vad/assign.py: live cues onto voiced regions (spec 13.3, 18.1 row ``vad/assign.py``)."""

from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anki_miner_game.models.config import CueSettings
from anki_miner_game.models.constants import MIN_CUE_MS
from anki_miner_game.models.cue import Cue, Region
from anki_miner_game.models.profile import TextMode
from anki_miner_game.vad.assign import (
    CHAIN_GAP_MS,
    END_PAD_MS,
    TAIL_SKIP_MS,
    WINDOW_LEAD_MS,
    WINDOW_MAX_MS,
    assign,
)

CFG = CueSettings()
HOOK = TextMode.HOOK
OCR = TextMode.OCR


def cue(index: int, start_ms: int, end_ms: int) -> Cue:
    return Cue(index=index, start_ms=start_ms, end_ms=end_ms, text=f"line {index}", source_id="textractor")


def spans(cues: list[Cue]) -> list[tuple[int, int]]:
    return [(c.start_ms, c.end_ms) for c in cues]


# Spec 13.3 worked example, hook mode: R1 5.31-8.92 s, R2 9.60-11.05 s, R3 14.2-16.0 s.
EXAMPLE_CUES = [cue(1, 5230, 13850), cue(2, 14200, 29200)]
EXAMPLE_REGIONS = [Region(5310, 8920), Region(9600, 11050), Region(14200, 16000)]


def test_thresholds_are_the_spec_values():
    assert (WINDOW_LEAD_MS, WINDOW_MAX_MS, CHAIN_GAP_MS, TAIL_SKIP_MS, END_PAD_MS) == (200, 30_000, 1_500, 2_000, 150)


def test_worked_example():
    out = assign(EXAMPLE_CUES, EXAMPLE_REGIONS, HOOK, CFG)

    # Cue 1: window 5.03-14.20, chain R1 + R2 (gap 0.68 s), end 11.05 + 0.15. Cue 2: chain R3, end 16.15.
    assert spans(out) == [(5230, 11200), (14200, 16150)]
    assert [(c.index, c.text, c.source_id) for c in out] == [(1, "line 1", "textractor"), (2, "line 2", "textractor")]


def test_region_order_does_not_matter():
    assert assign(EXAMPLE_CUES, EXAMPLE_REGIONS[::-1], HOOK, CFG) == assign(EXAMPLE_CUES, EXAMPLE_REGIONS, HOOK, CFG)


def test_end_gap_comes_from_cfg():
    out = assign(EXAMPLE_CUES, EXAMPLE_REGIONS, HOOK, CueSettings(end_gap_ms=5000))

    assert out[0].end_ms == 14200 - 5000


def test_no_live_cues():
    assert assign([], EXAMPLE_REGIONS, HOOK, CFG) == []


@pytest.mark.parametrize("mode", [HOOK, OCR])
def test_no_regions_keeps_live_cues(mode):
    assert assign(EXAMPLE_CUES, [], mode, CFG) == EXAMPLE_CUES


# ---- step 1: chains -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("region", "end_ms"),
    [
        (Region(10000 - WINDOW_LEAD_MS, 11000), 11000 + END_PAD_MS),  # begins on the window's start
        (Region(9000, 10000 - WINDOW_LEAD_MS), 25000),  # ends on it: not in progress, no chain
    ],
)
def test_window_opens_before_the_live_start(region, end_ms):
    [out] = assign([cue(1, 10000, 25000)], [region], HOOK, CFG)

    assert out.end_ms == end_ms


@pytest.mark.parametrize(
    ("gap_ms", "end_ms"),
    [(CHAIN_GAP_MS, 11000 + CHAIN_GAP_MS + 500 + END_PAD_MS), (CHAIN_GAP_MS + 1, 11000 + END_PAD_MS)],
)
def test_chain_follows_regions_across_gaps_up_to_the_limit(gap_ms, end_ms):
    regions = [Region(10100, 11000), Region(11000 + gap_ms, 11000 + gap_ms + 500)]

    [out] = assign([cue(1, 10000, 25000)], regions, HOOK, CFG)

    assert out.end_ms == end_ms


def test_window_closes_after_its_maximum_and_a_cue_may_outgrow_the_live_cap():
    cues = [cue(1, 0, 15000), cue(2, 60000, 65000)]

    grown = assign(cues, [Region(WINDOW_MAX_MS - 1000, 33000)], HOOK, CFG)
    outside = assign(cues, [Region(WINDOW_MAX_MS, 33000)], HOOK, CFG)

    assert grown[0].end_ms == 33000 + END_PAD_MS
    assert outside == cues


@pytest.mark.parametrize(
    ("regions", "first_end_ms"),
    [
        ([Region(20000, 22000)], 15000),  # cannot start cue 1's chain
        ([Region(18000, 19000), Region(20000, 22000)], 19000 + END_PAD_MS),  # nor extend it (gap 1 s)
    ],
)
def test_region_starting_at_the_next_cue_start_belongs_to_the_next_cue(regions, first_end_ms):
    cues = [cue(1, 0, 15000), cue(2, 20000, 25000)]

    out = assign(cues, regions, HOOK, CFG)

    assert spans(out) == [(0, first_end_ms), (20000, 22000 + END_PAD_MS)]


def test_region_in_progress_at_window_start_is_skipped_when_another_begins_within_2_s():
    # Window starts at 9800. A is still sounding there (the previous voice's tail); B begins 1.7 s in,
    # 1.6 s after A ends, so B is not chained to A: the result tells which one the chain started from.
    regions = [Region(9000, 9900), Region(11500, 13000)]

    [out] = assign([cue(1, 10000, 25000)], regions, HOOK, CFG)

    assert out.end_ms == 13000 + END_PAD_MS


def test_region_in_progress_at_window_start_is_kept_when_nothing_else_begins_within_2_s():
    regions = [Region(9000, 12000), Region(12300, 13000)]  # B begins 2.5 s after the window's start

    [out] = assign([cue(1, 10000, 25000)], regions, HOOK, CFG)

    assert out.end_ms == 13000 + END_PAD_MS  # chain A, B (gap 0.3 s)


@pytest.mark.parametrize(
    ("other_start", "end_ms"),
    [
        (9800 + TAIL_SKIP_MS, 9800 + TAIL_SKIP_MS + 1000 + END_PAD_MS),  # within 2 s: A skipped, chain B
        (9800 + TAIL_SKIP_MS + 1, 10000 + MIN_CUE_MS),  # not within: chain A, whose end + pad is floored
    ],
)
def test_skip_rule_boundary(other_start, end_ms):
    regions = [Region(9000, 9900), Region(other_start, other_start + 1000)]

    [out] = assign([cue(1, 10000, 25000)], regions, HOOK, CFG)

    assert out.end_ms == end_ms


# ---- step 2: starts ----------------------------------------------------------------------------------

# Cue 2's voice (R2) starts at 9.70 s, after cue 1's live end (9.65 s) and before cue 2's shifted OCR
# start (10.00 s). R2 is also inside cue 1's window and 1.2 s after R1, so step 1 chains it to cue 1.
SNAP_CUES = [cue(1, 5000, 9650), cue(2, 10000, 15000)]
SNAP_REGIONS = [Region(5100, 8500), Region(9700, 11500)]


def test_hook_mode_never_moves_starts():
    out = assign(SNAP_CUES, SNAP_REGIONS, HOOK, CFG)

    assert spans(out) == [(5000, 10000 - CFG.end_gap_ms), (10000, 11500 + END_PAD_MS)]


def test_ocr_start_snap_claims_a_region_from_the_previous_chain():
    out = assign(SNAP_CUES, SNAP_REGIONS, OCR, CFG)

    # Cue 2 starts at R2; cue 1's chain loses R2 and ends on R1 (not on 9.70 - 0.35).
    assert spans(out) == [(5000, 8500 + END_PAD_MS), (9700, 11500 + END_PAD_MS)]


@pytest.mark.parametrize(("region_start", "start_ms"), [(9650, 9650), (9649, 10000)])
def test_ocr_snap_interval_opens_at_the_previous_live_end(region_start, start_ms):
    out = assign(SNAP_CUES, [Region(region_start, 12000)], OCR, CFG)

    assert out[1].start_ms == start_ms
    assert out[1].end_ms == 12000 + END_PAD_MS


def test_ocr_snap_takes_the_latest_region_in_the_interval():
    regions = [Region(9660, 9800), Region(9900, 12000)]

    out = assign(SNAP_CUES, regions, OCR, CFG)

    assert out[1].start_ms == 9900


def test_ocr_first_cue_snaps_back_to_the_session_start():
    [out] = assign([cue(1, 5000, 9000)], [Region(1000, 2000), Region(3000, 6000)], OCR, CFG)

    assert (out.start_ms, out.end_ms) == (3000, 6000 + END_PAD_MS)


def test_ocr_snap_overrides_the_skip_rule():
    # A starts inside [9.65, 10.00] and is in progress at the window's start (9.80); B begins 1.7 s
    # into the window. Hook mode skips A for B; OCR mode snaps to A.
    regions = [Region(9700, 9900), Region(11500, 13000)]

    hook = assign(SNAP_CUES, regions, HOOK, CFG)
    ocr = assign(SNAP_CUES, regions, OCR, CFG)

    assert (hook[1].start_ms, hook[1].end_ms) == (10000, 13000 + END_PAD_MS)
    assert (ocr[1].start_ms, ocr[1].end_ms) == (9700, 9700 + MIN_CUE_MS)


def test_ocr_without_a_snap_region_keeps_the_skip_rule():
    # A began before cue 1's live end, so it cannot be snapped; B begins after cue 2's start.
    regions = [Region(9000, 9900), Region(11500, 13000)]

    out = assign(SNAP_CUES, regions, OCR, CFG)

    assert (out[1].start_ms, out[1].end_ms) == (10000, 13000 + END_PAD_MS)


# ---- step 3: ends ------------------------------------------------------------------------------------


def test_end_clamps_against_a_snapped_next_start():
    # Cue 1's voice runs to 9.60 s; cue 2 snaps from 10.00 to 9.66 s, so cue 1 ends 0.35 s before that.
    regions = [Region(5100, 9600), Region(9660, 11000)]

    out = assign(SNAP_CUES, regions, OCR, CFG)

    assert spans(out) == [(5000, 9660 - CFG.end_gap_ms), (9660, 11000 + END_PAD_MS)]


def test_end_is_at_least_min_cue_after_the_start():
    out = assign([cue(1, 10000, 19650), cue(2, 20000, 25000)], [Region(9900, 10100)], HOOK, CFG)

    assert out[0].end_ms == 10000 + MIN_CUE_MS


def test_end_never_passes_the_next_start():
    # Next line 0.32 s later: neither the pad nor the minimum length fits, the end meets the next start.
    out = assign([cue(1, 10000, 10320), cue(2, 10320, 15000)], [Region(9900, 10100)], HOOK, CFG)

    assert spans(out) == [(10000, 10320), (10320, 15000)]


def test_last_cue_has_only_the_minimum_length_floor():
    [out] = assign([cue(1, 10000, 25000)], [Region(9900, 10100)], HOOK, CFG)

    assert out.end_ms == 10000 + MIN_CUE_MS


# ---- invariant ---------------------------------------------------------------------------------------


@st.composite
def scenarios(draw):
    """Live cues that hold the section 9 invariant, and arbitrary regions around them."""
    n = draw(st.integers(min_value=0, max_value=10))
    starts = [draw(st.integers(min_value=0, max_value=3000))]
    for _ in range(n - 1):
        starts.append(starts[-1] + draw(st.integers(min_value=1, max_value=40_000)))
    cues = []
    for i, start in enumerate(starts[:n]):
        limit = starts[i + 1] if i + 1 < n else start + 60_000
        cues.append(cue(i + 1, start, draw(st.integers(min_value=start + 1, max_value=limit))))
    span = (cues[-1].end_ms if cues else 0) + 5000
    regions = draw(
        st.lists(
            st.builds(
                lambda start, length: Region(start, start + length),
                st.integers(min_value=0, max_value=span),
                st.integers(min_value=1, max_value=8000),
            ),
            max_size=30,
        )
    )
    return cues, regions


@given(scenarios(), st.sampled_from([HOOK, OCR]), st.integers(min_value=0, max_value=2000))
def test_invariant_holds(scenario, mode, end_gap_ms):
    cues, regions = scenario

    out = assign(cues, regions, mode, CueSettings(end_gap_ms=end_gap_ms))

    assert [(c.index, c.text, c.source_id) for c in out] == [(c.index, c.text, c.source_id) for c in cues]
    assert all(0 <= c.start_ms < c.end_ms for c in out)
    for before, after in pairwise(out):
        assert before.end_ms <= after.start_ms
    for i, (live, final) in enumerate(zip(cues, out, strict=True)):
        if mode == HOOK:
            assert final.start_ms == live.start_ms
        else:
            assert (cues[i - 1].end_ms if i else 0) <= final.start_ms <= live.start_ms
    assert assign(cues, regions[::-1], mode, CueSettings(end_gap_ms=end_gap_ms)) == out


@given(scenarios(), st.sampled_from([HOOK, OCR]))
def test_no_regions_is_the_identity(scenario, mode):
    cues, _ = scenario

    assert assign(cues, [], mode, CFG) == cues
