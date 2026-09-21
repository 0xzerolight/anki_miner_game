"""Reconcile on connect: every row of spec 6.3, the lag-corrected fallback clock (spec 7 as amended),
launch duties and orphan finalise (spec 6.2, 6.3, 10.3; 17 row "Unclean previous exit"; 18.1 row
Reconcile; R2 items 8 and 10)."""

from pathlib import Path

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.manifest import (
    ClockKind,
    ClockRecord,
    DriftSample,
    Flag,
    GameRef,
    ManifestState,
    ObsRecord,
    SessionManifest,
)
from anki_miner_game.models.messages import AppState, CommandKind, LineAccepted, RecordingStarted
from anki_miner_game.models.obs import ObsConnectError, ObsEventName, OutputState
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import (
    Journal,
    JournalRecord,
    LineRecord,
    PauseRecord,
    ResumeRecord,
    read_journal,
)
from anki_miner_game.session.manifest import load_manifest, write_manifest_atomic
from anki_miner_game.session.restore import ObsRestore, restore_path, save_restore
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, SLUG, T0, TITLE, Harness

ZERO = T0 + 1.0
ORPHAN_STEM = "2026-10-01 20-00-00"


def leave_session(
    h: Harness,
    stem: str,
    *,
    state: ManifestState = ManifestState.RECORDING,
    records: tuple[JournalRecord, ...] = (),
    samples: tuple[DriftSample, ...] = (),
) -> Path:
    """What a crashed or restarted app leaves in ``_incoming/``: video, manifest and journal."""
    video = h.incoming / f"{stem}.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x1a\x45\xdf\xa3 not really matroska")
    manifest = SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug=SLUG, title=TITLE),
        index=1,
        state=state,
        started_at="2026-10-01T20:00:00Z",
        obs=ObsRecord(
            version="32.2.2",
            websocket="5.7.4",
            profile=OBS_PROFILE_NAME,
            collection=OBS_COLLECTION_NAME,
            output_path=str(video),
        ),
        clock=ClockRecord(drift_samples=samples),
        text_mode=TextMode.HOOK,
    )
    write_manifest_atomic(h.incoming / f"{stem}.session.json", manifest)
    if state is ManifestState.RECORDING:
        journal = Journal(h.incoming / f"{stem}.lines.jsonl")
        for record in records:
            journal.append(record)
        journal.close()
    return video


def incoming_manifest(h: Harness) -> SessionManifest:
    return load_manifest(h.incoming / f"{OBS_STEM}.session.json")


async def recording(h: Harness) -> None:
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)


async def reconnect(h: Harness, lost: float, back: float) -> None:
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST, {}, lost)
    h.gateway.connected = True
    await h.emit(ObsEventName.CONNECTED, {}, back)


async def test_launch_reconciles_first(h: Harness):
    assert h.gateway.names()[:2] == ["GetVersion", "GetRecordStatus"]


async def test_every_connect_reconciles_again(h: Harness):
    await h.send(CommandKind.ARM, SLUG)
    before = h.gateway.names().count("GetVersion")
    await h.emit(ObsEventName.CONNECTED)
    assert h.gateway.names().count("GetVersion") == before + 1


async def test_row1_a_recording_that_stopped_while_disconnected_is_treated_as_stopped(h: Harness):
    await recording(h)
    h.obs.record_active = False
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 30.0)
    assert h.actor.state is AppState.ARMED
    # stop offset = the reading when the connection dropped (2010)
    assert (h.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,010 --> 00:00:01,660\nまえ\n"
    )


async def test_row1_a_new_recording_after_the_drop_ends_ours_and_is_not_a_session(h: Harness):
    await recording(h)
    other = h.video_path("2026-10-02 18-30-00")
    other.write_bytes(b"")
    h.obs.output_path = str(other)
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 30.0)
    assert h.actor.state is AppState.ARMED
    assert len(h.finalised()) == 1
    assert not (h.incoming / "2026-10-02 18-30-00.session.json").exists()


async def test_row2_the_same_recording_continues(h: Harness):
    await recording(h)
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 3.0)
    await h.line("あと", ZERO + 4.0)
    assert h.actor.state is AppState.RECORDING
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, 4010)
    assert Flag.CLOCK_DEGRADED not in incoming_manifest(h).flags


async def test_row2_in_advanced_mode_the_file_comes_from_adv_file_output(h: Harness):
    h.obs.output_name = "adv_file_output"  # R2 item 8: the output of [Output] Mode Advanced
    await recording(h)
    other = h.video_path("2026-10-02 18-30-00")
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 3.0)
    assert h.actor.state is AppState.RECORDING
    h.obs.output_path = str(other)  # a different file: ours stopped while the app was away
    await reconnect(h, lost=ZERO + 4.0, back=ZERO + 5.0)
    assert h.actor.state is AppState.ARMED


async def test_row3_a_missed_pause_switches_to_the_output_duration_clock(h: Harness):
    await recording(h)
    h.obs.record_paused = True
    h.obs.output_duration = 1_500
    await reconnect(h, lost=ZERO + 2.0, back=ZERO + 10.0)
    manifest = incoming_manifest(h)
    assert (manifest.clock.kind, manifest.clock.degraded) == (ClockKind.OUTPUT_DURATION, True)
    assert Flag.CLOCK_DEGRADED in manifest.flags
    assert "off by" in h.banners()[BannerKey.CLOCK]  # the only sample read 0: no lag to add
    await h.line("ていし", ZERO + 11.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)  # paused: dropped
    h.obs.record_paused = False
    await h.emit(
        ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 20.0
    )
    h.obs.output_duration = 2_800
    await h.tick(ZERO + 21.0)  # re-anchor: 10 s after the anchor taken at the reconnect
    await h.line("あと", ZERO + 22.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")[-3:] == [
        PauseRecord(offset_ms=1_500),  # the missed edge, at the new clock's reading
        ResumeRecord(offset_ms=1_500),
        LineRecord(offset_ms=3_800, text="あと", source="textractor"),  # 2 800 re-anchored + 1 000
    ]
    assert incoming_manifest(h).clock.drift_samples == (DriftSample(at_ms=10, output_duration_ms=0),)


async def test_row3_the_fallback_adds_the_lag_of_the_latest_drift_sample(h: Harness):
    await recording(h)
    h.obs.output_duration = 9_450
    await h.tick(ZERO + session_mod.DRIFT_SAMPLE_AFTER_S)  # sample (10 010, 9 450): lag 560
    h.obs.record_paused = True
    h.obs.output_duration = 11_000
    await reconnect(h, lost=ZERO + 11.0, back=ZERO + 13.0)
    assert BannerKey.CLOCK not in h.banners()  # spec 7: with a lag the fallback keeps the bound
    assert Flag.CLOCK_DEGRADED in incoming_manifest(h).flags
    h.obs.record_paused = False
    await h.emit(
        ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 15.0
    )
    await h.line("あと", ZERO + 16.0)
    h.obs.output_duration = 13_000
    await h.tick(ZERO + 23.0)  # re-anchor: outputDuration + the same lag
    await h.line("もっと", ZERO + 24.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")[-4:] == [
        PauseRecord(offset_ms=11_560),
        ResumeRecord(offset_ms=11_560),
        LineRecord(offset_ms=12_560, text="あと", source="textractor"),
        LineRecord(offset_ms=14_560, text="もっと", source="textractor"),  # 13 000 + 560 + 1 000
    ]


async def test_a_pause_edge_that_changes_nothing_switches_to_the_output_duration_clock(h: Harness):
    await recording(h)
    h.obs.output_duration = 3_000
    await h.emit(  # its PAUSED never reached the app (R2 missed_pause)
        ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 6.0
    )
    manifest = incoming_manifest(h)
    assert manifest.clock.kind is ClockKind.OUTPUT_DURATION and Flag.CLOCK_DEGRADED in manifest.flags
    assert BannerKey.CLOCK in h.banners()
    await h.line("あと", ZERO + 7.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")[-2:] == [
        ResumeRecord(offset_ms=3_000),  # the edge, at the new clock's reading
        LineRecord(offset_ms=4_000, text="あと", source="textractor"),
    ]


async def test_row4_an_app_restart_resumes_the_running_session(rig: Harness):
    video = leave_session(rig, OBS_STEM, records=(LineRecord(offset_ms=5_000, text="まえ", source="textractor"),))
    rig.obs.record_active = True
    rig.obs.output_path = str(video)
    rig.obs.output_duration = 60_000
    await rig.start()
    assert rig.states() == [(AppState.RECORDING, SLUG)]
    assert RecordingStarted(OBS_STEM) in rig.events
    assert rig.sources[0].starts == 1
    manifest = incoming_manifest(rig)
    assert manifest.clock.kind is ClockKind.OUTPUT_DURATION and Flag.CLOCK_DEGRADED in manifest.flags
    assert BannerKey.CLOCK in rig.banners()
    await rig.line("あと", T0 + 1.0)
    await rig.stopped(T0 + 4.0)
    assert (rig.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:05,000 --> 00:00:20,000\nまえ\n\n2\n00:01:01,000 --> 00:01:03,650\nあと\n"
    )


async def test_row4_the_resumed_clock_adds_the_lag_of_the_sessions_drift_samples(rig: Harness):
    samples = (
        DriftSample(at_ms=10, output_duration_ms=0),
        DriftSample(at_ms=10_010, output_duration_ms=9_450),  # lag 560
        DriftSample(at_ms=30_010, output_duration_ms=29_900),  # the latest: lag 110
    )
    video = leave_session(rig, OBS_STEM, samples=samples)
    rig.obs.record_active = True
    rig.obs.output_path = str(video)
    rig.obs.output_duration = 60_000
    await rig.start()
    assert BannerKey.CLOCK not in rig.banners()
    await rig.line("あと", T0 + 1.0)
    assert read_journal(rig.incoming / f"{OBS_STEM}.lines.jsonl") == [
        LineRecord(offset_ms=61_110, text="あと", source="textractor")
    ]


async def test_row5_an_active_recording_without_a_manifest_is_not_ours(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.obs.record_active = True
    rig.obs.output_path = str(rig.video_path("2026-10-02 19-00-00"))
    await rig.start()
    assert rig.actor.state is AppState.IDLE
    assert len(rig.finalised()) == 1  # the orphan: the live file is known and is not it
    await rig.arm()
    assert "recording" in rig.banners()[BannerKey.ARM]


async def test_row5_an_unmatchable_recording_holds_the_orphans_back(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.obs.record_active = True
    rig.obs.output_path = None  # GetOutputSettings answers for no output
    await rig.start()
    assert rig.finalised() == []
    assert (rig.incoming / f"{ORPHAN_STEM}.session.json").exists()
    rig.obs.record_active = False
    await rig.emit(ObsEventName.RECORD_STATE_CHANGED, {"outputState": OutputState.STOPPED, "outputPath": "x.mkv"})
    assert len(rig.finalised()) == 1  # nothing records any more


async def test_row6_orphans_are_finalised_after_reconcile(rig: Harness):
    leave_session(rig, ORPHAN_STEM, records=(LineRecord(offset_ms=2_000, text="まえ", source="textractor"),))
    leave_session(rig, "2026-10-01 21-00-00", state=ManifestState.FINALISE_PENDING)
    await rig.start()
    placed = sorted(path.name for path in rig.finalised())
    assert placed == [f"{TITLE} - 01.session.json", f"{TITLE} - 02.session.json"]
    assert list(rig.incoming.iterdir()) == []
    # no stop record: stop = the last offset + max_cue_seconds, so the cue runs to the 15 s cap
    assert (rig.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8") == (
        "1\n00:00:02,000 --> 00:00:16,650\nまえ\n"
    )


async def test_finalise_pending_is_retried_at_launch_only(h: Harness):
    leave_session(h, ORPHAN_STEM, state=ManifestState.FINALISE_PENDING)  # after the launch sweep
    await h.emit(ObsEventName.CONNECTED)  # a reconnect sweeps `recording` orphans only
    await h.stopped(T0 + 5.0)  # so does a STOPPED with no session
    assert h.finalised() == []
    assert (h.incoming / f"{ORPHAN_STEM}.session.json").exists()


async def test_orphans_wait_while_obs_runs_but_cannot_be_reached(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    rig.gateway.connect_error = ObsConnectError("connection refused")
    await rig.start()
    assert rig.finalised() == []
    assert "connection refused" in rig.banners()[BannerKey.OBS]


async def test_orphans_are_finalised_at_launch_when_obs_is_not_running(rig: Harness):
    leave_session(rig, ORPHAN_STEM)
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    rig.discovery.running = False
    await rig.start()
    assert len(rig.finalised()) == 1
    assert rig.gateway.connects == 0
    assert restore_path().exists()  # waits for the next connection


async def test_launch_restores_the_profile_an_unclean_exit_left(rig: Harness):
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    rig.obs.profiles.append("Mine")
    rig.obs.collections.append("Scenes")
    rig.obs.profile, rig.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    await rig.start()
    assert (rig.obs.profile, rig.obs.collection) == ("Mine", "Scenes")
    assert not restore_path().exists()
