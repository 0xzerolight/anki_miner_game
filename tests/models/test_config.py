import dataclasses
import json

import pytest

from anki_miner_game.models.codec import DecodeError, dump_document, load_document
from anki_miner_game.models.config import (
    DEFAULT_TEXT_SOURCES,
    AppConfig,
    CueSettings,
    FeedSettings,
    ObsSettings,
    RecordingSettings,
    TextSourceConfig,
    VadSettings,
)


def test_spec_defaults():
    cfg = AppConfig()
    assert cfg.output_root == "~/Videos/Anki Miner Game"
    assert cfg.obs == ObsSettings(host="127.0.0.1", port=None, password_override=None)
    assert cfg.feed == FeedSettings(enabled=True, ws_port=6678, http_port=6679)
    assert cfg.hotkey == "Ctrl+Shift+F9"
    assert cfg.recording == RecordingSettings(max_height=1080, fps=30)
    assert cfg.cue == CueSettings(max_cue_seconds=15, end_gap_ms=350)
    assert cfg.vad == VadSettings(enabled=True)
    assert cfg.last_game is None


def test_default_text_sources_are_the_three_hookers():
    assert AppConfig().text_sources == DEFAULT_TEXT_SOURCES
    assert [(s.id, s.name, s.uri, s.enabled) for s in DEFAULT_TEXT_SOURCES] == [
        ("textractor", "Textractor", "localhost:6677", True),
        ("agent", "Agent", "localhost:9001", True),
        ("luna", "LunaTranslator", "localhost:2333", True),
    ]


@pytest.mark.parametrize("seconds", [5, 15, 60])
def test_max_cue_seconds_accepts_5_to_60(seconds):
    assert CueSettings(max_cue_seconds=seconds).max_cue_seconds == seconds


@pytest.mark.parametrize("seconds", [4, 61, 0, -15])
def test_max_cue_seconds_rejects_outside_5_to_60(seconds):
    with pytest.raises(ValueError, match="max_cue_seconds must be 5-60"):
        CueSettings(max_cue_seconds=seconds)


def test_end_gap_cannot_be_negative():
    with pytest.raises(ValueError, match="end_gap_ms"):
        CueSettings(end_gap_ms=-1)


def test_out_of_range_cue_in_json_is_a_decode_error():
    with pytest.raises(DecodeError, match="max_cue_seconds must be 5-60"):
        load_document(AppConfig, '{"schema": 1, "cue": {"max_cue_seconds": 61}}')


def test_frozen_and_replace():
    cfg = AppConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.hotkey = "F10"  # type: ignore[misc]
    changed = dataclasses.replace(cfg, cue=dataclasses.replace(cfg.cue, max_cue_seconds=30))
    assert changed.cue.max_cue_seconds == 30
    assert cfg.cue.max_cue_seconds == 15


def test_password_override_never_appears_in_repr():
    cfg = AppConfig(obs=ObsSettings(password_override="hunter2"))
    assert "hunter2" not in repr(cfg)
    assert "hunter2" not in repr(cfg.obs)


def test_json_round_trip_with_schema():
    cfg = AppConfig(
        output_root="D:/Recordings",
        obs=ObsSettings(host="192.168.1.5", port=4460, password_override="typed"),
        text_sources=(TextSourceConfig(id="mine", name="Mine", uri="localhost:7000/ws", enabled=False),),
        feed=FeedSettings(enabled=False, ws_port=7001, http_port=7002),
        hotkey="Ctrl+F10",
        recording=RecordingSettings(max_height=720, fps=60),
        cue=CueSettings(max_cue_seconds=20, end_gap_ms=400),
        vad=VadSettings(enabled=False),
        last_game="steins-gate",
    )
    text = dump_document(cfg)
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["text_sources"] == [{"id": "mine", "name": "Mine", "uri": "localhost:7000/ws", "enabled": False}]
    assert load_document(AppConfig, text) == cfg


def test_empty_document_loads_as_defaults():
    assert load_document(AppConfig, '{"schema": 1}') == AppConfig()
