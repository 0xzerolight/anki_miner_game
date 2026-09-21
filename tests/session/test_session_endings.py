"""Ending a session without a plain STOPPED: split, OBS exit, lost connection (spec 6.4, 7; 17 rows
RecordFileChanged, connection lost while recording, OBS exits while recording; R2 items 10, 12)."""

from anki_miner_game.models.manifest import Flag
from anki_miner_game.models.messages import AppState, LineAccepted
from anki_miner_game.models.obs import ObsEventName
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.journal import LineRecord, StopRecord, read_journal
from anki_miner_game.session.manifest import load_manifest
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import OBS_STEM, T0, TITLE, Harness

ZERO = T0 + 1.0
SECOND_STEM = "2026-10-02 18-05-00"


def srt(h: Harness) -> str:
    return (h.game_dir() / f"{TITLE} - 01.srt").read_text(encoding="utf-8")


async def lose_connection(h: Harness, t: float) -> None:
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST, {}, t)


async def test_a_split_finalises_the_first_file_and_flags_it(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    second = h.video_path(SECOND_STEM)
    second.write_bytes(b"the second file")
    await h.emit(ObsEventName.RECORD_FILE_CHANGED, {"newOutputPath": str(second)}, ZERO + 5.0)
    await h.line("あと", ZERO + 6.0)
    assert h.accepted()[-1] == LineAccepted(h.accepted()[-1].line, None)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl") == [
        LineRecord(offset_ms=1010, text="まえ", source="textractor"),
        StopRecord(offset_ms=5010),
    ]
    assert "split" in h.banners()[BannerKey.SPLIT]
    await h.stopped(ZERO + 10.0)  # its outputPath still names the first file (R2 item 10)
    assert srt(h) == "1\n00:00:01,010 --> 00:00:04,660\nまえ\n"
    assert Flag.SPLIT_UNSUPPORTED in load_manifest(h.finalised()[0]).flags
    assert sorted(p.name for p in h.incoming.iterdir()) == [f"{SECOND_STEM}.mkv"]


async def test_exit_started_ends_the_session_flagged_obs_exited(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    await h.emit(ObsEventName.EXIT_STARTED, {}, ZERO + 8.0)
    assert srt(h) == "1\n00:00:01,010 --> 00:00:07,660\nまえ\n"
    assert Flag.OBS_EXITED in load_manifest(h.finalised()[0]).flags
    assert "OBS closed" in h.banners()[BannerKey.OBS_EXITED]
    assert h.actor.state is AppState.ARMED
    await lose_connection(h, ZERO + 8.3)  # R2 item 12: close 1001, no STOPPED
    assert len(h.finalised()) == 1


async def test_lines_keep_being_journalled_while_the_connection_is_lost(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await lose_connection(h, ZERO + 2.0)
    await h.line("まだ", ZERO + 3.0)
    assert read_journal(h.incoming / f"{OBS_STEM}.lines.jsonl") == [
        LineRecord(offset_ms=3010, text="まだ", source="textractor")
    ]
    checks = h.discovery.running_checks
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S - 0.5)
    assert h.discovery.running_checks == checks  # not due yet
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S)
    assert h.discovery.running_checks == checks + 1  # OBS still runs: the recording goes on
    assert h.actor.state is AppState.RECORDING


async def test_obs_gone_after_a_lost_connection_ends_the_session(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await h.line("まえ", ZERO + 1.0)
    await lose_connection(h, ZERO + 2.0)  # R2 item 12: a killed OBS closes with 1006 and no ExitStarted
    h.discovery.running = False
    await h.line("あと", ZERO + 3.0)
    await h.line("もっと", ZERO + 4.0)
    await h.tick(ZERO + 2.0 + session_mod.OBS_GONE_CHECK_S)
    assert h.actor.state is AppState.RECORDING  # one "not running" is not enough
    await h.tick(ZERO + 2.0 + 2 * session_mod.OBS_GONE_CHECK_S)
    # The stop is the reading taken when the connection dropped (2010): both lines journalled after
    # the loss lie past it, so they are skips and only the first line is a cue.
    assert srt(h) == "1\n00:00:01,010 --> 00:00:01,660\nまえ\n"
    manifest = load_manifest(h.finalised()[0])
    assert Flag.OBS_EXITED in manifest.flags
    assert manifest.counts.skip == 2
    assert h.actor.state is AppState.ARMED


async def test_obs_gone_needs_two_false_answers_in_a_row(h: Harness):
    await h.arm()
    await h.started(ZERO)
    await lose_connection(h, ZERO + 2.0)
    h.discovery.answers = [False, True, False]  # is_running answers True when it cannot tell
    for n in range(1, 4):
        await h.tick(ZERO + 2.0 + n * session_mod.OBS_GONE_CHECK_S)
    assert h.actor.state is AppState.RECORDING
    assert h.finalised() == []
    h.discovery.running = False
    await h.tick(ZERO + 2.0 + 4 * session_mod.OBS_GONE_CHECK_S)  # the second False in a row
    assert h.actor.state is AppState.ARMED
    assert Flag.OBS_EXITED in load_manifest(h.finalised()[0]).flags
