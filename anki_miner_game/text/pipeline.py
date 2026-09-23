"""Text pipeline (spec 8.2): cleans each received line and decides whether it is kept.

One instance per game session, fed from the session actor's thread only; it keeps the previous
accepted line for the duplicate check (step 8) and the typewriter merge (step 9).

"The previous accepted line" means the previous line journalled in this recording. The pipeline
cannot see the journal, so the actor calls ``reset`` whenever the line it accepted last will not be
journalled: after dropping an accepted line as ``paused``, at ``STARTED`` (unless the auto-start
line held while armed is the one journalled at offset 0), and after a split stop. A ``Replaced``
is journalled as a ``ReplaceRecord`` only when its base line is the journal's last ``LineRecord``;
otherwise as a new ``LineRecord`` at ``clock.offset_ms(line.t_mono)`` (``None``: dropped as ``paused``).
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

# Max length of a colon-prefixed name (below): long enough for a full name plus an honorific or
# title (e.g. "岡部倫太郎", "ドクター中鉢"), short enough that an unrelated sentence fragment
# ending in ": <quote>" is not mistaken for one.
_SPEAKER_NAME_MAX = 16

# A name has no whitespace, colon or bracket character, so a real sentence such as "注意：これは…"
# (no bracket follows the colon) or "A: B" (no bracket at all) can never match up to that point.
_SPEAKER_NAME_CHARS = r"[^\s:：【】「」『』()（）\"]"

_SPEAKER = re.compile(
    r"^(?:"
    r"【[^】]*】\s*"  # 【name】 group (LunaTranslator and similar)
    rf"|{_SPEAKER_NAME_CHARS}{{1,{_SPEAKER_NAME_MAX}}}[:：]\s*(?=[「『（\"])"  # name: (Agent and similar)
    r")"
)


def _normalise(raw: str, *, speaker_strip: bool) -> str:
    """Steps 1-4: NFC, drop control and zero-width characters, collapse whitespace, strip speaker.

    Step 2 also drops lone surrogates (``Cs``): ``json.loads`` keeps a ``\\udXXX`` escape from a
    UTF-16 pair cut in half, and such a string cannot be encoded to write the journal, the subtitle
    or the feed.

    The speaker strip covers two prefix shapes: a bracketed ``【name】`` group (LunaTranslator), and
    a bare ``name:``/``name：`` prefix (Agent's STEINS;GATE script) that is only removed when it is
    immediately followed by an opening quote bracket, so a real line containing a colon (``注意：
    これは…``, ``A: B``) is left untouched.
    """
    text = unicodedata.normalize("NFC", raw)
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
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

    def reset(self) -> None:
        """Forget the previous accepted line: the next line is neither a duplicate of it nor merged into it."""
        self._previous = None

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
