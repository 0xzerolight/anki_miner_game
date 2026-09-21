"""OBS provisioning: the app's profile, scene collection, capture inputs and window list (spec 11.3).

``parse_obs_window_target`` and ``get_video_source_priority`` are ported from GameSentenceMiner
``GameSentenceMiner/obs/launch.py`` (lines 134 and 59) at commit
479747fe82d64f66980797a50bd6782ea06f58fa (GPL-3.0). Kept: the ``<title>:<class>:<exe>`` split and
the priority table. Changed: the parts are decoded the way OBS escapes them (``#3A`` for ``:``,
``#22`` for ``#``, ``obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:8-12``) instead of
GSM's ``rsplit`` and ``strip``, since OBS never writes a raw colon inside a part; the result is a
frozen dataclass instead of a dict.

Values from M0 are named constants below: source reading (``docs/m0/source-findings.md``), R1
(``docs/m0/clock.md``) and R2 (``docs/m0/obs-behaviour.md``), which confirmed them on a real OBS, and
E1 (``docs/m0/m1-exit-linux.md``), which measured ``INPUT_RELEASE_TIMEOUT_S``'s delay.
"""

import asyncio
import logging
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from anki_miner_game import paths
from anki_miner_game.interfaces.obs import ObsGateway
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME, OBS_SCENE_NAME
from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsError, ObsEventName, ObsRequestError, ProvisionResult, WindowItem
from anki_miner_game.models.profile import AudioMode, CaptureKind, GameProfile

log = logging.getLogger(__name__)

_X11_SEP: Final = "\r\n"

VIDEO_SOURCE_PRIORITY: Final = {"game_capture": 0, "window_capture": 1, "monitor_capture": 2}
"""Lower is preferred; any other kind ranks 999 (GSM ``VIDEO_SOURCE_PRIORITY``)."""


@dataclass(frozen=True)
class WindowTarget:
    """A Windows window string (``game_capture``, ``window_capture``, ``wasapi_process_output_capture``), decoded."""

    title: str
    window_class: str
    exe: str


def _decode(part: str) -> str:
    # OBS's own order (libobs/util/windows/window-helpers.c): "#3A" first, then "#22".
    return part.replace("#3A", ":").replace("#22", "#")


def parse_obs_window_target(value: str) -> WindowTarget | None:
    """``<title>:<class>:<exe>`` with each part decoded; ``None`` for an X11 value or anything else."""
    if _X11_SEP in value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    title, window_class, exe = (_decode(part) for part in parts)
    return WindowTarget(title=title, window_class=window_class, exe=exe)


def window_class_exe(value: str) -> tuple[str, str] | None:
    """Class and exe of a Windows window string, casefolded as OBS's ``window_rating`` compares them."""
    target = parse_obs_window_target(value)
    if target is None:
        return None
    return target.window_class.casefold(), target.exe.casefold()


def get_video_source_priority(input_kind: str | None) -> int:
    return VIDEO_SOURCE_PRIORITY.get(input_kind or "", 999)


# Profile ---------------------------------------------------------------------------------------

CONTAINER: Final = "mkv"
"""An ``.mkv`` survives an OBS crash (spec 6.4)."""

CONTAINER_KEYS: Final = (("SimpleOutput", "RecFormat2"), ("AdvOut", "RecFormat2"))
"""Read from source (source findings 1) and confirmed in a real ``basic.ini`` (``docs/m0/obs-behaviour.md`` item 1)."""

OFF_KEYS: Final = (("AdvOut", "RecSplitFile"), ("Video", "AutoRemux"))
"""Bool keys provisioning turns off: file splitting (Advanced mode only; Simple mode never splits)
and auto-remux, which would race finalise's rename with a second video (source findings 1 and 7,
summary item 15), both confirmed in a real ``basic.ini`` (``docs/m0/obs-behaviour.md`` item 1)."""

REACTIVATE_KEYS: Final = frozenset(
    {*CONTAINER_KEYS, ("Output", "Mode"), ("SimpleOutput", "RecQuality"), ("AdvOut", "RecEncoder")}
)
"""Keys OBS reads only when it builds its output handler, at launch and at each profile activation
(source findings section 2): the muxer from the container, the handler type from the output mode,
and whether recording has its own encoder (and so can pause) from the recording quality or encoder.
The first recording after provisioning was MP4 data under a ``.mkv`` name until the profile was
activated again (``docs/m0/obs-behaviour.md`` item 2). After writing one of them provisioning
switches to the profile it came from and back; OBS is never restarted. Every other row applies at
the next ``StartRecord``."""

SIMPLE_REC_QUALITY: Final = "Small"
"""``[SimpleOutput] RecQuality`` written in place of ``Stream`` ("Same as stream", OBS's default), with
which the recording shares the stream encoder and OBS cannot pause it: ``PauseRecord`` then does
nothing and no ``PAUSED`` event comes (``docs/m0/clock.md`` "Pause", ``docs/m0/obs-behaviour.md``
item 7). ``Small`` gives it its own encoder, OBS's default one for that quality
(``[SimpleOutput] RecEncoder`` is left alone); R2 recorded ``PAUSED`` and ``RESUMED`` with it
(``tests/fixtures/obs_transcripts/pause_resume.jsonl``)."""

ADV_STREAM_ENCODER_DEFAULT: Final = "obs_x264"
"""OBS's default ``[AdvOut] Encoder`` (``obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:770``)."""

AUDIO_KEYS: Final = (("Audio", "SampleRate"), ("Audio", "ChannelSetup"))
"""Copied from the profile provisioning starts on into the app's profile: a profile switch between
profiles where they differ stops at OBS's modal "Restart" question, and the switch's answer never
comes (``docs/m0/obs-behaviour.md`` item 3). They are copied before anything leaves the app's
profile, since ``SetProfileParameter`` writes the running profile, the side OBS compares."""

SWITCH_TIMEOUT_S: Final = 15.0
"""How long a profile or scene collection switch may take, answer and ``...Changed`` event (spec 6.2's
switch timeout)."""

INPUT_RELEASE_TIMEOUT_S: Final = 1.0
"""How long to wait for OBS to free the name of an input ``RemoveInput`` removed before creating it again.

``CreateInput`` refuses any name a source still holds (``obs-websocket@1ef34bf4
src/requesthandler/RequestHandler_Inputs.cpp:154-156``), and a removed source keeps its name until it
is destroyed, after the scene's next render and the UI have dropped their references
(``obs-studio@ba2f32bd libobs/obs-source.c:754-755``, ``libobs/obs-scene.c:1015-1021``). E1 measured
3.6 to 33.7 ms over 21 removals on OBS 32.2.2 at 30 fps, idle and while recording, never more than one
frame (``docs/m0/m1-exit-linux.md``); the app's own second arm found the name free at its second check
(``tests/fixtures/obs_transcripts/app_provision.jsonl``). One second is about 30 times the longest."""

POLL_S: Final = 0.2

RESOURCE_NOT_FOUND: Final = 600
"""obs-websocket ``RequestStatus::ResourceNotFound``."""


def scaled_output_size(base_width: int, base_height: int, max_height: int) -> tuple[int, int]:
    """Output size for a base (canvas) size: height capped at ``max_height``, aspect kept.

    Rounded down the way libobs aligns the output at video reset (width to a multiple of 4, height
    to a multiple of 2, ``obs-studio@ba2f32bd libobs/obs.c:1541-1543``), before it is compared or
    sent, so ``GetVideoSettings`` reports back exactly this size and the next arm sends nothing: R2
    set 854x480 and OBS ran 852x480 (``docs/m0/obs-behaviour.md`` item 16). Never below
    ``SetVideoSettings``' minimum of 8.
    """
    height = min(base_height, max_height)
    width = base_width if height == base_height else (base_width * height + base_height // 2) // base_height
    return max(width & ~3, 8), max(height & ~1, 8)


def _config_bool(value: str | None) -> bool:
    """``config_get_bool``: true for ``true`` or a non-zero integer (``libobs/util/config-file.c:682-689``)."""
    if value is None:
        return False
    text = value.strip()
    if text.lower() == "true":
        return True
    try:
        return int(text) != 0
    except ValueError:
        return False


# Scene collection -------------------------------------------------------------------------------

GAME_CAPTURE: Final = "game_capture"
WINDOW_CAPTURE: Final = "window_capture"
APP_AUDIO_CAPTURE: Final = "wasapi_process_output_capture"
WINDOWS_DESKTOP_AUDIO: Final = "wasapi_output_capture"
XCOMPOSITE: Final = "xcomposite_input"
PIPEWIRE: Final = "pipewire-screen-capture-source"
PULSE_DESKTOP_AUDIO: Final = "pulse_output_capture"

GAME_CAPTURE_INPUT: Final = "Game Capture"
WINDOW_CAPTURE_INPUT: Final = "Window Capture"
XCOMPOSITE_INPUT: Final = "Window Capture (X11)"
PIPEWIRE_INPUT: Final = "Screen Capture (PipeWire)"
APP_AUDIO_INPUT: Final = "Game Audio"
DESKTOP_AUDIO_INPUT: Final = "Desktop Audio Capture"
"""A regular input, not OBS's ``desktop1`` special input: a collection OBS creates after its first run
has no special audio inputs (``obs-studio@ba2f32bd frontend/widgets/OBSBasic_SceneCollections.cpp:1033-1048``,
``:1163-1165``). The name differs from the special input's default ("Desktop Audio") so both can exist."""

WINDOWS_INPUTS: Final = (GAME_CAPTURE_INPUT, WINDOW_CAPTURE_INPUT, APP_AUDIO_INPUT, DESKTOP_AUDIO_INPUT)
LINUX_INPUTS: Final = (XCOMPOSITE_INPUT, PIPEWIRE_INPUT, DESKTOP_AUDIO_INPUT)

XCOMPOSITE_PLACEHOLDER: Final = "0\r\nno window pinned\r\nanki-miner-game"
"""``capture_window`` of the X11 input while no window is pinned: it captures nothing, and OBS 32.2.2
aborts when the windows of an ``xcomposite_input`` with an empty ``capture_window`` are listed
(R1 side finding 2, ``docs/m0/clock.md``). R2 created it so: OBS lists the placeholder as a disabled item 0
and the live windows after it (``docs/m0/obs-behaviour.md`` section 1)."""

SPECIAL_AUDIO_SLOTS: Final = ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4")
"""``GetSpecialInputs`` fields; every one present in the app's collection is muted: the microphones by
spec 11.3, the desktop ones because the app's own inputs carry the game audio."""


@dataclass(eq=False)
class _Switch:
    """A switch waiting for its ``...Changed`` event: ``event`` whose ``data[field]`` is ``name``."""

    event: str
    field: str
    name: str
    loop: asyncio.AbstractEventLoop
    changed: asyncio.Future[None]


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


@dataclass(frozen=True)
class InputSpec:
    name: str
    kind: str
    settings: Mapping[str, object]


@dataclass(frozen=True)
class CollectionPlan:
    """What the scene ``Game`` should hold for one game profile on one OBS."""

    video: tuple[InputSpec, ...]
    """Bottom to top."""
    audio: tuple[InputSpec, ...]
    capture: str | None
    """The input kind that captures the game (the dialog's "capture method in use"); ``None`` when none is available."""


def _is_windows(platform: str) -> bool:
    return platform == "win32"


def plan_collection(profile: GameProfile, kinds: frozenset[str], platform: str) -> CollectionPlan:
    """Spec 11.3's collection table for ``profile``, skipping every input kind OBS does not report.

    A ``capture.kind`` that cannot work here (another platform's kind, a missing input kind, a
    window kind without a pinned window) falls back to ``auto``. The input the window list is read
    from is always present when its kind is: ``game_capture`` on Windows, ``xcomposite_input`` on X11
    (idle while it does not capture).
    """
    if _is_windows(platform):
        video, capture, audio = _plan_windows(profile, kinds)
    else:
        video, capture, audio = _plan_linux(profile, kinds)
    ordered = sorted(video, key=lambda spec: -get_video_source_priority(spec.kind))
    return CollectionPlan(video=tuple(ordered), audio=tuple(audio), capture=capture)


def _plan_windows(profile: GameProfile, kinds: frozenset[str]) -> tuple[list[InputSpec], str | None, list[InputSpec]]:
    window = profile.capture.window
    pinned = window if window and parse_obs_window_target(window) is not None else None
    kind = profile.capture.kind
    window_kind = kind is CaptureKind.WINDOW and pinned is not None and WINDOW_CAPTURE in kinds
    game_kind = kind is CaptureKind.GAME and GAME_CAPTURE in kinds
    video: list[InputSpec] = []
    capture: str | None = None
    if pinned is not None and WINDOW_CAPTURE in kinds and not game_kind:
        # The capture itself for the window kind; for auto, the fallback underneath game capture
        # for games that refuse the hook.
        video.append(InputSpec(WINDOW_CAPTURE_INPUT, WINDOW_CAPTURE, {"window": pinned, "cursor": False}))
        capture = WINDOW_CAPTURE
    if GAME_CAPTURE in kinds:
        settings: dict[str, object]
        if window_kind:
            # Kept only for its window list, which keeps minimized windows; it captures nothing.
            settings = {"capture_mode": "window", "window": ""}
        elif pinned is not None:
            settings = {"capture_mode": "window", "window": pinned, "capture_cursor": False}
        else:
            settings = {"capture_mode": "any_fullscreen", "capture_cursor": False}
        video.append(InputSpec(GAME_CAPTURE_INPUT, GAME_CAPTURE, settings))
        if not window_kind:
            capture = GAME_CAPTURE
    audio: list[InputSpec] = []
    if profile.audio.mode is AudioMode.APP and pinned is not None and APP_AUDIO_CAPTURE in kinds:
        audio.append(InputSpec(APP_AUDIO_INPUT, APP_AUDIO_CAPTURE, {"window": pinned}))
    elif WINDOWS_DESKTOP_AUDIO in kinds:
        audio.append(InputSpec(DESKTOP_AUDIO_INPUT, WINDOWS_DESKTOP_AUDIO, {"device_id": "default"}))
    return video, capture, audio


def _plan_linux(profile: GameProfile, kinds: frozenset[str]) -> tuple[list[InputSpec], str | None, list[InputSpec]]:
    window = profile.capture.window
    pinned = window if window and _X11_SEP in window else None
    wants_pipewire = profile.capture.kind is CaptureKind.PIPEWIRE and PIPEWIRE in kinds
    use_xcomposite = pinned is not None and XCOMPOSITE in kinds and not wants_pipewire
    use_pipewire = not use_xcomposite and PIPEWIRE in kinds
    video: list[InputSpec] = []
    capture: str | None = None
    if use_pipewire:
        video.append(InputSpec(PIPEWIRE_INPUT, PIPEWIRE, {"ShowCursor": False}))
        capture = PIPEWIRE
    if XCOMPOSITE in kinds:
        target = pinned if use_xcomposite and pinned is not None else XCOMPOSITE_PLACEHOLDER
        video.append(InputSpec(XCOMPOSITE_INPUT, XCOMPOSITE, {"capture_window": target, "show_cursor": False}))
        if use_xcomposite:
            capture = XCOMPOSITE
    audio: list[InputSpec] = []
    if PULSE_DESKTOP_AUDIO in kinds:
        audio.append(InputSpec(DESKTOP_AUDIO_INPUT, PULSE_DESKTOP_AUDIO, {"device_id": "default"}))
    return video, capture, audio


class ObsProvisioner:
    """``Provisioner`` over an ``ObsGateway``: reads first and sends a change only where OBS differs."""

    def __init__(
        self,
        gateway: ObsGateway,
        *,
        platform: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``platform`` defaults to ``sys.platform``, read at call time. Subscribes to ``gateway``'s events."""
        self._gateway = gateway
        self._platform = platform
        self._sleep = sleep
        self._switches: list[_Switch] = []
        self._switches_lock = threading.Lock()
        gateway.subscribe(self._on_event)

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        """Make the app's profile current (creating it when missing) and apply spec 11.3's profile rows.

        Besides spec 11.3's rows the recording gets its own encoder, so that OBS can pause it
        (``_ensure_own_recording_encoder``); every other encoder setting stays OBS's. Started on
        another profile (the user's), it copies that profile's ``AUDIO_KEYS`` into the app's, and
        after writing any of ``REACTIVATE_KEYS`` switches to it and back so OBS rebuilds its outputs.
        Started on the app's profile it has nowhere to switch to, so such a write sets
        ``needs_restart``: OBS applies it at the next profile activation (a disarm and arm) or
        restart, and the app never restarts OBS.

        It switches OBS to the app's profile and never back: the caller restores the user's profile,
        the session actor at disarm (T15) and the wizard right after provisioning (T21).
        """
        listing = await self._request("GetProfileList")
        home = listing.get("currentProfileName")
        if not isinstance(home, str) or not home or home == OBS_PROFILE_NAME:
            home = None
        audio = {key: await self._profile_parameter(*key) for key in AUDIO_KEYS} if home else {}
        changed = await self._use_profile(listing)
        written: list[tuple[str, str]] = []
        for key, value in audio.items():
            if value is not None and await self._profile_parameter(*key) != value:
                await self._set_profile_parameter(*key, value)
                written.append(key)
        changed |= await self._ensure_record_directory(cfg)
        changed |= await self._ensure_video(cfg)
        for key in CONTAINER_KEYS:
            if await self._profile_parameter(*key) != CONTAINER:
                await self._set_profile_parameter(*key, CONTAINER)
                written.append(key)
        for key in OFF_KEYS:
            if _config_bool(await self._profile_parameter(*key)):
                await self._set_profile_parameter(*key, "false")
                written.append(key)
        written += await self._ensure_own_recording_encoder()
        needs_restart = False
        if REACTIVATE_KEYS.intersection(written):
            if home is None:
                log.info("OBS applies the app profile's new recording settings at its next activation")
                needs_restart = True
            else:
                await self._switch_profile("SetCurrentProfile", home)
                await self._switch_profile("SetCurrentProfile", OBS_PROFILE_NAME)
        return ProvisionResult(changed=changed or bool(written), needs_restart=needs_restart)

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult:
        """Make the app's scene collection current (creating it when missing), make ``Game`` its
        program scene, and set the scene's inputs and mutes for ``profile`` (spec 11.3).

        ``Game`` is made the program scene because a new collection keeps OBS's own ``Scene`` as
        program scene (``docs/m0/obs-behaviour.md`` item 13). Only the app's own inputs
        (``WINDOWS_INPUTS`` / ``LINUX_INPUTS``) are created, changed or removed; any other input in
        the collection is left as it is.

        It switches OBS to the app's collection and never back: the caller restores the user's
        collection, the session actor at disarm (T15) and the wizard right after provisioning (T21).
        """
        changed = await self._use_collection()
        changed |= await self._use_scene()
        plan = await self._plan(profile)
        log.info("OBS capture for %s: %s", profile.slug, plan.capture or "none available")
        changed |= await self._apply_inputs(plan)
        changed |= await self._mute_special_inputs()
        return ProvisionResult(changed=changed, needs_restart=False)

    async def list_windows(self) -> list[WindowItem]:
        """The window list of the app's scene (``Provisioner.list_windows``).

        Windows reads ``game_capture``'s ``window``, whose list keeps minimized windows; X11 reads
        ``xcomposite_input``'s ``capture_window``, and only when that value is set, since listing an
        empty one aborts OBS (R1 side finding 2). Items without a string value (``game_capture``'s
        empty first item) are left out, so X11 item 0 stays the configured window.
        """
        if _is_windows(self._current_platform()):
            name, prop = GAME_CAPTURE_INPUT, "window"
        else:
            name, prop = XCOMPOSITE_INPUT, "capture_window"
            found = await self._input_settings(name)
            if found is None or found[0] != XCOMPOSITE or not found[1].get("capture_window"):
                return []
        try:
            data = await self._request("GetInputPropertiesListPropertyItems", inputName=name, propertyName=prop)
        except ObsRequestError as exc:
            if exc.code == RESOURCE_NOT_FOUND:
                return []
            raise
        raw = data.get("propertyItems")
        if not isinstance(raw, list):
            raise ObsError(f"GetInputPropertiesListPropertyItems returned no propertyItems list for {name!r}")
        items: list[WindowItem] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = item.get("itemValue")
            if not isinstance(value, str) or not value:
                continue
            label = item.get("itemName")
            items.append(
                WindowItem(
                    name=label if isinstance(label, str) and label else value,
                    value=value,
                    enabled=item.get("itemEnabled") is True,
                )
            )
        return items

    async def capture_method(self, profile: GameProfile) -> str:
        """``plan_collection``'s capture kind for ``profile`` on this OBS (``Provisioner.capture_method``).

        Reads ``GetInputKindList`` only and switches nothing; ``""`` when no capture kind is available.
        """
        return (await self._plan(profile)).capture or ""

    async def _plan(self, profile: GameProfile) -> CollectionPlan:
        kinds = frozenset((await self._request("GetInputKindList")).get("inputKinds") or [])
        return plan_collection(profile, kinds, self._current_platform())

    def _current_platform(self) -> str:
        return self._platform or sys.platform

    async def _request(self, name: str, **fields: Any) -> dict[str, Any]:
        return await self._gateway.request(name, **fields)

    async def _use_collection(self) -> bool:
        listing = await self._request("GetSceneCollectionList")
        if listing.get("currentSceneCollectionName") == OBS_COLLECTION_NAME:
            return False
        exists = OBS_COLLECTION_NAME in (listing.get("sceneCollections") or [])
        await self._switch(
            "SetCurrentSceneCollection" if exists else "CreateSceneCollection",
            ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED,
            "sceneCollectionName",
            OBS_COLLECTION_NAME,
        )
        return True

    async def _switch_profile(self, request: str, name: str) -> None:
        await self._switch(request, ObsEventName.CURRENT_PROFILE_CHANGED, "profileName", name)

    async def _switch(self, request: str, event: str, field: str, name: str) -> None:
        """Send ``request`` for ``name`` and wait for its answer and for OBS's ``event`` naming ``name``.

        A switch is done on its ``...Changed`` event, never on the answer alone: the answer can come
        first (``docs/m0/obs-behaviour.md`` item 5), and ``CreateProfile`` answers before OBS has even
        begun to switch. The waiter is in place before the request goes out. Never sent for the
        profile or collection already current, which OBS answers without an event (item 4). Raises
        ``ObsError`` after ``SWITCH_TIMEOUT_S``; an answer that does not come usually means OBS is
        asking whether to restart (item 3).
        """
        loop = asyncio.get_running_loop()
        switch = _Switch(event, field, name, loop, loop.create_future())
        with self._switches_lock:
            self._switches.append(switch)
        try:
            async with asyncio.timeout(SWITCH_TIMEOUT_S):
                await self._request(request, **{field: name})
                await switch.changed
        except TimeoutError:
            raise ObsError(
                f"OBS did not finish switching to {name!r} within {SWITCH_TIMEOUT_S:g} s; "
                "an OBS dialog may be waiting for an answer"
            ) from None
        finally:
            with self._switches_lock:
                self._switches.remove(switch)

    def _on_event(self, event: ObsEvent) -> None:
        """The gateway's event handler (obsws-python's thread or the loop): wakes the switch it completes."""
        with self._switches_lock:
            done = [s for s in self._switches if s.event == event.name and event.data.get(s.field) == s.name]
        for switch in done:
            switch.loop.call_soon_threadsafe(_resolve, switch.changed)

    async def _use_scene(self) -> bool:
        listing = await self._request("GetSceneList")
        names = {scene.get("sceneName") for scene in listing.get("scenes") or [] if isinstance(scene, dict)}
        changed = False
        if OBS_SCENE_NAME not in names:
            await self._request("CreateScene", sceneName=OBS_SCENE_NAME)
            changed = True
        if listing.get("currentProgramSceneName") != OBS_SCENE_NAME:
            await self._request("SetCurrentProgramScene", sceneName=OBS_SCENE_NAME)
            changed = True
        return changed

    async def _input_settings(self, name: str) -> tuple[str, dict[str, Any]] | None:
        """``(kind, settings)`` of an input of the current collection, ``None`` when it does not exist."""
        try:
            data = await self._request("GetInputSettings", inputName=name)
        except ObsRequestError as exc:
            if exc.code == RESOURCE_NOT_FOUND:
                return None
            raise
        settings = data.get("inputSettings")
        return str(data.get("inputKind")), settings if isinstance(settings, dict) else {}

    async def _apply_inputs(self, plan: CollectionPlan) -> bool:
        managed = WINDOWS_INPUTS if _is_windows(self._current_platform()) else LINUX_INPUTS
        wanted = {spec.name: spec for spec in plan.video + plan.audio}
        existing: dict[str, dict[str, Any]] = {}
        removed: set[str] = set()
        changed = False
        for name in managed:
            found = await self._input_settings(name)
            if found is None:
                continue
            kind, settings = found
            spec = wanted.get(name)
            if spec is None or spec.kind != kind:
                await self._request("RemoveInput", inputName=name)
                removed.add(name)
                changed = True
            else:
                existing[name] = settings
        # CreateInput puts the new item on top: once a video input has to be created, every video
        # input planned above it is created again, so the window capture fallback stays underneath.
        missing_below = False
        for spec in plan.video:
            if spec.name not in existing:
                missing_below = True
            elif missing_below:
                await self._request("RemoveInput", inputName=spec.name)
                removed.add(spec.name)
                del existing[spec.name]
        for spec in plan.video + plan.audio:
            settings = dict(spec.settings)
            if spec.name not in existing:
                if spec.name in removed:
                    await self._wait_released(spec.name)
                await self._request(
                    "CreateInput",
                    sceneName=OBS_SCENE_NAME,
                    inputName=spec.name,
                    inputKind=spec.kind,
                    inputSettings=settings,
                )
                changed = True
            elif any(existing[spec.name].get(key) != value for key, value in settings.items()):
                await self._request("SetInputSettings", inputName=spec.name, inputSettings=settings)
                changed = True
        return changed

    async def _wait_released(self, name: str) -> None:
        """Wait until OBS has freed the name of the removed input ``name`` (``INPUT_RELEASE_TIMEOUT_S``).

        A removed source still answers ``GetInputSettings`` until OBS destroys it; then 600.
        """

        async def released() -> bool:
            return await self._input_settings(name) is None

        if not await self._poll(released, INPUT_RELEASE_TIMEOUT_S):
            raise ObsError(f"OBS still holds the removed input {name!r} after {INPUT_RELEASE_TIMEOUT_S:g} s")

    async def _poll(self, done: Callable[[], Awaitable[bool]], timeout_s: float) -> bool:
        """Check ``done`` now and every ``POLL_S`` until it holds (``True``) or ``timeout_s`` has passed (``False``)."""
        for attempt in range(round(timeout_s / POLL_S) + 1):
            if attempt:
                await self._sleep(POLL_S)
            if await done():
                return True
        return False

    async def _mute_special_inputs(self) -> bool:
        special = await self._request("GetSpecialInputs")
        changed = False
        for slot in SPECIAL_AUDIO_SLOTS:
            name = special.get(slot)
            if not isinstance(name, str) or not name:
                continue
            if not (await self._request("GetInputMute", inputName=name)).get("inputMuted"):
                await self._request("SetInputMute", inputName=name, inputMuted=True)
                changed = True
        return changed

    async def _use_profile(self, listing: Mapping[str, Any]) -> bool:
        """Make the app's profile current; ``listing`` is ``GetProfileList``'s answer from just before."""
        if listing.get("currentProfileName") == OBS_PROFILE_NAME:
            return False
        exists = OBS_PROFILE_NAME in (listing.get("profiles") or [])
        # CreateProfile answers before the profile exists; OBS then switches to it (source findings 8).
        await self._switch_profile("SetCurrentProfile" if exists else "CreateProfile", OBS_PROFILE_NAME)
        return True

    async def _ensure_own_recording_encoder(self) -> list[tuple[str, str]]:
        """Give the recording its own encoder so that OBS can pause it; return the keys written.

        The app never sends ``PauseRecord``: a pause made in OBS reaches it as ``PAUSED`` and
        ``RESUMED``, which OBS sends only for a recording with its own encoder (Simple mode: a
        ``RecQuality`` other than ``Stream``; Advanced mode: a ``RecEncoder`` other than ``none``,
        ``obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:371-392``). Both modes are set,
        whichever is active, as the container is, so a mode changed later in OBS's settings still
        pauses. Advanced mode records with its own instance of the stream encoder's kind
        (``[AdvOut] Encoder``); bitrate and every other encoder setting stay OBS's.
        """
        written: list[tuple[str, str]] = []
        if await self._profile_parameter("SimpleOutput", "RecQuality") in (None, "Stream"):
            await self._set_profile_parameter("SimpleOutput", "RecQuality", SIMPLE_REC_QUALITY)
            written.append(("SimpleOutput", "RecQuality"))
        encoder = await self._profile_parameter("AdvOut", "RecEncoder")
        if encoder is None or encoder.casefold() == "none":  # OBS compares case-insensitively
            stream = await self._profile_parameter("AdvOut", "Encoder") or ADV_STREAM_ENCODER_DEFAULT
            await self._set_profile_parameter("AdvOut", "RecEncoder", stream)
            written.append(("AdvOut", "RecEncoder"))
        return written

    async def _ensure_record_directory(self, cfg: AppConfig) -> bool:
        wanted = str(paths.incoming_dir(cfg))
        if (await self._request("GetRecordDirectory")).get("recordDirectory") == wanted:
            return False
        await self._request("SetRecordDirectory", recordDirectory=wanted)
        return True

    async def _ensure_video(self, cfg: AppConfig) -> bool:
        video = await self._request("GetVideoSettings")
        width, height = scaled_output_size(video["baseWidth"], video["baseHeight"], cfg.recording.max_height)
        pairs: dict[str, int] = {}
        if (video.get("outputWidth"), video.get("outputHeight")) != (width, height):
            pairs.update(outputWidth=width, outputHeight=height)
        if (video.get("fpsNumerator"), video.get("fpsDenominator")) != (cfg.recording.fps, 1):
            pairs.update(fpsNumerator=cfg.recording.fps, fpsDenominator=1)
        if not pairs:
            return False
        await self._request("SetVideoSettings", **pairs)
        return True

    async def _profile_parameter(self, section: str, key: str) -> str | None:
        data = await self._request("GetProfileParameter", parameterCategory=section, parameterName=key)
        value = data.get("parameterValue")
        if value is None:
            value = data.get("defaultParameterValue")
        return value if isinstance(value, str) else None

    async def _set_profile_parameter(self, section: str, key: str, value: str) -> None:
        await self._request("SetProfileParameter", parameterCategory=section, parameterName=key, parameterValue=value)
