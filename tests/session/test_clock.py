"""Record clocks (spec 7; spec 18.1 row ``session/clock.py``)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anki_miner_game.interfaces.record_clock import RecordClock
from anki_miner_game.models.manifest import ClockKind, DriftSample
from anki_miner_game.session.clock import EventClock, OutputDurationClock


class FakeNow:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --- EventClock -------------------------------------------------------------


def test_event_offset_is_time_since_the_zero_event() -> None:
    clock = EventClock()
    clock.start(100.0)
    assert clock.offset_ms(105.23) == 5230


def test_event_capture_latency_is_added() -> None:
    clock = EventClock(capture_latency_ms=40)
    clock.start(100.0)
    assert clock.offset_ms(101.0) == 1040


def test_event_negative_capture_latency_is_subtracted_and_clamped_at_zero() -> None:
    clock = EventClock(capture_latency_ms=-40)
    clock.start(100.0)
    assert clock.offset_ms(100.01) == 0
    assert clock.offset_ms(101.0) == 960


def test_event_pause_spans_are_excluded() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    clock.resume(115.0)
    assert clock.offset_ms(120.0) == 15_000
    clock.pause(130.0)
    clock.resume(132.0)
    # (140 - 100 - (5 + 2)) s
    assert clock.offset_ms(140.0) == 33_000


def test_event_line_during_a_pause_is_none() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    assert clock.offset_ms(112.0) is None
    clock.resume(115.0)
    assert clock.offset_ms(116.0) == 11_000


def test_event_line_stamped_inside_a_past_pause_is_none() -> None:
    """A line stamped during the pause but read after the resume is still dropped."""
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    clock.resume(115.0)
    assert clock.offset_ms(113.0) is None


def test_event_line_stamped_before_a_pause_read_while_paused_keeps_its_offset() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    assert clock.offset_ms(109.5) == 9500


def test_event_pause_and_resume_edges_read_the_frozen_offset() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    assert clock.offset_ms(110.0) == 10_000
    clock.resume(115.0)
    assert clock.offset_ms(115.0) == 10_000


def test_event_line_before_the_zero_event_clamps_to_zero() -> None:
    clock = EventClock()
    clock.start(100.0)
    assert clock.offset_ms(99.5) == 0


def test_event_offsets_never_decrease() -> None:
    clock = EventClock()
    clock.start(100.0)
    assert clock.offset_ms(105.0) == 5000
    assert clock.offset_ms(104.0) == 5000


def test_event_repeated_pause_and_resume_are_ignored() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.resume(105.0)
    assert clock.offset_ms(106.0) == 6000
    clock.pause(110.0)
    clock.pause(112.0)
    clock.resume(115.0)
    clock.resume(117.0)
    assert clock.offset_ms(120.0) == 15_000


def test_event_paused_property_follows_the_edges() -> None:
    clock = EventClock()
    assert not clock.paused
    clock.start(100.0)
    assert not clock.paused
    clock.pause(110.0)
    assert clock.paused
    clock.resume(115.0)
    assert not clock.paused


def test_event_start_again_begins_a_new_recording() -> None:
    clock = EventClock()
    clock.start(100.0)
    clock.pause(105.0)
    assert clock.offset_ms(110.0) is None
    clock.start(200.0)
    assert not clock.paused
    assert clock.offset_ms(201.0) == 1000


def test_event_offset_before_start_raises() -> None:
    clock = EventClock()
    with pytest.raises(RuntimeError):
        clock.offset_ms(1.0)
    with pytest.raises(RuntimeError):
        clock.pause(1.0)
    with pytest.raises(RuntimeError):
        clock.resume(1.0)


def test_event_clock_exposes_kind_and_latency() -> None:
    assert EventClock.kind is ClockKind.EVENT
    assert EventClock(capture_latency_ms=25).capture_latency_ms == 25


# --- reading and drift samples ------------------------------------------------


def test_reading_while_paused_is_the_frozen_position() -> None:
    """A stop or a drift sample while paused needs a position, not ``None``."""
    clock = EventClock()
    clock.start(100.0)
    clock.pause(110.0)
    assert clock.reading_ms(112.0) == 10_000
    assert clock.offset_ms(112.0) is None


def test_reading_defaults_to_now() -> None:
    now = FakeNow(100.0)
    clock = EventClock(now=now)
    clock.start(100.0)
    now.t = 103.5
    assert clock.reading_ms() == 3500


def test_reading_does_not_move_the_offset_clamp() -> None:
    """Spec 7: a drift sample is a check, never input; a line queued behind a reading keeps its offset."""
    clock = EventClock()
    clock.start(100.0)
    assert clock.reading_ms(105.0) == 5000
    assert clock.offset_ms(104.0) == 4000


def test_drift_sample_does_not_move_the_offset_clamp() -> None:
    clock = EventClock(now=FakeNow(100.05))
    clock.start(100.0)
    assert clock.drift_sample(40) == DriftSample(at_ms=50, output_duration_ms=40)
    assert clock.offset_ms(100.02) == 20


def test_reading_never_falls_below_an_offset() -> None:
    """A stop reading is never before a journalled line (``build_cues`` relies on it)."""
    clock = EventClock()
    clock.start(100.0)
    assert clock.offset_ms(105.0) == 5000
    assert clock.reading_ms(104.0) == 5000


def test_drift_sample_pairs_the_reading_with_output_duration() -> None:
    clock = EventClock()
    clock.start(100.0)
    assert clock.drift_sample(4_950, 105.0) == DriftSample(at_ms=5000, output_duration_ms=4_950)


def test_drift_sample_while_paused_uses_the_frozen_position() -> None:
    now = FakeNow(112.0)
    clock = EventClock(now=now)
    clock.start(100.0)
    clock.pause(110.0)
    assert clock.drift_sample(9_900) == DriftSample(at_ms=10_000, output_duration_ms=9_900)


# --- OutputDurationClock ------------------------------------------------------


def test_output_duration_extrapolates_from_the_anchor() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 5000)
    assert clock.offset_ms(102.0) == 7000


def test_output_duration_start_anchors_at_zero() -> None:
    clock = OutputDurationClock()
    clock.start(100.0)
    assert clock.offset_ms(101.5) == 1500


def test_output_duration_reanchoring_moves_the_model() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    assert clock.offset_ms(110.0) == 10_000
    clock.anchor(120.0, 21_000)
    assert clock.offset_ms(121.0) == 22_000


def test_output_duration_reanchoring_backwards_is_clamped() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    assert clock.offset_ms(110.5) == 10_500
    clock.anchor(110.5, 10_300)  # the encoder fell further behind
    assert clock.offset_ms(110.6) == 10_500
    assert clock.offset_ms(111.0) == 10_800


def test_output_duration_line_before_a_new_anchor_uses_the_previous_one() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    clock.anchor(110.0, 10_200)
    assert clock.offset_ms(109.0) == 9000


def test_output_duration_pause_span_is_excluded() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    clock.pause(105.0)
    assert clock.offset_ms(106.0) is None
    clock.resume(110.0)
    assert clock.offset_ms(111.0) == 6000


def test_output_duration_anchor_while_paused_stays_frozen() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    clock.pause(105.0)
    clock.anchor(107.0, 5_010)
    assert clock.offset_ms(108.0) is None
    assert clock.reading_ms(108.0) == 5_010
    clock.resume(110.0)
    assert clock.offset_ms(111.0) == 6_010


def test_output_duration_reconcile_into_a_paused_recording() -> None:
    """Reconcile finds OBS paused: anchor, then pause at the same instant."""
    clock = OutputDurationClock()
    clock.anchor(100.0, 42_000)
    clock.pause(100.0)
    assert clock.paused
    assert clock.offset_ms(101.0) is None
    assert clock.reading_ms(101.0) == 42_000
    clock.resume(103.0)
    assert clock.offset_ms(104.0) == 43_000


def test_output_duration_edge_older_than_the_last_anchor_is_taken_at_the_anchor() -> None:
    clock = OutputDurationClock()
    clock.anchor(100.0, 0)
    clock.anchor(99.0, 500)
    assert clock.offset_ms(101.0) == 1500


def test_output_duration_before_any_anchor_raises() -> None:
    clock = OutputDurationClock()
    with pytest.raises(RuntimeError):
        clock.offset_ms(1.0)
    with pytest.raises(RuntimeError):
        clock.reading_ms(1.0)
    with pytest.raises(RuntimeError):
        clock.pause(1.0)


def test_output_duration_kind() -> None:
    assert OutputDurationClock.kind is ClockKind.OUTPUT_DURATION


def test_both_clocks_satisfy_the_record_clock_protocol() -> None:
    clocks: list[RecordClock] = [EventClock(), OutputDurationClock()]
    for clock in clocks:
        clock.start(10.0)
        clock.pause(11.0)
        assert clock.offset_ms(12.0) is None
        clock.resume(13.0)
        assert clock.offset_ms(14.0) == 2000


# --- properties ---------------------------------------------------------------

_times = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)


@given(
    zero=_times,
    latency=st.integers(min_value=-500, max_value=500),
    edges=st.lists(_times, max_size=8, unique=True),
    lines=st.lists(_times, max_size=30),
)
def test_event_clock_matches_the_spec_formula(
    zero: float, latency: int, edges: list[float], lines: list[float]
) -> None:
    """``offset = (t - zero - paused_total) * 1000 + capture_latency_ms``, ``None`` inside a pause."""
    edge_times = sorted(t for t in edges if t >= zero)
    spans = [
        (edge_times[i], edge_times[i + 1] if i + 1 < len(edge_times) else float("inf"))
        for i in range(0, len(edge_times), 2)
    ]
    events = [(t, 0, "pause" if i % 2 == 0 else "resume") for i, t in enumerate(edge_times)]
    events += [(t, 1, "line") for t in lines]
    clock = EventClock(capture_latency_ms=latency)
    clock.start(zero)
    for t, _, kind in sorted(events):
        if kind == "pause":
            clock.pause(t)
        elif kind == "resume":
            clock.resume(t)
        else:
            got = clock.offset_ms(t)
            if any(p < t < r for p, r in spans):
                assert got is None
            else:
                paused_total = sum(min(t, r) - p for p, r in spans if p < t)
                expected = max(0, round((t - zero - paused_total) * 1000 + latency))
                assert got is not None
                assert abs(got - expected) <= 1


@given(
    zero=_times,
    run=st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False),
    gap=st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False),
    latency=st.integers(min_value=0, max_value=500),
)
def test_pause_and_resume_edges_read_exactly_the_same_offset(zero: float, run: float, gap: float, latency: int) -> None:
    clock = EventClock(capture_latency_ms=latency)
    clock.start(zero)
    pause_at = zero + run
    clock.pause(pause_at)
    at_pause = clock.reading_ms(pause_at)
    clock.resume(pause_at + gap)
    assert clock.offset_ms(pause_at + gap) == at_pause


_op = st.one_of(
    st.tuples(st.sampled_from(["pause", "resume", "line", "reading"]), _times, st.just(0)),
    st.tuples(st.just("anchor"), _times, st.integers(min_value=0, max_value=10_000_000)),
)


@pytest.mark.parametrize("make", [EventClock, OutputDurationClock])
@given(ops=st.lists(_op, max_size=40))
def test_offsets_never_decrease_and_readings_never_fall_below_them_under_any_call_order(
    make: type[EventClock] | type[OutputDurationClock], ops: list[tuple[str, float, int]]
) -> None:
    clock = make()
    clock.start(0.0)
    last_offset = 0
    for kind, t, duration in ops:
        if kind == "pause":
            clock.pause(t)
        elif kind == "resume":
            clock.resume(t)
        elif kind == "anchor":
            if isinstance(clock, OutputDurationClock):
                clock.anchor(t, duration)
        elif kind == "reading":
            assert clock.reading_ms(t) >= last_offset >= 0
        else:
            got = clock.offset_ms(t)
            if got is not None:
                assert got >= last_offset
                last_offset = got
