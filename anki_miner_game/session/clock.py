"""Record clocks: a line's ``t_mono`` to its offset in the recording (spec 7).

Both clocks model the recording position as a piecewise-linear function of
``time.monotonic()``: a list of breakpoints ``(mono, value_ms, running)``.
Between breakpoints the position advances one millisecond per millisecond
while running and stays frozen while paused. ``EventClock`` gets its first
breakpoint from the zero event; ``OutputDurationClock`` from each
``GetRecordStatus.outputDuration`` anchor. Pause and resume edges are
breakpoints too, so the offset at a pause edge and at the following resume
edge is the same float, and a line stamped inside a past pause is still
dropped after the resume.

Edges (start, pause, resume, anchor) are expected in time order; one older
than the latest breakpoint is taken at that breakpoint. Every reading is
clamped to be non-negative and non-decreasing across calls. Neither clock does
I/O or schedules anything: the session actor feeds the events and samples.
"""

import time
from bisect import bisect_right
from collections.abc import Callable
from typing import ClassVar, NamedTuple

from anki_miner_game.models.manifest import ClockKind, DriftSample


class _Breakpoint(NamedTuple):
    mono: float
    value_ms: float
    """Recording position at ``mono``."""
    running: bool
    """Whether the position advances after ``mono``."""


class _PiecewiseClock:
    kind: ClassVar[ClockKind]

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._points: list[_Breakpoint] = []
        self._last_ms = 0

    @property
    def paused(self) -> bool:
        return bool(self._points) and not self._points[-1].running

    def pause(self, at_mono: float) -> None:
        """Pause edge (``OBS_WEBSOCKET_OUTPUT_PAUSED``); ignored while already paused."""
        last = self._last_point()
        if not last.running:
            return
        at_mono = max(at_mono, last.mono)
        self._points.append(_Breakpoint(at_mono, last.value_ms + (at_mono - last.mono) * 1000, running=False))

    def resume(self, at_mono: float) -> None:
        """Resume edge (``OBS_WEBSOCKET_OUTPUT_RESUMED``); ignored while running."""
        last = self._last_point()
        if last.running:
            return
        self._points.append(_Breakpoint(max(at_mono, last.mono), last.value_ms, running=True))

    def offset_ms(self, t_mono: float) -> int | None:
        """A line's offset; ``None`` when ``t_mono`` falls inside a pause (the line is dropped and counted).

        A line stamped exactly on a pause or resume edge gets the edge's offset.
        """
        value_ms, inside_pause = self._locate(t_mono)
        return None if inside_pause else self._clamp(value_ms)

    def reading_ms(self, t_mono: float | None = None) -> int:
        """The recording position at ``t_mono`` (default ``now()``); while paused, where the pause froze it.

        For stop offsets and drift samples, which need a position even while paused.
        """
        value_ms, _ = self._locate(self._now() if t_mono is None else t_mono)
        return self._clamp(value_ms)

    def drift_sample(self, output_duration_ms: int, t_mono: float | None = None) -> DriftSample:
        """Pair ``GetRecordStatus.outputDuration`` with this clock's reading at ``t_mono`` (default ``now()``).

        Stored in ``clock.drift_samples`` as a check, never as input (spec 7).
        """
        return DriftSample(at_ms=self.reading_ms(t_mono), output_duration_ms=output_duration_ms)

    def _last_point(self) -> _Breakpoint:
        if not self._points:
            raise RuntimeError(f"{type(self).__name__} has no zero yet: call start() first")
        return self._points[-1]

    def _locate(self, t_mono: float) -> tuple[float, bool]:
        """The position at ``t_mono`` and whether ``t_mono`` is strictly inside a pause.

        Before the first breakpoint the position extrapolates backwards from it.
        """
        self._last_point()
        i = bisect_right(self._points, t_mono, key=lambda p: p.mono) - 1
        point = self._points[max(i, 0)]
        if point.running or i < 0:
            return point.value_ms + (t_mono - point.mono) * 1000, False
        return point.value_ms, t_mono != point.mono

    def _clamp(self, value_ms: float) -> int:
        self._last_ms = max(self._last_ms, round(value_ms))
        return self._last_ms


class EventClock(_PiecewiseClock):
    """Primary clock: ``offset = (t_mono - zero_mono - paused_total) * 1000 + capture_latency_ms``.

    ``zero_mono`` is the monotonic time of the zero event; pause edges come from
    the ``OBS_WEBSOCKET_OUTPUT_PAUSED`` and ``_RESUMED`` events. Which event is
    the zero, and ``capture_latency_ms``, are M0 measurements (default
    ``STARTED`` and 0).
    """

    kind = ClockKind.EVENT

    def __init__(self, capture_latency_ms: int = 0, *, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__(now=now)
        self._capture_latency_ms = capture_latency_ms

    @property
    def capture_latency_ms(self) -> int:
        return self._capture_latency_ms

    def start(self, zero_mono: float) -> None:
        """The zero event; starts a new recording, forgetting any earlier one."""
        self._points = [_Breakpoint(zero_mono, float(self._capture_latency_ms), running=True)]
        self._last_ms = 0


class OutputDurationClock(_PiecewiseClock):
    """Fallback clock, used only when reconcile finds the event history incomplete (spec 6.3).

    Anchors on ``(monotonic midpoint of the GetRecordStatus round trip,
    outputDuration)``; the actor re-anchors every 10 s. ``outputDuration``
    trails capture by the encoder's latency, which is why it is the fallback.
    """

    kind = ClockKind.OUTPUT_DURATION

    def start(self, zero_mono: float) -> None:
        """A recording that starts at ``zero_mono``: a fresh anchor at duration 0."""
        self._points = []
        self._last_ms = 0
        self.anchor(zero_mono, 0)

    def anchor(self, mono_mid: float, output_duration_ms: int) -> None:
        """Re-anchor: the position at ``mono_mid`` is ``output_duration_ms``.

        Readings at or after ``mono_mid`` follow the new anchor; an earlier
        ``t_mono`` still reads from the previous one. While paused the new
        position stays frozen until ``resume``.
        """
        if not self._points:
            self._points.append(_Breakpoint(mono_mid, float(output_duration_ms), running=True))
            return
        last = self._points[-1]
        self._points.append(_Breakpoint(max(mono_mid, last.mono), float(output_duration_ms), last.running))
