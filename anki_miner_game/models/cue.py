"""Subtitle cues and VAD regions (spec 5, 9, 13)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    """One subtitle cue.

    Checked here: ``index >= 1`` and ``0 <= start_ms < end_ms``. The rule across
    cues, ``end_ms <= next.start_ms``, is the cue builder's job (spec 9).
    """

    index: int
    """1-based, final numbering."""
    start_ms: int
    end_ms: int
    text: str
    source_id: str

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError(f"cue index must be >= 1, got {self.index}")
        if not 0 <= self.start_ms < self.end_ms:
            raise ValueError(f"cue needs 0 <= start_ms < end_ms, got {self.start_ms}..{self.end_ms}")


@dataclass(frozen=True)
class Region:
    """A voiced span reported by the VAD worker (spec 13.2)."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_ms < self.end_ms:
            raise ValueError(f"region needs 0 <= start_ms < end_ms, got {self.start_ms}..{self.end_ms}")
