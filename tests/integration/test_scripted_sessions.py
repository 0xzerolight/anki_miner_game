"""Scripted sessions through the real composition (spec 18.2; card T25).

Each test runs ``App`` as ``launch.main`` builds it, with the real session actor, finalise worker,
``ObsClient``, ``ObsProvisioner``, ``WebsocketSource``, feed and auto mode, against a
``FakeHookerServer`` and an OBS that answers from T14's ``FakeObs`` and plays one transcript recorded
from a real OBS by R2 (``tests/integration/obs_replay.py``, ``scripted.py``). The app arms, lines
arrive at scripted moments of the recording, and the recording's own frames and events follow at
theirs; the test then reads the files: a byte-exact ``.srt``, the manifest's counts and flags, and the
final names.

Every transcript of ``tests/fixtures/obs_transcripts/`` that describes a session the app drives or
observes is replayed. Skipped:

- ``provision.jsonl``: setup only, R2's driver provisioning with its own input names and request
  order; ``tests/obs/test_provision_replay.py`` replays it against ``FakeObs``.
- ``obs_sigterm.jsonl``: OBS ignored SIGTERM and reported nothing, no event and no stop; the
  recording ended outside the transcript (SIGINT then crashed OBS, unrecorded) and the connection
  closes are the driver's own, so there is no ending to replay.
- ``switch_not_ready.jsonl``: the driver's own arm and disarm switches beside a third client polling
  through the collection change (207). The app sends nothing between ``...Changing`` and
  ``...Changed`` (spec 6.2 step 3) and its gateway waits a change out (``tests/obs/test_client.py``).
- ``switch_refused.jsonl``: switches to names OBS lacks or to the current names and creates of
  existing names, none of which the app sends; ``FakeObs`` answers them as recorded (T14's tests).
- ``app_provision.jsonl`` arms 2 and 3: recorded before ``ensure_profile`` took over the switch to
  the app's profile (a26e411), so they no longer are the app's requests;
  ``tests/obs/test_provision_replay.py`` replays their provisioning against ``ObsProvisioner``.
  Arm 1 is replayed request by request below.
"""

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from anki_miner_game.lifecycle import auto as auto_mod
from anki_miner_game.models.constants import OBS_PROFILE_NAME
from anki_miner_game.models.manifest import Counts, Flag, ManifestState
from anki_miner_game.models.messages import AppState, CommandKind, UserCommand
from anki_miner_game.models.obs import ObsEventName
from anki_miner_game.models.profile import AutoSettings, CaptureKind, CaptureSettings
from anki_miner_game.obs import provision
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.restore import ObsRestore, load_restore
from tests.fakes.fake_obs_server import FakeObsServer, load_transcript
from tests.integration.obs_replay import FIXTURES, SWITCH_REQUESTS, rewrite_paths
from tests.integration.scripted import SLUG, TITLE, WAIT_S, AppRun, Clock, Scripted, ServerThread, profile
from tests.session.actor_harness import FakeDiscovery

PROBE_WINDOW: Final = "4194311\r\namg-probe-window\r\nprobe_window.py"
"""The window R2's probe opened, as ``window_retitle.jsonl``'s window lists name it."""
PROFILE_SWITCH: Final = frozenset({"SetCurrentProfile"})
SAMPLE_RATE: Final = ("Audio", "SampleRate")


@pytest.fixture
def scripted(qtbot, tmp_path, monkeypatch) -> Iterator[Callable[..., Scripted]]:
    """``scripted(transcript, **options)``: a launched ``Scripted`` run, closed after the test."""
    made: list[Scripted] = []

    def make(transcript: str, **options: Any) -> Scripted:
        run = Scripted(qtbot, tmp_path, monkeypatch, transcript, **options)
        made.append(run)
        run.launch()
        return run

    yield make
    for run in made:
        run.close()


def names(index: int) -> list[str]:
    stem = f"{TITLE} - {index:02d}"
    return [f"{stem}.mkv", f"{stem}.session.json", f"{stem}.srt"]


def test_normal_session(scripted):
    s = scripted("normal.jsonl")
    s.arm()
    s.line(-2.0, "まだ録画していない")  # armed, not recording: shown, not subtitled
    s.start()
    s.line(1.0, "おはよう、まゆり。")
    s.line(3.5, "【まゆり】トゥットゥルー♪")
    s.line(3.7, "【まゆり】トゥットゥルー♪")  # duplicate
    s.line(6.0, "……")  # no letters
    s.line(6.1, "何だよ")  # shown for 200 ms: skipped
    s.line(6.3, "ここは秋葉原だ。")
    s.tick(10.2)  # the drift sample 10 s after STARTED
    s.line(12.0, "エル・プサイ・コングルゥ")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,847 --> 00:00:02,997\nおはよう、まゆり。\n\n"
        "2\n00:00:03,347 --> 00:00:05,797\nトゥットゥルー♪\n\n"
        "3\n00:00:06,147 --> 00:00:11,497\nここは秋葉原だ。\n\n"
        "4\n00:00:11,847 --> 00:00:14,667\nエル・プサイ・コングルゥ\n"
    )
    manifest = s.manifest(1)
    assert manifest.state is ManifestState.READY
    assert manifest.counts == Counts(received=7, accepted=4, duplicate=1, no_letters=1, skip=1)
    assert manifest.flags == ()
    assert s.game_files() == names(1)
    assert s.incoming_files() == []
    s.check_replay()


def test_a_pause_on_a_recording_that_cannot_pause_changes_nothing(scripted):
    s = scripted("pause_noop.jsonl")  # the user's PauseRecord at 3.152 answers 100 and sends no event
    s.arm()
    s.start()
    s.line(1.0, "今日は晴れだ。")
    s.line(4.0, "ポーズしたはずなのに。")
    s.line(6.5, "まだ録画している。")
    s.line(9.0, "再開もできない。")  # ResumeRecord at 8.158 answered 503
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,859 --> 00:00:03,509\n今日は晴れだ。\n\n"
        "2\n00:00:03,859 --> 00:00:06,009\nポーズしたはずなのに。\n\n"
        "3\n00:00:06,359 --> 00:00:08,509\nまだ録画している。\n\n"
        "4\n00:00:08,859 --> 00:00:10,672\n再開もできない。\n"
    )
    assert s.manifest(1).counts == Counts(received=4, accepted=4)
    assert s.game_files() == names(1)
    s.check_replay()


def test_a_pause_made_in_obs_drops_its_lines_and_shifts_the_rest(scripted):
    s = scripted("pause_resume.jsonl")  # PAUSED at 4.154, RESUMED at 8.158
    s.arm()
    s.start()
    s.line(1.0, "始めよう。")
    s.line(3.0, "準備はいい?")
    s.line(5.0, "もう一度。")  # while paused: dropped, and not "the previous line" either
    s.line(9.0, "もう一度。")
    s.line(11.0, "終わりだ。")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,861 --> 00:00:02,511\n始めよう。\n\n"
        "2\n00:00:02,861 --> 00:00:04,507\n準備はいい?\n\n"
        "3\n00:00:04,857 --> 00:00:06,507\nもう一度。\n\n"
        "4\n00:00:06,857 --> 00:00:07,670\n終わりだ。\n"
    )
    manifest = s.manifest(1)
    assert manifest.counts == Counts(received=5, accepted=4, paused=1)
    assert manifest.flags == ()
    s.check_replay()


def test_a_pause_missed_while_disconnected_degrades_the_clock(scripted):
    s = scripted("missed_pause.jsonl")  # lost at 3.164, paused by another client, back at 7.173
    s.arm()
    s.start()
    s.line(1.0, "一行目。")
    s.line(2.5, "二行目。")
    s.line(5.0, "切断中の行。")  # journalled on the event clock (spec 17), past the paused video
    s.line(8.0, "再開後の行。")  # RESUMED at 7.214 (the user in OBS)
    s.line(9.5, "最後の行。")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,850 --> 00:00:02,000\n一行目。\n\n"
        "2\n00:00:02,350 --> 00:00:04,500\n二行目。\n\n"
        "3\n00:00:04,850 --> 00:00:05,502\n再開後の行。\n\n"
        "4\n00:00:05,852 --> 00:00:06,352\n最後の行。\n"
    )
    manifest = s.manifest(1)
    assert manifest.flags == (Flag.CLOCK_DEGRADED,)
    assert manifest.clock.degraded
    assert manifest.counts == Counts(received=5, accepted=4, skip=1)
    assert "clock_degraded" in s.banners()  # no drift sample with a duration: no lag to correct by
    s.check_replay()


def test_a_reconnect_mid_session_keeps_the_event_clock(scripted):
    s = scripted("reconnect.jsonl")  # lost at 3.153, back at 8.157, still recording and unpaused
    s.arm()
    s.start()
    s.line(1.0, "つながっている。")
    s.line(2.0, "まだ大丈夫。")
    s.line(5.0, "切れている間の行。")
    s.line(9.0, "戻ってきた。")
    s.line(10.5, "続けよう。")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,860 --> 00:00:01,510\nつながっている。\n\n"
        "2\n00:00:01,860 --> 00:00:04,510\nまだ大丈夫。\n\n"
        "3\n00:00:04,860 --> 00:00:08,510\n切れている間の行。\n\n"
        "4\n00:00:08,860 --> 00:00:10,010\n戻ってきた。\n\n"
        "5\n00:00:10,360 --> 00:00:10,860\n続けよう。\n"
    )
    manifest = s.manifest(1)
    assert manifest.flags == ()
    assert not manifest.clock.degraded
    assert manifest.counts == Counts(received=5, accepted=5)
    s.check_replay()


def test_a_split_ends_the_subtitle_at_the_first_file(scripted):
    s = scripted("split.jsonl")  # SplitRecordFile at 4.051 (in OBS), RecordFileChanged at 9.675
    s.arm()
    s.start()
    s.line(1.0, "分割の前。")
    s.line(3.0, "まだ一つ目のファイル。")
    s.line(6.0, "分割はまだ届いていない。")
    s.line(11.0, "二つ目のファイルの行。")  # shown, not subtitled
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,963 --> 00:00:02,613\n分割の前。\n\n"
        "2\n00:00:02,963 --> 00:00:05,613\nまだ一つ目のファイル。\n\n"
        "3\n00:00:05,963 --> 00:00:09,288\n分割はまだ届いていない。\n"
    )
    manifest = s.manifest(1)
    assert manifest.flags == (Flag.SPLIT_UNSUPPORTED,)
    assert manifest.counts == Counts(received=4, accepted=3)
    assert manifest.files is not None and manifest.files.video == f"{TITLE} - 01.mkv"
    assert "split_unsupported" in s.banners()
    assert s.incoming_files() == ["2026-09-21 18-41-08.mkv"]  # the second file stays where OBS wrote it
    s.check_replay()


def test_a_refused_split_leaves_the_session_whole(scripted):
    s = scripted("split_off_runtime.jsonl")  # SplitRecordFile at 2.047 answered 702, no event
    s.arm()
    s.start()
    s.line(1.0, "分割しない。")
    s.line(3.0, "ファイルは一つ。")
    s.line(6.0, "最後まで。")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,965 --> 00:00:02,615\n分割しない。\n\n"
        "2\n00:00:02,965 --> 00:00:05,615\nファイルは一つ。\n\n"
        "3\n00:00:05,965 --> 00:00:09,675\n最後まで。\n"
    )
    assert s.manifest(1).flags == ()
    assert s.incoming_files() == []
    s.check_replay()


def test_obs_exiting_ends_the_session_at_exit_started(scripted):
    s = scripted("obs_exit.jsonl")  # ExitStarted at 5.180, OBS closes with 1001 at 5.506
    s.arm()
    s.start()
    s.line(1.0, "OBSが閉じる前。")
    s.line(3.0, "もう少し。")
    s.line(4.5, "最後の台詞。")
    s.until(5.6)
    s.line(6.0, "OBSはもういない。")  # armed without OBS: not subtitled
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,857 --> 00:00:02,507\nOBSが閉じる前。\n\n"
        "2\n00:00:02,857 --> 00:00:04,007\nもう少し。\n\n"
        "3\n00:00:04,357 --> 00:00:04,857\n最後の台詞。\n"
    )
    manifest = s.manifest(1)
    assert manifest.flags == (Flag.OBS_EXITED,)
    assert manifest.counts == Counts(received=3, accepted=3)
    assert "obs_exited" in s.banners()
    s.close()
    assert s.restore_file().exists()  # OBS is gone: the restore waits for the next connection (spec 6.2)
    s.check_replay()


def test_obs_killed_ends_the_session_where_the_connection_dropped(scripted):
    s = scripted("obs_killed.jsonl")  # 1006 at 5.172, no ExitStarted
    s.arm()
    s.start()
    s.line(1.0, "まだ生きている。")
    s.line(3.0, "録画中。")
    s.line(4.5, "突然。")
    s.until(5.2)
    s.line(7.0, "誰も録画していない。")  # journalled on the event clock, past the video's end
    s.tick(10.2)  # "OBS gone" needs two "not running" answers in a row (spec 6.4)
    s.tick(15.2)
    s.settled()

    assert s.srt(1) == (
        "1\n00:00:00,864 --> 00:00:02,514\nまだ生きている。\n\n"
        "2\n00:00:02,864 --> 00:00:04,014\n録画中。\n\n"
        "3\n00:00:04,364 --> 00:00:04,864\n突然。\n"
    )
    manifest = s.manifest(1)
    assert manifest.flags == (Flag.OBS_EXITED,)
    assert manifest.counts == Counts(received=4, accepted=3, skip=1)
    assert "obs_exited" in s.banners()
    s.check_replay()


def test_two_sessions_in_one_arm_with_the_profile_switched_between(scripted):
    s = scripted("settings_apply.jsonl")  # between the sessions the user switched to Untitled and back
    s.arm()
    s.start()
    s.line(1.0, "一回目。")
    s.line(3.0, "一回目の終わり。")
    s.stop()
    s.line(6.5, "間の行。")  # armed, not recording
    s.start()
    s.line(9.0, "二回目。")
    s.line(11.0, "二回目の途中。")
    s.line(12.5, "二回目の終わり。")
    s.stop()
    s.until_end()

    assert s.srt(1) == (
        "1\n00:00:00,771 --> 00:00:02,421\n一回目。\n\n2\n00:00:02,771 --> 00:00:04,664\n一回目の終わり。\n"
    )
    assert s.srt(2) == (
        "1\n00:00:00,818 --> 00:00:02,468\n二回目。\n\n"
        "2\n00:00:02,818 --> 00:00:03,968\n二回目の途中。\n\n"
        "3\n00:00:04,318 --> 00:00:04,818\n二回目の終わり。\n"
    )
    assert [s.manifest(i).counts for i in (1, 2)] == [Counts(received=2, accepted=2), Counts(received=3, accepted=3)]
    assert s.game_files() == sorted(names(1) + names(2))
    s.close()
    assert (s.obs.current_profile, s.obs.current_collection) == ("Untitled", "Untitled")
    s.check_replay()


def test_auto_mode_starts_on_a_line_and_stops_once_the_window_is_gone(scripted, monkeypatch):
    monkeypatch.setattr(auto_mod, "POLL_S", 0.02)
    game = profile(
        capture=CaptureSettings(kind=CaptureKind.XCOMPOSITE, window=PROBE_WINDOW),
        auto=AutoSettings(enabled=True, start_on_first_line=True, stop_on_window_close=True),
    )
    s = scripted("window_retitle.jsonl", game=game)  # OBS's window lists at 0.003, 5.559 and 10.563
    s.arm()
    s.line(0.0, "この行で録画が始まる。")  # auto-start: StartRecord at 0.003, the line at offset 0
    s.line(2.0, "窓はまだある。")
    s.line(4.0, "タイトルが変わる前。")
    s.line(7.0, "タイトルが変わっても窓は同じ。")  # retitled: the pinned item is disabled, its xid is not
    s.until_end()  # closed: two polls without it stop the recording (StopRecord at 12.569)

    assert s.srt(1) == (
        "1\n00:00:00,000 --> 00:00:01,515\nこの行で録画が始まる。\n\n"
        "2\n00:00:01,865 --> 00:00:03,515\n窓はまだある。\n\n"
        "3\n00:00:03,865 --> 00:00:06,515\nタイトルが変わる前。\n\n"
        "4\n00:00:06,865 --> 00:00:12,085\nタイトルが変わっても窓は同じ。\n"
    )
    assert s.manifest(1).counts == Counts(received=4, accepted=4)
    assert s.state() is AppState.ARMED
    s.check_replay()


@pytest.mark.parametrize("transcript", ["start_failed_missing_dir.jsonl", "start_failed_unwritable.jsonl"])
def test_a_start_obs_never_reports_fails_after_ten_seconds(scripted, transcript):
    s = scripted(transcript)  # StartRecord answers 100; OBS shows a modal and sends no STARTED
    s.arm()
    s.start()
    s.tick(9.9)
    assert "start_failed" not in s.banners()
    s.tick(10.1)

    assert s.banners()["start_failed"] == "OBS did not start recording within 10 s; OBS's window says why."
    assert s.state() is AppState.ARMED
    assert s.incoming_files() == []
    s.check_replay()


@pytest.mark.parametrize(
    ("transcript", "output"),
    [
        ("switch_recording.jsonl", "recording"),
        ("switch_streaming.jsonl", "stream"),
        ("switch_replay_buffer.jsonl", "replay buffer"),
        ("synthetic-arm-virtualcam-active.jsonl", "virtual camera"),
    ],
)
def test_arming_is_refused_while_an_output_is_active(scripted, transcript, output):
    s = scripted(transcript, app_sends=frozenset())  # the user's own output, started before the arm
    s.command(CommandKind.ARM, s.recording.asked("GetStreamStatus"), slug=SLUG)
    s.settled()
    assert s.banners()["arm"] == f"OBS has an active {output}; stop it first."
    s.until_end()  # the user switches to the app's profile and back, then stops their output

    assert s.state() is None  # never armed
    assert not s.restore_file().exists()
    assert s.game_files() == [] and s.incoming_files() == []
    assert (s.obs.current_profile, s.obs.current_collection) == ("Untitled", "Untitled")
    s.check_replay()


def test_arm_and_disarm_with_every_output_idle(scripted):
    # The recorded switches are the recording client's own arm and disarm; the app's come from FakeObs.
    s = scripted("arm_disarm.jsonl", ignored=SWITCH_REQUESTS)
    s.arm(s.recording.asked("GetStreamStatus"))
    assert load_restore(s.restore_file()) == ObsRestore(profile="Untitled", collection="Untitled")
    assert s.obs.mutating() == ["SetCurrentProfile", "SetCurrentSceneCollection"]  # provisioned: nothing to write
    s.disarm(2.207)
    s.until_end()

    assert s.state() is AppState.IDLE
    assert s.obs.mutating()[2:] == ["SetCurrentSceneCollection", "SetCurrentProfile"]
    assert (s.obs.current_profile, s.obs.current_collection) == ("Untitled", "Untitled")
    assert not s.restore_file().exists()
    s.check_replay()


def test_arming_waits_out_obs_asking_to_restart_and_then_gives_up(scripted, monkeypatch):
    monkeypatch.setattr(provision, "SWITCH_TIMEOUT_S", 1.0)
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 0.2)
    s = scripted("switch_restart_prompt.jsonl", app_sends=PROFILE_SWITCH, settles=False, hooker=False)
    s.obs.profiles["Untitled"][SAMPLE_RATE] = "44100"  # changed since the app provisioned at 48000
    s.command(CommandKind.ARM, 0.004, slug=SLUG)
    s.until(0.063)  # the app's switch: both events came, the answer never does
    s.wait(lambda: "arm" in s.banners(), "the arm to fail")
    s.settled()

    banners = s.banners()
    assert banners["obs_question"].startswith("OBS is asking to restart: answer it in OBS's window")
    assert banners["arm"] == (
        f"Could not prepare OBS for {TITLE}: OBS did not finish switching to '{OBS_PROFILE_NAME}' within 1 s; "
        "an OBS dialog may be waiting for an answer."
    )
    assert s.state() is None
    assert s.obs.current_profile == OBS_PROFILE_NAME  # OBS switched; the question only holds the answer
    assert load_restore(s.restore_file()) == ObsRestore(profile="Untitled", collection="Untitled")  # waits
    assert ObsEventName.CONNECTION_LOST in [event.name for event in s.received]  # the unanswered request dropped it
    s.check_replay()


def test_arming_twice_when_obs_answers_every_switch(scripted):
    s = scripted("switch_restart_prompt_matched.jsonl", app_sends=PROFILE_SWITCH, settles=False, hooker=False)
    for name in ("Untitled", OBS_PROFILE_NAME):
        s.obs.profiles[name][SAMPLE_RATE] = "44100"
    for arm_at, disarm_at, until in ((0.005, 1.050, 2.0), (2.084, 3.132, 4.0)):
        s.command(CommandKind.ARM, arm_at, slug=SLUG)  # answered before its events (R2 item 5)
        s.until(disarm_at - 0.01)
        s.settled()
        assert s.state() is AppState.ARMED
        s.disarm(disarm_at)  # answered after its events
        s.until(until)
        s.settled()
        assert s.state() is AppState.IDLE

    assert "obs_question" not in s.banners() and s.obs.restart_questions == []
    assert (s.obs.current_profile, s.obs.current_collection) == ("Untitled", "Untitled")
    assert not s.restore_file().exists()
    s.check_replay()


# The app's own provisioning, request by request (E1, docs/m0/m1-exit-linux.md section 3) -----------

E1_GAME: Final = profile(
    slug="e1-provision", title="E1 Provision", capture=CaptureSettings(kind=CaptureKind.XCOMPOSITE)
)
"""E1's ``e1-provision`` game: ``capture.kind`` ``xcomposite``, no window pinned."""


class LaunchGate(FakeDiscovery):
    """OBS runs; the launch's first look waits for ``gate``, so an ``--arm`` given at launch is queued first."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def is_running(self) -> bool:
        self.gate.wait(WAIT_S)
        return super().is_running()


def first_app_run(tmp_path: Path) -> Path:
    """``app_provision.jsonl``'s first app run (conns 1 and 2) with this run's folders, as a transcript file.

    One exchange is added: the quit now polls ``GetRecordStatus`` before its restore until OBS says the
    recording is inactive (ba636a4, E1 finding 1), which the recorded app did not yet do. OBS's answer
    is the one it gave the reconcile just before: inactive.
    """
    lines = (FIXTURES / "app_provision.jsonl").read_text(encoding="utf-8").splitlines()
    records = [record for record in map(json.loads, lines) if record["conn"] in (1, 2)]
    records = rewrite_paths(records, tmp_path / "out" / "_incoming", tmp_path / "Videos")

    def frames(op: int, request_type: str) -> list[int]:
        return [
            i
            for i, r in enumerate(records)
            if r.get("msg", {}).get("op") == op and r["msg"]["d"]["requestType"] == request_type
        ]

    reconcile_answer = records[frames(7, "GetRecordStatus")[1]]  # after arm step 1's
    quit_at = frames(6, "GetStreamStatus")[1]  # the restore's first output check
    poll = {"op": 6, "d": {"requestType": "GetRecordStatus", "requestId": "t25-quit-poll"}}
    records[quit_at:quit_at] = [
        {**records[quit_at], "msg": poll},
        {**reconcile_answer, "t_mono": records[quit_at]["t_mono"]},
    ]
    path = tmp_path / "app_provision-run1.jsonl"
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    return path


def test_the_apps_first_arm_replays_request_by_request(qtbot, tmp_path, monkeypatch):
    """Arm 1 on an OBS that has never seen the app, as E1 recorded it through the whole composition.

    Launch with ``--arm``, provisioning (the audio copy, ``CreateProfile``, the re-activation, the new
    collection, scene and inputs), the first connection's reconcile after it, and the quit's restore.
    """
    servers = ServerThread()
    server = FakeObsServer(transcript=load_transcript(first_app_run(tmp_path)))
    servers.run(server.start())
    discovery = LaunchGate()
    run = AppRun(
        tmp_path,
        monkeypatch,
        clock=Clock(),
        servers=servers,
        server=server,
        game=E1_GAME,
        hooker=None,
        discovery=discovery,
    )
    try:
        run.launch_app(qtbot)
        run.post(UserCommand(CommandKind.ARM, slug=E1_GAME.slug))
        discovery.gate.set()
        servers.run(run.settle())
        assert run.state() is AppState.ARMED, run.banners()
        run.close()
        servers.run(asyncio.wait_for(server.replay_finished.wait(), WAIT_S))
    finally:
        run.close()
        servers.run(server.stop())
        servers.stop()

    assert server.unscripted == [] and server.errors == [], server.replay_position
    assert run.banners() == {}
    assert not run.restore_file().exists()
