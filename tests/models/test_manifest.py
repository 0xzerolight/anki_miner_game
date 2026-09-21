import dataclasses
import json

import pytest

from anki_miner_game.models.codec import DecodeError, UnsupportedSchemaError
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.manifest import (
    COUNT_NAMES,
    ClockKind,
    ClockRecord,
    Counts,
    DriftSample,
    FilesRecord,
    Flag,
    GameRef,
    LiveCue,
    ManifestState,
    ObsRecord,
    SessionManifest,
    VadRecord,
    VadState,
    from_json,
    to_json,
)
from anki_miner_game.models.profile import TextMode

# The example from spec section 5, verbatim.
SPEC_EXAMPLE = """
{
  "schema": 1,
  "app_version": "0.1.0",
  "game": {"slug": "steins-gate", "title": "Steins;Gate"},
  "index": 3,
  "state": "ready",
  "started_at": "2026-10-02T18:04:11Z",
  "stopped_at": "2026-10-02T19:31:40Z",
  "obs": {"version": "31.0.2", "websocket": "5.5.4", "profile": "Anki Miner Game",
          "collection": "Anki Miner Game", "output_path": ".../_incoming/2026-10-02 18-04-11.mkv"},
  "clock": {"kind": "event", "zero_event": "STARTED", "capture_latency_ms": 0,
            "degraded": false, "drift_samples": [{"at_ms": 0, "output_duration_ms": 0}]},
  "text_mode": "hook",
  "sources_used": ["textractor"],
  "counts": {"received": 1412, "accepted": 1260, "duplicate": 96, "no_letters": 31,
             "junk": 4, "paused": 0, "skip": 21},
  "flags": [],
  "live_cues": [{"i": 1, "start_ms": 5230, "end_ms": 9410, "text": "…", "source": "textractor"}],
  "vad": {"state": "done", "model": "silero_vad_v6", "trimmed": 1104, "no_speech": 156},
  "files": {"video": "Steins;Gate - 03.mkv", "subtitle": "Steins;Gate - 03.srt"}
}
"""


def _recording_manifest() -> SessionManifest:
    return SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug="steins-gate", title="Steins;Gate"),
        index=3,
        state=ManifestState.RECORDING,
        started_at="2026-10-02T18:04:11Z",
        obs=ObsRecord(
            version="31.0.2",
            websocket="5.5.4",
            profile="Anki Miner Game",
            collection="Anki Miner Game",
            output_path="/v/_incoming/2026-10-02 18-04-11.mkv",
        ),
        text_mode=TextMode.HOOK,
    )


def test_spec_example_parses():
    manifest = from_json(SPEC_EXAMPLE)
    assert manifest.game == GameRef(slug="steins-gate", title="Steins;Gate")
    assert manifest.index == 3
    assert manifest.state is ManifestState.READY
    assert manifest.clock == ClockRecord(
        kind=ClockKind.EVENT,
        zero_event="STARTED",
        capture_latency_ms=0,
        degraded=False,
        drift_samples=(DriftSample(at_ms=0, output_duration_ms=0),),
    )
    assert manifest.text_mode is TextMode.HOOK
    assert manifest.sources_used == ("textractor",)
    assert manifest.counts == Counts(
        received=1412, accepted=1260, duplicate=96, no_letters=31, junk=4, paused=0, skip=21
    )
    assert manifest.flags == ()
    assert manifest.live_cues == (LiveCue(i=1, start_ms=5230, end_ms=9410, text="…", source="textractor"),)
    assert manifest.vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=1104, no_speech=156)
    assert manifest.files == FilesRecord(video="Steins;Gate - 03.mkv", subtitle="Steins;Gate - 03.srt")


def test_spec_example_round_trips():
    manifest = from_json(SPEC_EXAMPLE)
    assert from_json(to_json(manifest)) == manifest


def test_json_keys_follow_the_spec_order():
    data = json.loads(to_json(from_json(SPEC_EXAMPLE)))
    assert list(data) == list(json.loads(SPEC_EXAMPLE))


def test_recording_manifest_defaults_and_round_trip():
    manifest = _recording_manifest()
    assert manifest.stopped_at is None
    assert manifest.clock == ClockRecord()
    assert manifest.counts == Counts()
    assert (manifest.sources_used, manifest.flags, manifest.live_cues) == ((), (), ())
    assert (manifest.vad, manifest.files) == (None, None)
    data = json.loads(to_json(manifest))
    assert data["schema"] == 1
    assert data["state"] == "recording"
    assert data["vad"] is None
    assert from_json(to_json(manifest)) == manifest


def test_flags_and_states_round_trip():
    manifest = dataclasses.replace(
        _recording_manifest(),
        state=ManifestState.FINALISE_PENDING,
        flags=(Flag.NO_CUES, Flag.OBS_EXITED, Flag.SPLIT_UNSUPPORTED, Flag.CLOCK_DEGRADED),
        clock=ClockRecord(kind=ClockKind.OUTPUT_DURATION, degraded=True),
        vad=VadRecord(state=VadState.UNAVAILABLE, message="VAD add-on is not installed"),
        files=FilesRecord(video="Steins;Gate - 03.mkv", subtitle=None),
    )
    data = json.loads(to_json(manifest))
    assert data["flags"] == ["no_cues", "obs_exited", "split_unsupported", "clock_degraded"]
    assert data["clock"]["kind"] == "output_duration"
    assert from_json(to_json(manifest)) == manifest


def test_unknown_flag_is_a_decode_error():
    text = to_json(_recording_manifest()).replace('"flags": []', '"flags": ["bogus"]')
    with pytest.raises(DecodeError, match="bogus"):
        from_json(text)


def test_newer_schema_is_refused():
    with pytest.raises(UnsupportedSchemaError):
        from_json(to_json(_recording_manifest()).replace('"schema": 1', '"schema": 2'))


def test_live_cue_converts_to_and_from_cue():
    cue = Cue(index=4, start_ms=100, end_ms=900, text="はい", source_id="luna")
    live = LiveCue.from_cue(cue)
    assert live == LiveCue(i=4, start_ms=100, end_ms=900, text="はい", source="luna")
    assert live.to_cue() == cue


def test_counts_incremented():
    counts = Counts().incremented("received").incremented("skip", by=3)
    assert (counts.received, counts.skip) == (1, 3)
    assert Counts().received == 0
    with pytest.raises(KeyError):
        Counts().incremented("empty")


def test_count_names_match_the_spec():
    assert set(COUNT_NAMES) == {"received", "accepted", "duplicate", "no_letters", "junk", "paused", "skip"}


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _recording_manifest().index = 4  # type: ignore[misc]
