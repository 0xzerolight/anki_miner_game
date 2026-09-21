"""Session journal (spec 10.2; 18.1 row ``session/journal.py`` + finalise)."""

import pytest

from anki_miner_game.models.lines import TimedLine
from anki_miner_game.session.journal import (
    Journal,
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
    read_journal,
    timed_lines,
)

# One record of each type, and the lines spec 10.2 shows for them.
RECORDS = [
    LineRecord(offset_ms=5230, text="…", source="textractor"),
    ReplaceRecord(text="…"),
    PauseRecord(offset_ms=61200),
    ResumeRecord(offset_ms=61200),
    StopRecord(offset_ms=5248120),
]
SPEC_LINES = (
    '{"t":"line","offset_ms":5230,"text":"…","source":"textractor"}\n'
    '{"t":"replace","text":"…"}\n'
    '{"t":"pause","offset_ms":61200}\n'
    '{"t":"resume","offset_ms":61200}\n'
    '{"t":"stop","offset_ms":5248120}\n'
)


def _write(path, records):
    journal = Journal(path)
    for record in records:
        journal.append(record)
    journal.close()


def test_each_record_type_is_one_json_line_as_the_spec_shows(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS)
    assert path.read_bytes() == SPEC_LINES.encode("utf-8")  # UTF-8 kept, "\n" even on Windows


def test_records_read_back_in_order(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS)
    assert read_journal(path) == RECORDS


def test_every_record_is_on_disk_as_soon_as_it_is_appended(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    journal = Journal(path)
    for count, record in enumerate(RECORDS, start=1):
        journal.append(record)
        assert read_journal(path) == RECORDS[:count]  # read through another handle, before close
    journal.close()


@pytest.mark.parametrize(
    "fragment",
    [
        b'{"t":"line","offset_ms":9',
        '{"t":"line","offset_ms":9,"text":"こ'.encode()[:-1],  # cut inside a UTF-8 sequence
        b'{"t":"sto',
    ],
)
def test_a_torn_last_line_is_skipped(tmp_path, fragment):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:2])
    with path.open("ab") as fh:
        fh.write(fragment)
    assert read_journal(path) == RECORDS[:2]


def test_a_journal_resumed_after_a_torn_line_keeps_every_new_record(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:1])
    with path.open("ab") as fh:
        fh.write(b'{"t":"line","offset_ms":7')
    _write(path, [PauseRecord(offset_ms=8000)])
    assert read_journal(path) == [RECORDS[0], PauseRecord(offset_ms=8000)]
    assert path.read_bytes().endswith(b'{"t":"pause","offset_ms":8000}\n')


def test_a_resumed_journal_appends_after_the_existing_records(tmp_path):
    path = tmp_path / "s.lines.jsonl"
    _write(path, RECORDS[:2])
    _write(path, RECORDS[2:])
    assert path.read_bytes() == SPEC_LINES.encode("utf-8")


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"[1, 2]",
        b'"a sentence"',
        b'{"t":"shout","text":"a"}',
        b'{"offset_ms":5}',
        b'{"t":"line","offset_ms":"5","text":"a","source":"s"}',
        b'{"t":"line","offset_ms":5,"text":"a"}',
        b'{"t":"replace"}',
        b"\xff\xfe",
    ],
)
def test_a_line_that_is_not_a_record_is_skipped(tmp_path, line):
    path = tmp_path / "s.lines.jsonl"
    path.write_bytes(line + b"\n" + SPEC_LINES.encode("utf-8"))
    assert read_journal(path) == RECORDS


def test_a_replace_rewrites_the_line_before_it_and_keeps_its_offset():
    records = [
        LineRecord(offset_ms=1000, text="え", source="agent"),
        PauseRecord(offset_ms=1500),
        ReplaceRecord(text="えっと…"),
        ResumeRecord(offset_ms=1500),
        LineRecord(offset_ms=4000, text="岡部", source="agent"),
        StopRecord(offset_ms=9000),
    ]
    assert timed_lines(records) == [
        TimedLine(offset_ms=1000, text="えっと…", source_id="agent"),
        TimedLine(offset_ms=4000, text="岡部", source_id="agent"),
    ]


def test_a_replace_with_no_line_before_it_is_ignored():
    records = [ReplaceRecord(text="x"), LineRecord(offset_ms=10, text="y", source="luna")]
    assert timed_lines(records) == [TimedLine(offset_ms=10, text="y", source_id="luna")]
