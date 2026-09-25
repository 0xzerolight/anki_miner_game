"""VAD assignment: fit live cues to the voiced regions of the recording (spec 13.3).

A pure function: no clock, file or network access. The trimmer runs it over the manifest's
``live_cues`` and the worker's regions; every threshold is a module constant, tuned in M3 against
real recordings, never a setting.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import replace
from typing import Final

from anki_miner_game.models.config import CueSettings
from anki_miner_game.models.constants import MIN_CUE_MS
from anki_miner_game.models.cue import Cue, Region
from anki_miner_game.models.profile import TextMode

WINDOW_LEAD_MS: Final = 200
"""A cue's window opens this long before its live start."""
WINDOW_MAX_MS: Final = 30_000
"""A cue's window closes at the next cue's live start, or this long after its own start if sooner."""
CHAIN_GAP_MS: Final = 2_000
"""Longest silence between two regions of one chain; keeps a dramatic pause inside one voiced line."""
TAIL_SKIP_MS: Final = 2_000
"""A region still sounding at the window's start is skipped when another begins within this long."""
END_PAD_MS: Final = 150
"""Added to the end of a cue's chain."""
SNAP_LOOKBACK_MS: Final = 10_000
"""OCR mode: how far before its live start a cue's start may snap back. Covers a line's typewriter
effect before owocr sees it settle; keeps title music or a sound effect long before the line from
being taken. Provisional, tuned at T32 against real OCR sessions."""


def assign(live_cues: Sequence[Cue], regions: Sequence[Region], text_mode: TextMode, cfg: CueSettings) -> list[Cue]:
    """Move cue ends (and, in OCR mode, starts) onto voiced regions.

    ``live_cues`` are the cues before the VAD pass, in order and holding the section 9 invariant;
    ``regions`` are the worker's, in any order. Returns one cue per live cue with the same index,
    text and source; a cue whose chain is empty keeps its live start and end, except that when the
    next cue's start snapped back (OCR) its live end gets the same end clamp as a chain's end.

    Windows are half-open: a region that begins exactly at the next cue's live start belongs to the
    next cue. The last cue's end has no clamp but the ``MIN_CUE_MS`` floor.
    """
    ordered = sorted(regions, key=lambda r: (r.start_ms, r.end_ms))
    starts = [r.start_ms for r in ordered]
    count = len(live_cues)

    # Steps 1 and 2 together: a start snap edits only the previous cue's chain, which is final by then.
    chains: list[list[int]] = []
    final_starts: list[int] = []
    for i, cue in enumerate(live_cues):
        window_start = cue.start_ms - WINDOW_LEAD_MS
        window_end = cue.start_ms + WINDOW_MAX_MS
        if i + 1 < count:
            window_end = min(window_end, live_cues[i + 1].start_ms)
        first = _chain_start(ordered, starts, window_start, window_end)
        start = cue.start_ms
        if text_mode == TextMode.OCR:
            # The latest region starting inside [max(prev.live_end, start - SNAP_LOOKBACK_MS), start].
            floor = max(live_cues[i - 1].end_ms if i else 0, cue.start_ms - SNAP_LOOKBACK_MS)
            snap = bisect_right(starts, cue.start_ms) - 1
            if snap >= 0 and starts[snap] >= floor:
                first = snap
                start = starts[snap]
                if i:
                    chains[i - 1] = [k for k in chains[i - 1] if k != snap]
        chains.append([] if first is None else _follow(ordered, first, window_start, window_end))
        final_starts.append(start)

    # Step 3.
    out: list[Cue] = []
    for i, cue in enumerate(live_cues):
        start = final_starts[i]
        next_start = final_starts[i + 1] if i + 1 < count else None
        if chains[i]:
            end = max(ordered[k].end_ms for k in chains[i]) + END_PAD_MS
        elif next_start is not None and next_start < live_cues[i + 1].start_ms:
            end = cue.end_ms  # no chain, but the next cue snapped back towards it: clamp the live end
        else:
            out.append(cue)
            continue
        if next_start is not None:
            end = max(min(end, next_start - cfg.end_gap_ms), min(next_start, start + MIN_CUE_MS))
        else:
            end = max(end, start + MIN_CUE_MS)
        out.append(replace(cue, start_ms=start, end_ms=end))
    return out


def _chain_start(ordered: list[Region], starts: list[int], window_start: int, window_end: int) -> int | None:
    """Index of the region a hook-rule chain starts at, or ``None``."""
    first_inside = bisect_left(starts, window_start)
    inside = first_inside if first_inside < len(starts) and starts[first_inside] < window_end else None
    if first_inside and ordered[first_inside - 1].end_ms > window_start:
        # In progress at the window's start: usually the previous voice's tail.
        if inside is not None and starts[inside] - window_start <= TAIL_SKIP_MS:
            return inside
        return first_inside - 1
    return inside


def _follow(ordered: list[Region], first: int, window_start: int, window_end: int) -> list[int]:
    """The chain from ``first``: each next region while it begins inside the window and close enough."""
    chain = [first]
    chain_end = ordered[first].end_ms
    k = first + 1
    while (
        k < len(ordered)
        and window_start <= ordered[k].start_ms < window_end
        and ordered[k].start_ms - chain_end <= CHAIN_GAP_MS
    ):
        chain.append(k)
        chain_end = max(chain_end, ordered[k].end_ms)
        k += 1
    return chain
