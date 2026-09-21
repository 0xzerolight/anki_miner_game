"""Record clock Protocol (spec 7)."""

from typing import Protocol


class RecordClock(Protocol):
    """Maps a line's ``t_mono`` to its offset in the recording.

    All times are ``time.monotonic()`` values. ``offset_ms`` is clamped to be
    non-negative and non-decreasing across calls, so it is stateful; it
    returns ``None`` while paused (the line is dropped and counted).
    """

    def start(self, zero_mono: float) -> None: ...

    def pause(self, at_mono: float) -> None: ...

    def resume(self, at_mono: float) -> None: ...

    def offset_ms(self, t_mono: float) -> int | None: ...
