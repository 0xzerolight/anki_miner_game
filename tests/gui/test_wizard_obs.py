"""Wizard step 1 without Qt: OBS found, websocket on, connected, profile and collection provisioned,
and OBS switched back to the user's profile and collection (spec 16, 11.1, 11.3, 17; R2 items 4-6, 14).

The real ``ObsProvisioner`` runs against ``WizardObs`` (T14's ``FakeObs`` plus events), so the switch
back is checked against the provisioner's own switching.
"""

import asyncio
from collections.abc import Callable

import pytest

from anki_miner_game.gui.wizard import (
    NEEDS_RESTART_TEXT,
    OBS_DOWNLOAD_URL,
    OBS_LAUNCH_TIMEOUT_S,
    ObsSetup,
    ObsStatus,
    obs_changes_text,
)
from anki_miner_game.models.config import AppConfig, RecordingSettings
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import AppState
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsRequestError,
    ObsUnsupportedError,
    WsConfig,
)
from anki_miner_game.obs.provision import ObsProvisioner
from tests.gui.wizard_fakes import FakeDiscovery, FakeSession, WizardObs
from tests.obs.fake_obs import Sleeps

APP = OBS_PROFILE_NAME
USER = "Untitled"


def make_setup(
    obs: WizardObs,
    discovery: FakeDiscovery | None = None,
    *,
    after_provisioning: Callable[[], None] | None = None,
    **kwargs,
) -> tuple[ObsSetup, FakeDiscovery]:
    """The wizard's OBS step over the real provisioner; ``after_provisioning`` runs once
    ``ensure_collection`` returns, so a test can change OBS's behaviour for the switch back only."""
    discovery = discovery or FakeDiscovery()
    provisioner = ObsProvisioner(obs, platform="linux", sleep=Sleeps())
    if after_provisioning is not None:
        ensure_collection = provisioner.ensure_collection

        async def then(profile):
            result = await ensure_collection(profile)
            after_provisioning()
            return result

        provisioner.ensure_collection = then  # type: ignore[method-assign]
    return ObsSetup(discovery, obs, provisioner, **kwargs), discovery


async def until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """Poll ``condition`` on the loop until it holds; fail after ``timeout_s``."""
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.005)


# Discovery and connection ----------------------------------------------------------------------


async def test_obs_not_installed_blocks_with_the_download_link_and_touches_nothing():
    obs = WizardObs()
    setup, discovery = make_setup(obs, FakeDiscovery(installed=False, running=False))
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.NOT_INSTALLED
    assert OBS_DOWNLOAD_URL == "https://obsproject.com/download"
    assert "launch" not in discovery.calls and "ensure_server_enabled" not in discovery.calls
    assert obs.connects == 0 and obs.calls == []


async def test_server_off_while_obs_runs_asks_the_user_and_leaves_the_file_alone():
    obs = WizardObs()
    off = WsConfig(server_enabled=False, port=4455, password=None, auth_required=True)
    setup, discovery = make_setup(obs, FakeDiscovery(ws=off, running=True))
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.SERVER_OFF
    assert "Tools -> WebSocket Server Settings" in check.text
    assert "close OBS" in check.text
    assert "ensure_server_enabled" not in discovery.calls and "launch" not in discovery.calls
    assert obs.connects == 0


async def test_server_off_while_obs_is_closed_turns_it_on_launches_and_waits_up_to_30_s():
    obs = WizardObs()
    off = WsConfig(server_enabled=False, port=4455, password=None, auth_required=True)
    setup, discovery = make_setup(obs, FakeDiscovery(ws=off, running=False))
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert OBS_LAUNCH_TIMEOUT_S == 30.0
    order = [c for c in discovery.calls if c in ("ensure_server_enabled", "launch") or isinstance(c, tuple)]
    assert order == ["ensure_server_enabled", "launch", ("wait_ready", 30.0)]
    assert obs.connects == 1


async def test_a_missing_websocket_config_with_obs_closed_is_created_by_turning_the_server_on():
    obs = WizardObs()
    discovery = FakeDiscovery(running=False)
    discovery.ws = None
    setup, _ = make_setup(obs, discovery)
    assert (await setup.run(AppConfig())).status is ObsStatus.READY
    assert "ensure_server_enabled" in discovery.calls


async def test_obs_closed_with_the_server_on_is_launched_without_touching_the_file():
    obs = WizardObs()
    setup, discovery = make_setup(obs, FakeDiscovery(running=False))
    assert (await setup.run(AppConfig())).status is ObsStatus.READY
    assert "ensure_server_enabled" not in discovery.calls
    assert "launch" in discovery.calls


async def test_obs_running_with_the_server_on_is_not_launched_again():
    obs = WizardObs()
    setup, discovery = make_setup(obs)
    assert (await setup.run(AppConfig())).status is ObsStatus.READY
    assert "launch" not in discovery.calls
    assert not any(isinstance(c, tuple) for c in discovery.calls)


async def test_obs_that_does_not_answer_in_time_names_the_dialog_it_may_be_showing():
    obs = WizardObs()
    setup, _ = make_setup(obs, FakeDiscovery(running=False, ready=False))
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.NOT_READY
    assert "30 s" in check.text and "dialog" in check.text
    assert obs.connects == 0


@pytest.mark.parametrize("where", ["wait_ready", "connect"])
async def test_a_refused_password_asks_for_the_override(where):
    obs = WizardObs()
    discovery = FakeDiscovery()
    if where == "wait_ready":
        discovery.running = False
        discovery.ready = ObsAuthError("refused twice")
    else:
        obs.connect_error = ObsAuthError("refused twice")
    setup, _ = make_setup(obs, discovery)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.AUTH_FAILED
    assert "password" in check.text
    assert obs.calls == []


async def test_an_obs_without_a_required_request_names_it_and_the_version():
    obs = WizardObs()
    obs.connect_error = ObsUnsupportedError("29.1.3", ("GetSceneList", "RemoveInput"))
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.UNSUPPORTED
    assert "29.1.3" in check.text and "GetSceneList" in check.text and "RemoveInput" in check.text
    assert obs.calls == []


@pytest.mark.parametrize(
    ("side", "error"),
    [("connect", ObsConnectError("connection refused")), ("config", ObsConfigError("config.json: not JSON"))],
)
async def test_other_connection_failures_are_reported_with_their_message(side, error):
    obs = WizardObs()
    discovery = FakeDiscovery()
    if side == "connect":
        obs.connect_error = error
    else:
        discovery.ws = error
    setup, _ = make_setup(obs, discovery)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.FAILED
    assert str(error) in check.text
    assert obs.calls == []


async def test_nothing_is_touched_while_a_game_is_armed():
    obs = WizardObs()
    setup, discovery = make_setup(obs, session=FakeSession(AppState.ARMED))
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.BUSY
    assert "Disarm" in check.text
    assert discovery.calls == [] and obs.calls == []


# Spec 6.2 step 1: every output inactive --------------------------------------------------------


async def test_an_active_output_refuses_and_names_it():
    obs = WizardObs()
    obs.active["GetRecordStatus"] = True
    obs.active["GetStreamStatus"] = True
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.OUTPUT_ACTIVE
    assert "streaming" in check.text and "recording" in check.text
    assert obs.mutating() == []


@pytest.mark.parametrize(("attr", "label"), [("replay_buffer", "replay buffer"), ("virtual_cam", "virtual camera")])
async def test_a_running_replay_buffer_or_virtual_camera_refuses_too(attr, label):
    obs = WizardObs()
    setattr(obs, attr, True)
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.OUTPUT_ACTIVE
    assert label in check.text
    assert obs.mutating() == []


async def test_outputs_obs_reports_as_not_available_count_as_inactive():
    obs = WizardObs()  # replay buffer and virtual camera answer 604 (R2 item 6)
    setup, _ = make_setup(obs)
    assert (await setup.run(AppConfig())).status is ObsStatus.READY
    names = obs.names()
    for request in ("GetStreamStatus", "GetRecordStatus", "GetReplayBufferStatus", "GetVirtualCamStatus"):
        assert request in names
    assert names.index("GetVirtualCamStatus") < names.index("CreateProfile")


# Provisioning and the switch back ----------------------------------------------------------------


async def test_provisions_from_the_users_profile_and_switches_both_back():
    obs = WizardObs()
    setup, _ = make_setup(obs)
    cfg = AppConfig(output_root="/games", recording=RecordingSettings(max_height=720, fps=30))
    check = await setup.run(cfg)
    assert check.status is ObsStatus.READY
    assert "32.2.2" in check.text
    assert check.notes == ()
    # Provisioned: the app's profile and collection exist with their settings.
    assert APP in obs.profiles and OBS_COLLECTION_NAME in obs.collections
    assert obs.record_dirs[APP] == "/games/_incoming"
    assert obs.profiles[APP][("SimpleOutput", "RecFormat2")] == "mkv"
    assert "Game" in obs.collections[OBS_COLLECTION_NAME].scenes
    # Back on the user's profile and collection; no restart question on the way (audio copied).
    assert (obs.current_profile, obs.current_collection) == (USER, USER)
    assert obs.restart_questions == []
    assert obs.events[-2:] == ["CurrentSceneCollectionChanging", "CurrentSceneCollectionChanged"]
    # The user's names were read before provisioning started.
    names = obs.names()
    assert names.index("GetProfileList") < names.index("CreateProfile")
    assert names.index("GetSceneCollectionList") < names.index("CreateProfile")


async def test_ensure_profile_runs_while_the_users_profile_is_current():
    obs = WizardObs()
    seen = []
    provisioner = ObsProvisioner(obs, platform="linux", sleep=Sleeps())
    original = provisioner.ensure_profile

    async def spy(cfg):
        seen.append((obs.current_profile, obs.current_collection))
        return await original(cfg)

    provisioner.ensure_profile = spy
    setup = ObsSetup(FakeDiscovery(), obs, provisioner)
    assert (await setup.run(AppConfig())).status is ObsStatus.READY
    assert seen == [(USER, USER)]


async def test_a_second_run_changes_nothing_and_switches_back_again():
    obs = WizardObs()
    setup, _ = make_setup(obs)
    await setup.run(AppConfig())
    obs.reset_calls()
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert set(obs.mutating()) == {"SetCurrentProfile", "SetCurrentSceneCollection"}
    assert (obs.current_profile, obs.current_collection) == (USER, USER)


async def test_subscribes_to_the_gateway_once():
    obs = WizardObs()
    setup, _ = make_setup(obs)
    await setup.run(AppConfig())
    await setup.run(AppConfig())
    assert len(obs.handlers) == 1


async def test_a_switch_back_answered_first_still_waits_for_its_changed_event():
    obs = WizardObs()
    setup, _ = make_setup(obs, after_provisioning=lambda: setattr(obs, "switch_mode", "answer_first"))
    task = asyncio.create_task(setup.run(AppConfig()))
    # Provisioning re-activates the app's profile (two switches); the third is the wizard's.
    await until(lambda: obs.names().count("SetCurrentProfile") == 3 or task.done())
    for _ in range(20):
        await asyncio.sleep(0)
    assert obs.current_profile == USER  # answered
    assert not task.done()
    assert "SetCurrentSceneCollection" not in obs.names()  # the profile switch is not done yet

    def released() -> bool:
        obs.release_events()
        return task.done()

    await until(released)
    check = task.result()
    assert check.status is ObsStatus.READY and check.notes == ()
    assert (obs.current_profile, obs.current_collection) == (USER, USER)


async def test_a_switch_back_completes_on_its_event_when_the_answer_never_comes():
    obs = WizardObs()
    setup, _ = make_setup(obs, after_provisioning=lambda: setattr(obs, "switch_mode", "no_answer"))
    check = await asyncio.wait_for(setup.run(AppConfig()), 5)
    assert check.status is ObsStatus.READY and check.notes == ()
    assert (obs.current_profile, obs.current_collection) == (USER, USER)
    obs.answer_pending(asyncio.get_running_loop())
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_switch_back_without_its_event_times_out_into_a_note_and_the_next_is_still_tried():
    obs = WizardObs()
    setup, _ = make_setup(
        obs, after_provisioning=lambda: setattr(obs, "switch_mode", "no_event"), switch_timeout_s=0.05
    )
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert len(check.notes) == 2
    assert f'"{USER}"' in check.notes[0] and "Profile" in check.notes[0]
    assert f'"{USER}"' in check.notes[1] and "Scene Collection" in check.notes[1]
    assert "SetCurrentSceneCollection" in obs.names()


async def test_a_refused_switch_back_is_a_note():
    obs = WizardObs()
    obs.fail["SetCurrentSceneCollection"] = ObsRequestError("SetCurrentSceneCollection", 600, "not found")
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert len(check.notes) == 1 and "Scene Collection" in check.notes[0]
    assert obs.current_profile == USER


async def test_no_switch_back_to_what_is_already_current():
    obs = WizardObs()
    obs.profiles[APP] = {}
    obs.record_dirs[APP] = "/x"
    obs.video[APP] = dict(obs.video[USER])
    obs.current_profile = APP  # OBS was left on the app's profile, and nothing remembers the user's
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert obs.current_profile == APP and obs.current_collection == USER
    assert "SetCurrentProfile" not in obs.names()


async def test_needs_restart_is_shown_as_text_and_obs_is_never_restarted():
    obs = WizardObs()
    obs.profiles[APP] = {}
    obs.record_dirs[APP] = "/x"
    obs.video[APP] = dict(obs.video[USER])
    obs.current_profile = APP  # started on the app's profile: nowhere to switch to (spec 11.3)
    setup, discovery = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY
    assert check.notes == (NEEDS_RESTART_TEXT,)
    assert "restart" in NEEDS_RESTART_TEXT.lower()
    assert "launch" not in discovery.calls


async def test_a_provisioning_failure_still_switches_back_and_reports_it():
    obs = WizardObs()
    obs.fail["SetVideoSettings"] = ObsRequestError("SetVideoSettings", 702, "could not apply")
    setup, _ = make_setup(obs, FakeDiscovery())
    check = await setup.run(AppConfig(recording=RecordingSettings(max_height=480)))
    assert check.status is ObsStatus.FAILED
    assert "could not apply" in check.text
    assert (obs.current_profile, obs.current_collection) == (USER, USER)


async def test_an_unexpected_provisioning_error_still_switches_back():
    obs = WizardObs()
    obs.fail["GetInputKindList"] = KeyError("inputKinds")
    setup, _ = make_setup(obs)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.FAILED
    assert (obs.current_profile, obs.current_collection) == (USER, USER)


async def test_a_rerun_after_a_failed_switch_back_returns_to_the_names_read_first():
    obs = WizardObs()
    busy = {
        "SetCurrentSceneCollection": ObsRequestError("SetCurrentSceneCollection", 600, "busy"),
        "SetCurrentProfile": ObsRequestError("SetCurrentProfile", 600, "busy"),
    }
    runs = []

    def refuse_once() -> None:
        if not runs:
            obs.fail.update(busy)
        runs.append(1)

    setup, _ = make_setup(obs, after_provisioning=refuse_once)
    # First run: provisioning leaves OBS on the app's names; neither switch back works.
    check = await setup.run(AppConfig())
    assert len(check.notes) == 2
    assert (obs.current_profile, obs.current_collection) == (APP, OBS_COLLECTION_NAME)
    del obs.fail["SetCurrentSceneCollection"], obs.fail["SetCurrentProfile"]
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.READY and check.notes == ()
    assert (obs.current_profile, obs.current_collection) == (USER, USER)


async def test_stages_are_reported_while_it_runs():
    obs = WizardObs()
    setup, _ = make_setup(obs, FakeDiscovery(running=False))
    stages: list[str] = []
    await setup.run(AppConfig(), stages.append)
    assert stages[0].startswith("Looking for OBS")
    assert any(stage.startswith("Starting OBS") for stage in stages)
    assert any("back" in stage for stage in stages)


# The "what the app changes in OBS" text ------------------------------------------------------------


def test_the_changes_text_names_every_change_including_the_auto_configuration_offer():
    text = obs_changes_text(AppConfig(output_root="/games", recording=RecordingSettings(max_height=720, fps=30)))
    assert f'"{OBS_PROFILE_NAME}"' in text and f'"{OBS_COLLECTION_NAME}"' in text
    assert "_incoming" in text and "/games" in text
    assert "720" in text and "30 fps" in text and ".mkv" in text
    assert "websocket server" in text
    assert "auto-configuration wizard" in text  # R2 item 14: CreateProfile sets ConfigOnNewProfile=false
    assert "switched back" in text
    assert "never restarts OBS" in text


async def test_a_failure_before_any_switch_sends_no_switch_back():
    obs = WizardObs()
    obs.fail["CreateProfile"] = ObsRequestError("CreateProfile", 601, "exists")
    setup, _ = make_setup(obs, switch_timeout_s=0.05)
    check = await setup.run(AppConfig())
    assert check.status is ObsStatus.FAILED and check.notes == ()
    assert "SetCurrentProfile" not in obs.names() and "SetCurrentSceneCollection" not in obs.names()
