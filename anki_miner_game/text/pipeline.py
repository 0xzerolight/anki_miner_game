"""Text pipeline (spec 8.2): cleans each received line and decides whether it is kept.

One instance per game session, fed from the session actor's thread only; it keeps the previous
accepted line for the duplicate check (step 8) and the typewriter merge (step 9).
"""

import re
import unicodedata

from anki_miner_game.models.constants import MAX_LINE_CHARS, TYPEWRITER_WINDOW_S
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import LineReceived
from anki_miner_game.models.pipeline import (
    Accepted,
    Dropped,
    DropReason,
    PipelineResult,
    Replaced,
)
from anki_miner_game.models.profile import FilterSettings

_SPEAKER = re.compile(r"^【[^】]*】\s*")


def _normalise(raw: str, *, speaker_strip: bool) -> str:
    """Steps 1-4: NFC, drop control and zero-width characters, collapse whitespace, strip speaker."""
    text = unicodedata.normalize("NFC", raw)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf"))
    text = " ".join(text.split())
    if speaker_strip:
        text = _SPEAKER.sub("", text, count=1)
    return text


class TextPipeline:
    def __init__(self, filters: FilterSettings) -> None:
        self._filters = filters
        self._previous: GameLine | None = None
        """The previous accepted line, holding merged text and the first frame's ``t_mono``."""
        self._previous_arrival = 0.0
        """``t_mono`` of the latest frame accepted or merged: the typewriter window runs from it."""

    def process(self, msg: LineReceived) -> PipelineResult:
        text = _normalise(msg.raw, speaker_strip=self._filters.speaker_strip)
        if not text:
            return Dropped(DropReason.EMPTY)
        if not any(ch.isalpha() for ch in text):
            return Dropped(DropReason.NO_LETTERS)
        if len(text) > MAX_LINE_CHARS:
            return Dropped(DropReason.JUNK)
        previous = self._previous
        if previous is not None and text == previous.text:
            return Dropped(DropReason.DUPLICATE)

        if (
            self._filters.typewriter_merge
            and previous is not None
            and len(text) > len(previous.text)
            and text.startswith(previous.text)
            and msg.t_mono - self._previous_arrival <= TYPEWRITER_WINDOW_S
        ):
            merged = GameLine(text=text, raw=msg.raw, t_mono=previous.t_mono, source_id=previous.source_id)
            self._previous = merged
            self._previous_arrival = msg.t_mono
            return Replaced(merged)

        line = GameLine(text=text, raw=msg.raw, t_mono=msg.t_mono, source_id=msg.source_id)
        self._previous = line
        self._previous_arrival = msg.t_mono
        return Accepted(line)
