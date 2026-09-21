"""SRT output (spec 9; 18.1 row ``session/srt_writer.py``)."""

import os

import pysubs2
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pysubs2.time import TIMESTAMP, timestamp_to_ms

from anki_miner_game.models.config import CueSettings
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.lines import TimedLine
from anki_miner_game.session.cues import build_cues
from anki_miner_game.session.srt_writer import format_srt, format_timestamp, write_srt_atomic

CUES = [
    Cue(index=1, start_ms=5_230, end_ms=9_410, text="「こんにちは」", source_id="textractor"),
    Cue(index=2, start_ms=3_599_500, end_ms=3_601_250, text="…えっと、岡部？", source_id="agent"),
]
EXPECTED = (
    "1\n"
    "00:00:05,230 --> 00:00:09,410\n"
    "「こんにちは」\n"
    "\n"
    "2\n"
    "00:59:59,500 --> 01:00:01,250\n"
    "…えっと、岡部？\n"
)


@pytest.mark.parametrize(
    ("ms", "stamp"),
    [
        (0, "00:00:00,000"),
        (999, "00:00:00,999"),
        (1_000, "00:00:01,000"),
        (61_001, "00:01:01,001"),
        (3_599_999, "00:59:59,999"),
        (3_600_000, "01:00:00,000"),
        (5_248_120, "01:27:28,120"),
        (359_999_999, "99:59:59,999"),
        (360_000_000, "100:00:00,000"),  # hours grow past two digits rather than wrap
    ],
)
def test_format_timestamp(ms, stamp):
    assert format_timestamp(ms) == stamp


def test_format_timestamp_rejects_a_negative_time():
    with pytest.raises(ValueError, match="-1"):
        format_timestamp(-1)


@given(st.integers(min_value=0, max_value=359_999_999))
def test_pysubs2_reads_every_timestamp_back_to_the_same_milliseconds(ms):
    match = TIMESTAMP.fullmatch(format_timestamp(ms))
    assert match is not None
    assert timestamp_to_ms(match.groups()) == ms


def test_format_srt_is_byte_exact():
    assert format_srt(CUES) == EXPECTED


def test_no_cues_format_to_nothing():
    assert format_srt([]) == ""


def test_cues_are_numbered_from_one_by_position():
    renumbered = [Cue(index=7, start_ms=0, end_ms=500, text="あ", source_id="ocr")]
    assert format_srt(renumbered).startswith("1\n00:00:00,000 --> 00:00:00,500\n")


def test_a_cue_is_written_on_one_text_line():
    cue = Cue(index=1, start_ms=0, end_ms=1_000, text="一行目\n二行目\r\n三行目", source_id="clipboard")
    assert format_srt([cue]) == "1\n00:00:00,000 --> 00:00:01,000\n一行目 二行目 三行目\n"


def test_write_is_byte_exact_utf8_without_bom_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "_incoming" / "2026-10-02 18-04-11.srt"
    write_srt_atomic(path, CUES)

    data = path.read_bytes()
    assert data == EXPECTED.encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert os.listdir(path.parent) == [path.name]


def test_write_replaces_an_existing_subtitle(tmp_path):
    path = tmp_path / "session.srt"
    path.write_text("stale", encoding="utf-8")
    write_srt_atomic(path, CUES)
    assert path.read_bytes() == EXPECTED.encode("utf-8")
    assert os.listdir(tmp_path) == [path.name]


def test_a_failed_write_keeps_the_old_subtitle_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "session.srt"
    path.write_text("previous", encoding="utf-8")

    def refuse(src, dst):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        write_srt_atomic(path, CUES)
    assert path.read_text(encoding="utf-8") == "previous"
    assert os.listdir(tmp_path) == [path.name]


def test_pysubs2_parses_the_written_file_back_to_the_same_cues(tmp_path):
    lines = [
        TimedLine(offset_ms=5_230, text="「未来ガジェット研究所へようこそ」", source_id="textractor"),
        TimedLine(offset_ms=9_600, text="click-through", source_id="textractor"),
        TimedLine(offset_ms=9_760, text="…え？", source_id="textractor"),
        TimedLine(offset_ms=3_598_000, text="エル・プサイ・コングルゥ", source_id="agent"),
        TimedLine(offset_ms=3_603_400, text="Steins;Gate 2010", source_id="luna"),
    ]
    cues = build_cues(lines, stop_ms=3_700_000, shift_ms=0, cfg=CueSettings())
    path = tmp_path / "Steins;Gate - 03.srt"
    write_srt_atomic(path, cues)

    subs = pysubs2.load(str(path))  # how Anki Miner loads it (subtitle_parser.py)
    assert subs.format == "srt"
    assert [(e.start, e.end, e.plaintext) for e in subs] == [(c.start_ms, c.end_ms, c.text) for c in cues]
    assert len(subs) == 4
