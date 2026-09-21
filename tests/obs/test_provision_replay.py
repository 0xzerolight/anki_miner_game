"""FakeObs replayed against real OBS frames (card T14: provisioning transcript replay).

Every provisioning test runs on ``FakeObs``, so "the second run sends no mutating request" holds only
as far as the fake answers the way OBS does. The fixtures hold frames recorded against OBS 32.2.2
(Flatpak) with obs-websocket 5.7.4 on the M0 rig:

- ``tests/fixtures/obs_provision/r1-trial2-provisioning.jsonl``: R1 run
  ``campaign-simple-default-trial2-173143``, ``transcript.jsonl`` lines 9-22 (``docs/m0/clock.md``),
  copied verbatim: video settings written and read back, an ``xcomposite_input`` created with a
  placeholder window, its window list, a window set on it.
- ``tests/fixtures/obs_transcripts/provision.jsonl``: R2's first provisioning (its ``README.md``): the
  app's profile created, its record directory, video and ``basic.ini`` rows written and read back,
  the app's collection and scene ``Game`` created, an ``xcomposite_input`` and a
  ``pulse_output_capture`` created, the special inputs read. Requests the fake does not implement
  (``GetVersion``, the output statuses) are not provisioning's and are skipped.

Each recorded request that provisioning also sends goes to a ``FakeObs`` that knows only the rig
(screen size, input kinds, the windows on screen). The fake must answer with OBS's status code and,
for every field provisioning reads, OBS's value. ``InputCreated`` and ``InputSettingsChanged`` carry
the settings serialised the way ``GetInputSettings`` serialises them (``obs-websocket@1ef34bf4
src/eventhandler/EventHandler_Inputs.cpp:44,53,123,128``,
``src/requesthandler/RequestHandler_Inputs.cpp:322-325``), so at each such event the fake's
``GetInputSettings`` must match it too. The switch events the fake sends (``SWITCH_EVENTS``) must be
OBS's, in OBS's order; at each recorded event the replay yields once, so a switch OBS makes after its
answer (``CreateProfile``) has landed in the fake before the next request, as it had in OBS.

Follow-up E1-PROVISION-REPLAY (master plan E1 card): no real frame covers ``RemoveInput`` and how
long OBS then holds the name (``INPUT_RELEASE_TIMEOUT_S``), ``GetInputMute`` and ``SetInputMute``,
``SetCurrentProgramScene``, ``GetProfileParameter`` on a fresh profile before any write, a setting
written equal to its default, the audio copy or the profile re-activation. E1 records
``ObsProvisioner``'s own first and second run through ``tools/obs_transcript_recorder.py``, and that
transcript is replayed request by request against the provisioner.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from anki_miner_game.models.config import AppConfig, RecordingSettings
from anki_miner_game.models.constants import OBS_SCENE_NAME
from anki_miner_game.models.obs import ObsRequestError, ProvisionResult
from anki_miner_game.models.profile import CaptureSettings, GameProfile
from anki_miner_game.obs.provision import (
    CONTAINER_KEYS,
    DESKTOP_AUDIO_INPUT,
    OFF_KEYS,
    XCOMPOSITE_INPUT,
    ObsProvisioner,
)
from tests.obs.fake_obs import FakeObs, Sleeps

FIXTURES = Path(__file__).parents[1] / "fixtures"
R1_CAPTURE = FIXTURES / "obs_provision" / "r1-trial2-provisioning.jsonl"
R2_PROVISION = FIXTURES / "obs_transcripts" / "provision.jsonl"

READ_PARAMETERS = frozenset(CONTAINER_KEYS + OFF_KEYS + (("Output", "Mode"), ("SimpleOutput", "RecQuality")))
"""The ``basic.ini`` keys the fake models that the recorded run reads; it models no other key's default."""

SETTINGS_EVENTS = ("InputCreated", "InputSettingsChanged")

SWITCH_EVENTS = (
    "CurrentProfileChanging",
    "CurrentProfileChanged",
    "CurrentSceneCollectionChanging",
    "CurrentSceneCollectionChanged",
)


def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def payloads(frames: list[dict[str, Any]], op: int) -> list[dict[str, Any]]:
    return [f["msg"]["d"] for f in frames if f.get("msg", {}).get("op") == op]


def recorded(frames: list[dict[str, Any]], request_type: str) -> dict[str, Any]:
    """The first response to ``request_type`` in ``frames``."""
    return next(d for d in payloads(frames, 7) if d["requestType"] == request_type)["responseData"]


def rig_kinds() -> list[str]:
    """Input kinds of the rig's OBS: both fixtures come from the same Flatpak OBS on the same host."""
    return recorded(load(R2_PROVISION), "GetInputKindList")["inputKinds"]


def view(request_type: str, data: dict[str, Any] | None) -> object:
    """What provisioning reads of a response: names as sets, UUIDs (new every run) masked."""
    data = data or {}
    if request_type == "GetProfileList":
        return data["currentProfileName"], sorted(data["profiles"])
    if request_type == "GetSceneCollectionList":
        return data["currentSceneCollectionName"], sorted(data["sceneCollections"])
    if request_type == "GetSceneList":
        return data["currentProgramSceneName"], sorted(scene["sceneName"] for scene in data["scenes"])
    return {key: "<uuid>" if key.endswith("Uuid") else value for key, value in data.items()}


def provisioning_sends(request_type: str, fields: dict[str, Any]) -> bool:
    if not hasattr(FakeObs, "_" + request_type):
        return False  # Not a request provisioning sends (FakeObs implements exactly those).
    if request_type == "GetProfileParameter":
        return (fields["parameterCategory"], fields["parameterName"]) in READ_PARAMETERS
    return True


async def replay(obs: FakeObs, frames: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Send the provisioning requests of ``frames`` to ``obs`` in order; return (checked, mismatches)."""
    responses = {d["requestId"]: d for d in payloads(frames, 7)}
    checked: list[str] = []
    mismatches: list[str] = []
    for frame in frames:
        msg = frame.get("msg", {})
        data = msg.get("d", {})
        if msg.get("op") == 5:
            await asyncio.sleep(0)
        if msg.get("op") == 5 and data["eventType"] in SETTINGS_EVENTS:
            event = data["eventData"]
            got = await obs.request("GetInputSettings", inputName=event["inputName"])
            want = {"inputKind": event.get("inputKind", got["inputKind"]), "inputSettings": event["inputSettings"]}
            checked.append(data["eventType"])
            if got != want:
                mismatches.append(f"{data['eventType']} {event['inputName']}: fake {got}, OBS {want}")
        if msg.get("op") != 6:
            continue
        request_type, fields = data["requestType"], data.get("requestData") or {}
        if not provisioning_sends(request_type, fields):
            continue
        response = responses[data["requestId"]]
        if request_type == "GetInputPropertiesListPropertyItems":
            # The windows on screen belong to the rig, not to OBS's state.
            kind = obs.collection.inputs[fields["inputName"]].kind
            obs.window_lists[kind] = response["responseData"]["propertyItems"]
        try:
            answer: dict[str, Any] | None = await obs.request(request_type, **fields)
            code = 100
        except ObsRequestError as exc:
            answer, code = None, exc.code
        checked.append(request_type)
        want_code = response["requestStatus"]["code"]
        if code != want_code:
            mismatches.append(f"{request_type} {fields}: fake code {code}, OBS {want_code}")
        elif code == 100 and view(request_type, answer) != view(request_type, response.get("responseData")):
            mismatches.append(f"{request_type} {fields}: fake {answer}, OBS {response.get('responseData')}")
    want_events = [(d["eventType"], d["eventData"]) for d in payloads(frames, 5) if d["eventType"] in SWITCH_EVENTS]
    got_events = [(name, data) for _, name, data in obs.events if name in SWITCH_EVENTS]
    if got_events != want_events:
        mismatches.append(f"switch events: fake {got_events}, OBS {want_events}")
    return checked, mismatches


def r2_rig() -> FakeObs:
    base = recorded(load(R2_PROVISION), "GetVideoSettings")
    return FakeObs(input_kinds=rig_kinds(), base_size=(base["baseWidth"], base["baseHeight"]))


async def test_fake_obs_answers_the_r2_provisioning_run_as_obs_did():
    obs = r2_rig()

    checked, mismatches = await replay(obs, load(R2_PROVISION))

    assert mismatches == []
    assert set(checked) == {
        "GetProfileList",
        "GetSceneCollectionList",
        "CreateProfile",
        "SetRecordDirectory",
        "GetRecordDirectory",
        "GetVideoSettings",
        "SetVideoSettings",
        "SetProfileParameter",
        "GetProfileParameter",
        "CreateSceneCollection",
        "CreateScene",
        "GetSceneList",
        "GetInputKindList",
        "CreateInput",
        "InputCreated",
        "GetInputPropertiesListPropertyItems",
        "SetInputSettings",
        "GetInputSettings",
        "GetSpecialInputs",
        "InputSettingsChanged",
    }
    assert checked.count("GetProfileParameter") == len(READ_PARAMETERS)
    assert [name for _, name, _ in obs.events if name in SWITCH_EVENTS] == [
        "CurrentProfileChanged",
        "CurrentSceneCollectionChanging",
        "CurrentSceneCollectionChanged",
    ]


async def test_fake_obs_answers_the_r1_capture_setup_as_obs_did():
    obs = FakeObs(input_kinds=rig_kinds())

    checked, mismatches = await replay(obs, load(R1_CAPTURE))

    assert mismatches == []
    assert checked == [
        "SetVideoSettings",
        "GetVideoSettings",
        "CreateInput",
        "InputCreated",
        "GetInputPropertiesListPropertyItems",
        "SetInputSettings",
        "InputSettingsChanged",
    ]


async def test_the_profile_r2_wrote_on_a_real_obs_needs_the_app_record_directory_and_its_own_encoder(tmp_path):
    # R2 wrote the rows provisioning writes, at the default 720p30, but left the recording sharing
    # the stream encoder (it read back RecQuality=Stream); the output root differs.
    obs = r2_rig()
    await replay(obs, load(R2_PROVISION))
    obs.reset_calls()
    provisioner = ObsProvisioner(obs, platform="linux", sleep=Sleeps())

    result = await provisioner.ensure_profile(
        AppConfig(output_root=str(tmp_path), recording=RecordingSettings(720, 30))
    )

    # Started on the app's profile: nowhere to switch to for the re-activation.
    assert result == ProvisionResult(changed=True, needs_restart=True)
    assert obs.mutating() == ["SetRecordDirectory", "SetProfileParameter", "SetProfileParameter"]
    assert obs.recording_pausable is False


async def test_the_collection_r2_made_on_a_real_obs_gets_game_as_program_scene_and_the_app_inputs():
    """R2 item 13: ``CreateScene Game`` left OBS's ``Scene`` as the program scene (``provision.jsonl``)."""
    frames = load(R2_PROVISION)
    obs = r2_rig()
    await replay(obs, frames)
    obs.reset_calls()
    assert obs.collection.program_scene == "Scene"
    window = recorded(frames, "GetInputSettings")["inputSettings"]["capture_window"]  # the probe window
    provisioner = ObsProvisioner(obs, platform="linux", sleep=Sleeps())

    await provisioner.ensure_collection(
        GameProfile(slug="probe", title="Probe", capture=CaptureSettings(window=window))
    )

    assert ("SetCurrentProgramScene", {"sceneName": OBS_SCENE_NAME}) in obs.calls
    assert obs.collection.program_scene == OBS_SCENE_NAME
    # R2's driver named its inputs differently; they are not the app's and stay as they were.
    assert obs.scene_items() == ["Game capture", "Game audio", XCOMPOSITE_INPUT, DESKTOP_AUDIO_INPUT]
    assert obs.collection.inputs[XCOMPOSITE_INPUT].settings["capture_window"] == window
