"""Arming, disarming and the restore file (spec 6.2; 17 rows OBS not running, missing request, active
output, switch timeout, output folder not writable, free space; R2 items 3-6)."""

import asyncio

import pytest

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    CommandKind,
    SourceStatus,
    SourceStatusChanged,
    UserCommand,
)
from anki_miner_game.models.obs import ObsAuthError, ObsError, ObsUnsupportedError
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.restore import ObsRestore, load_restore, restore_path, save_restore
from anki_miner_game.session.session import BannerKey
from tests.session.actor_harness import SLUG, T0, FakeSource, Harness, profile

SWITCH_REQUESTS = ["SetCurrentProfile", "SetCurrentSceneCollection"]


async def test_arm_switches_obs_to_the_app_profile_and_collection(h: Harness):
    h.gateway.sent.clear()
    await h.arm()
    assert h.gateway.names() == [
        "GetStreamStatus",
        "GetRecordStatus",
        "GetReplayBufferStatus",
        "GetVirtualCamStatus",
        "GetProfileList",
        "GetSceneCollectionList",
        "SetCurrentProfile",
        "SetCurrentSceneCollection",
    ]
    assert (h.obs.profile, h.obs.collection) == (OBS_PROFILE_NAME, OBS_COLLECTION_NAME)
    assert load_restore(restore_path()) == ObsRestore(profile="Untitled", collection="Untitled")
    assert h.provisioner.profiles == [h.cfg]
    assert h.provisioner.collections == [h.profiles[SLUG]]
    assert h.sources[0].starts == 1
    assert h.states() == [(AppState.ARMED, SLUG)]
    assert h.banners() == {}


async def test_arm_creates_the_output_folder(h: Harness):
    await h.arm()
    assert h.incoming.is_dir()


async def test_arm_on_the_app_profile_writes_no_restore_file(h: Harness):
    h.obs.profile, h.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    await h.arm()
    assert not restore_path().exists()
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())  # R2 item 4: no switch to what is current
    assert h.actor.state is AppState.ARMED


async def test_arm_skips_the_switch_that_is_already_current(h: Harness):
    h.obs.profile = OBS_PROFILE_NAME
    await h.arm()
    assert "SetCurrentProfile" not in h.gateway.names()  # R2 item 4: it would answer and send no event
    assert h.gateway.names().count("SetCurrentSceneCollection") == 1
    assert load_restore(restore_path()) == ObsRestore(profile=OBS_PROFILE_NAME, collection="Untitled")


async def test_arm_keeps_the_names_of_an_earlier_unfinished_arm(h: Harness):
    save_restore(restore_path(), ObsRestore(profile="Mine", collection="Scenes"))
    await h.arm()
    assert load_restore(restore_path()) == ObsRestore(profile="Mine", collection="Scenes")


async def test_a_missing_app_profile_is_left_to_the_provisioner(h: Harness):
    h.obs.profiles = ["Untitled"]
    h.obs.collections = ["Untitled"]
    await h.arm()
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())
    assert h.provisioner.profiles == [h.cfg]
    assert h.actor.state is AppState.ARMED


@pytest.mark.parametrize(
    ("attribute", "label"),
    [
        ("stream_active", "stream"),
        ("record_active", "recording"),
        ("replay_active", "replay buffer"),
        ("vcam_active", "virtual camera"),
    ],
)
async def test_arm_refuses_while_an_output_is_active(h: Harness, attribute: str, label: str):
    setattr(h.obs, attribute, True)
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert label in h.banners()[BannerKey.ARM]
    assert not set(SWITCH_REQUESTS) & set(h.gateway.names())
    assert not restore_path().exists()
    assert h.sources[0].starts == 0


async def test_unavailable_replay_buffer_and_virtual_camera_do_not_block(h: Harness):
    assert h.obs.replay_active is None and h.obs.vcam_active is None  # 604 "not available", R2 item 6
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_arm_launches_obs_when_it_is_not_running(rig: Harness):
    rig.discovery.running = False
    await rig.start()
    assert rig.gateway.connects == 0
    await rig.arm()
    assert (rig.discovery.enabled, rig.discovery.launches, rig.gateway.connects) == (1, 1, 1)
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.CONNECTING) in rig.events
    assert rig.actor.state is AppState.ARMED


async def test_obs_that_never_answers_after_launch_is_a_banner(rig: Harness):
    rig.discovery.running = False
    rig.discovery.ready = False
    await rig.start()
    await rig.arm()
    assert "30 s" in rig.banners()[BannerKey.OBS]
    assert rig.gateway.connects == 0
    assert rig.actor.state is AppState.IDLE


async def test_missing_request_names_the_request_and_the_version(rig: Harness):
    rig.gateway.connect_error = ObsUnsupportedError("29.1.3", ("SetRecordDirectory",))
    await rig.start()
    await rig.arm()
    text = rig.banners()[BannerKey.OBS]
    assert "29.1.3" in text and "SetRecordDirectory" in text
    assert rig.actor.state is AppState.IDLE


async def test_authentication_failure_asks_for_the_password(rig: Harness):
    rig.gateway.connect_error = ObsAuthError("authentication failed")
    await rig.start()
    await rig.arm()
    assert "password" in rig.banners()[BannerKey.OBS]
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.DISCONNECTED) in rig.events


async def test_a_switch_that_times_out_is_undone(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "SWITCH_TIMEOUT_S", 0.05)
    h.obs.lost_switch_events = 1  # OBS switches and answers, but the CurrentProfileChanged event never comes
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "within" in h.banners()[BannerKey.ARM]
    assert h.obs.profile == "Untitled"
    assert not restore_path().exists()
    assert h.provisioner.profiles == []


async def test_obs_asking_to_restart_fails_the_arm_and_restores_at_the_retry(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 0.05)
    h.obs.restart_question = asyncio.Event()  # nobody answers it
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "OBS is asking to restart" in h.banners()[BannerKey.ARM]
    assert h.obs.profile == OBS_PROFILE_NAME  # OBS switched before it asked (R2 item 3)
    assert h.sources[0].starts == 0 and h.provisioner.profiles == []
    assert h.gateway.drops == 1  # the unanswered request was cancelled: the gateway dropped the link
    # Neither the failed arm nor the reconnect restores at once: a switch now would meet the open question.
    assert h.gateway.names().count("SetCurrentProfile") == 1
    assert restore_path().exists()
    h.obs.restart_question = None  # the user answers it
    await h.tick(h.clock.t + session_mod.RESTORE_RETRY_S)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_an_answer_within_the_question_window_lets_the_arm_go_on(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 5.0)
    question = h.obs.restart_question = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, question.set)  # OBS answers a little late
    h.actor.post(UserCommand(CommandKind.ARM, slug=SLUG))
    async with asyncio.timeout(5):
        while h.actor.state is not AppState.ARMED:
            await asyncio.sleep(0.01)
    assert h.gateway.drops == 0
    assert BannerKey.ARM not in h.banners()


async def test_a_restore_that_meets_the_restart_question_is_done(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 0.05)
    await h.arm()
    h.obs.restart_question = asyncio.Event()  # OBS asks when it goes back to the user's profile
    await h.send(CommandKind.DISARM)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")  # R2 item 5: done on the event
    assert not restore_path().exists()
    assert "answer it in OBS's window" in h.banners()[BannerKey.OBS_QUESTION]


async def test_a_provisioning_failure_restores_obs(h: Harness):
    h.provisioner.error = ObsError("CreateInput failed")
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "CreateInput failed" in h.banners()[BannerKey.ARM]
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()
    assert h.sources[0].starts == 0


async def test_an_output_folder_that_cannot_be_written_refuses_arming(h: Harness):
    h.output_root.parent.mkdir(parents=True, exist_ok=True)
    h.output_root.write_text("a file where the folder should be", encoding="utf-8")
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "cannot be written" in h.banners()[BannerKey.ARM]
    assert "GetProfileList" not in h.gateway.names()


async def test_low_free_space_warns_and_arms_anyway(h: Harness):
    h.free_bytes = 3_200_000_000
    await h.arm()
    assert h.actor.state is AppState.ARMED
    assert "3.2 GB" in h.banners()[BannerKey.LOW_DISK]


async def test_settings_that_apply_at_the_next_arm_are_a_warning(h: Harness):
    h.provisioner.needs_restart = True
    await h.arm()
    assert h.actor.state is AppState.ARMED
    text = h.banners()[BannerKey.OBS_RESTART]
    assert "disarm and arm" in text
    assert "SetCurrentProfile" in h.gateway.names()  # the arm's own switch; nothing else restarts OBS


async def test_unknown_and_invalid_games_are_refused(h: Harness):
    await h.arm("no-such-game")
    assert "no-such-game" in h.banners()[BannerKey.ARM]
    h.profiles["bad"] = profile(slug="bad", title=" ")
    await h.arm("bad")
    assert "title is empty" in h.banners()[BannerKey.ARM]
    assert h.actor.state is AppState.IDLE


async def test_arming_another_game_restarts_the_sources_and_names_it(h: Harness):
    h.profiles["zero"] = profile(slug="zero", title="Zero Escape")
    first = h.sources
    await h.arm()
    h.sources = [FakeSource("agent")]
    await h.arm("zero")
    assert (first[0].stops, first[0].closed) == (1, 1)
    assert h.sources[0].starts == 1
    assert h.states() == [(AppState.ARMED, SLUG), (AppState.ARMED, "zero")]
    assert h.provisioner.collections[-1].slug == "zero"
    assert load_restore(restore_path()) == ObsRestore(profile="Untitled", collection="Untitled")


async def test_arming_another_game_is_refused_while_a_stream_runs(h: Harness):
    h.profiles["zero"] = profile(slug="zero", title="Zero Escape")
    await h.arm()
    h.obs.stream_active = True
    await h.arm("zero")
    assert h.states() == [(AppState.ARMED, SLUG)]
    assert h.sources[0].stops == 0


async def test_disarm_closes_the_sources_and_restores_obs(h: Harness):
    await h.arm()
    await h.send(CommandKind.DISARM)
    assert (h.sources[0].stops, h.sources[0].closed) == (1, 1)
    assert h.states() == [(AppState.ARMED, SLUG), (AppState.IDLE, None)]
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_restore_waits_for_an_active_stream(h: Harness):
    await h.arm()
    h.obs.stream_active = True
    await h.send(CommandKind.DISARM)
    assert h.actor.state is AppState.IDLE
    assert h.obs.profile == OBS_PROFILE_NAME
    assert restore_path().exists()
    h.obs.stream_active = False
    await h.tick(h.clock.t + session_mod.RESTORE_RETRY_S)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_a_restore_left_at_launch_waits_for_obs_to_open(rig: Harness):
    save_restore(restore_path(), ObsRestore(profile="Untitled", collection="Untitled"))
    rig.obs.profile, rig.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    rig.discovery.running = False
    await rig.start()
    await rig.tick(T0 + 1.0)
    assert rig.gateway.connects == 0
    rig.discovery.running = True  # the user opens OBS
    await rig.tick(T0 + 1.0 + session_mod.RESTORE_RETRY_S)
    assert rig.gateway.connects == 1
    assert (rig.obs.profile, rig.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()


async def test_restore_skips_a_profile_obs_no_longer_has(h: Harness):
    await h.arm()
    h.obs.profiles.remove("Untitled")
    await h.send(CommandKind.DISARM)
    assert h.obs.profile == OBS_PROFILE_NAME
    assert h.obs.collection == "Untitled"
    assert not restore_path().exists()


async def test_disarm_while_idle_does_nothing(h: Harness):
    await h.send(CommandKind.DISARM)
    assert h.states() == []


async def test_quitting_while_armed_disarms(h: Harness):
    await h.arm()
    await h.stop()
    assert (h.sources[0].stops, h.sources[0].closed) == (1, 1)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
