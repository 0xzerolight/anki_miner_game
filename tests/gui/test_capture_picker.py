"""The window picker's listing and the capture method in use (spec 11.3; T20 card).

``list_windows`` reads the current collection, so the picker lists at once only while OBS is on the
app's collection (any state but ``idle``). While idle it runs spec 6.2 step 1's output check, reads
the current collection, provisions the app's collection, lists, and switches back, done on
``CurrentSceneCollectionChanged``. The real ``ObsProvisioner`` runs against T14's ``FakeObs``;
window lists are the ones a real OBS answered in R2's ``window_retitle.jsonl``.
"""

import asyncio
from dataclasses import replace

import pytest

from anki_miner_game.gui.game_profile_dialog import CapturePicker, WindowListError
from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.messages import AppState
from anki_miner_game.models.obs import ObsConnectError, ObsError, ObsRequestError, WindowItem
from anki_miner_game.models.profile import CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.obs.provision import ObsProvisioner
from tests.gui.obs_listing_fake import STATUS_REQUESTS, ListingObs, recorded_window_lists
from tests.obs.fake_obs import LINUX_X11_KINDS, WINDOWS_KINDS

BEFORE, RETITLED, CLOSED = recorded_window_lists("window_retitle.jsonl")
STORED = BEFORE[0]["itemValue"]
"""The pinned probe window: ``4194311\\r\\namg-probe-window\\r\\nprobe_window.py``."""

X11_PROFILE = GameProfile(slug="probe", title="Probe", capture=CaptureSettings(window=STORED))


class FakeSession:
    def __init__(self, state: AppState = AppState.IDLE) -> None:
        self.state = state

    def post(self, msg: object) -> None:
        raise AssertionError("the picker never posts to the session")

    def subscribe(self, cb: object) -> None:
        raise AssertionError("the picker never subscribes to the session")


def make(
    obs: ListingObs, state: AppState = AppState.IDLE, *, platform: str = "linux", timeout_s: float = 2.0
) -> tuple[CapturePicker, FakeSession]:
    session = FakeSession(state)
    provisioner = ObsProvisioner(obs, platform=platform, sleep=_no_sleep)
    return CapturePicker(obs, provisioner, session, switch_timeout_s=timeout_s), session


async def _no_sleep(seconds: float) -> None:
    pass


def x11_obs(window_list: list[dict[str, object]] = RETITLED, **kwargs: object) -> ListingObs:
    return ListingObs(input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": window_list}, **kwargs)


def items(raw: list[dict[str, object]]) -> list[WindowItem]:
    return [WindowItem(str(i["itemName"]), str(i["itemValue"]), i["itemEnabled"] is True) for i in raw]


async def test_idle_listing_provisions_the_app_collection_lists_it_and_switches_back() -> None:
    obs = x11_obs()
    picker, _ = make(obs)

    listing = await picker.list_windows(X11_PROFILE)

    assert obs.statuses() == list(STATUS_REQUESTS)
    assert obs.names()[: len(STATUS_REQUESTS) + 1] == [*STATUS_REQUESTS, "GetSceneCollectionList"]
    assert "CreateSceneCollection" in obs.mutating()
    assert obs.listed_in == [OBS_COLLECTION_NAME]
    assert obs.mutating()[-1] == "SetCurrentSceneCollection"
    assert obs.calls[-1] == ("SetCurrentSceneCollection", {"sceneCollectionName": "Untitled"})
    assert obs.current_collection == "Untitled"
    assert listing.warning is None
    assert [item.value for item in listing.items] == [i.value for i in items(RETITLED) if i.enabled]


async def test_the_picker_offers_enabled_items_only() -> None:
    """R2 item 15: a retitled window is a disabled item with the stored value plus an enabled one."""
    obs = x11_obs(RETITLED)
    picker, _ = make(obs)

    listing = await picker.list_windows(X11_PROFILE)

    assert all(item.enabled for item in listing.items)
    assert STORED not in [item.value for item in listing.items]
    assert "amg-probe-window - level 2 - 59 fps" in [item.name for item in listing.items]


async def test_a_closed_pinned_window_is_not_offered() -> None:
    obs = x11_obs(CLOSED)
    picker, _ = make(obs)

    listing = await picker.list_windows(X11_PROFILE)

    assert [item.value for item in listing.items] == [i.value for i in items(CLOSED) if i.enabled]
    assert STORED not in [item.value for item in listing.items]


@pytest.mark.parametrize("state", [AppState.ARMED, AppState.RECORDING, AppState.FINALISING])
async def test_while_obs_is_on_the_app_collection_it_lists_at_once(state: AppState) -> None:
    obs = x11_obs(BEFORE)
    armed, _ = make(obs, AppState.IDLE)
    await armed._provisioner.ensure_collection(X11_PROFILE)  # what arming did
    obs.reset_calls()
    picker, _ = make(obs, state)

    listing = await picker.list_windows(replace(X11_PROFILE, capture=CaptureSettings()))

    assert obs.statuses() == []
    assert obs.mutating() == []
    assert obs.current_collection == OBS_COLLECTION_NAME
    assert [item.name for item in listing.items] == [i.name for i in items(BEFORE)]


@pytest.mark.parametrize(
    ("request_name", "named"),
    [
        ("GetStreamStatus", "stream"),
        ("GetRecordStatus", "recording"),
        ("GetReplayBufferStatus", "replay buffer"),
        ("GetVirtualCamStatus", "virtual camera"),
    ],
)
async def test_an_active_output_refuses_names_it_and_switches_nothing(request_name: str, named: str) -> None:
    obs = x11_obs(active=[request_name])
    picker, _ = make(obs)

    with pytest.raises(WindowListError, match=named):
        await picker.list_windows(X11_PROFILE)

    assert obs.names() == list(STATUS_REQUESTS)
    assert obs.listed_in == []
    assert obs.current_collection == "Untitled"


async def test_604_from_the_replay_buffer_and_virtual_camera_means_inactive() -> None:
    obs = x11_obs(unconfigured=["GetReplayBufferStatus", "GetVirtualCamStatus"])
    picker, _ = make(obs)

    await picker.list_windows(X11_PROFILE)

    assert obs.listed_in == [OBS_COLLECTION_NAME]


async def test_another_failure_of_a_status_request_is_reported() -> None:
    obs = x11_obs(unconfigured=["GetRecordStatus"])
    picker, _ = make(obs)

    with pytest.raises(ObsRequestError):
        await picker.list_windows(X11_PROFILE)

    assert obs.listed_in == []


async def test_the_switch_back_is_done_on_the_changed_event_even_after_the_answer() -> None:
    """R2 item 5: the answer came before ``...Changed`` twice; the step completes on the event."""
    obs = x11_obs(changed_event="after", event_delay_s=0.1)
    picker, _ = make(obs)

    listing = await picker.list_windows(X11_PROFILE)

    assert listing.warning is None
    assert ("CurrentSceneCollectionChanged", {"sceneCollectionName": "Untitled"}) in obs.events_sent


async def test_no_switch_back_when_the_app_collection_was_already_current() -> None:
    """R2 item 4: switching to the current collection sends no event, so it is never asked for."""
    obs = x11_obs(changed_event="never")
    setup, _ = make(obs)
    await setup._provisioner.ensure_collection(X11_PROFILE)
    obs.reset_calls()
    picker, _ = make(obs, timeout_s=0.05)

    listing = await picker.list_windows(X11_PROFILE)

    assert "SetCurrentSceneCollection" not in obs.names()
    assert listing.warning is None
    assert obs.current_collection == OBS_COLLECTION_NAME


async def test_a_switch_back_without_its_event_returns_the_windows_and_a_warning() -> None:
    obs = x11_obs(changed_event="never")
    picker, _ = make(obs, timeout_s=0.05)

    listing = await picker.list_windows(X11_PROFILE)

    assert listing.items
    assert listing.warning is not None
    assert "Untitled" in listing.warning


async def test_a_refused_switch_back_returns_a_warning() -> None:
    obs = x11_obs()
    picker, _ = make(obs)
    original = obs._SetCurrentSceneCollection

    def refuse_untitled(sceneCollectionName: str) -> None:  # noqa: N803
        if sceneCollectionName == "Untitled":
            raise ObsRequestError("SetCurrentSceneCollection", 600, "No scene collection was found.")
        original(sceneCollectionName)

    obs._SetCurrentSceneCollection = refuse_untitled  # type: ignore[method-assign]

    listing = await picker.list_windows(X11_PROFILE)

    assert listing.items
    assert listing.warning is not None and "Untitled" in listing.warning


async def test_a_failed_listing_still_switches_back() -> None:
    obs = x11_obs()
    picker, _ = make(obs)

    def broken(inputName: str, propertyName: str) -> dict[str, object]:  # noqa: N803
        raise ObsRequestError("GetInputPropertiesListPropertyItems", 702, "boom")

    obs._GetInputPropertiesListPropertyItems = broken  # type: ignore[method-assign]

    with pytest.raises(ObsError, match="boom"):
        await picker.list_windows(X11_PROFILE)

    assert obs.current_collection == "Untitled"


async def test_a_failed_listing_whose_switch_back_fails_says_both() -> None:
    obs = x11_obs(changed_event="never")
    picker, _ = make(obs, timeout_s=0.05)

    def broken(inputName: str, propertyName: str) -> dict[str, object]:  # noqa: N803
        raise ObsRequestError("GetInputPropertiesListPropertyItems", 702, "boom")

    obs._GetInputPropertiesListPropertyItems = broken  # type: ignore[method-assign]

    with pytest.raises(WindowListError) as caught:
        await picker.list_windows(X11_PROFILE)

    assert "boom" in str(caught.value) and "Untitled" in str(caught.value)


async def test_a_cancelled_listing_still_switches_back() -> None:
    obs = x11_obs()
    picker, _ = make(obs)
    entered = asyncio.Event()
    provisioner = picker._provisioner
    real_list = provisioner.list_windows

    async def stuck() -> list[WindowItem]:
        entered.set()
        await asyncio.sleep(60)
        return await real_list()

    provisioner.list_windows = stuck  # type: ignore[method-assign]
    task = asyncio.create_task(picker.list_windows(X11_PROFILE))
    await entered.wait()
    assert obs.current_collection == OBS_COLLECTION_NAME

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert obs.current_collection == "Untitled"


async def test_no_switch_back_once_the_app_armed_meanwhile() -> None:
    """Arming (tray, CLI) while the picker lists puts OBS on the app's collection to stay."""
    obs = x11_obs()
    picker, session = make(obs)
    provisioner = picker._provisioner
    real_list = provisioner.list_windows

    async def arm_meanwhile() -> list[WindowItem]:
        session.state = AppState.ARMED
        return await real_list()

    provisioner.list_windows = arm_meanwhile  # type: ignore[method-assign]

    await picker.list_windows(X11_PROFILE)

    assert obs.current_collection == OBS_COLLECTION_NAME


async def test_an_unreachable_obs_is_reported() -> None:
    obs = x11_obs()
    obs.crashed = True
    picker, _ = make(obs)

    with pytest.raises(ObsConnectError):
        await picker.list_windows(X11_PROFILE)


async def test_windows_lists_the_game_capture_input() -> None:
    game_list = [
        {"itemEnabled": True, "itemName": "", "itemValue": ""},
        {"itemEnabled": True, "itemName": "[game.exe]: Game", "itemValue": "Game:UnityWndClass:game.exe"},
        {"itemEnabled": False, "itemName": "[old.exe]: Old", "itemValue": "Old:Cls:old.exe"},
    ]
    obs = ListingObs(input_kinds=WINDOWS_KINDS, window_lists={"game_capture": game_list})
    picker, _ = make(obs, platform="win32")

    listing = await picker.list_windows(GameProfile(slug="g", title="G"))

    assert [(item.name, item.value) for item in listing.items] == [("[game.exe]: Game", "Game:UnityWndClass:game.exe")]
    assert obs.current_collection == "Untitled"


@pytest.mark.parametrize(
    ("capture", "platform", "kinds", "expected"),
    [
        (CaptureSettings(window=STORED), "linux", LINUX_X11_KINDS, "xcomposite_input"),
        (CaptureSettings(kind=CaptureKind.PIPEWIRE), "linux", LINUX_X11_KINDS, "pipewire-screen-capture-source"),
        (CaptureSettings(), "win32", WINDOWS_KINDS, "game_capture"),
    ],
)
async def test_the_capture_method_in_use_comes_from_the_provisioner_and_switches_nothing(
    capture: CaptureSettings, platform: str, kinds: tuple[str, ...], expected: str
) -> None:
    obs = ListingObs(input_kinds=kinds)
    picker, _ = make(obs, platform=platform)

    method = await picker.capture_method(GameProfile(slug="g", title="G", capture=capture))

    assert method == expected
    assert obs.names() == ["GetInputKindList"]
