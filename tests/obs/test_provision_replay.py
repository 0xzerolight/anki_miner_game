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

- ``tests/fixtures/obs_transcripts/app_provision.jsonl`` (E1-PROVISION-REPLAY, ``docs/m0/m1-exit-linux.md``):
  the app's own three arms on the same OBS, recorded through ``tools/obs_transcript_recorder.py``.
  Arm 1 on an OBS that has never seen the app, its user profile at 44.1 kHz: the audio copy, the
  profile re-activation, ``SetCurrentProgramScene``, ``GetProfileParameter`` on a fresh profile.
  Arm 2 after a direct client replaced the app's X11 input with an ``xshm_input_v2`` of that name
  and a muted special input was added to the collection: ``RemoveInput``, the wait for OBS to free
  the name, ``CreateInput``, ``GetInputMute``. Arm 3 after the special input was unmuted:
  ``SetInputMute``. These frames are replayed request by request against ``ObsProvisioner`` itself
  (``TranscriptGateway``), and arm 1 also against ``FakeObs``.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from anki_miner_game.models.config import AppConfig, RecordingSettings
from anki_miner_game.models.constants import OBS_SCENE_NAME
from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsInfo, ObsRequestError, ProvisionResult
from anki_miner_game.models.profile import CaptureKind, CaptureSettings, GameProfile
from anki_miner_game.obs.provision import (
    CONTAINER_KEYS,
    DESKTOP_AUDIO_INPUT,
    INPUT_RELEASE_TIMEOUT_S,
    OFF_KEYS,
    XCOMPOSITE_INPUT,
    ObsProvisioner,
)
from tests.obs.fake_obs import FakeObs, Sleeps

FIXTURES = Path(__file__).parents[1] / "fixtures"
R1_CAPTURE = FIXTURES / "obs_provision" / "r1-trial2-provisioning.jsonl"
R2_PROVISION = FIXTURES / "obs_transcripts" / "provision.jsonl"
APP_PROVISION = FIXTURES / "obs_transcripts" / "app_provision.jsonl"

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


# --- the app's own provisioning (E1-PROVISION-REPLAY) ------------------------------------------------

APP_CONFIG = AppConfig(output_root="/home/user/Videos/Anki Miner Game")
"""The recorded run's settings: defaults, its output root rewritten as in every fixture."""
APP_PROFILE = GameProfile(
    slug="e1-provision", title="Provision Probe", capture=CaptureSettings(kind=CaptureKind.XCOMPOSITE)
)
"""The game profile the recorded arms used: an X11 capture with no window pinned."""
TO_OBS, FROM_OBS = "client->obs", "obs->client"
NOT_READY = 207


def app_requests(records: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """``(conn, request)`` of every request the app sent, in order."""
    return [(r["conn"], r["msg"]["d"]) for r in records if r.get("dir") == TO_OBS and r.get("msg", {}).get("op") == 6]


def provisioning_starts(records: list[dict[str, Any]]) -> list[int]:
    """Index in ``app_requests`` of each arm's first provisioning request.

    An arm of the app that recorded the transcript read ``GetProfileList`` and
    ``GetSceneCollectionList``, switched to the app's profile and collection where they existed, and
    then provisioning began with ``ensure_profile``'s own ``GetProfileList``. (The actor now leaves
    the profile switch to ``ensure_profile``; the recorded arms are replayed as they happened.)
    """
    types = [request["requestType"] for _, request in app_requests(records)]
    starts = []
    for i in range(len(types) - 1):
        if types[i : i + 2] != ["GetProfileList", "GetSceneCollectionList"]:
            continue
        j = i + 2
        while types[j] in ("SetCurrentProfile", "SetCurrentSceneCollection"):
            j += 1
        if types[j] == "GetProfileList":
            starts.append(j)
    return starts


def exchanges(records: list[dict[str, Any]], conn: int) -> list[tuple[float, dict[str, Any], dict[str, Any]]]:
    """``(t_sent, request, answer)`` on one connection. obsws-python sends one request at a time and
    its request ids repeat, so each answer is the frame after its request."""
    frames = [r for r in records if r.get("conn") == conn and r.get("msg", {}).get("op") in (6, 7)]
    return [(q["t_mono"], q["msg"]["d"], a["msg"]["d"]) for q, a in zip(frames[::2], frames[1::2], strict=True)]


class TranscriptGateway:
    """An ``ObsGateway`` answering from the frames the app exchanged with a real OBS.

    From ``start`` on, every request must be the next one the app sent on that connection, with the
    same fields, and gets OBS's recorded answer (``ObsRequestError`` with OBS's code for a failure).
    A request OBS answered 207 was retried by the gateway (``ObsClient``) and is skipped. After each
    answer, the events OBS sent before the app's next request reach the handlers on the loop, as
    they reached the app: a switch's ``...Changed`` event may come before or after its answer.
    """

    def __init__(self, records: list[dict[str, Any]], start: int) -> None:
        requests = app_requests(records)
        self.conn = requests[start][0]
        self._sent = exchanges(records, self.conn)
        self._events = [
            (r["t_mono"], r["msg"]["d"])
            for r in records
            if r.get("dir") == FROM_OBS and r.get("msg", {}).get("op") == 5
        ]
        self._next = sum(1 for conn, _ in requests[:start] if conn == self.conn)
        self._handlers: list[Callable[[ObsEvent], None]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> ObsInfo:
        raise NotImplementedError

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None:
        self._handlers.append(handler)

    @property
    def collection_changing(self) -> bool:
        return False

    async def close(self) -> None:
        pass

    def next_request(self) -> str | None:
        return self._sent[self._next][1]["requestType"] if self._next < len(self._sent) else None

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        while self._sent[self._next][2]["requestStatus"]["code"] == NOT_READY:
            self._next += 1
        t_sent, recorded, answer = self._sent[self._next]
        assert (name, fields) == (recorded["requestType"], recorded.get("requestData") or {})
        self._next += 1
        self.sent.append((name, fields))
        t_next = self._sent[self._next][0] if self._next < len(self._sent) else float("inf")
        loop = asyncio.get_running_loop()
        for t, event in self._events:
            if t_sent < t <= t_next:
                obs_event = ObsEvent(event["eventType"], event.get("eventData") or {}, t)
                for handler in self._handlers:
                    loop.call_soon(handler, obs_event)
        status = answer["requestStatus"]
        if status["code"] != 100:
            raise ObsRequestError(name, status["code"], status.get("comment", ""))
        return dict(answer.get("responseData") or {})

    def mutating(self) -> list[str]:
        return [name for name, _ in self.sent if not name.startswith("Get")]


async def provision_arm(records: list[dict[str, Any]], arm: int) -> tuple[TranscriptGateway, list[ProvisionResult]]:
    gateway = TranscriptGateway(records, provisioning_starts(records)[arm])
    provisioner = ObsProvisioner(gateway, platform="linux", sleep=Sleeps())
    results = [await provisioner.ensure_profile(APP_CONFIG), await provisioner.ensure_collection(APP_PROFILE)]
    return gateway, results


def test_the_app_transcript_holds_three_arms():
    assert len(provisioning_starts(load(APP_PROVISION))) == 3


@pytest.mark.parametrize("arm", [0, 1, 2])
async def test_the_provisioner_sends_exactly_what_the_app_sent_to_a_real_obs(arm):
    gateway, _ = await provision_arm(load(APP_PROVISION), arm)

    # Provisioning ends where the recorded arm's did: next came the reconcile of the app's fresh
    # connection (arms 1 and 2, started with --arm) or the quit's output check (arm 3).
    assert gateway.next_request() in {"GetVersion", "GetStreamStatus"}


async def test_the_first_arm_copies_the_audio_rate_and_reactivates_the_profile():
    gateway, results = await provision_arm(load(APP_PROVISION), 0)

    assert results == [ProvisionResult(changed=True, needs_restart=False), ProvisionResult(True, False)]
    assert (
        "SetProfileParameter",
        {"parameterCategory": "Audio", "parameterName": "SampleRate", "parameterValue": "44100"},
    ) in gateway.sent
    switches = [(n, f) for n, f in gateway.sent if n == "SetCurrentProfile"]
    assert switches == [
        ("SetCurrentProfile", {"profileName": "Untitled"}),
        ("SetCurrentProfile", {"profileName": "Anki Miner Game"}),
    ]
    assert ("SetCurrentProgramScene", {"sceneName": OBS_SCENE_NAME}) in gateway.sent


async def test_the_second_arm_only_removes_and_recreates_the_replaced_input():
    gateway, results = await provision_arm(load(APP_PROVISION), 1)

    assert gateway.mutating() == ["RemoveInput", "CreateInput"]
    assert [f["inputName"] for n, f in gateway.sent if n in ("RemoveInput", "CreateInput")] == [XCOMPOSITE_INPUT] * 2
    assert ("GetInputMute", {"inputName": "Desktop Audio"}) in gateway.sent  # the special input, muted already
    assert results == [ProvisionResult(changed=False, needs_restart=False), ProvisionResult(True, False)]


async def test_the_third_arm_only_mutes_the_special_input():
    gateway, results = await provision_arm(load(APP_PROVISION), 2)

    assert gateway.sent[-1] == ("SetInputMute", {"inputName": "Desktop Audio", "inputMuted": True})
    assert gateway.mutating() == ["SetInputMute"]
    assert results == [ProvisionResult(changed=False, needs_restart=False), ProvisionResult(True, False)]


def test_obs_freed_the_removed_name_inside_the_release_timeout():
    records = load(APP_PROVISION)
    (conn,) = {conn for conn, request in app_requests(records) if request["requestType"] == "RemoveInput"}
    sent = exchanges(records, conn)
    i = next(i for i, (_, request, _) in enumerate(sent) if request["requestType"] == "RemoveInput")
    created = next(j for j in range(i, len(sent)) if sent[j][1]["requestType"] == "CreateInput")
    checks = [
        (t, answer["requestStatus"]["code"])
        for t, request, answer in sent[i:created]
        if request.get("requestData") == {"inputName": XCOMPOSITE_INPUT}
        and request["requestType"] == "GetInputSettings"
    ]

    assert [code for _, code in checks] == [100, 600]  # held at the first check, free at the next
    assert checks[-1][0] - sent[i][0] < INPUT_RELEASE_TIMEOUT_S


async def test_fake_obs_answers_the_apps_first_arm_as_obs_did():
    records = load(APP_PROVISION)
    first_close = next(i for i, r in enumerate(records) if r.get("event") == "close")
    obs = r2_rig()
    obs.profiles["Untitled"][("Audio", "SampleRate")] = "44100"  # seeded before the run (README)

    checked, mismatches = await replay(obs, records[:first_close])

    assert mismatches == []
    assert {"CreateProfile", "SetCurrentProfile", "SetCurrentProgramScene", "CreateInput"} <= set(checked)
