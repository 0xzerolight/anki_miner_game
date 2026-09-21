"""Text lines on their way from a source to the cue builder (spec 5, 9)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GameLine:
    text: str
    """After the pipeline (spec 8.2)."""
    raw: str
    """As received from the source."""
    t_mono: float
    """``time.monotonic()`` read inside the source at frame receipt; nothing downstream re-stamps it."""
    source_id: str
    """``"textractor"``, ``"agent"``, ``"luna"``, ``"clipboard"``, ``"ocr"``, or a user source id."""


@dataclass(frozen=True)
class TimedLine:
    """A journalled line: its record-clock offset in place of a monotonic time. Input of ``build_cues``."""

    offset_ms: int
    text: str
    source_id: str
