"""WebsocketSource: a hooker's websocket client (spec 3.2, 8.1)."""

import pytest

from anki_miner_game.text.sources.websocket_source import parse_frame

# --- frame parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "line"),
    [
        pytest.param("お前は誰だ？", "お前は誰だ？", id="plain text"),
        pytest.param(
            '{"sentence": "行くぞ", "time": "2026-09-21T10:00:00", "source": "Textractor"}',
            "行くぞ",
            id="dict with sentence",
        ),
        pytest.param('{"text": "行くぞ"}', '{"text": "行くぞ"}', id="dict without sentence is the frame"),
        pytest.param('{"sentence": 5}', '{"sentence": 5}', id="non-string sentence is the frame"),
        pytest.param('["行くぞ"]', '["行くぞ"]', id="JSON list is the frame"),
        pytest.param('"行くぞ"', '"行くぞ"', id="JSON string is the frame"),
        pytest.param("123", "123", id="JSON number is the frame"),
        pytest.param("null", "null", id="JSON null is the frame"),
        pytest.param("{broken", "{broken", id="invalid JSON is the frame"),
        pytest.param("", "", id="empty frame passes through for the pipeline to count"),
        pytest.param("[" * 100_000, "[" * 100_000, id="nesting too deep to parse is the frame"),
        pytest.param("行くぞ".encode(), "行くぞ", id="binary frame decoded as UTF-8"),
        pytest.param(b"\xff\xfe", "��", id="undecodable bytes replaced"),
    ],
)
def test_parse_frame(frame, line):
    assert parse_frame(frame) == line
