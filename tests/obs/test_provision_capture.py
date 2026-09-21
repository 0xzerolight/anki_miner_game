"""``Provisioner.capture_method`` (spec 11.3: the game-profile dialog's capture method in use)."""

import pytest

from anki_miner_game.models.obs import ObsConnectError
from anki_miner_game.models.profile import AudioMode, AudioSettings, CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.obs.provision import ObsProvisioner, plan_collection
from tests.obs.fake_obs import LINUX_WAYLAND_KINDS, LINUX_X11_KINDS, WINDOWS_KINDS, FakeObs


def profile(kind: CaptureKind = CaptureKind.AUTO) -> GameProfile:
    return GameProfile(
        slug="g",
        title="G",
        capture=CaptureSettings(kind=kind),
        audio=AudioSettings(mode=AudioMode.DESKTOP),
    )


@pytest.mark.parametrize(
    ("platform", "kinds", "expected"),
    [
        ("win32", WINDOWS_KINDS, "game_capture"),
        ("linux", LINUX_X11_KINDS, "pipewire-screen-capture-source"),
        ("linux", LINUX_WAYLAND_KINDS, "pipewire-screen-capture-source"),
    ],
)
async def test_names_the_input_kind_provisioning_would_create(platform, kinds, expected):
    obs = FakeObs(input_kinds=kinds)
    provisioner = ObsProvisioner(obs, platform=platform)

    assert await provisioner.capture_method(profile()) == expected
    assert expected == plan_collection(profile(), frozenset(kinds), platform).capture


@pytest.mark.parametrize("kind", list(CaptureKind))
async def test_follows_the_collection_plan_for_every_capture_kind(kind):
    obs = FakeObs(input_kinds=WINDOWS_KINDS)
    provisioner = ObsProvisioner(obs, platform="win32")

    expected = plan_collection(profile(kind), frozenset(WINDOWS_KINDS), "win32").capture
    assert await provisioner.capture_method(profile(kind)) == (expected or "")


async def test_reads_only_the_input_kind_list_and_changes_nothing():
    obs = FakeObs(input_kinds=WINDOWS_KINDS)
    provisioner = ObsProvisioner(obs, platform="win32")

    await provisioner.capture_method(profile())

    assert obs.names() == ["GetInputKindList"]
    assert obs.current_collection == "Untitled"


async def test_no_usable_capture_kind_is_an_empty_string():
    obs = FakeObs(input_kinds=("pulse_output_capture",))
    provisioner = ObsProvisioner(obs, platform="linux")

    assert await provisioner.capture_method(profile()) == ""


async def test_an_unreachable_obs_raises_an_obs_error():
    obs = FakeObs(input_kinds=WINDOWS_KINDS)
    obs.crashed = True
    provisioner = ObsProvisioner(obs, platform="win32")

    with pytest.raises(ObsConnectError):
        await provisioner.capture_method(profile())
