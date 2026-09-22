"""Recording: ownership, the session's files, lines, pauses, drift samples, auto-start holds and the
normal stop (spec 6.2 ownership, 7, 8.2, 10.2, 10.3, 12 actor side; 17 rows StartRecord fails, no
text source connected at Start, zero cues; R2 items 9 and 11)."""

import json
import threading

from anki_miner_game.models.config import AppConfig, VadSettings
from anki_miner_game.models.manifest import ClockKind, ClockRecord, Counts, DriftSample, Flag, ManifestState
from anki_miner_game.models.messages import (
    START_FAILED_BANNER_KEY,
    AppState,
    CommandKind,
    LineAccepted,
    LineReceived,
    RecordingStarted,
    RecordingStopped,
    SourceStatus,
    StateChanged,
)
from anki_miner_game.models.obs import ObsRequestError, OutputState
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import (
    LineRecord,
    PauseRecord,
    ReplaceRecord,
    ResumeRecord,
    StopRecord,
    read_journal,
)
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.restore import restore_path
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, SLUG, T0, TITLE, Harness, profile

ZERO = T0 + 1.0
"""When ``STARTED`` arrives in these tests: offset = (t - ZERO) * 1000 + 10."""


def incoming_manifest(h: Harness):
    return load_manifest(h.incoming / f"{OBS_STEM}.session.json")


def journal(h: Harness):
    return read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl")


async def test_a_whole_session_leaves_the_pair_in_the_game_folder(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    assert "StartRecord" in h.gateway.names()
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 5.0)
    await h.line("さようなら", ZERO + 9.0)
    h.clock.t = ZERO + 18.0
    h.obs.output_duration = 17_400
    await h.send(CommandKind.STOP)
    assert h.gateway.names()[-2:] == ["GetRecordStatus", "StopRecord"]
    assert incoming_manifest(h).clock.drift_samples[-1] == DriftSample(at_ms=18_010, output_duration_ms=17_400)
    await h.stopping(ZERO + 18.1)  # the video ends here (R2 item 9)
    await h.line("おわり", ZERO + 18.4)  # shown, but past the video's end: not journalled
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)
    await h.stopped(ZERO + 18.7)

    stem = f"{TITLE} - 01"
    assert (h.game_dir() / f"{stem}.srt").read_bytes() == (
        "1\n00:00:05,010 --> 00:00:08,660\nこんにちは\n\n2\n00:00:09,010 --> 00:00:17,760\nさようなら\n"
    ).encode()
    assert (h.game_dir() / f"{stem}.mkv").exists()
    assert sorted(p.name for p in h.incoming.iterdir()) == []
    manifest = load_manifest(h.game_dir() / f"{stem}.session.json")
    assert manifest.state is ManifestState.READY
    assert (manifest.game.slug, manifest.game.title, manifest.index) == (SLUG, TITLE, 1)
    assert (manifest.started_at, manifest.stopped_at) == ("2026-10-02T18:04:11Z", "2026-10-02T18:04:11Z")
    assert manifest.obs.output_path == str(h.video_path())
    assert (manifest.obs.version, manifest.obs.websocket) == ("32.2.2", "5.7.4")
    assert manifest.clock == ClockRecord(
        kind=ClockKind.EVENT,
        zero_event="STARTED",
        capture_latency_ms=10,
        drift_samples=(
            DriftSample(at_ms=10, output_duration_ms=0),
            DriftSample(at_ms=18_010, output_duration_ms=17_400),
        ),
    )
    assert manifest.counts == Counts(received=3, accepted=2)  # finalise: accepted = cues
    assert manifest.sources_used == ("textractor",)
    assert h.vad.queued == [h.game_dir() / f"{stem}.session.json"]
    assert h.finalised() == [h.game_dir() / f"{stem}.session.json"]
    assert h.states() == [
        (AppState.ARMED, SLUG),
        (AppState.RECORDING, SLUG),
        (AppState.FINALISING, SLUG),
        (AppState.ARMED, SLUG),
    ]
    assert RecordingStarted(OBS_STEM) in h.events and RecordingStopped(OBS_STEM) in h.events


async def test_started_writes_the_manifest_and_journal_at_once(h: Harness):
    await h.arm()
    await h.started(ZERO)
    manifest = incoming_manifest(h)
    assert manifest.state is ManifestState.RECORDING
    assert manifest.index == 1
    assert (manifest.obs.profile, manifest.obs.collection) == ("Anki Miner Game", "Anki Miner Game")
    assert manifest.clock.drift_samples == (DriftSample(at_ms=10, output_duration_ms=0),)
    assert (h.incoming / f"{OBS_STEM}.lines.jsonl").exists()
    await h.line("こんにちは", ZERO + 5.0)
    assert journal(h) == [LineRecord(offset_ms=5010, text="こんにちは", source="textractor")]
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, 5010)


async def test_manifest_writes_leave_the_loop_thread(h: Harness, monkeypatch):
    loop_thread = threading.get_ident()
    threads: list[int] = []
    write = session_mod.write_manifest_atomic

    def recording_write(*args, **kwargs):
        threads.append(threading.get_ident())
        write(*args, **kwargs)

    monkeypatch.setattr(session_mod, "write_manifest_atomic", recording_write)
    await h.arm()
    await h.started(ZERO)
    await h.emit("RecordStateChanged", {"outputState": OutputState.PAUSED, "outputPath": None}, ZERO + 2.0)
    assert len(threads) >= 3  # STARTED, its drift sample, the pause edge
    assert loop_thread not in threads


async def test_the_manifest_on_disk_carries_schema_1(h: Harness):
    await h.arm()
    await h.started(ZERO)
    data = json.loads((h.incoming / f"{OBS_STEM}.session.json").read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["state"] == "recording"


async def test_the_session_number_follows_the_sessions_already_there(h: Harness):
    h.game_dir().mkdir(parents=True)
    (h.game_dir() / f"{TITLE} - 04.mkv").write_bytes(b"")
    await h.arm()
    await h.started(ZERO)
    assert incoming_manifest(h).index == 5


async def test_a_recording_that_starts_while_idle_is_not_a_session(h: Harness):
    await h.started(ZERO)
    assert not (h.incoming / f"{OBS_STEM}.session.json").exists()
    assert h.actor.state is AppState.IDLE


async def test_a_recording_outside_the_output_folder_is_not_a_session(h: Harness, tmp_path):
    await h.arm()
    elsewhere = tmp_path / "Videos"
    elsewhere.mkdir()
    (elsewhere / "mine.mkv").write_bytes(b"")
    await h.emit("RecordStateChanged", {"outputState": OutputState.STARTED, "outputPath": str(elsewhere / "mine.mkv")})
    assert h.actor.state is AppState.ARMED
    assert "outside the app's folder" in h.banners()[BannerKey.FOREIGN_RECORDING]
    assert not any(h.incoming.glob("*.session.json"))


async def test_lines_before_arming_are_ignored(h: Harness):
    h.actor.post(LineReceived("こんにちは", T0 + 1, "textractor"))
    await h.settle()
    assert h.accepted() == []


async def test_a_line_from_another_thread_is_handled_on_the_loop(h: Harness):
    await h.arm()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda e: seen.append(threading.get_ident()) if isinstance(e, LineAccepted) else None)
    worker = threading.Thread(target=h.sources[0].line, args=("こんにちは", T0 + 5))
    worker.start()
    worker.join()
    await h.settle()
    assert [e.line.text for e in h.accepted()] == ["こんにちは"]
    assert seen == [loop_thread]


async def test_lines_while_armed_are_shown_but_not_journalled(h: Harness):
    await h.arm()
    await h.line("こんにちは", T0 + 0.5)
    assert h.accepted() == [LineAccepted(h.accepted()[0].line, None)]
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 1.0)  # not a duplicate: STARTED reset the pipeline
    assert journal(h) == [LineRecord(offset_ms=1010, text="こんにちは", source="textractor")]


async def test_dropped_lines_are_counted_in_the_manifest(h: Harness):
    await h.arm()
    await h.started(ZERO)
    for raw, t in (("こんにちは", 1.0), ("こんにちは", 2.0), ("123", 3.0), ("", 4.0), ("あ" * 301, 5.0)):
        await h.line(raw, ZERO + t)
    await h.stopped(ZERO + 20.0)
    manifest = load_manifest(h.finalised()[0])
    assert manifest.counts == Counts(received=5, accepted=1, duplicate=1, no_letters=2, junk=1)


async def test_a_pause_drops_lines_and_is_journalled_with_a_drift_sample_at_resume(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("はい", ZERO + 1.0)
    h.obs.output_duration = 2_000
    h.obs.record_paused = True
    await h.emit("RecordStateChanged", {"outputState": OutputState.PAUSED, "outputPath": None}, ZERO + 2.0)
    await h.line("いいえ", ZERO + 3.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)
    h.obs.record_paused = False
    await h.emit("RecordStateChanged", {"outputState": OutputState.RESUMED, "outputPath": None}, ZERO + 5.0)
    await h.line("いいえ", ZERO + 6.0)  # not a duplicate: the paused line was never journalled
    assert journal(h) == [
        LineRecord(offset_ms=1010, text="はい", source="textractor"),
        PauseRecord(offset_ms=2010),
        ResumeRecord(offset_ms=2010),
        LineRecord(offset_ms=3010, text="いいえ", source="textractor"),
    ]
    manifest = incoming_manifest(h)
    assert manifest.counts.paused == 1
    # spec 7 as amended: at STARTED and at RESUMED, never at PAUSED
    assert manifest.clock.drift_samples == (
        DriftSample(at_ms=10, output_duration_ms=0),
        DriftSample(at_ms=2010, output_duration_ms=2_000),
    )


async def test_a_drift_sample_is_taken_ten_seconds_after_started(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.obs.output_duration = 9_450
    await h.tick(ZERO + session_mod.DRIFT_SAMPLE_AFTER_S - 1.0)
    assert len(incoming_manifest(h).clock.drift_samples) == 1  # not due yet
    await h.tick(ZERO + session_mod.DRIFT_SAMPLE_AFTER_S)
    await h.tick(ZERO + 2 * session_mod.DRIFT_SAMPLE_AFTER_S)  # once only
    assert incoming_manifest(h).clock.drift_samples == (
        DriftSample(at_ms=10, output_duration_ms=0),
        DriftSample(at_ms=10_010, output_duration_ms=9_450),
    )


async def test_no_drift_sample_while_paused(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.obs.record_paused = True
    await h.emit("RecordStateChanged", {"outputState": OutputState.PAUSED, "outputPath": None}, ZERO + 2.0)
    await h.tick(ZERO + session_mod.DRIFT_SAMPLE_AFTER_S)
    await h.send(CommandKind.STOP)
    assert incoming_manifest(h).clock.drift_samples == (DriftSample(at_ms=10, output_duration_ms=0),)


async def test_a_merge_into_the_last_journalled_line_is_a_replace_record(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.started(ZERO)
    await rig.line("え", ZERO + 1.0)
    await rig.line("えっと…", ZERO + 1.5)
    assert journal(rig) == [LineRecord(offset_ms=1010, text="え", source="textractor"), ReplaceRecord(text="えっと…")]
    assert rig.accepted()[-1] == LineAccepted(rig.accepted()[-1].line, 1010, replaces_previous=True)


async def test_auto_start_holds_lines_until_started_and_journals_them_at_zero(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    trigger = h.accepted()[0].line
    await h.send(CommandKind.START, line=trigger)
    await h.line("つづき", T0 + 0.6)
    await h.started(ZERO)
    await h.line("つづき", ZERO + 1.0)  # a duplicate of the held line: no pipeline reset at STARTED
    assert journal(h) == [
        LineRecord(offset_ms=0, text="はじまり", source="textractor"),
        LineRecord(offset_ms=0, text="つづき", source="textractor"),
    ]


async def test_held_lines_are_published_again_with_their_offsets_between_the_state_and_the_start(h: Harness):
    """The window counts journalled lines from the events; the feed skips this second publication."""
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    await h.line("つづき", T0 + 0.6)
    held = [event.line for event in h.accepted()]
    mark = len(h.events)
    await h.started(ZERO)
    after = h.events[mark:]
    recording = after.index(StateChanged(AppState.RECORDING, SLUG))
    started = next(i for i, event in enumerate(after) if isinstance(event, RecordingStarted))
    assert after[recording + 1 : started] == [LineAccepted(held[0], 0), LineAccepted(held[1], 0)]


async def test_a_start_without_held_lines_publishes_no_line_at_started(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    mark = len(h.events)
    await h.started(ZERO)
    assert not [event for event in h.events[mark:] if isinstance(event, LineAccepted)]


async def test_a_merge_into_a_held_line_replaces_it(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.line("え", T0 + 0.2)
    await rig.send(CommandKind.START, line=rig.accepted()[0].line)
    await rig.line("えっと…", T0 + 0.4)
    await rig.started(ZERO)
    assert journal(rig) == [LineRecord(offset_ms=0, text="えっと…", source="textractor")]


async def test_a_merge_whose_base_was_never_journalled_is_a_new_line(rig: Harness):
    rig.profiles[SLUG] = profile(typewriter=True)
    await rig.start()
    await rig.arm()
    await rig.line("はじまり", T0 + 0.1)
    trigger = rig.accepted()[0].line
    await rig.line("え", T0 + 0.2)  # accepted before auto mode's START arrives: not held
    await rig.send(CommandKind.START, line=trigger)
    await rig.started(ZERO)
    await rig.line("えっと…", ZERO + 0.5)  # merges into "え", which the journal never saw
    assert journal(rig) == [
        LineRecord(offset_ms=0, text="はじまり", source="textractor"),
        LineRecord(offset_ms=0, text="えっと…", source="textractor"),
    ]


async def test_a_start_without_a_line_holds_nothing(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    await h.line("まえ", T0 + 0.5)
    await h.started(ZERO)
    assert journal(h) == []


async def test_a_failed_start_record_is_a_banner_and_drops_the_held_lines(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    h.gateway.fail("StartRecord", ObsRequestError("StartRecord", 500, "Output already running"))
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    assert "Output already running" in h.banners()[START_FAILED_BANNER_KEY]
    assert h.actor.state is AppState.ARMED
    await h.send(CommandKind.START)
    await h.started(ZERO)
    assert journal(h) == []
    assert START_FAILED_BANNER_KEY not in h.banners()


async def test_no_started_within_the_timeout_is_a_failed_start(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    await h.tick(h.clock.t + session_mod.START_TIMEOUT_S - 1.0)
    assert START_FAILED_BANNER_KEY not in h.banners()
    await h.tick(h.clock.t + 1.0)  # and GetRecordStatus says inactive (R2 item 11)
    assert "OBS's window" in h.banners()[START_FAILED_BANNER_KEY]
    await h.send(CommandKind.START)  # may be tried again
    assert h.gateway.names().count("StartRecord") == 2


async def test_a_start_still_active_at_the_timeout_waits_for_started(h: Harness):
    await h.arm()
    await h.line("はじまり", T0 + 0.2)
    await h.send(CommandKind.START, line=h.accepted()[0].line)
    h.obs.record_active = True  # OBS is recording; its STARTED is late
    await h.tick(h.clock.t + session_mod.START_TIMEOUT_S)
    assert START_FAILED_BANNER_KEY not in h.banners()
    await h.started(h.clock.t + 1.0)
    assert journal(h) == [LineRecord(offset_ms=0, text="はじまり", source="textractor")]


async def test_a_stopped_while_starting_is_a_failed_start(h: Harness):
    await h.arm()
    await h.send(CommandKind.START)
    await h.stopped(T0 + 0.5)
    assert START_FAILED_BANNER_KEY in h.banners()


async def test_toggle_starts_and_stops(h: Harness):
    await h.arm()
    await h.send(CommandKind.TOGGLE)
    await h.started(ZERO)
    await h.send(CommandKind.TOGGLE)
    assert [n for n in h.gateway.names() if n.endswith("Record")] == ["StartRecord", "StopRecord"]


async def test_the_app_never_pauses_a_recording(h: Harness):
    await h.arm()
    await h.send(CommandKind.TOGGLE)
    await h.started(ZERO)
    await h.send(CommandKind.TOGGLE)
    assert not {"PauseRecord", "ResumeRecord", "ToggleRecordPause"} & set(h.gateway.names())


async def test_a_failed_stop_record_is_a_banner(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.gateway.fail("StopRecord", ObsRequestError("StopRecord", 501, "Output not running"))
    await h.send(CommandKind.STOP)
    assert "Output not running" in h.banners()[BannerKey.STOP_FAILED]
    assert h.actor.state is AppState.RECORDING


async def test_no_connected_source_at_start_is_a_warning_until_one_connects(h: Harness):
    h.sources[0].set_status(SourceStatus.CONNECTING)
    await h.arm()
    await h.started(ZERO)
    assert BannerKey.NO_SOURCE in h.banners()
    h.sources[0].set_status(SourceStatus.CONNECTED)
    await h.settle()
    assert BannerKey.NO_SOURCE not in h.banners()


async def test_zero_cues_keep_the_video_without_a_subtitle(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.stopped(ZERO + 30.0)
    manifest = load_manifest(h.finalised()[0])
    assert Flag.NO_CUES in manifest.flags
    assert manifest.files is not None and manifest.files.subtitle is None
    assert list(h.game_dir().glob("*.srt")) == []
    assert "no lines" in h.banners()[BannerKey.NO_CUES]
    assert h.vad.queued == []


async def test_arm_and_disarm_are_refused_while_recording(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.send(CommandKind.DISARM)
    await h.arm()
    assert h.actor.state is AppState.RECORDING
    assert "Stop the recording" in h.banners()[BannerKey.ARM]


async def test_a_finalise_failure_is_a_banner_and_the_game_stays_armed(h: Harness):
    await h.arm()
    await h.started(ZERO)
    h.video_path().unlink()
    await h.stopped(ZERO + 5.0)
    assert "gone" in h.banners()[BannerKey.FINALISE]
    assert h.actor.state is AppState.ARMED


async def test_vad_is_not_queued_when_disabled(h: Harness):
    h.cfg = AppConfig(output_root=h.cfg.output_root, vad=VadSettings(enabled=False))
    await h.arm()
    await h.started(ZERO)
    await h.line("こんにちは", ZERO + 1.0)
    await h.stopped(ZERO + 5.0)
    assert h.finalised() and h.vad.queued == []


async def test_quitting_while_recording_stops_obs_and_finishes_the_session(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    h.obs.stops_on_request = True
    h.obs.stale_active_reads = 1  # R2 item 9: still "active" for a moment after STOPPED
    h.clock.t = ZERO + 4.0
    await h.stop()
    assert h.gateway.names().count("StopRecord") == 1
    srt = (h.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8")
    assert srt == "1\n00:00:01,010 --> 00:00:03,660\nまえ\n"  # stop: the reading when StopRecord was answered
    assert list(h.incoming.iterdir()) == []
    assert h.sources[0].closed == 1
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_quitting_right_after_a_stop_waits_for_obs_to_say_inactive_then_restores(h: Harness):
    """E1: a quit 114 ms after ``STOPPED`` met a ``GetRecordStatus`` still saying active (R2 item 9)."""
    await h.arm()
    await h.started(ZERO)
    await h.stopped(ZERO + 5.0)
    h.obs.stale_active_reads = 1
    await h.stop()
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_quitting_while_idle_runs_a_restore_a_disarm_put_off_once_obs_says_inactive(h: Harness):
    """A disarm right after ``STOPPED`` meets ``GetRecordStatus`` still saying active and puts the restore
    off to the idle tick; a quit before that tick restores once the recording reports inactive."""
    await h.arm()
    await h.started(ZERO)
    await h.stopped(ZERO + 5.0)
    h.obs.stale_active_reads = 1
    await h.send(CommandKind.DISARM)
    assert h.actor.state is AppState.IDLE
    assert restore_path().exists()  # put off: the disarm read the recording as still active
    h.obs.stale_active_reads = 1
    await h.stop()
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_quitting_while_recording_leaves_the_session_to_the_next_launch_when_obs_does_not_stop(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    h.clock.t = ZERO + 4.0
    await h.stop()  # StopRecord is answered, but no STOPPED comes within QUIT_STOP_TIMEOUT_S
    manifest = incoming_manifest(h)
    assert manifest.state is ManifestState.RECORDING
    assert (manifest.counts.accepted, manifest.sources_used) == (1, ("textractor",))
    assert journal(h) == [  # the stop is kept, so the next launch's finalise ends the last cue with the video
        LineRecord(offset_ms=1010, text="まえ", source="textractor"),
        StopRecord(offset_ms=4010),
    ]
    assert h.sources[0].closed == 1
    assert restore_path().exists()
