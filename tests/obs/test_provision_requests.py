"""Every request provisioning sends must be one ``GetVersion.availableRequests`` is checked for (spec 3.3, 11.1)."""

from pathlib import Path

import pytest

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.obs import REQUIRED_REQUESTS
from anki_miner_game.models.profile import AudioMode, AudioSettings, CaptureSettings, GameProfile
from anki_miner_game.obs.provision import ObsProvisioner
from tests.obs.fake_obs import LINUX_X11_KINDS, WINDOWS_KINDS, FakeObs

PENDING_CONTRACT_REQUESTS = frozenset(
    {"GetSceneList", "SetCurrentProgramScene", "GetInputSettings", "GetInputMute", "RemoveInput"}
)
"""Requests provisioning needs beyond spec 3.3's 26, all obs-websocket 5.0.0 (the OBS 30.0 floor holds).
Filed as T14's contract change request for ``REQUIRED_REQUESTS``; empty this set once it lands."""


@pytest.mark.parametrize(
    ("platform", "kinds", "window", "other"),
    [
        ("win32", WINDOWS_KINDS, "T:Cls:game.exe", None),
        ("linux", LINUX_X11_KINDS, None, "0x1\r\nGame\r\ngame"),
    ],
)
async def test_provisioning_sends_only_checked_requests(tmp_path: Path, platform, kinds, window, other):
    obs = FakeObs(input_kinds=kinds)
    provisioner = ObsProvisioner(obs, platform=platform)
    first = GameProfile(slug="a", title="A", capture=CaptureSettings(window=window), audio=AudioSettings(AudioMode.APP))
    second = GameProfile(slug="b", title="B", capture=CaptureSettings(window=other))

    await provisioner.ensure_profile(AppConfig(output_root=str(tmp_path)))
    await provisioner.ensure_collection(first)
    obs.add_special_input("mic1", "Mic/Aux", kinds[0])
    await provisioner.ensure_collection(second)
    await provisioner.list_windows()

    sent = set(obs.names())
    assert sent - set(REQUIRED_REQUESTS) - PENDING_CONTRACT_REQUESTS == set()
    assert PENDING_CONTRACT_REQUESTS - set(REQUIRED_REQUESTS) <= sent
