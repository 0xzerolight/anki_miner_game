"""Provisioning of the app's OBS profile (spec 11.3 profile table; docs/m0/source-findings.md 1, 2)."""

import inspect
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anki_miner_game import paths
from anki_miner_game.interfaces.obs import Provisioner
from anki_miner_game.models.config import AppConfig, RecordingSettings
from anki_miner_game.models.constants import OBS_PROFILE_NAME
from anki_miner_game.models.obs import ObsError, ProvisionResult
from anki_miner_game.obs.provision import (
    AUDIO_KEYS,
    CONTAINER_KEYS,
    PROFILE_SWITCH_TIMEOUT_S,
    REACTIVATE_KEYS,
    ObsProvisioner,
    scaled_output_size,
)
from tests.obs.fake_obs import LINUX_X11_KINDS, FakeObs, Sleeps


def make_cfg(tmp_path: Path, *, max_height: int = 1080, fps: int = 30) -> AppConfig:
    return AppConfig(output_root=str(tmp_path / "Recordings"), recording=RecordingSettings(max_height, fps))


def make(obs: FakeObs) -> tuple[ObsProvisioner, Sleeps]:
    sleeps = Sleeps()
    return ObsProvisioner(obs, platform="linux", sleep=sleeps), sleeps


def param(obs: FakeObs, section: str, key: str) -> str | None:
    return obs.profiles[OBS_PROFILE_NAME].get((section, key))


def switches(obs: FakeObs) -> list[str]:
    return [fields["profileName"] for name, fields in obs.calls if name == "SetCurrentProfile"]


def user_audio(obs: FakeObs, sample_rate: str = "44100", channels: str = "Mono") -> None:
    obs.profiles["Untitled"].update({("Audio", "SampleRate"): sample_rate, ("Audio", "ChannelSetup"): channels})


# Scaling math ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "max_height", "expected"),
    [
        ((1920, 1080), 1080, (1920, 1080)),
        ((1920, 1080), 720, (1280, 720)),
        ((2560, 1440), 1080, (1920, 1080)),
        ((3840, 2160), 720, (1280, 720)),
        ((3440, 1440), 1080, (2580, 1080)),
        ((2560, 1600), 1080, (1728, 1080)),
        ((1080, 1920), 1080, (608, 1080)),
        ((1366, 768), 720, (1280, 720)),
        # Base already under the cap: kept, aligned the way libobs aligns it (width to 4, height to 2).
        ((1366, 768), 1080, (1364, 768)),
        ((1280, 721), 1080, (1280, 720)),
        ((1921, 1081), 1080, (1916, 1080)),
        # Never below SetVideoSettings' minimum of 8.
        ((32, 4096), 720, (8, 720)),
    ],
)
def test_output_size_scales_the_base_to_the_height_cap(base, max_height, expected):
    assert scaled_output_size(*base, max_height) == expected


@given(
    base_w=st.integers(min_value=32, max_value=4096),
    base_h=st.integers(min_value=32, max_value=4096),
    max_height=st.sampled_from([720, 1080]),
)
def test_output_size_is_capped_aligned_and_keeps_the_aspect(base_w, base_h, max_height):
    width, height = scaled_output_size(base_w, base_h, max_height)
    capped = min(base_h, max_height)
    assert height == capped & ~1
    # libobs aligns the same way, so OBS reports back exactly this size and the next run sees no change.
    assert width % 4 == 0 and height % 2 == 0
    ideal = base_w * capped / base_h
    assert width == 8 or ideal - 4 < width <= ideal + 0.5


# ensure_profile ------------------------------------------------------------------------------


async def test_first_run_creates_the_profile_and_applies_every_row(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS, base_size=(2560, 1440), create_profile_delay=3)
    provisioner, sleeps = make(obs)
    cfg = make_cfg(tmp_path)

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=True, needs_restart=False)
    assert obs.current_profile == OBS_PROFILE_NAME
    assert obs.record_dirs[OBS_PROFILE_NAME] == str(paths.incoming_dir(cfg))
    video = obs.video[OBS_PROFILE_NAME]
    assert (video["outputWidth"], video["outputHeight"]) == (1920, 1080)
    assert (video["fpsNumerator"], video["fpsDenominator"]) == (30, 1)
    assert (video["baseWidth"], video["baseHeight"]) == (2560, 1440)
    assert param(obs, "SimpleOutput", "RecFormat2") == "mkv"
    assert param(obs, "AdvOut", "RecFormat2") == "mkv"
    # Split and auto-remux are off by default, so a fresh profile needs no write for them.
    assert param(obs, "AdvOut", "RecSplitFile") is None
    assert param(obs, "Video", "AutoRemux") is None
    # CreateProfile answers before the switch; provisioning polls until OBS is on the new profile.
    create = obs.names().index("CreateProfile")
    assert obs.names()[create + 1 : create + 4] == ["GetProfileList"] * 3
    assert len(sleeps.waits) == 2


async def test_second_run_sends_no_mutating_request(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS, base_size=(1366, 768))
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path, max_height=720, fps=60)
    await provisioner.ensure_profile(cfg)
    obs.reset_calls()

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=False, needs_restart=False)
    assert obs.calls and obs.mutating() == []


async def test_an_existing_profile_is_made_current_before_its_settings_are_read(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    obs.profiles[OBS_PROFILE_NAME] = {}
    obs.record_dirs[OBS_PROFILE_NAME] = "/somewhere"
    obs.video[OBS_PROFILE_NAME] = obs._default_video(1920, 1080)
    provisioner, _ = make(obs)

    await provisioner.ensure_profile(make_cfg(tmp_path))

    switch = obs.calls.index(("SetCurrentProfile", {"profileName": OBS_PROFILE_NAME}))
    # Before the switch only the user's audio values are read, and nothing is written.
    assert obs.names()[:switch] == ["GetProfileList"] + ["GetProfileParameter"] * len(AUDIO_KEYS)
    assert obs.record_dirs["Untitled"] == "/home/user/Videos"
    assert not any(key in obs.profiles["Untitled"] for key in CONTAINER_KEYS)


async def test_a_run_from_the_users_profile_sends_only_the_switch(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path)
    await provisioner.ensure_profile(cfg)
    await obs.request("SetCurrentProfile", profileName="Untitled")  # a disarm
    obs.reset_calls()

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=True, needs_restart=False)
    assert obs.mutating() == ["SetCurrentProfile"]


# The restart question and the output rebuild (docs/m0/obs-behaviour.md items 2 and 3) ----------


async def test_the_fake_asks_to_restart_between_profiles_whose_audio_differs():
    """The fake follows OBS's rule, so the tests below can show that provisioning never meets it."""
    obs = FakeObs(input_kinds=LINUX_X11_KINDS, create_profile_delay=0)
    user_audio(obs)
    await obs.request("CreateProfile", profileName="Other")
    await obs.request("GetProfileList")
    assert obs.restart_questions == []  # the new profile's file has neither key

    await obs.request("SetCurrentProfile", profileName="Untitled")

    assert obs.restart_questions == [("Other", "Untitled")]


async def test_first_run_gives_the_new_profile_the_users_audio_so_no_switch_asks_to_restart(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    user_audio(obs)
    provisioner, _ = make(obs)

    await provisioner.ensure_profile(make_cfg(tmp_path))
    await obs.request("SetCurrentProfile", profileName="Untitled")  # a disarm
    await obs.request("SetCurrentProfile", profileName=OBS_PROFILE_NAME)  # the next arm

    assert obs.restart_questions == []
    assert (param(obs, "Audio", "SampleRate"), param(obs, "Audio", "ChannelSetup")) == ("44100", "Mono")
    names = obs.names()
    reads = [
        i for i, (n, f) in enumerate(obs.calls) if n == "GetProfileParameter" and f["parameterCategory"] == "Audio"
    ]
    writes = [
        i for i, (n, f) in enumerate(obs.calls) if n == "SetProfileParameter" and f["parameterCategory"] == "Audio"
    ]
    # Read while the user's profile runs, written into the new one before anything leaves it.
    assert max(reads[:2]) < names.index("CreateProfile") < min(writes)
    assert max(writes) < obs.calls.index(("SetCurrentProfile", {"profileName": "Untitled"}))


async def test_first_run_rebuilds_the_output_so_the_first_recording_is_matroska(tmp_path):
    """R2 item 2: OBS keeps the hybrid MP4 muxer it built until the profile is activated again."""
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)

    result = await provisioner.ensure_profile(make_cfg(tmp_path))

    assert result.needs_restart is False
    assert obs.recording_format == "mkv"
    assert switches(obs) == ["Untitled", OBS_PROFILE_NAME]
    assert obs.current_profile == OBS_PROFILE_NAME


async def test_a_run_from_a_profile_whose_audio_changed_heals_it_after_one_question(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path)
    await provisioner.ensure_profile(cfg)
    await obs.request("SetCurrentProfile", profileName="Untitled")
    user_audio(obs, "44100", "Stereo")  # the user changes their own profile afterwards

    await provisioner.ensure_profile(cfg)
    await obs.request("SetCurrentProfile", profileName="Untitled")
    await obs.request("SetCurrentProfile", profileName=OBS_PROFILE_NAME)

    # OBS asks at the switch into the app's profile; after the copy, no switch asks again.
    assert obs.restart_questions == [("Untitled", OBS_PROFILE_NAME)]
    assert param(obs, "Audio", "SampleRate") == "44100"


async def test_a_profile_obs_never_switches_to_raises_after_the_switch_timeout(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS, create_profile_delay=10**9)
    provisioner, sleeps = make(obs)

    with pytest.raises(ObsError, match=OBS_PROFILE_NAME):
        await provisioner.ensure_profile(make_cfg(tmp_path))

    assert sum(sleeps.waits) == pytest.approx(PROFILE_SWITCH_TIMEOUT_S)
    assert "SetRecordDirectory" not in obs.names()


def test_the_keys_obs_reads_only_when_it_builds_its_outputs_trigger_a_reactivation():
    """Source findings section 2: output mode, recording quality or encoder, and the container."""
    assert frozenset(CONTAINER_KEYS) <= REACTIVATE_KEYS
    assert {("Output", "Mode"), ("SimpleOutput", "RecQuality"), ("AdvOut", "RecEncoder")} <= REACTIVATE_KEYS


@pytest.mark.parametrize("current", [None, "hybrid_mp4", "hybrid_mov", "mp4", "flv"])
async def test_a_container_change_reactivates_through_the_profile_provisioning_came_from(tmp_path, current):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path)
    await provisioner.ensure_profile(cfg)
    for section in ("SimpleOutput", "AdvOut"):
        if current is None:  # absent: OBS's default, hybrid_mp4
            obs.profiles[OBS_PROFILE_NAME].pop((section, "RecFormat2"))
        else:
            obs.profiles[OBS_PROFILE_NAME][(section, "RecFormat2")] = current
    await obs.request("SetCurrentProfile", profileName="Untitled")
    obs.reset_calls()

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=True, needs_restart=False)
    assert param(obs, "SimpleOutput", "RecFormat2") == param(obs, "AdvOut", "RecFormat2") == "mkv"
    assert switches(obs) == [OBS_PROFILE_NAME, "Untitled", OBS_PROFILE_NAME]
    assert obs.recording_format == "mkv"
    assert obs.restart_questions == []


async def test_a_container_change_with_the_app_profile_already_current_needs_a_restart(tmp_path):
    """Nowhere to switch to: the change applies at the next disarm and arm, or an OBS restart."""
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path)
    await provisioner.ensure_profile(cfg)
    obs.profiles[OBS_PROFILE_NAME][("SimpleOutput", "RecFormat2")] = "hybrid_mp4"
    obs.reset_calls()

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=True, needs_restart=True)
    assert switches(obs) == []


@pytest.mark.parametrize(
    ("value", "written"), [("true", True), ("1", True), ("5", True), ("false", False), ("0", False)]
)
@pytest.mark.parametrize(("section", "key"), [("AdvOut", "RecSplitFile"), ("Video", "AutoRemux")])
async def test_split_and_auto_remux_are_switched_off_only_when_on(tmp_path, section, key, value, written):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    cfg = make_cfg(tmp_path)
    await provisioner.ensure_profile(cfg)
    obs.profiles[OBS_PROFILE_NAME][(section, key)] = value
    obs.reset_calls()

    result = await provisioner.ensure_profile(cfg)

    assert result == ProvisionResult(changed=written, needs_restart=False)
    expected = [
        ("SetProfileParameter", {"parameterCategory": section, "parameterName": key, "parameterValue": "false"})
    ]
    assert [c for c in obs.calls if c[0] == "SetProfileParameter"] == (expected if written else [])


async def test_only_the_video_pair_that_differs_is_sent(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    await provisioner.ensure_profile(make_cfg(tmp_path))
    obs.reset_calls()

    result = await provisioner.ensure_profile(make_cfg(tmp_path, fps=60))

    assert result.changed
    assert [c for c in obs.calls if c[0] == "SetVideoSettings"] == [
        ("SetVideoSettings", {"fpsNumerator": 60, "fpsDenominator": 1})
    ]


async def test_a_changed_output_root_moves_the_record_directory(tmp_path):
    obs = FakeObs(input_kinds=LINUX_X11_KINDS)
    provisioner, _ = make(obs)
    await provisioner.ensure_profile(make_cfg(tmp_path))
    moved = AppConfig(output_root=str(tmp_path / "elsewhere"))
    obs.reset_calls()

    await provisioner.ensure_profile(moved)

    assert obs.mutating() == ["SetRecordDirectory"]
    assert obs.record_dirs[OBS_PROFILE_NAME] == str(tmp_path / "elsewhere" / "_incoming")


def test_the_provisioner_conforms_to_the_provisioner_protocol():
    members = [attr for attr in vars(Provisioner) if not attr.startswith("_")]
    assert sorted(members) == ["capture_method", "ensure_collection", "ensure_profile", "list_windows"]
    for member in members:
        expected = inspect.getattr_static(Provisioner, member)
        actual = inspect.getattr_static(ObsProvisioner, member)
        assert inspect.iscoroutinefunction(actual) and inspect.iscoroutinefunction(expected), member
        assert list(inspect.signature(actual).parameters) == list(inspect.signature(expected).parameters), member
