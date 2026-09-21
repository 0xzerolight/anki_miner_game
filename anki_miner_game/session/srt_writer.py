"""SRT output (spec 9): UTF-8 without BOM, ``\\n`` line ends, numbered from 1, one text line per cue.

Timestamps are formatted from integer milliseconds. GSM's ``_format_srt_time``
(``longplay_handler.py:302`` at ``479747fe``) floors the seconds and takes the
milliseconds from a different value; it is deliberately not ported.
"""

from collections.abc import Sequence
from pathlib import Path

from anki_miner_game.models.cue import Cue
from anki_miner_game.store import write_text_atomic


def format_timestamp(ms: int) -> str:
    """``HH:MM:SS,mmm``. Hours keep growing past 99 rather than wrap; a negative time is a ``ValueError``."""
    if ms < 0:
        raise ValueError(f"an SRT timestamp cannot be negative, got {ms}")
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def format_srt(cues: Sequence[Cue]) -> str:
    """The subtitle text: cues numbered by position from 1, a blank line between two cues.

    A line break inside a cue's text becomes a space, so each cue has exactly one
    text line. No cues give an empty string.
    """
    blocks = []
    for number, cue in enumerate(cues, start=1):
        text = " ".join(cue.text.splitlines())
        blocks.append(f"{number}\n{format_timestamp(cue.start_ms)} --> {format_timestamp(cue.end_ms)}\n{text}\n")
    return "\n".join(blocks)


def write_srt_atomic(path: Path, cues: Sequence[Cue]) -> None:
    """Write ``format_srt(cues)`` to a temporary name in ``path``'s folder and ``os.replace`` it onto ``path``.

    Raises ``OSError`` when the file cannot be written; an existing ``path`` is
    then left untouched and no temporary file remains.
    """
    write_text_atomic(path, format_srt(cues))
