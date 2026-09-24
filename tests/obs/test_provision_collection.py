"""Provisioning of the app's scene collection, scene and inputs (spec 11.3 collection table)."""

from typing import Any

import pytest

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_SCENE_NAME
from anki_miner_game.models.obs import ObsError, ProvisionResult
from anki_miner_game.models.profile import AudioMode, AudioSettings, CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.obs import provision
from anki_miner_game.obs.provision import (
    APP_AUDIO_INPUT,
    DESKTOP_AUDIO_INPUT,
    GAME_CAPTURE_INPUT,
    INPUT_RELEASE_TIMEOUT_S,
    MONITOR_CAPTURE_INPUT,
    PIPEWIRE_INPUT,
    POLL_S,
    WINDOW_CAPTURE_INPUT,
    XCOMPOSITE_INPUT,
    XCOMPOSITE_PLACEHOLDER,
    ObsProvisioner,
    plan_collection,
)
from tests.obs import fake_obs
from tests.obs.fake_obs import (
    LINUX_WAYLAND_KINDS,
    LINUX_X11_KINDS,
    NEW_ITEM_TRANSFORM,
    WINDOWS_KINDS,
    FakeCollection,
    FakeObs,
    Sleeps,
)

WIN_WINDOW = "Steins#3AGate:UnityWndClass:SteinsGate.exe"
X11_WINDOW = "0x3a00007\r\nSteins;Gate\r\nsteinsgate"

GAME_ANY = {"capture_mode": "any_fullscreen", "capture_cursor": False}
GAME_ON_WINDOW = {"capture_mode": "window", "window": WIN_WINDOW, "capture_cursor": False}
GAME_LIST_ONLY = {"capture_mode": "window", "window": ""}
WINDOW_ON_WINDOW = {"window": WIN_WINDOW, "cursor": False}
APP_AUDIO = {"window": WIN_WINDOW}
DESKTOP_AUDIO = {"device_id": "default"}
XCOMPOSITE_ON_WINDOW = {"capture_window": X11_WINDOW, "show_cursor": False}
XCOMPOSITE_IDLE = {"capture_window": XCOMPOSITE_PLACEHOLDER, "show_cursor": False}
PIPEWIRE = {"ShowCursor": False}

PRIMARY_MONITOR = r"\\?\DISPLAY#GSM5B09#5&2e7f1a3&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
SIDE_MONITOR = r"\\?\DISPLAY#DEL40F4#5&2e7f1a3&0&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
MONITORS = [
    # A new monitor_capture's monitor_id is "DUMMY": OBS lists it first, disabled, and captures nothing.
    {"itemName": "[Select a display to capture]", "itemValue": "DUMMY", "itemEnabled": False},
    {"itemName": "DELL U2415: 1920x1200 @ -1920,0", "itemValue": SIDE_MONITOR, "itemEnabled": True},
    {"itemName": "LG ULTRAGEAR: 2560x1440 @ 0,0 (Primary Monitor)", "itemValue": PRIMARY_MONITOR, "itemEnabled": True},
]
"""``monitor_id`` items of a ``monitor_capture`` as OBS 32 builds them (``obs-studio@ba2f32bd
plugins/win-capture/duplicator-monitor-capture.c:727-755, 813-816``): ``<name>: <w>x<h> @ <x>,<y>``,
the primary monitor, always at 0,0, with a localized suffix."""
DISPLAY_ON_PRIMARY = {"capture_cursor": False, "monitor_id": PRIMARY_MONITOR}


def fitted(width: int, height: int) -> dict[str, Any]:
    """OBS's Fit to screen (``obs-studio@ba2f32bd frontend/widgets/OBSBasic_SceneItems.cpp:1222-1253``):
    the item's box is the canvas, the source scaled inside it keeping its aspect and centred."""
    return {
        **NEW_ITEM_TRANSFORM,
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsAlignment": 0,
        "boundsWidth": width,
        "boundsHeight": height,
    }


def profile(
    kind: CaptureKind = CaptureKind.AUTO, window: str | None = None, audio: AudioMode = AudioMode.DESKTOP
) -> GameProfile:
    return GameProfile(
        slug="steins-gate",
        title="Steins;Gate",
        capture=CaptureSettings(kind=kind, window=window),
        audio=AudioSettings(mode=audio),
    )


def windows_obs(kinds: tuple[str, ...] = WINDOWS_KINDS) -> tuple[FakeObs, ObsProvisioner]:
    obs = FakeObs(input_kinds=kinds, window_lists={"monitor_capture": MONITORS})
    return obs, ObsProvisioner(obs, platform="win32", sleep=Sleeps())


def linux_obs(kinds: tuple[str, ...] = LINUX_X11_KINDS) -> tuple[FakeObs, ObsProvisioner]:
    obs = FakeObs(input_kinds=kinds)
    return obs, ObsProvisioner(obs, platform="linux", sleep=Sleeps())


def inputs(obs: FakeObs) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every input of the app's collection: name -> (kind, settings)."""
    return {name: (i.kind, i.settings) for name, i in obs.collections[OBS_COLLECTION_NAME].inputs.items()}


# Collection and scene ---------------------------------------------------------------------------


async def test_first_run_creates_the_collection_and_makes_game_the_program_scene():
    obs, provisioner = windows_obs()

    result = await provisioner.ensure_collection(profile())

    assert result == ProvisionResult(changed=True, needs_restart=False)
    assert obs.current_collection == OBS_COLLECTION_NAME
    assert obs.collections["Untitled"].inputs == {}
    assert obs.collection.program_scene == OBS_SCENE_NAME
    assert ("CreateSceneCollection", {"sceneCollectionName": OBS_COLLECTION_NAME}) in obs.calls
    assert ("CreateScene", {"sceneName": OBS_SCENE_NAME}) in obs.calls
    assert ("SetCurrentProgramScene", {"sceneName": OBS_SCENE_NAME}) in obs.calls


async def test_an_existing_collection_is_made_current_and_its_scene_reused():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.current_collection = "Untitled"
    obs.reset_calls()

    result = await provisioner.ensure_collection(profile())

    assert result.changed
    assert obs.mutating() == ["SetCurrentSceneCollection"]
    assert obs.current_collection == OBS_COLLECTION_NAME


async def test_a_program_scene_the_user_moved_away_from_game_is_moved_back():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.collection.program_scene = "Scene"
    obs.reset_calls()

    await provisioner.ensure_collection(profile())

    assert obs.mutating() == ["SetCurrentProgramScene"]


async def test_a_collection_switch_answered_before_its_event_waits_for_the_event():
    """R2 item 5: the answer came before ``...Changed`` once; the switch is done on the event."""
    obs = FakeObs(input_kinds=WINDOWS_KINDS, events_after_answer=True)
    provisioner = ObsProvisioner(obs, platform="win32", sleep=Sleeps())
    await provisioner.ensure_collection(profile())
    await obs.request("SetCurrentSceneCollection", sceneCollectionName="Untitled")  # a disarm
    await provisioner.ensure_collection(profile())

    switched = [i for i, (name, _) in enumerate(obs.calls) if name.endswith("SceneCollection")]
    assert [obs.calls[i][0] for i in switched] == [
        "CreateSceneCollection",
        "SetCurrentSceneCollection",
        "SetCurrentSceneCollection",
    ]
    for i in switched[0], switched[2]:
        assert (i + 1, "CurrentSceneCollectionChanged", obs.calls[i][1]) in obs.events, i


@pytest.mark.parametrize("exists", [False, True])
async def test_a_collection_switch_whose_event_never_comes_raises_after_the_switch_timeout(monkeypatch, exists):
    monkeypatch.setattr(provision, "SWITCH_TIMEOUT_S", 0.05)
    obs = FakeObs(input_kinds=WINDOWS_KINDS, lost_events=["CurrentSceneCollectionChanged"])
    if exists:
        obs.collections[OBS_COLLECTION_NAME] = FakeCollection()
    provisioner = ObsProvisioner(obs, platform="win32", sleep=Sleeps())

    with pytest.raises(ObsError, match=OBS_COLLECTION_NAME):
        await provisioner.ensure_collection(profile())

    assert obs.mutating() == ["SetCurrentSceneCollection" if exists else "CreateSceneCollection"]


# Platform rows --------------------------------------------------------------------------------


async def test_windows_without_a_pinned_window_captures_the_primary_monitor_under_any_fullscreen_game():
    """S2-2: any-fullscreen game capture alone recorded a windowed game black."""
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile())

    assert inputs(obs) == {
        MONITOR_CAPTURE_INPUT: ("monitor_capture", DISPLAY_ON_PRIMARY),
        GAME_CAPTURE_INPUT: ("game_capture", GAME_ANY),
        DESKTOP_AUDIO_INPUT: ("wasapi_output_capture", DESKTOP_AUDIO),
    }
    items = obs.scene_items()
    assert items.index(MONITOR_CAPTURE_INPUT) < items.index(GAME_CAPTURE_INPUT)


async def test_the_display_capture_follows_the_primary_monitor_in_any_obs_language():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.window_lists["monitor_capture"] = [
        {"itemName": "LG ULTRAGEAR: 2560x1440 @ 1920,0", "itemValue": PRIMARY_MONITOR, "itemEnabled": True},
        {
            "itemName": "DELL U2415: 1920x1200 @ 0,0 (プライマリモニター)",
            "itemValue": SIDE_MONITOR,
            "itemEnabled": True,
        },
    ]
    obs.reset_calls()

    await provisioner.ensure_collection(profile())

    assert obs.mutating() == ["SetInputSettings"]
    assert inputs(obs)[MONITOR_CAPTURE_INPUT][1]["monitor_id"] == SIDE_MONITOR


async def test_a_monitor_list_without_one_at_0_0_takes_its_first_enabled_monitor():
    obs, provisioner = windows_obs()
    obs.window_lists["monitor_capture"] = [MONITORS[0], MONITORS[1]]

    await provisioner.ensure_collection(profile())

    assert inputs(obs)[MONITOR_CAPTURE_INPUT][1]["monitor_id"] == SIDE_MONITOR


async def test_no_monitor_to_pick_leaves_the_display_capture_as_obs_made_it():
    obs, provisioner = windows_obs()
    obs.window_lists["monitor_capture"] = [MONITORS[0]]

    await provisioner.ensure_collection(profile())

    assert inputs(obs)[MONITOR_CAPTURE_INPUT] == ("monitor_capture", {"capture_cursor": False})


async def test_a_display_capture_without_a_monitor_id_list_is_left_on_its_default_monitor(monkeypatch):
    # OBS on the OpenGL renderer registers the older monitor_capture, whose list is "monitor" (an index).
    monkeypatch.setitem(fake_obs.LIST_PROPERTY, "monitor_capture", "monitor")
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile())

    assert inputs(obs)[MONITOR_CAPTURE_INPUT] == ("monitor_capture", {"capture_cursor": False})


async def test_windows_game_kind_without_a_window_uses_any_fullscreen_game_capture_alone():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(CaptureKind.GAME))

    assert inputs(obs) == {
        GAME_CAPTURE_INPUT: ("game_capture", GAME_ANY),
        DESKTOP_AUDIO_INPUT: ("wasapi_output_capture", DESKTOP_AUDIO),
    }


async def test_windows_without_monitor_capture_keeps_any_fullscreen_game_capture_alone():
    obs, provisioner = windows_obs(tuple(k for k in WINDOWS_KINDS if k != "monitor_capture"))

    await provisioner.ensure_collection(profile())

    assert set(inputs(obs)) == {GAME_CAPTURE_INPUT, DESKTOP_AUDIO_INPUT}


async def test_windows_with_a_pinned_window_adds_the_window_capture_fallback_underneath_and_app_audio():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.APP))

    assert inputs(obs) == {
        WINDOW_CAPTURE_INPUT: ("window_capture", WINDOW_ON_WINDOW),
        GAME_CAPTURE_INPUT: ("game_capture", GAME_ON_WINDOW),
        APP_AUDIO_INPUT: ("wasapi_process_output_capture", APP_AUDIO),
    }
    items = obs.scene_items()
    assert items.index(WINDOW_CAPTURE_INPUT) < items.index(GAME_CAPTURE_INPUT)


async def test_windows_pinned_window_with_desktop_audio_keeps_desktop_audio():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.DESKTOP))

    assert set(inputs(obs)) == {WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT, DESKTOP_AUDIO_INPUT}


async def test_windows_game_kind_uses_game_capture_alone():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(CaptureKind.GAME, WIN_WINDOW, AudioMode.APP))

    assert inputs(obs) == {
        GAME_CAPTURE_INPUT: ("game_capture", GAME_ON_WINDOW),
        APP_AUDIO_INPUT: ("wasapi_process_output_capture", APP_AUDIO),
    }


async def test_windows_window_kind_captures_the_window_and_keeps_game_capture_only_for_its_window_list():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(CaptureKind.WINDOW, WIN_WINDOW, AudioMode.APP))

    assert inputs(obs) == {
        WINDOW_CAPTURE_INPUT: ("window_capture", WINDOW_ON_WINDOW),
        GAME_CAPTURE_INPUT: ("game_capture", GAME_LIST_ONLY),
        APP_AUDIO_INPUT: ("wasapi_process_output_capture", APP_AUDIO),
    }


async def test_windows_window_kind_without_a_window_falls_back_to_any_fullscreen():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(CaptureKind.WINDOW))

    assert inputs(obs)[GAME_CAPTURE_INPUT] == ("game_capture", GAME_ANY)
    assert inputs(obs)[MONITOR_CAPTURE_INPUT] == ("monitor_capture", DISPLAY_ON_PRIMARY)
    assert WINDOW_CAPTURE_INPUT not in inputs(obs)


@pytest.mark.parametrize("kind", [CaptureKind.PIPEWIRE, CaptureKind.XCOMPOSITE])
async def test_windows_ignores_linux_capture_kinds(kind):
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(kind, WIN_WINDOW, AudioMode.APP))

    assert inputs(obs)[GAME_CAPTURE_INPUT] == ("game_capture", GAME_ON_WINDOW)
    assert WINDOW_CAPTURE_INPUT in inputs(obs)


async def test_windows_without_process_audio_capture_records_desktop_audio():
    kinds = tuple(k for k in WINDOWS_KINDS if k != "wasapi_process_output_capture")
    obs, provisioner = windows_obs(kinds)

    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.APP))

    assert set(inputs(obs)) == {WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT, DESKTOP_AUDIO_INPUT}


async def test_windows_without_game_capture_uses_window_capture_on_the_pinned_window():
    kinds = tuple(k for k in WINDOWS_KINDS if k != "game_capture")
    obs, provisioner = windows_obs(kinds)

    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.APP))

    assert set(inputs(obs)) == {WINDOW_CAPTURE_INPUT, APP_AUDIO_INPUT}


async def test_windows_ignores_an_x11_window_string():
    obs, provisioner = windows_obs()

    await provisioner.ensure_collection(profile(window=X11_WINDOW, audio=AudioMode.APP))

    assert inputs(obs) == {
        MONITOR_CAPTURE_INPUT: ("monitor_capture", DISPLAY_ON_PRIMARY),
        GAME_CAPTURE_INPUT: ("game_capture", GAME_ANY),
        DESKTOP_AUDIO_INPUT: ("wasapi_output_capture", DESKTOP_AUDIO),
    }


async def test_linux_x11_with_a_pinned_window_captures_it_with_xcomposite():
    obs, provisioner = linux_obs()

    await provisioner.ensure_collection(profile(window=X11_WINDOW, audio=AudioMode.APP))

    assert inputs(obs) == {
        XCOMPOSITE_INPUT: ("xcomposite_input", XCOMPOSITE_ON_WINDOW),
        DESKTOP_AUDIO_INPUT: ("pulse_output_capture", DESKTOP_AUDIO),
    }


async def test_linux_without_a_pinned_window_uses_pipewire_and_keeps_an_idle_xcomposite_for_the_window_list():
    obs, provisioner = linux_obs()

    await provisioner.ensure_collection(profile())

    assert inputs(obs) == {
        XCOMPOSITE_INPUT: ("xcomposite_input", XCOMPOSITE_IDLE),
        PIPEWIRE_INPUT: ("pipewire-screen-capture-source", PIPEWIRE),
        DESKTOP_AUDIO_INPUT: ("pulse_output_capture", DESKTOP_AUDIO),
    }


async def test_linux_pipewire_kind_wins_over_a_pinned_window():
    obs, provisioner = linux_obs()

    await provisioner.ensure_collection(profile(CaptureKind.PIPEWIRE, X11_WINDOW))

    assert inputs(obs)[XCOMPOSITE_INPUT] == ("xcomposite_input", XCOMPOSITE_IDLE)
    assert PIPEWIRE_INPUT in inputs(obs)


async def test_linux_xcomposite_kind_without_a_window_falls_back_to_pipewire():
    obs, provisioner = linux_obs()

    await provisioner.ensure_collection(profile(CaptureKind.XCOMPOSITE))

    assert set(inputs(obs)) == {XCOMPOSITE_INPUT, PIPEWIRE_INPUT, DESKTOP_AUDIO_INPUT}


async def test_linux_wayland_offers_only_pipewire():
    obs, provisioner = linux_obs(LINUX_WAYLAND_KINDS)

    await provisioner.ensure_collection(profile(CaptureKind.XCOMPOSITE, X11_WINDOW))

    assert inputs(obs) == {
        PIPEWIRE_INPUT: ("pipewire-screen-capture-source", PIPEWIRE),
        DESKTOP_AUDIO_INPUT: ("pulse_output_capture", DESKTOP_AUDIO),
    }


async def test_linux_x11_without_pipewire_and_without_a_window_captures_nothing_but_keeps_the_window_list():
    obs, provisioner = linux_obs(("xcomposite_input", "pulse_output_capture"))

    await provisioner.ensure_collection(profile())

    assert inputs(obs) == {
        XCOMPOSITE_INPUT: ("xcomposite_input", XCOMPOSITE_IDLE),
        DESKTOP_AUDIO_INPUT: ("pulse_output_capture", DESKTOP_AUDIO),
    }


async def test_input_kinds_obs_does_not_report_are_skipped():
    obs, provisioner = linux_obs(("pipewire-screen-capture-source",))

    await provisioner.ensure_collection(profile())

    assert set(inputs(obs)) == {PIPEWIRE_INPUT}
    assert all(kind in obs.input_kinds for kind, _ in inputs(obs).values())


@pytest.mark.parametrize(
    ("platform", "kinds", "game", "capture"),
    [
        ("win32", WINDOWS_KINDS, profile(), "game_capture"),
        ("win32", WINDOWS_KINDS, profile(CaptureKind.WINDOW, WIN_WINDOW), "window_capture"),
        ("win32", ("monitor_capture", "wasapi_output_capture"), profile(), "monitor_capture"),
        ("linux", LINUX_X11_KINDS, profile(window=X11_WINDOW), "xcomposite_input"),
        ("linux", LINUX_X11_KINDS, profile(), "pipewire-screen-capture-source"),
        ("linux", ("xcomposite_input",), profile(), None),
    ],
)
def test_the_plan_names_the_capture_method_in_use(platform, kinds, game, capture):
    assert plan_collection(game, frozenset(kinds), platform).capture == capture


# Canvas fit ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "kinds", "game", "video"),
    [
        ("win32", WINDOWS_KINDS, profile(), [MONITOR_CAPTURE_INPUT, GAME_CAPTURE_INPUT]),
        ("win32", WINDOWS_KINDS, profile(window=WIN_WINDOW), [WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT]),
        ("win32", WINDOWS_KINDS, profile(CaptureKind.WINDOW, WIN_WINDOW), [WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT]),
        ("linux", LINUX_X11_KINDS, profile(window=X11_WINDOW), [XCOMPOSITE_INPUT]),
        ("linux", LINUX_X11_KINDS, profile(), [PIPEWIRE_INPUT, XCOMPOSITE_INPUT]),
    ],
)
async def test_every_video_input_is_fitted_to_the_canvas(platform, kinds, game, video):
    """S2-3: a 1280x720 window sat unscaled at the top left of a 1920x1080 canvas."""
    obs = FakeObs(input_kinds=kinds, window_lists={"monitor_capture": MONITORS})
    provisioner = ObsProvisioner(obs, platform=platform, sleep=Sleeps())

    await provisioner.ensure_collection(game)

    assert {name: obs.transform(name) for name in video} == {name: fitted(1920, 1080) for name in video}
    audio = [name for name in obs.scene_items() if name not in video]
    assert audio and all(obs.transform(name) == NEW_ITEM_TRANSFORM for name in audio)


async def test_the_fit_follows_the_canvas_size():
    obs = FakeObs(input_kinds=WINDOWS_KINDS, base_size=(2560, 1080))
    provisioner = ObsProvisioner(obs, platform="win32", sleep=Sleeps())

    await provisioner.ensure_collection(profile(window=WIN_WINDOW))

    assert obs.transform(GAME_CAPTURE_INPUT) == fitted(2560, 1080)


async def test_items_made_before_the_fit_are_fitted_in_place():
    """A collection provisioned by 1.0.0 keeps its inputs; only their items change."""
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile(window=WIN_WINDOW))
    for name in (WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT):
        obs.transform(name).update(NEW_ITEM_TRANSFORM)
    obs.reset_calls()

    await provisioner.ensure_collection(profile(window=WIN_WINDOW))

    assert obs.mutating() == ["SetSceneItemTransform"] * 2
    assert obs.transform(WINDOW_CAPTURE_INPUT) == obs.transform(GAME_CAPTURE_INPUT) == fitted(1920, 1080)


async def test_an_item_moved_in_obs_is_fitted_again():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.transform(GAME_CAPTURE_INPUT).update(positionX=300.0, boundsType="OBS_BOUNDS_NONE", rotation=90.0)
    obs.reset_calls()

    await provisioner.ensure_collection(profile())

    item = obs.collection.items[(OBS_SCENE_NAME, GAME_CAPTURE_INPUT)].item_id
    assert [c for c in obs.calls if c[0] == "SetSceneItemTransform"] == [
        (
            "SetSceneItemTransform",
            {
                "sceneName": OBS_SCENE_NAME,
                "sceneItemId": item,
                "sceneItemTransform": {
                    "positionX": 0,
                    "positionY": 0,
                    "rotation": 0,
                    "alignment": 5,
                    "boundsType": "OBS_BOUNDS_SCALE_INNER",
                    "boundsAlignment": 0,
                    "boundsWidth": 1920,
                    "boundsHeight": 1080,
                },
            },
        )
    ]
    assert obs.transform(GAME_CAPTURE_INPUT) == fitted(1920, 1080)


# Audio special inputs ----------------------------------------------------------------------------


async def test_every_special_audio_input_is_muted_once():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.add_special_input("mic1", "Mic/Aux", "wasapi_input_capture")
    obs.add_special_input("mic3", "Mic/Aux 3", "wasapi_input_capture", muted=True)
    obs.add_special_input("desktop1", "Desktop Audio", "wasapi_output_capture")
    obs.reset_calls()

    result = await provisioner.ensure_collection(profile())

    assert result.changed
    mutes = sorted((c[1]["inputName"], c[1]["inputMuted"]) for c in obs.calls if c[0] == "SetInputMute")
    assert mutes == [("Desktop Audio", True), ("Mic/Aux", True)]
    obs.reset_calls()
    assert await provisioner.ensure_collection(profile()) == ProvisionResult(changed=False, needs_restart=False)


# Idempotent diff ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "kinds", "game"),
    [
        ("win32", WINDOWS_KINDS, profile()),
        ("win32", WINDOWS_KINDS, profile(window=WIN_WINDOW, audio=AudioMode.APP)),
        ("win32", WINDOWS_KINDS, profile(CaptureKind.WINDOW, WIN_WINDOW, AudioMode.APP)),
        ("linux", LINUX_X11_KINDS, profile()),
        ("linux", LINUX_X11_KINDS, profile(window=X11_WINDOW)),
        ("linux", LINUX_WAYLAND_KINDS, profile()),
    ],
)
async def test_second_run_sends_no_mutating_request(platform, kinds, game):
    obs = FakeObs(input_kinds=kinds, window_lists={"monitor_capture": MONITORS})
    provisioner = ObsProvisioner(obs, platform=platform)
    await provisioner.ensure_collection(game)
    obs.reset_calls()

    result = await provisioner.ensure_collection(game)

    assert result == ProvisionResult(changed=False, needs_restart=False)
    assert obs.calls and obs.mutating() == []


async def test_switching_games_changes_only_the_inputs_that_differ():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.APP))
    other = "Chaos#3BHead:UnityWndClass:ChaosHead.exe"
    obs.reset_calls()

    await provisioner.ensure_collection(profile(window=other, audio=AudioMode.APP))

    assert obs.mutating() == ["SetInputSettings"] * 3
    assert {c[1]["inputName"] for c in obs.calls if c[0] == "SetInputSettings"} == {
        WINDOW_CAPTURE_INPUT,
        GAME_CAPTURE_INPUT,
        APP_AUDIO_INPUT,
    }
    assert inputs(obs)[GAME_CAPTURE_INPUT][1]["window"] == other


async def test_unpinning_swaps_the_window_fallback_for_the_display_one_and_app_audio_for_desktop_audio():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile(window=WIN_WINDOW, audio=AudioMode.APP))

    await provisioner.ensure_collection(profile())

    assert inputs(obs)[GAME_CAPTURE_INPUT][1]["capture_mode"] == "any_fullscreen"
    assert set(inputs(obs)) == {MONITOR_CAPTURE_INPUT, GAME_CAPTURE_INPUT, DESKTOP_AUDIO_INPUT}
    items = obs.scene_items()
    assert items.index(MONITOR_CAPTURE_INPUT) < items.index(GAME_CAPTURE_INPUT)


async def test_pinning_later_recreates_game_capture_so_the_fallback_stays_underneath():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.reset_calls()

    await provisioner.ensure_collection(profile(window=WIN_WINDOW))

    items = obs.scene_items()
    assert items.index(WINDOW_CAPTURE_INPUT) < items.index(GAME_CAPTURE_INPUT)
    assert MONITOR_CAPTURE_INPUT not in items
    assert inputs(obs)[GAME_CAPTURE_INPUT] == ("game_capture", GAME_ON_WINDOW)
    creates = [c[1]["inputName"] for c in obs.calls if c[0] == "CreateInput"]
    assert creates == [WINDOW_CAPTURE_INPUT, GAME_CAPTURE_INPUT]


async def test_unpinning_on_x11_recreates_the_window_list_input_above_pipewire():
    obs, provisioner = linux_obs()
    await provisioner.ensure_collection(profile(window=X11_WINDOW))

    await provisioner.ensure_collection(profile())

    assert inputs(obs)[XCOMPOSITE_INPUT] == ("xcomposite_input", XCOMPOSITE_IDLE)
    items = obs.scene_items()
    assert items.index(PIPEWIRE_INPUT) < items.index(XCOMPOSITE_INPUT)


async def test_a_removed_input_is_created_again_only_once_obs_has_released_its_name():
    # OBS frees a removed input's name only after its next render and the UI drop their references;
    # the fake holds it for four requests: CreateInput of the fallback, then three polls.
    obs = FakeObs(input_kinds=WINDOWS_KINDS, release_delay=4)
    sleeps = Sleeps()
    provisioner = ObsProvisioner(obs, platform="win32", sleep=sleeps)
    await provisioner.ensure_collection(profile())
    obs.reset_calls()

    await provisioner.ensure_collection(profile(window=WIN_WINDOW))

    assert inputs(obs)[GAME_CAPTURE_INPUT] == ("game_capture", GAME_ON_WINDOW)
    removed = obs.names().index("RemoveInput")
    polls = [c for c in obs.calls[removed:] if c == ("GetInputSettings", {"inputName": GAME_CAPTURE_INPUT})]
    assert len(polls) == 4
    assert sleeps.waits == [POLL_S] * 3


async def test_an_input_obs_never_releases_raises_after_the_release_timeout():
    obs = FakeObs(input_kinds=WINDOWS_KINDS, release_delay=10**9)
    sleeps = Sleeps()
    provisioner = ObsProvisioner(obs, platform="win32", sleep=sleeps)
    await provisioner.ensure_collection(profile())
    obs.reset_calls()

    with pytest.raises(ObsError, match=GAME_CAPTURE_INPUT):
        await provisioner.ensure_collection(profile(window=WIN_WINDOW))

    assert sum(sleeps.waits) == pytest.approx(INPUT_RELEASE_TIMEOUT_S)
    assert [c[1]["inputName"] for c in obs.calls if c[0] == "CreateInput"] == [WINDOW_CAPTURE_INPUT]


async def test_a_drifted_setting_is_rewritten_with_the_planned_settings_only():
    obs, provisioner = windows_obs()
    await provisioner.ensure_collection(profile())
    obs.collection.inputs[GAME_CAPTURE_INPUT].settings.update(capture_mode="hotkey", allow_transparency=True)
    obs.reset_calls()

    await provisioner.ensure_collection(profile())

    assert [c for c in obs.calls if c[0] == "SetInputSettings"] == [
        ("SetInputSettings", {"inputName": GAME_CAPTURE_INPUT, "inputSettings": GAME_ANY})
    ]
    assert inputs(obs)[GAME_CAPTURE_INPUT][1]["allow_transparency"] is True


async def test_an_input_with_the_app_name_but_another_kind_is_replaced():
    obs, provisioner = linux_obs()
    await provisioner.ensure_collection(profile())
    collection = obs.collection
    collection.inputs[DESKTOP_AUDIO_INPUT].kind = "pulse_input_capture"
    obs.reset_calls()

    await provisioner.ensure_collection(profile())

    assert obs.mutating() == ["RemoveInput", "CreateInput"]
    assert inputs(obs)[DESKTOP_AUDIO_INPUT] == ("pulse_output_capture", DESKTOP_AUDIO)


async def test_inputs_of_the_user_are_left_alone():
    obs, provisioner = linux_obs()
    await provisioner.ensure_collection(profile())
    await obs.request(
        "CreateInput", sceneName=OBS_SCENE_NAME, inputName="Webcam", inputKind="xshm_input_v2", inputSettings={}
    )
    obs.reset_calls()

    await provisioner.ensure_collection(profile(window=X11_WINDOW))

    assert "Webcam" in inputs(obs)
    assert all(c[1].get("inputName") != "Webcam" for c in obs.calls)
    assert obs.transform("Webcam") == NEW_ITEM_TRANSFORM
