"""In-memory OBS behind the ``ObsGateway.request`` surface, for provisioning tests.

It keeps the state provisioning reads and writes (profiles and their ``basic.ini`` values, video
settings, scene collections, scenes, inputs, special inputs, mute flags, window lists) and answers
the obs-websocket requests the provisioner sends with the shapes and status codes
obs-websocket 5.7.4 uses (``docs/m0/source-findings.md``; ``RequestHandler_Config.cpp``,
``RequestHandler_Inputs.cpp`` at ``obs-websocket@1ef34bf4``):

- ``GetProfileParameter`` returns the effective value when the key has a default, else the user
  value, else ``null``; the defaults are OBS's (``PROFILE_DEFAULTS``,
  ``obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:743-778, 871-872``).
- A profile switch between profiles whose audio values differ is OBS's modal "Restart" question
  (``docs/m0/obs-behaviour.md`` item 3): the running profile's effective values against the values
  saved in the target's file, skipping a key the target lacks, so ``CreateProfile`` never asks. OBS
  saves both values into a profile's file when it leaves it. The fake records each question in
  ``restart_questions`` and completes the switch, as if the user answered "No".
- The recording format is fixed when OBS builds its output handler, at launch and at each profile
  activation (source findings section 2, R2 item 2): ``recording_format`` is the running profile's
  ``[SimpleOutput] RecFormat2`` as it was when the profile was last activated, and
  ``recording_pausable`` whether the recording had its own encoder then, so that OBS can pause it
  (``OBSBasic::UpdateIsRecordingPausable``, ``frontend/widgets/OBSBasic_Recording.cpp:371-392``;
  Advanced mode's FFmpeg recording type is not modelled).
- Handlers given to ``subscribe`` get the switch events: ``CurrentProfileChanging`` and
  ``CurrentProfileChanged``, ``CurrentSceneCollectionChanging`` and ``CurrentSceneCollectionChanged``,
  each ``...Changing`` naming the old profile or collection and each ``...Changed`` the new one, in
  the usual order before the answer (``docs/m0/obs-behaviour.md`` section 3). With
  ``events_after_answer`` they come after it (R2 item 5); an event named in ``lost_events`` never
  comes. A switch to the current profile or collection answers with no event (R2 item 4). Every
  event sent is recorded in ``events``.
- ``CreateProfile`` answers before the profile exists; OBS then switches to it
  ``create_profile_delay`` seconds later with ``CurrentProfileChanged`` and no ``...Changing`` event
  (R2 section 3); ``None`` never switches.
- A profile's default output size is its base scaled down to at most 1280x720 pixels
  (``obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:599, 830-843``); ``SetVideoSettings`` and
  that default are aligned as libobs aligns them (width to 4, height to 2).
- A new scene collection holds one scene, ``Scene``, and no inputs or special inputs.
- ``CreateInput`` appends the scene item on top; names are unique per collection.
- ``RemoveInput`` takes the input out of its scenes, but OBS frees its name only once the source is
  destroyed, after the scene's next render and the UI have dropped their references
  (``obs-studio@ba2f32bd libobs/obs-source.c:754-755``, ``libobs/obs-scene.c:1015-1021``). For the next
  ``release_delay`` requests the name still answers ``GetInputSettings`` with the removed input, and
  ``CreateInput`` refuses it with 601 (``RequestHandler_Inputs.cpp:154-156``).
- Listing the windows of an ``xcomposite_input`` whose ``capture_window`` is empty aborts OBS
  (R1 side finding 2); the fake records it and drops the connection.

Every request is recorded in ``calls``; a request whose name does not start with ``Get`` mutates.
"""

# Handler parameters carry obs-websocket's camelCase field names, so a misspelt field fails the call.
# ruff: noqa: N803

import asyncio
import copy
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsConnectError, ObsInfo, ObsRequestError

RESOURCE_NOT_FOUND = 600
RESOURCE_ALREADY_EXISTS = 601
INVALID_INPUT_KIND = 605

PROFILE_DEFAULTS = {
    ("Output", "Mode"): "Simple",
    ("SimpleOutput", "RecQuality"): "Stream",
    ("AdvOut", "Encoder"): "obs_x264",
    ("AdvOut", "RecEncoder"): "none",
    ("SimpleOutput", "RecFormat2"): "hybrid_mp4",
    ("AdvOut", "RecFormat2"): "hybrid_mp4",
    ("Audio", "SampleRate"): "48000",
    ("Audio", "ChannelSetup"): "Stereo",
}
AUDIO_KEYS = (("Audio", "SampleRate"), ("Audio", "ChannelSetup"))

DEFAULT_OUTPUT_SCALES = (1.0, 1.25, 1.0 / 0.75, 1.5, 1.0 / 0.6, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0)
"""OBS's ``scaled_vals``: the first one that brings the base to at most 1280x720 pixels gives the
default output size of a new profile."""

LIST_PROPERTY = {
    "game_capture": "window",
    "window_capture": "window",
    "wasapi_process_output_capture": "window",
    "xcomposite_input": "capture_window",
    "monitor_capture": "monitor_id",
}

WINDOWS_KINDS = (
    "game_capture",
    "window_capture",
    "monitor_capture",
    "wasapi_process_output_capture",
    "wasapi_output_capture",
    "wasapi_input_capture",
)
LINUX_X11_KINDS = ("xcomposite_input", "xshm_input_v2", "pipewire-screen-capture-source", "pulse_output_capture")
LINUX_WAYLAND_KINDS = ("pipewire-screen-capture-source", "pulse_output_capture", "pulse_input_capture")


@dataclass
class FakeInput:
    kind: str
    settings: dict[str, Any]
    muted: bool = False


@dataclass
class FakeCollection:
    scenes: dict[str, list[str]] = field(default_factory=lambda: {"Scene": []})
    """Scene name -> input names bottom to top."""
    program_scene: str = "Scene"
    inputs: dict[str, FakeInput] = field(default_factory=dict)
    special: dict[str, str | None] = field(default_factory=dict)
    removed: dict[str, tuple[FakeInput, int]] = field(default_factory=dict)
    """Removed inputs OBS has not destroyed yet: name -> (input, number of the ``RemoveInput`` request)."""


class Sleeps:
    """An injected ``sleep`` that records each wait and returns at once."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class FakeObs:
    def __init__(
        self,
        *,
        input_kinds: Iterable[str],
        base_size: tuple[int, int] = (1920, 1080),
        window_lists: Mapping[str, list[dict[str, Any]]] | None = None,
        create_profile_delay: float | None = 0.0,
        release_delay: int = 2,
        events_after_answer: bool = False,
        lost_events: Iterable[str] = (),
    ) -> None:
        self.input_kinds = list(input_kinds)
        self.window_lists = dict(window_lists or {})
        """Input kind -> ``propertyItems`` of its list property (``LIST_PROPERTY``: windows, or monitors)."""
        self.create_profile_delay = create_profile_delay
        self.events_after_answer = events_after_answer
        self.lost_events = frozenset(lost_events)
        self.release_delay = release_delay
        """Requests after ``RemoveInput`` during which OBS still holds the removed input's name."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[int, str, dict[str, Any]]] = []
        """``(len(calls) when it was sent, eventType, eventData)`` of every event sent to the handlers."""
        self.handlers: list[Callable[[ObsEvent], None]] = []
        self.profiles: dict[str, dict[tuple[str, str], str]] = {"Untitled": {}}
        self.current_profile = "Untitled"
        self.record_dirs: dict[str, str] = {"Untitled": "/home/user/Videos"}
        self.video: dict[str, dict[str, int]] = {"Untitled": self._default_video(*base_size)}
        self.collections: dict[str, FakeCollection] = {"Untitled": FakeCollection()}
        self.current_collection = "Untitled"
        self.crashed = False
        self.restart_questions: list[tuple[str, str]] = []
        """``(from, to)`` of every profile switch at which OBS asked to restart."""
        self.recording_format = self._effective("Untitled", ("SimpleOutput", "RecFormat2"))
        self.recording_pausable = self._pausable("Untitled")
        self._base_size = base_size
        self._requests = 0

    # ObsGateway surface ------------------------------------------------------------------

    async def connect(self) -> ObsInfo:
        raise NotImplementedError

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None:
        self.handlers.append(handler)

    @property
    def collection_changing(self) -> bool:
        return False

    async def close(self) -> None:
        pass

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        self._requests += 1
        self.calls.append((name, copy.deepcopy(fields)))
        if self.crashed:
            raise ObsConnectError("OBS is gone")
        handler = getattr(self, "_" + name, None)
        if handler is None:
            raise AssertionError(f"FakeObs does not implement {name}")
        result = handler(**fields)
        return {} if result is None else result

    # Helpers for tests ---------------------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def mutating(self) -> list[str]:
        return [name for name in self.names() if not name.startswith("Get")]

    def reset_calls(self) -> None:
        self.calls.clear()
        self.events.clear()

    @property
    def collection(self) -> FakeCollection:
        return self.collections[self.current_collection]

    def scene_items(self, scene: str = "Game") -> list[str]:
        return list(self.collection.scenes[scene])

    def add_special_input(self, slot: str, name: str, kind: str, *, muted: bool = False) -> None:
        self.collection.inputs[name] = FakeInput(kind, {}, muted)
        self.collection.special[slot] = name

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        if name in self.lost_events:
            return
        self.events.append((len(self.calls), name, dict(data)))
        for handler in self.handlers:
            handler(ObsEvent(name, dict(data), 0.0))

    def _announce(self, *events: tuple[str, dict[str, Any]]) -> None:
        """Send the events of a switch that is already done: before the answer, or after it."""
        if self.events_after_answer:
            asyncio.get_running_loop().call_soon(self._announce_now, events)
        else:
            self._announce_now(events)

    def _announce_now(self, events: tuple[tuple[str, dict[str, Any]], ...]) -> None:
        for name, data in events:
            self._emit(name, data)

    def _effective(self, profile: str, key: tuple[str, str]) -> str | None:
        return self.profiles[profile].get(key, PROFILE_DEFAULTS.get(key))

    def _activate(self, target: str) -> None:
        """Leave the running profile for ``target`` (``OBSBasic_Profiles.cpp:663-748``)."""
        left = self.current_profile
        running = {key: self._effective(left, key) for key in AUDIO_KEYS}
        saved = self.profiles[target]
        if any(key in saved and saved[key] != running[key] for key in AUDIO_KEYS):
            self.restart_questions.append((left, target))
        for key, value in running.items():
            if value is not None:
                self.profiles[left].setdefault(key, value)
        self.current_profile = target
        self.recording_format = self._effective(target, ("SimpleOutput", "RecFormat2"))
        self.recording_pausable = self._pausable(target)

    def _pausable(self, profile: str) -> bool:
        if (self._effective(profile, ("Output", "Mode")) or "").casefold() == "advanced":
            return (self._effective(profile, ("AdvOut", "RecEncoder")) or "").casefold() != "none"
        return self._effective(profile, ("SimpleOutput", "RecQuality")) != "Stream"

    def _land_profile(self, target: str) -> None:
        """OBS switches to the profile ``CreateProfile`` made (``OBSBasic::CreateNewProfile``)."""
        self._activate(target)
        self._emit("CurrentProfileChanged", {"profileName": target})

    def _switch_collection(self, target: str) -> None:
        left, self.current_collection = self.current_collection, target
        self._announce(
            ("CurrentSceneCollectionChanging", {"sceneCollectionName": left}),
            ("CurrentSceneCollectionChanged", {"sceneCollectionName": target}),
        )

    def _default_video(self, base_w: int, base_h: int) -> dict[str, int]:
        out_w, out_h = base_w, base_h
        for scale in DEFAULT_OUTPUT_SCALES:
            if out_w * out_h <= 1280 * 720:
                break
            out_w, out_h = int(base_w / scale), int(base_h / scale)
        return {
            "baseWidth": base_w,
            "baseHeight": base_h,
            "outputWidth": out_w & ~3,
            "outputHeight": out_h & ~1,
            "fpsNumerator": 30,
            "fpsDenominator": 1,
        }

    @staticmethod
    def _fail(request: str, code: int, comment: str = "") -> ObsRequestError:
        return ObsRequestError(request, code, comment)

    def _held(self, input_name: str) -> FakeInput | None:
        """A removed input whose source OBS has not destroyed yet, so it still owns its name."""
        held = self.collection.removed.get(input_name)
        if held is None:
            return None
        found, removed_at = held
        if self._requests - removed_at > self.release_delay:
            del self.collection.removed[input_name]
            return None
        return found

    def _input(self, request: str, input_name: str) -> FakeInput:
        found = self.collection.inputs.get(input_name) or self._held(input_name)
        if found is None:
            raise self._fail(request, RESOURCE_NOT_FOUND, f"No source was found by the name of `{input_name}`.")
        return found

    # Profiles ------------------------------------------------------------------------------

    def _GetProfileList(self) -> dict[str, Any]:
        return {"currentProfileName": self.current_profile, "profiles": list(self.profiles)}

    def _CreateProfile(self, profileName: str) -> None:
        if profileName in self.profiles:
            raise self._fail("CreateProfile", RESOURCE_ALREADY_EXISTS)
        self.profiles[profileName] = {}
        self.record_dirs[profileName] = self.record_dirs[self.current_profile]
        self.video[profileName] = self._default_video(*self._base_size)
        loop = asyncio.get_running_loop()
        if self.create_profile_delay == 0:
            loop.call_soon(self._land_profile, profileName)  # before the caller's next step
        elif self.create_profile_delay is not None:
            loop.call_later(self.create_profile_delay, self._land_profile, profileName)

    def _SetCurrentProfile(self, profileName: str) -> None:
        if profileName not in self.profiles:
            raise self._fail("SetCurrentProfile", RESOURCE_NOT_FOUND)
        if profileName != self.current_profile:  # the current one answers at once, no event (R2 item 4)
            left = self.current_profile
            self._activate(profileName)
            self._announce(
                ("CurrentProfileChanging", {"profileName": left}),
                ("CurrentProfileChanged", {"profileName": profileName}),
            )

    def _GetProfileParameter(self, parameterCategory: str, parameterName: str) -> dict[str, Any]:
        key = (parameterCategory, parameterName)
        values = self.profiles[self.current_profile]
        default = PROFILE_DEFAULTS.get(key)
        if default is not None:
            return {"parameterValue": values.get(key, default), "defaultParameterValue": default}
        return {"parameterValue": values.get(key), "defaultParameterValue": None}

    def _SetProfileParameter(self, parameterCategory: str, parameterName: str, parameterValue: str) -> None:
        self.profiles[self.current_profile][(parameterCategory, parameterName)] = parameterValue

    def _GetRecordDirectory(self) -> dict[str, Any]:
        return {"recordDirectory": self.record_dirs[self.current_profile]}

    def _SetRecordDirectory(self, recordDirectory: str) -> None:
        self.record_dirs[self.current_profile] = recordDirectory

    def _GetVideoSettings(self) -> dict[str, Any]:
        return dict(self.video[self.current_profile])

    def _SetVideoSettings(self, **pairs: int) -> None:
        video = self.video[self.current_profile]
        video.update(pairs)
        video["outputWidth"] &= ~3
        video["outputHeight"] &= ~1

    # Scene collections and scenes -----------------------------------------------------------

    def _GetSceneCollectionList(self) -> dict[str, Any]:
        return {"currentSceneCollectionName": self.current_collection, "sceneCollections": list(self.collections)}

    def _CreateSceneCollection(self, sceneCollectionName: str) -> None:
        if sceneCollectionName in self.collections:
            raise self._fail("CreateSceneCollection", RESOURCE_ALREADY_EXISTS)
        self.collections[sceneCollectionName] = FakeCollection()
        self._switch_collection(sceneCollectionName)

    def _SetCurrentSceneCollection(self, sceneCollectionName: str) -> None:
        if sceneCollectionName not in self.collections:
            raise self._fail("SetCurrentSceneCollection", RESOURCE_NOT_FOUND)
        if sceneCollectionName != self.current_collection:  # the current one: no event (R2 item 4)
            self._switch_collection(sceneCollectionName)

    def _GetSceneList(self) -> dict[str, Any]:
        names = list(self.collection.scenes)
        return {
            "currentProgramSceneName": self.collection.program_scene,
            "currentPreviewSceneName": None,
            "scenes": [{"sceneIndex": i, "sceneName": n, "sceneUuid": f"uuid-{n}"} for i, n in enumerate(names)],
        }

    def _CreateScene(self, sceneName: str) -> dict[str, Any]:
        if sceneName in self.collection.scenes:
            raise self._fail("CreateScene", RESOURCE_ALREADY_EXISTS)
        self.collection.scenes[sceneName] = []
        return {"sceneUuid": f"uuid-{sceneName}"}

    def _SetCurrentProgramScene(self, sceneName: str) -> None:
        if sceneName not in self.collection.scenes:
            raise self._fail("SetCurrentProgramScene", RESOURCE_NOT_FOUND)
        self.collection.program_scene = sceneName

    # Inputs --------------------------------------------------------------------------------

    def _GetInputKindList(self, unversioned: bool = False) -> dict[str, Any]:
        return {"inputKinds": list(self.input_kinds)}

    def _GetInputSettings(self, inputName: str) -> dict[str, Any]:
        found = self._input("GetInputSettings", inputName)
        return {"inputKind": found.kind, "inputSettings": copy.deepcopy(found.settings)}

    def _CreateInput(
        self,
        sceneName: str,
        inputName: str,
        inputKind: str,
        inputSettings: dict[str, Any] | None = None,
        sceneItemEnabled: bool = True,
    ) -> dict[str, Any]:
        collection = self.collection
        if sceneName not in collection.scenes:
            raise self._fail("CreateInput", RESOURCE_NOT_FOUND)
        if inputName in collection.inputs or self._held(inputName) is not None:
            raise self._fail("CreateInput", RESOURCE_ALREADY_EXISTS, "A source already exists by that input name.")
        if inputKind not in self.input_kinds:
            raise self._fail("CreateInput", INVALID_INPUT_KIND)
        collection.inputs[inputName] = FakeInput(inputKind, copy.deepcopy(inputSettings or {}))
        collection.scenes[sceneName].append(inputName)
        return {"inputUuid": f"uuid-{inputName}", "sceneItemId": len(collection.scenes[sceneName])}

    def _SetInputSettings(self, inputName: str, inputSettings: dict[str, Any], overlay: bool = True) -> None:
        found = self._input("SetInputSettings", inputName)
        if overlay:
            found.settings.update(copy.deepcopy(inputSettings))
        else:
            found.settings = copy.deepcopy(inputSettings)

    def _RemoveInput(self, inputName: str) -> None:
        found = self._input("RemoveInput", inputName)
        collection = self.collection
        if collection.inputs.get(inputName) is not found:
            return  # Already removed: obs_source_remove does nothing to a removed source.
        del collection.inputs[inputName]
        collection.removed[inputName] = (found, self._requests)
        for items in collection.scenes.values():
            if inputName in items:
                items.remove(inputName)

    def _GetSpecialInputs(self) -> dict[str, Any]:
        slots = ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4")
        return {slot: self.collection.special.get(slot) for slot in slots}

    def _GetInputMute(self, inputName: str) -> dict[str, Any]:
        return {"inputMuted": self._input("GetInputMute", inputName).muted}

    def _SetInputMute(self, inputName: str, inputMuted: bool) -> None:
        self._input("SetInputMute", inputName).muted = inputMuted

    def _GetInputPropertiesListPropertyItems(self, inputName: str, propertyName: str) -> dict[str, Any]:
        found = self._input("GetInputPropertiesListPropertyItems", inputName)
        if LIST_PROPERTY.get(found.kind) != propertyName:
            raise self._fail("GetInputPropertiesListPropertyItems", RESOURCE_NOT_FOUND, "no such property")
        if found.kind == "xcomposite_input" and not found.settings.get("capture_window"):
            self.crashed = True
            raise ObsConnectError("OBS aborted: basic_string: construction from null is not valid")
        return {"propertyItems": copy.deepcopy(self.window_lists.get(found.kind, []))}
