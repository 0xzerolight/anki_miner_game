"""Cue rules (spec 9): journalled lines in, subtitle cues out.

Pure: no clock, file or network access. Cue ends are never journalled; they are
a function of the journal, so finalise can rebuild them after a crash.
"""

from collections.abc import Sequence

from anki_miner_game.models.config import CueSettings
from anki_miner_game.models.constants import MIN_CUE_MS, SKIP_MS
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.lines import TimedLine


def build_cues(lines: Sequence[TimedLine], stop_ms: int, shift_ms: int, cfg: CueSettings) -> list[Cue]:
    """The cues for one session, numbered from 1, each satisfying ``0 <= start < end <= next.start``.

    ``lines`` are in journal order; ``stop_ms`` is the recording's stop offset;
    ``shift_ms`` is the session's ``START_SHIFT_MS`` entry. Evaluated in the
    spec's order:

    1. ``start = max(0, prev.start, offset_ms + shift_ms)`` for every line.
    2. Drop a line shown for less than ``SKIP_MS``, with ``D`` measured to the
       immediately following line, kept or not, so a burst of click-through
       lines goes as a whole. For the last line ``D`` is measured to the stop;
       so is it for a line whose next line lies past the stop, which is the
       last line inside the recording.
    3. ``end = min(start + cap, max(next.start - end_gap_ms, min(next.start,
       start + MIN_CUE_MS)))``, with ``next`` the next *kept* line (the stop for
       the last one), so a dropped line never shortens its neighbour.

    The skip rule is the only reason a line yields no cue, so the manifest's
    ``skip`` counter is ``len(lines) - len(cues)``.
    """
    starts: list[int] = []
    start = 0
    for line in lines:
        start = max(start, line.offset_ms + shift_ms)  # start begins at 0, so this also clamps at 0
        starts.append(start)

    following = [*starts[1:], stop_ms]  # the immediately following line's start; the stop after the last
    kept = [i for i in range(len(starts)) if min(following[i], stop_ms) - starts[i] >= SKIP_MS]

    cap_ms = cfg.max_cue_seconds * 1000
    cues: list[Cue] = []
    for number, i in enumerate(kept, start=1):
        start = starts[i]
        next_start = starts[kept[number]] if number < len(kept) else stop_ms
        end = min(start + cap_ms, max(next_start - cfg.end_gap_ms, min(next_start, start + MIN_CUE_MS)))
        cues.append(Cue(index=number, start_ms=start, end_ms=end, text=lines[i].text, source_id=lines[i].source_id))
    return cues
