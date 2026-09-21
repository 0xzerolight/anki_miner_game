"""``Provisioner.list_windows`` (spec 11.3 window picker, 12 window-closed check; amendments items 10-11).

``r1-xcomposite-window-list.jsonl`` holds two frames copied verbatim from R1's run
``campaign-simple-default-trial2-173143`` (``transcript.jsonl`` lines 18-19; OBS 32.2.2 Flatpak,
obs-websocket 5.7.4, ``docs/m0/clock.md``): the window list of an ``xcomposite_input`` created with a
placeholder ``capture_window``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from anki_miner_game.lifecycle.auto import window_open
from anki_miner_game.models.obs import ObsConnectError, ObsError, ObsRequestError, WindowItem
from anki_miner_game.models.profile import AudioMode, AudioSettings, CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.obs.provision import GAME_CAPTURE_INPUT, XCOMPOSITE_INPUT, ObsProvisioner
from tests.obs.fake_obs import LINUX_WAYLAND_KINDS, LINUX_X11_KINDS, WINDOWS_KINDS, FakeObs

FIXTURE = Path(__file__).parents[1] / "fixtures" / "obs_provision" / "r1-xcomposite-window-list.jsonl"

WIN_WINDOW = "Steins#3AGate:UnityWndClass:SteinsGate.exe"
X11_WINDOW = "0x3a00007\r\nSteins;Gate\r\nsteinsgate"

GAME_CAPTURE_LIST = [
    {"itemName": "", "itemEnabled": True, "itemValue": ""},
    {"itemName": "[SteinsGate.exe]: Steins:Gate", "itemEnabled": True, "itemValue": WIN_WINDOW},
    {
        "itemName": "[notepad.exe]: notes.txt - Notepad",
        "itemEnabled": True,
        "itemValue": "notes.txt:Notepad:notepad.exe",
    },
]
WINDOW_CAPTURE_LIST = [{"itemName": "window capture list", "itemEnabled": True, "itemValue": "a:b:c.exe"}]


def recorded_items() -> list[dict[str, Any]]:
    frames = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    response = next(f["msg"]["d"] for f in frames if f["dir"] == "obs->client")
    assert response["requestType"] == "GetInputPropertiesListPropertyItems"
    return response["responseData"]["propertyItems"]


def profile(window: str | None = None, kind: CaptureKind = CaptureKind.AUTO) -> GameProfile:
    return GameProfile(
        slug="g",
        title="G",
        capture=CaptureSettings(kind=kind, window=window),
        audio=AudioSettings(mode=AudioMode.APP if window else AudioMode.DESKTOP),
    )


async def provisioned(
    platform: str, kinds: tuple[str, ...], game: GameProfile, lists: dict[str, list[dict[str, Any]]]
) -> tuple[FakeObs, ObsProvisioner]:
    obs = FakeObs(input_kinds=kinds, window_lists=lists)
    provisioner = ObsProvisioner(obs, platform=platform)
    await provisioner.ensure_collection(game)
    obs.reset_calls()
    return obs, provisioner


async def test_windows_reads_the_game_capture_list_never_the_window_capture_one():
    lists = {"game_capture": GAME_CAPTURE_LIST, "window_capture": WINDOW_CAPTURE_LIST}
    obs, provisioner = await provisioned("win32", WINDOWS_KINDS, profile(WIN_WINDOW), lists)

    items = await provisioner.list_windows()

    assert items == [
        WindowItem(name="[SteinsGate.exe]: Steins:Gate", value=WIN_WINDOW, enabled=True),
        WindowItem(name="[notepad.exe]: notes.txt - Notepad", value="notes.txt:Notepad:notepad.exe", enabled=True),
    ]
    assert obs.calls == [
        ("GetInputPropertiesListPropertyItems", {"inputName": GAME_CAPTURE_INPUT, "propertyName": "window"})
    ]


async def test_windows_keeps_the_disabled_item_obs_lists_for_a_window_that_is_gone():
    gone = {"itemName": "[SteinsGate.exe]: Steins:Gate", "itemEnabled": False, "itemValue": WIN_WINDOW}
    _, provisioner = await provisioned("win32", WINDOWS_KINDS, profile(WIN_WINDOW), {"game_capture": [gone]})

    items = await provisioner.list_windows()

    assert items == [WindowItem(name=gone["itemName"], value=WIN_WINDOW, enabled=False)]
    assert window_open(items, WIN_WINDOW) is False


async def test_x11_replays_the_window_list_r1_recorded_from_a_real_obs():
    lists = {"xcomposite_input": recorded_items()}
    obs, provisioner = await provisioned("linux", LINUX_X11_KINDS, profile(), lists)

    items = await provisioner.list_windows()

    assert items == [
        WindowItem(name="amg-placeholder", value="0\r\namg-placeholder\r\namg-placeholder", enabled=False),
        WindowItem(name="amg-sync-probe-0", value="10485767\r\namg-sync-probe-0\r\nflasher.py", enabled=True),
        WindowItem(
            name="OBS 32.2.2 - Profile: Untitled - Scenes: Untitled",
            value="4194311\r\nOBS 32.2.2 - Profile: Untitled - Scenes: Untitled\r\nobs",
            enabled=True,
        ),
    ]
    assert (
        "GetInputPropertiesListPropertyItems",
        {"inputName": XCOMPOSITE_INPUT, "propertyName": "capture_window"},
    ) in (obs.calls)
    # Item 0 stays the configured window, as auto mode's X11 rule expects.
    assert window_open(items, "10485767\r\namg-sync-probe-0\r\nflasher.py") is True


async def test_x11_never_lists_an_input_whose_capture_window_is_empty():
    obs, provisioner = await provisioned("linux", LINUX_X11_KINDS, profile(X11_WINDOW), {})
    obs.collection.inputs[XCOMPOSITE_INPUT].settings["capture_window"] = ""
    obs.reset_calls()

    assert await provisioner.list_windows() == []

    assert not obs.crashed
    assert obs.names() == ["GetInputSettings"]


async def test_x11_ignores_an_input_of_the_app_name_with_another_kind():
    obs, provisioner = await provisioned("linux", LINUX_X11_KINDS, profile(X11_WINDOW), {})
    obs.collection.inputs[XCOMPOSITE_INPUT].kind = "xshm_input_v2"

    assert await provisioner.list_windows() == []
    assert "GetInputPropertiesListPropertyItems" not in obs.names()


@pytest.mark.parametrize(
    ("platform", "kinds"),
    [("linux", LINUX_WAYLAND_KINDS), ("win32", ("window_capture", "wasapi_output_capture"))],
)
async def test_no_window_list_input_gives_an_empty_list(platform, kinds):
    _, provisioner = await provisioned(platform, kinds, profile(), {})

    assert await provisioner.list_windows() == []


@pytest.mark.parametrize(("platform", "kinds"), [("linux", LINUX_X11_KINDS), ("win32", WINDOWS_KINDS)])
async def test_another_collection_being_current_gives_an_empty_list(platform, kinds):
    obs, provisioner = await provisioned(platform, kinds, profile(), {})
    obs.current_collection = "Untitled"

    assert await provisioner.list_windows() == []


async def test_items_without_a_string_value_are_skipped():
    odd = [
        "not an item",
        {"itemName": "no value", "itemEnabled": True, "itemValue": None},
        {"itemName": "number", "itemEnabled": True, "itemValue": 3},
        {"itemEnabled": True, "itemValue": WIN_WINDOW},
    ]
    _, provisioner = await provisioned("win32", WINDOWS_KINDS, profile(), {"game_capture": odd})

    assert await provisioner.list_windows() == [WindowItem(name=WIN_WINDOW, value=WIN_WINDOW, enabled=True)]


@pytest.mark.parametrize("platform", ["win32", "linux"])
async def test_failures_other_than_a_missing_input_raise_obs_errors(platform, monkeypatch):
    kinds = WINDOWS_KINDS if platform == "win32" else LINUX_X11_KINDS
    obs, provisioner = await provisioned(platform, kinds, profile(), {})

    async def refuse(name: str, **fields: Any) -> dict[str, Any]:
        raise ObsRequestError(name, 702, "processing failed")

    monkeypatch.setattr(obs, "request", refuse)
    with pytest.raises(ObsRequestError):
        await provisioner.list_windows()

    async def gone(name: str, **fields: Any) -> dict[str, Any]:
        raise ObsConnectError("not connected")

    monkeypatch.setattr(obs, "request", gone)
    with pytest.raises(ObsConnectError):
        await provisioner.list_windows()


async def test_a_malformed_response_raises_an_obs_error(monkeypatch):
    obs, provisioner = await provisioned("win32", WINDOWS_KINDS, profile(), {})

    async def malformed(name: str, **fields: Any) -> dict[str, Any]:
        return {"propertyItems": "not a list"}

    monkeypatch.setattr(obs, "request", malformed)
    with pytest.raises(ObsError, match="propertyItems"):
        await provisioner.list_windows()
