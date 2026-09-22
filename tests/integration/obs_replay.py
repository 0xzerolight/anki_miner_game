"""An OBS for the scripted sessions: T14's ``FakeObs`` behind ``FakeObsServer``, plus one R2 recording.

R2's driver provisioned the OBS in its transcripts itself (other input names, its own request order;
``tests/fixtures/obs_transcripts/README.md``), so a transcript cannot answer the app's provisioning
request by request. ``ReplayedObs`` is a ``FakeObsServer`` in live mode that answers:

- every request provisioning, arming and the restore send from T14's ``FakeObs`` (profiles, scene
  collections, inputs, window lists), whose switch events it forwards as OBS sends them;
- the recording's own frames from the transcript (``Recording``): the answers to the recording,
  pause, split, output and switch requests of ``PLAYED`` that the app sends itself, and the output
  statuses (``STATUSES``) as OBS reported them at that moment of the recording (``ReplayedObs.status``).

What the transcript's other clients did (a pause made in OBS, a split, the user's own recording, a
profile switched away and back) and what OBS did on its own (events, an exit, a crash, a dropped
connection) are steps the harness plays in time order (``tests/integration/scripted.py``).
"""

import asyncio
import collections
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from anki_miner_game.models.obs import ObsConnectError, ObsRequestError
from tests.fakes.fake_obs_server import (
    OP_IDENTIFY,
    OP_RESPONSE,
    FakeObsServer,
    RecordedRequest,
    Reply,
    _Client,
    _response,
    version_data,
)
from tests.obs.fake_obs import FakeObs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "obs_transcripts"

APP_INCOMING = "/home/user/Videos/Anki Miner Game/_incoming"
"""The run's app record folder, as the README says every transcript names it."""
USER_VIDEOS = "/home/user/Videos"
"""The user profile's record folder in the transcripts."""

RECORD_REQUESTS = frozenset({"StartRecord", "StopRecord", "PauseRecord", "ResumeRecord", "SplitRecordFile"})
OUTPUT_REQUESTS = frozenset({"StartStream", "StopStream", "StartReplayBuffer", "StopReplayBuffer"})
SWITCH_REQUESTS = frozenset({"SetCurrentProfile", "SetCurrentSceneCollection"})
PLAYED = RECORD_REQUESTS | OUTPUT_REQUESTS | SWITCH_REQUESTS
"""Requests whose recorded answers and following events are the recording's."""

SWITCH_EVENTS = frozenset(
    {
        "CurrentProfileChanging",
        "CurrentProfileChanged",
        "CurrentSceneCollectionChanging",
        "CurrentSceneCollectionChanged",
    }
)
STATE_EVENT = {
    "GetRecordStatus": "RecordStateChanged",
    "GetStreamStatus": "StreamStateChanged",
    "GetReplayBufferStatus": "ReplayBufferStateChanged",
    "GetVirtualCamStatus": "VirtualcamStateChanged",
}
"""Each output status request and the event that changes what it answers."""
STATUSES = frozenset({*STATE_EVENT, "GetOutputSettings"})
WINDOW_LIST = "GetInputPropertiesListPropertyItems"

NOT_AVAILABLE = 604
RESOURCE_NOT_FOUND = 600
UNKNOWN_REQUEST = 204
NOT_READY = 207
STATE_PREFIX = "OBS_WEBSOCKET_OUTPUT_"


@dataclass(frozen=True)
class Step:
    """One thing the recording holds, at ``t`` seconds after its first record (to the millisecond)."""

    index: int
    """Position in the transcript: steps and status answers sort by it."""
    t: float
    kind: Literal["request", "response", "event", "drop", "reopen", "obs_closed", "windows"]
    name: str = ""
    """Request or event type."""
    data: Mapping[str, Any] = field(default_factory=dict)
    """``requestData``, a response's ``d``, ``eventData``, or a window list's ``propertyItems`` (under ``items``)."""
    request: int | None = None
    """A response: the ``index`` of its request."""
    intent: int = 0
    code: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class Status:
    index: int
    t: float
    name: str
    data: Mapping[str, Any]
    """``requestData`` (``outputName`` for ``GetOutputSettings``)."""
    response: Mapping[str, Any]
    """The response's ``d``."""


@dataclass(frozen=True)
class Recording:
    """What ``load`` keeps of a transcript: the played steps and every output status OBS answered."""

    name: str
    steps: tuple[Step, ...]
    statuses: tuple[Status, ...]
    version: Mapping[str, Any] | None

    def asked(self, request_type: str) -> float:
        """When OBS first answered ``request_type``: where an arm of the recorded client began."""
        return next(status.t for status in self.statuses if status.name == request_type)


def load(name: str) -> Recording:
    """Read ``tests/fixtures/obs_transcripts/<name>`` (``tools/obs_transcript_recorder.py`` records).

    - Requests and answers of every client count, the recorded app's own and the others' (a third
      client's switch in ``switch_restart_prompt``); an answer pairs with the oldest unanswered
      request on its connection, never by id (obsws-python's ids repeat).
    - Event connections are the ones whose Identify subscribed to events. When one closes and a later
      one opens, the recorded client lost its connection and came back: ``drop`` then ``reopen``.
      The last one closed by OBS is ``obs_closed``; closed by the recorded client, the recording ended.
    - A 207 answer is not a status: OBS was not ready to give one.
    """
    records = [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line]
    t0 = records[0]["t_mono"]
    events_conns = [
        r["conn"]
        for r in records
        if r.get("msg", {}).get("op") == OP_IDENTIFY and r["msg"]["d"].get("eventSubscriptions")
    ]
    steps: list[Step] = []
    statuses: list[Status] = []
    version: Mapping[str, Any] | None = None
    pending: dict[int, collections.deque[tuple[int, dict[str, Any]]]] = collections.defaultdict(collections.deque)
    for index, record in enumerate(records):
        t = round(record["t_mono"] - t0, 3)
        conn = record["conn"]
        msg = record.get("msg")
        if record.get("event") == "open" and conn in events_conns[1:]:
            steps.append(Step(index, t, "reopen"))
        elif record.get("event") == "close" and conn in events_conns:
            if conn != events_conns[-1]:
                steps.append(Step(index, t, "drop"))
            elif record["by"] == "obs":
                steps.append(Step(index, t, "obs_closed", code=record["code"], reason=record["reason"] or ""))
        elif msg is None:
            continue
        elif msg["op"] == 6:
            pending[conn].append((index, msg["d"]))
            if msg["d"]["requestType"] in PLAYED:
                data = dict(msg["d"].get("requestData") or {})
                steps.append(Step(index, t, "request", msg["d"]["requestType"], data))
        elif msg["op"] == OP_RESPONSE:
            request_index, request = pending[conn].popleft()
            request_type = request["requestType"]
            if request_type in PLAYED:
                steps.append(Step(index, t, "response", request_type, msg["d"], request=request_index))
            elif request_type in STATUSES and msg["d"]["requestStatus"]["code"] != NOT_READY:
                data = dict(request.get("requestData") or {})
                statuses.append(Status(index, t, request_type, data, msg["d"]))
            elif request_type == WINDOW_LIST:
                steps.append(Step(index, t, "windows", data={"items": msg["d"]["responseData"]["propertyItems"]}))
            elif request_type == "GetVersion" and version is None and msg["d"]["requestStatus"]["result"]:
                version = msg["d"]["responseData"]
        elif msg["op"] == 5:
            d = msg["d"]
            steps.append(Step(index, t, "event", d["eventType"], d.get("eventData") or {}, intent=d["eventIntent"]))
    return Recording(name.removesuffix(".jsonl"), tuple(steps), tuple(statuses), version)


def rewrite_paths(value: Any, incoming: Path, videos: Path) -> Any:
    """``value`` with the recorded record folders replaced by this run's (``APP_INCOMING``, ``USER_VIDEOS``)."""
    if isinstance(value, dict):
        return {key: rewrite_paths(item, incoming, videos) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, incoming, videos) for item in value]
    if isinstance(value, str):
        for recorded, here in ((APP_INCOMING, incoming), (USER_VIDEOS, videos)):
            if value == recorded or value.startswith(recorded + "/"):
                return str(here.joinpath(*PurePosixPath(value[len(recorded) :].lstrip("/")).parts))
    return value


def _state(event_data: Mapping[str, Any]) -> str:
    return str(event_data.get("outputState", "")).removeprefix(STATE_PREFIX)


@dataclass
class _Bound:
    """An app request of a played type, waiting for the step that answers it."""

    client: _Client
    request_id: Any


class ReplayedObs(FakeObsServer):
    """``FakeObsServer`` in live mode answering from ``obs`` (T14's ``FakeObs``) and from ``recording``.

    ``app_sends`` names the played requests the app itself sends in the scenario; the harness answers
    each with its recorded answer at the recorded moment (``answer``). A played request the app sends
    that the recording does not hold is noted in ``unscripted`` and refused with 204. ``now`` gives the
    moment of the recording (seconds after its first record) for ``status``.
    """

    def __init__(
        self,
        obs: FakeObs,
        recording: Recording,
        *,
        app_sends: frozenset[str],
        now: Callable[[], float],
        incoming: Path,
        videos: Path,
    ) -> None:
        super().__init__(version=recording.version or version_data())
        self.obs = obs
        self.recording = recording
        self.app_sends = app_sends
        self._now = now
        self._incoming = incoming
        self._videos = videos
        self._bound: dict[int, _Bound] = {}
        """Request step index -> the app's request it answers."""
        self._arrived: dict[int, asyncio.Event] = collections.defaultdict(asyncio.Event)
        self.played_events: list[Step] = []
        """Events and reopens the harness has played, in order."""
        self._collecting: list[tuple[str, dict[str, Any]]] | None = None
        self.muted = False
        """While set, ``FakeObs``'s own events are not forwarded (an outside switch plays the recorded ones)."""
        obs.subscribe(self._forward)

    # --- routing ----------------------------------------------------------------------------------

    async def _request(self, client: _Client, d: dict[str, Any]) -> None:
        request_type, request_id = d["requestType"], d["requestId"]
        data = dict(d.get("requestData") or {})
        self.requests.append(RecordedRequest(client.index, request_type, data))
        if request_type == "GetVersion":
            await self._reply(client, request_type, request_id, Reply(self.version))
        elif request_type in self.app_sends:
            await self._bind(client, request_type, request_id, data)
        elif request_type in STATUSES:
            await self._reply(client, request_type, request_id, self.status(request_type, data))
        else:
            await self._from_fake_obs(client, request_type, request_id, data)

    async def _bind(self, client: _Client, request_type: str, request_id: Any, data: dict[str, Any]) -> None:
        """Hold the app's request for the recorded answer; a switch is made in ``FakeObs`` at once, as OBS makes it."""
        step = next(
            (
                step
                for step in self.recording.steps
                if (step.kind, step.name, dict(step.data)) == ("request", request_type, data)
                and step.index not in self._bound
            ),
            None,
        )
        if step is None:
            self.unscripted.append(f"{self.recording.name}: the app sent {request_type} {data}, which it does not hold")
            await self._reply(client, request_type, request_id, Reply(code=UNKNOWN_REQUEST))
            return
        self._bound[step.index] = _Bound(client, request_id)
        if request_type in SWITCH_REQUESTS:
            await self.switch_quietly(request_type, data)
        self._arrived[step.index].set()

    async def switch_quietly(self, request_type: str, data: dict[str, Any]) -> None:
        """Make a switch in ``FakeObs`` without sending its events: the recording's own are played instead."""
        self.muted = True
        try:
            await self.obs.request(request_type, **data)
        except ObsRequestError:  # refused (an unknown name): nothing changes
            pass
        finally:
            self.muted = False

    async def arrived(self, step: Step) -> None:
        """Wait until the app has sent the request ``step`` recorded."""
        await self._arrived[step.index].wait()

    async def answer(self, step: Step) -> None:
        """Send the recorded answer ``step`` to the app's request it belongs to."""
        assert step.request is not None
        bound = self._bound[step.request]
        response = self.rewrite({**step.data, "requestId": bound.request_id})
        await self._send(bound.client, OP_RESPONSE, response)

    async def _from_fake_obs(self, client: _Client, request_type: str, request_id: Any, data: dict[str, Any]) -> None:
        self._collecting = []
        try:
            reply = Reply(await self.obs.request(request_type, **data))
        except ObsRequestError as exc:
            reply = Reply(code=exc.code, comment=exc.comment or None)
        except ObsConnectError:  # FakeObs crashed, as OBS aborts on an empty xcomposite window list
            self._collecting = None
            await self.drop_clients(None)
            return
        except AssertionError as exc:  # a request FakeObs does not model
            self.unscripted.append(f"{self.recording.name}: {exc}")
            reply = Reply(code=UNKNOWN_REQUEST)
        events, self._collecting = self._collecting, None
        for name, event_data in events:  # before the answer, where FakeObs sends them
            await self.emit(name, event_data)
        await self._reply(client, request_type, request_id, reply)

    def _forward(self, event: Any) -> None:
        """``FakeObs``'s event handler, on this server's loop."""
        if self.muted:
            return
        if self._collecting is not None:
            self._collecting.append((event.name, dict(event.data)))
        else:  # sent after an answer (``CreateProfile``'s switch)
            asyncio.ensure_future(self.emit(event.name, dict(event.data)))

    async def _reply(self, client: _Client, request_type: str, request_id: Any, reply: Reply) -> None:
        await self._send(client, OP_RESPONSE, _response(request_type, request_id, reply))

    def rewrite(self, value: Any) -> Any:
        return rewrite_paths(value, self._incoming, self._videos)

    # --- output statuses --------------------------------------------------------------------------

    def status(self, request_type: str, data: Mapping[str, Any]) -> Reply:
        """What OBS answered ``request_type`` at this moment of the recording.

        - The latest answer recorded since the last event that changed that output (or since the
          last reconnect), up to now.
        - Right after a reconnect with none yet: the first answer after it. OBS changed while the
          recorded client was away (``missed_pause``), and that answer is all that says how.
        - Right after an event with none yet: the state the event set. A recording at ``STARTED``
          reports 0 ms (every transcript that asked says so); a pause edge keeps the last duration;
          ``STOPPING`` still reads active (R2 item 9).
        - Before any event: the latest answer up to now, else the first one recorded before the
          output's first event, else OBS's answer on this host with nothing configured (inactive; the
          replay buffer and the virtual camera 604, R2 item 6).

        ``GetOutputSettings`` answers the latest recorded answer for that output up to now, else the
        first, else 600, as OBS answers for the output of the other ``[Output] Mode`` (R2 item 8).
        """
        now = self._now()
        answers = [s for s in self.recording.statuses if s.name == request_type and dict(s.data) == dict(data)]
        if request_type == "GetOutputSettings":
            chosen = ([s for s in answers if s.t <= now] or answers[:1] or [None])[-1]
            if chosen is None:
                return Reply(code=RESOURCE_NOT_FOUND, comment=f"No output was found by the name of `{data}`.")
            return self._recorded(chosen)
        event_name = STATE_EVENT[request_type]
        marks = [s for s in self.played_events if s.kind == "reopen" or s.name == event_name]
        if not marks:
            before = [s for s in answers if s.t <= now]
            if before:
                return self._recorded(before[-1])
            first_event = next((step.index for step in self.recording.steps if step.name == event_name), None)
            early = [s for s in answers if first_event is None or s.index < first_event]
            return self._recorded(early[0]) if early else _idle(request_type)
        mark = marks[-1]
        since = [s for s in answers if s.index > mark.index]
        up_to_now = [s for s in since if s.t <= now]
        if up_to_now:
            return self._recorded(up_to_now[-1])
        if mark.kind == "reopen" and since:
            return self._recorded(since[0])
        events = [s for s in self.played_events if s.name == event_name]
        if not events:
            return _idle(request_type)
        last = events[-1]
        before = [s for s in answers if s.index < last.index]
        return Reply(_derived(request_type, _state(last.data), before[-1].response if before else None))

    def _recorded(self, status: Status) -> Reply:
        d = self.rewrite(dict(status.response))
        return Reply(d.get("responseData"), d["requestStatus"]["code"], d["requestStatus"].get("comment"))


def _idle(request_type: str) -> Reply:
    if request_type in ("GetReplayBufferStatus", "GetVirtualCamStatus"):
        what = "Replay buffer" if request_type == "GetReplayBufferStatus" else "VirtualCam"
        return Reply(code=NOT_AVAILABLE, comment=f"{what} is not available.")
    return Reply(_derived(request_type, "STOPPED", None))


def _derived(request_type: str, state: str, last: Mapping[str, Any] | None) -> dict[str, Any]:
    active = state in ("STARTED", "PAUSED", "RESUMED", "STOPPING")
    if request_type != "GetRecordStatus":
        return {"outputActive": active}
    last_data = (last or {}).get("responseData") or {}
    duration = int(last_data.get("outputDuration", 0)) if state in ("PAUSED", "RESUMED", "STOPPING") else 0
    return {"outputActive": active, "outputPaused": state == "PAUSED", "outputDuration": duration}
