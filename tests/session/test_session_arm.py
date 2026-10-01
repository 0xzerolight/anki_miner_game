"""Arming, disarming and the restore file (spec 6.2; 17 rows OBS not running, missing request, active
output, switch timeout, output folder not writable, free space; R2 items 3-6)."""

import ast
import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from anki_miner_game.gui.banner_keys import ARM_FAILED_KEYS
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    BannerRaised,
    CommandKind,
    ObsEvent,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsUnsupportedError,
    WsConfig,
)
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.restore import ObsRestore, load_restore, restore_path, save_restore
from anki_miner_game.session.session import OBS_AUTH_TEXT, OBS_SERVER_OFF_TEXT, BannerKey
from tests.gui.obs_listing_fake import ListingObs
from tests.obs.fake_obs import LINUX_X11_KINDS
from tests.session.actor_harness import SLUG, T0, FakeSource, Harness, profile

SWITCH_REQUESTS = ["SetCurrentProfile", "SetCurrentSceneCollection"]


class ActorObs(ListingObs):
    """T14's stateful fake OBS (audio values, restart questions, output statuses) as the actor's gateway."""

    async def connect(self) -> ObsInfo:
        for handler in self.handlers:
            handler(ObsEvent(ObsEventName.CONNECTED, {}, 0.0))
        return ObsInfo("32.2.2", "5.7.4", frozenset(REQUIRED_REQUESTS))

    def _GetVersion(self) -> dict[str, Any]:  # noqa: N802
        return {"obsVersion": "32.2.2", "obsWebSocketVersion": "5.7.4"}


async def _no_sleep(_seconds: float) -> None:
    pass


async def test_arm_switches_obs_to_the_app_profile_and_collection(h: Harness):
    """Spec 6.2 steps 2-3: the names saved, then the profile (by ``ensure_profile``), then the collection."""
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
    assert h.provisioner.started_on == ["Untitled"]  # spec 11.3: it copies the user's audio values
    assert h.provisioner.collections == [h.profiles[SLUG]]
    assert h.sources[0].starts == 1
    assert h.states() == [(AppState.ARMED, SLUG)]
    assert h.banners() == {}


async def test_only_the_first_switch_to_the_app_profile_meets_a_sample_rate_it_lacks(tmp_path):
    """The user arms from a second profile at 44.1 kHz; the app's profile was provisioned at 48 kHz.

    ``ensure_profile`` starts on the user's profile, so it copies the rate into the app's profile
    (spec 11.3): OBS asks to restart at that first switch only (R2 item 3), never at the disarm or a
    later arm. The fake OBS answers each question "No".
    """
    obs = ActorObs(input_kinds=LINUX_X11_KINDS)
    provisioner = ObsProvisioner(obs, platform="linux", sleep=_no_sleep)
    rig = Harness(tmp_path, gateway=obs, provisioner=provisioner)
    await provisioner.ensure_profile(rig.cfg)  # the wizard, from "Untitled" at OBS's 48 kHz
    await obs.request("SetCurrentProfile", profileName="Untitled")
    await obs.request("CreateProfile", profileName="Second")
    await asyncio.sleep(0)  # OBS lands on the new profile
    obs.profiles["Second"][("Audio", "SampleRate")] = "44100"
    assert obs.restart_questions == []
    await rig.start()
    try:
        for _ in range(2):
            await rig.arm()
            assert rig.actor.state is AppState.ARMED
            await rig.send(CommandKind.DISARM)
            assert rig.actor.state is AppState.IDLE
    finally:
        await rig.stop()
    assert obs.restart_questions == [("Second", OBS_PROFILE_NAME)]
    assert obs.profiles[OBS_PROFILE_NAME][("Audio", "SampleRate")] == "44100"
    assert (obs.current_profile, obs.current_collection) == ("Second", "Untitled")
    assert BannerKey.ARM not in rig.banners()


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


async def test_the_idle_restore_retry_does_not_overwrite_a_more_specific_obs_banner(h: Harness):
    """S5-2: OBS dies while armed/recording (``obs_restore.json`` kept), and the next Arm's own 30 s
    "OBS did not answer" banner must survive the idle restore retry that follows moments later, once
    OBS is running again but still stuck behind its own crash dialog (refusing the connection)."""
    save_restore(restore_path(), ObsRestore(profile="Untitled", collection="Untitled"))
    await h.emit(ObsEventName.CONNECTION_LOST)  # OBS died; no session, so this alone does not arm/idle-loop
    h.discovery.running = False
    h.discovery.ready = False
    await h.arm()
    assert "30 s" in h.banners()[BannerKey.OBS]
    assert h.discovery.running is True  # launch() ran meanwhile; OBS is up but stuck behind its dialog
    h.gateway.connect_error = ObsConnectError(
        "cannot connect to OBS at 127.0.0.1:4455: ConnectionRefusedError: [WinError 10061] ..."
    )
    await h.tick(T0 + 5.0)  # the idle restore retry's own connect attempt fails too
    assert "30 s" in h.banners()[BannerKey.OBS]  # kept, not replaced by the retry's raw ConnectionRefused


async def test_the_idle_restore_retry_shows_its_failure_when_no_obs_banner_is_up(h: Harness):
    """S5-2's quiet retry stays quiet only over a banner already shown: with none up (OBS started by
    hand after a launch without it, and still refusing), its failure is the only sign the restore
    is stuck, so it shows."""
    save_restore(restore_path(), ObsRestore(profile="Untitled", collection="Untitled"))
    await h.emit(ObsEventName.CONNECTION_LOST)
    h.discovery.running = True
    h.gateway.connect_error = ObsConnectError(
        "cannot connect to OBS at 127.0.0.1:4455: ConnectionRefusedError: [WinError 10061] ..."
    )
    assert BannerKey.OBS not in h.banners()
    await h.tick(T0 + 5.0)
    assert "Cannot connect to OBS" in h.banners()[BannerKey.OBS]


async def test_arm_with_the_server_off_says_how_set_up_obs_turns_it_on(rig: Harness):
    """S5-3, UJ-10: OBS runs with its WebSocket server off; the banner names Set up OBS (no menu path)."""
    rig.discovery.running = True
    rig.discovery.ws_config = WsConfig(server_enabled=False, port=4455, password=None, auth_required=False)
    rig.gateway.connect_error = ObsConnectError(
        "cannot connect to OBS at 127.0.0.1:4455: ConnectionRefusedError: [WinError 10061] ..."
    )
    await rig.start()
    await rig.arm()
    assert rig.banners()[BannerKey.OBS] == OBS_SERVER_OFF_TEXT
    assert OBS_SERVER_OFF_TEXT == "OBS's WebSocket server is off. Close OBS and press Set up OBS: the app turns it on."
    assert rig.actor.state is AppState.IDLE


async def test_obs_started_meanwhile_with_its_server_off_is_the_server_off_banner(rig: Harness):
    """D-03: the shared starter cannot turn the server on once OBS runs (``ObsServerOffError``)."""
    rig.discovery.running = False
    await rig.start()  # the launch's own look: not running
    rig.discovery.answers = [False, True]  # not running, then running once the enable failed
    rig.discovery.ensure_server_enabled = lambda: False  # type: ignore[method-assign]
    await rig.arm()
    assert rig.banners()[BannerKey.OBS] == OBS_SERVER_OFF_TEXT
    assert rig.discovery.launches == 0
    assert rig.actor.state is AppState.IDLE


async def test_arm_refused_with_the_server_on_hints_at_safe_mode(rig: Harness):
    """S5-3 variant: the config says the server is on but the connection is still refused (OBS Safe
    Mode, or a dialog in its window); the banner adds a short hint instead of the raw text alone."""
    rig.discovery.running = True
    rig.discovery.ws_config = WsConfig(server_enabled=True, port=4455, password=None, auth_required=False)
    rig.gateway.connect_error = ObsConnectError(
        "cannot connect to OBS at 127.0.0.1:4455: ConnectionRefusedError: [WinError 10061] ..."
    )
    await rig.start()
    await rig.arm()
    text = rig.banners()[BannerKey.OBS]
    assert "ConnectionRefusedError" in text
    assert "Safe Mode" in text


async def test_missing_request_names_the_request_and_the_version(rig: Harness):
    rig.gateway.connect_error = ObsUnsupportedError("29.1.3", ("SetRecordDirectory",))
    await rig.start()
    await rig.arm()
    text = rig.banners()[BannerKey.OBS]
    assert "29.1.3" in text and "SetRecordDirectory" in text
    assert rig.actor.state is AppState.IDLE


async def test_authentication_failure_points_at_set_up_obs(rig: Harness):
    rig.gateway.connect_error = ObsAuthError("authentication failed")
    await rig.start()
    await rig.arm()
    assert rig.banners()[BannerKey.OBS] == OBS_AUTH_TEXT
    assert OBS_AUTH_TEXT == "OBS rejected the app's password. Press Set up OBS to enter it."
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.DISCONNECTED) in rig.events


async def test_unreadable_websocket_settings_point_at_set_up_obs(rig: Harness):
    rig.gateway.connect_error = ObsConfigError("Expecting value: line 1 column 1")
    await rig.start()
    await rig.arm()
    assert rig.banners()[BannerKey.OBS] == (
        "OBS's WebSocket settings cannot be read (Expecting value: line 1 column 1); press Set up OBS."
    )


@pytest.mark.parametrize(
    "home_profile",
    [
        "Untitled",  # the profile switch, made by ensure_profile, loses its event
        OBS_PROFILE_NAME,  # already on the app's profile: the actor's collection switch loses it
    ],
)
async def test_a_switch_that_times_out_is_undone(h: Harness, monkeypatch, home_profile: str):
    monkeypatch.setattr(session_mod, "SWITCH_TIMEOUT_S", 0.05)
    h.provisioner.switch_timeout_s = 0.05
    h.obs.profile = home_profile
    h.obs.lost_switch_events = 1  # OBS switches and answers, but the ...Changed event never comes
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "within" in h.banners()[BannerKey.ARM]
    assert (h.obs.profile, h.obs.collection) == (home_profile, "Untitled")
    assert not restore_path().exists()
    assert h.provisioner.collections == []


async def test_a_switch_whose_answer_comes_first_waits_for_its_event(h: Harness):
    h.obs.late_switch_events = True  # R2 item 5: the step completes on ...Changed, not on the answer
    h.actor.post(UserCommand(CommandKind.ARM, slug=SLUG))
    async with asyncio.timeout(5):
        while h.actor.state is not AppState.ARMED:
            await asyncio.sleep(0.005)
    assert (h.obs.profile, h.obs.collection) == (OBS_PROFILE_NAME, OBS_COLLECTION_NAME)
    assert BannerKey.ARM not in h.banners()


async def test_an_unanswered_restart_question_fails_the_arm_and_restores_at_the_retry(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 0.05)
    h.provisioner.switch_timeout_s = 0.3
    h.obs.restart_question = asyncio.Event()  # nobody answers it
    await h.arm()
    assert h.actor.state is AppState.IDLE
    assert "OBS is asking to restart" in h.banners()[BannerKey.OBS_QUESTION]
    assert "within" in h.banners()[BannerKey.ARM]
    assert h.obs.profile == OBS_PROFILE_NAME  # OBS switched before it asked (R2 item 3)
    assert h.sources[0].starts == 0 and h.provisioner.collections == []
    assert h.gateway.drops == 1  # the unanswered request was cancelled: the gateway dropped the link
    # Neither the failed arm nor the reconnect restores at once: a switch now would meet the open question.
    assert h.gateway.names().count("SetCurrentProfile") == 1
    assert restore_path().exists()
    h.obs.restart_question = None  # the user answers it
    await h.tick(h.clock.t + session_mod.RESTORE_RETRY_S)
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()
    assert BannerKey.OBS_QUESTION not in h.banners()


async def test_the_restart_question_is_a_banner_while_provisioning_waits_for_the_answer(h: Harness, monkeypatch):
    """Spec 6.2 step 3: ``...Changed`` came and the answer did not; the arm goes on once it is answered."""
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 0.02)
    question = h.obs.restart_question = asyncio.Event()
    h.actor.post(UserCommand(CommandKind.ARM, slug=SLUG))
    async with asyncio.timeout(5):
        while BannerKey.OBS_QUESTION not in h.banners():
            await asyncio.sleep(0.005)
    assert "OBS is asking to restart" in h.banners()[BannerKey.OBS_QUESTION]
    assert h.actor.state is AppState.IDLE and h.provisioner.collections == []
    question.set()  # the user answers "No"
    async with asyncio.timeout(5):
        while h.actor.state is not AppState.ARMED:
            await asyncio.sleep(0.005)
    assert h.gateway.drops == 0
    assert BannerKey.OBS_QUESTION not in h.banners() and BannerKey.ARM not in h.banners()
    assert h.provisioner.collections == [h.profiles[SLUG]]


async def test_a_quick_answer_raises_no_question_banner(h: Harness, monkeypatch):
    monkeypatch.setattr(session_mod, "RESTART_QUESTION_S", 5.0)
    question = h.obs.restart_question = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, question.set)  # OBS answers a little late
    h.actor.post(UserCommand(CommandKind.ARM, slug=SLUG))
    async with asyncio.timeout(5):
        while h.actor.state is not AppState.ARMED:
            await asyncio.sleep(0.01)
    assert h.gateway.drops == 0
    assert not [e for e in h.events if isinstance(e, BannerRaised)]


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


def test_a_folder_windows_denies_is_unwritable_at_the_first_refusal(tmp_path, monkeypatch):
    """S5-4: ``tempfile.TemporaryFile`` retried the ``PermissionError`` of a folder whose ACL denies
    writing 2**31 times on Windows (``os.access`` calls any folder writable), hanging the actor on Arm."""
    calls: list[str] = []

    def denied(path, *args, **kwargs):
        calls.append(os.fspath(path))
        if len(calls) > 3:
            raise AssertionError("a PermissionError was retried")
        raise PermissionError(13, "Access is denied", os.fspath(path))

    monkeypatch.setattr(os, "name", "nt")  # with os.access(tmp_path, W_OK) true, as Windows says of any folder
    monkeypatch.setattr(os, "open", denied)
    assert session_mod._writable(tmp_path) is False
    assert len(calls) == 1


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
    assert "press Done playing and then Start recording again" in text
    assert h.gateway.names().count("SetCurrentProfile") == 1  # ensure_profile's switch; nothing restarts OBS


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


_ARM = re.compile(r"\b(dis)?arm(s|ed|ing)?\b|\bcues?\b", re.IGNORECASE)


class _ActorTexts(ast.NodeVisitor):
    """Every string constant in ``session.py`` that can reach the user: docstrings, ``BannerKey`` values and
    log calls are left out."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def _body(self, node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        for child in body:
            self.visit(child)

    def visit_Module(self, node: ast.Module) -> None:
        self._body(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name != "BannerKey":
            self._body(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._body(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):  # attribute docstrings
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "log"):
            self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.texts.append(node.value)


def test_no_actor_text_says_arm_cue_websocket_or_a_menu_path():
    """D-01, UJ-32: the user never meets "arm"; "line" not "cue"; "WebSocket"; no menu that does not exist."""
    source = Path(session_mod.__file__).read_text(encoding="utf-8")
    finder = _ActorTexts()
    finder.visit(ast.parse(source))
    wrong = [
        text
        for text in finder.texts
        if _ARM.search(text) or "websocket" in text or "Setup wizard" in text or "File ->" in text
    ]
    assert wrong == []


def raised_since(h: Harness, mark: int) -> set[str]:
    return {e.banner.key for e in h.events[mark:] if isinstance(e, BannerRaised)}


async def _unknown_game(h: Harness) -> str:
    return "no-such-game"


async def _invalid_game(h: Harness) -> str:
    h.profiles["bad"] = profile(slug="bad", title=" ")
    return "bad"


async def _unwritable_folder(h: Harness) -> str:
    h.output_root.parent.mkdir(parents=True, exist_ok=True)
    h.output_root.write_text("a file where the folder should be", encoding="utf-8")
    return SLUG


async def _obs_unreachable(h: Harness) -> str:
    h.gateway.connected = False
    await h.emit(ObsEventName.CONNECTION_LOST)
    h.gateway.connect_error = ObsConnectError("cannot connect to OBS at 127.0.0.1:4455")
    return SLUG


async def _output_active(h: Harness) -> str:
    h.obs.stream_active = True
    return SLUG


async def _provisioning_fails(h: Harness) -> str:
    h.provisioner.error = ObsError("CreateInput failed")
    return SLUG


async def _a_bug(h: Harness) -> str:
    h.provisioner.error = RuntimeError("a bug")
    return SLUG


@pytest.mark.parametrize(
    "prepare",
    [_unknown_game, _invalid_game, _unwritable_folder, _obs_unreachable, _output_active, _provisioning_fails, _a_bug],
)
async def test_a_get_ready_that_fails_raises_a_key_the_window_waits_for(
    h: Harness, prepare: Callable[[Harness], Awaitable[str]]
):
    """Master 4.6: the window's pending Get ready / Start recording ends on one of ``ARM_FAILED_KEYS``."""
    slug = await prepare(h)
    mark = len(h.events)
    await h.arm(slug)
    assert h.actor.state is AppState.IDLE
    assert not [e for e in h.events[mark:] if isinstance(e, StateChanged)]
    assert raised_since(h, mark) & ARM_FAILED_KEYS


async def test_a_get_ready_that_works_ends_in_ready_for_that_game(h: Harness):
    mark = len(h.events)
    await h.arm()
    assert StateChanged(AppState.ARMED, SLUG) in h.events[mark:]
