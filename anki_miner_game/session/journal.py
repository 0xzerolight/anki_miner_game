"""The session journal (spec 10.2): ``<obs stem>.lines.jsonl`` in ``_incoming/``, one JSON object per line.

Append-only and flushed after every record, so an accepted line survives an app crash the moment it
arrives. Cue ends are never journalled; finalise rebuilds them from the lines (spec 9, 10.3).
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import ClassVar

from anki_miner_game.models.lines import TimedLine


@dataclass(frozen=True)
class LineRecord:
    """An accepted line at its record-clock offset."""

    TAG: ClassVar[str] = "line"
    offset_ms: int
    text: str
    source: str


@dataclass(frozen=True)
class ReplaceRecord:
    """Typewriter merge (spec 8.2 step 9): new text for the line before it, which keeps its offset."""

    TAG: ClassVar[str] = "replace"
    text: str


@dataclass(frozen=True)
class PauseRecord:
    TAG: ClassVar[str] = "pause"
    offset_ms: int


@dataclass(frozen=True)
class ResumeRecord:
    TAG: ClassVar[str] = "resume"
    offset_ms: int


@dataclass(frozen=True)
class StopRecord:
    TAG: ClassVar[str] = "stop"
    offset_ms: int


JournalRecord = LineRecord | ReplaceRecord | PauseRecord | ResumeRecord | StopRecord


class Journal:
    """Appends records to one journal file. Used from the session actor's thread only.

    Opening a journal that already exists (the app restarted mid-session, spec 6.3) first ends a torn
    last line, so the next record starts on a line of its own. Close it before finalise, which deletes
    the file (Windows refuses to delete an open file).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        torn = _has_torn_tail(path)
        self._fh = path.open("a", encoding="utf-8", newline="\n")
        if torn:
            self._fh.write("\n")
            self._fh.flush()

    def append(self, record: JournalRecord) -> None:
        """Write one record as one line and flush it to the operating system."""
        self._fh.write(json.dumps({"t": record.TAG, **asdict(record)}, ensure_ascii=False, separators=(",", ":")))
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _has_torn_tail(path: Path) -> bool:
    """Whether ``path`` exists, is not empty, and does not end with a newline."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size == 0:
        return False
    with path.open("rb") as fh:
        fh.seek(size - 1)
        return fh.read(1) != b"\n"


def read_journal(path: Path) -> list[JournalRecord]:
    """Every record in ``path``, in file order; ``FileNotFoundError`` when there is no journal.

    A line that is not a valid record is skipped: a crash can leave the last line torn, and a journal
    resumed after that crash keeps the fragment on a line of its own.
    """
    records: list[JournalRecord] = []
    for raw in path.read_bytes().split(b"\n"):
        record = _decode(raw)
        if record is not None:
            records.append(record)
    return records


def _decode(raw: bytes) -> JournalRecord | None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError: a write cut short
        return None
    match obj:
        case {"t": "line", "offset_ms": int(offset), "text": str(text), "source": str(source)}:
            return LineRecord(offset_ms=offset, text=text, source=source)
        case {"t": "replace", "text": str(text)}:
            return ReplaceRecord(text=text)
        case {"t": "pause", "offset_ms": int(offset)}:
            return PauseRecord(offset_ms=offset)
        case {"t": "resume", "offset_ms": int(offset)}:
            return ResumeRecord(offset_ms=offset)
        case {"t": "stop", "offset_ms": int(offset)}:
            return StopRecord(offset_ms=offset)
    return None


def timed_lines(records: Iterable[JournalRecord]) -> list[TimedLine]:
    """The journalled lines in order, each with its latest text: the input of ``build_cues``.

    A replace record rewrites the line before it, which keeps its offset and source; one with no line
    before it is ignored. Pause, resume and stop records carry no line. The actor writes a replace
    record only when the merged line's base is the journal's last line record; a merge into a line
    that was never journalled (armed, paused, after a stop) is journalled as a line of its own.
    """
    lines: list[TimedLine] = []
    for record in records:
        if isinstance(record, LineRecord):
            lines.append(TimedLine(offset_ms=record.offset_ms, text=record.text, source_id=record.source))
        elif isinstance(record, ReplaceRecord) and lines:
            lines[-1] = replace(lines[-1], text=record.text)
    return lines
