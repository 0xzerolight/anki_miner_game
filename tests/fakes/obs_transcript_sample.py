"""A hand-written transcript in ``tools/obs_transcript_recorder.py``'s format, for FakeObsServer's own tests.

Synthetic: every record says so in its ``synthetic`` field. The recorded R2 sessions in
``tests/fixtures/obs_transcripts/`` are the real thing; this one exists so that the loader and the
replay rules are pinned by a file whose every line is known. It holds, in order: a request client
(conn 1) and an event client (conn 2); an event right after the event client identifies (no R2
recording has one; the gateway holds such an event until ``_Connected``); a ``GetVersion``
exchange; a recording start with an Inputs event among the Outputs events; a second request client
(conn 3) beside the first, whose request OBS never answers (another client's, as in
``switch_restart_prompt``); a scene collection switch; failed requests with and without a comment;
the recorded client dropping both connections and reconnecting (conns 4 and 5); ``ExitStarted``
without ``eventData``; OBS closing both connections; a refused reconnect (conn 6).

Request ids repeat, as obsws-python's random ids do (``randint(1, 1000)``; ``switch_streaming`` has
one id twice on its request connection, ``switch_not_ready`` 36 on its polling client): conn 3's
unanswered request has the id of conn 1's pending switch, and the two failed requests share one
id. Answers pair with requests by order on each connection, never by id.
"""

import json
from collections.abc import Iterable
from itertools import count
from pathlib import Path
from typing import Any

from anki_miner_game.models.obs import REQUIRED_REQUESTS

LABEL = "hand-written for tests/fakes/test_fake_obs_server.py"
UPSTREAM = "ws://127.0.0.1:4455"
SAMPLE_VERSION: dict[str, Any] = {
    "obsVersion": "32.2.2",
    "obsWebSocketVersion": "5.7.4",
    "rpcVersion": 1,
    "availableRequests": list(REQUIRED_REQUESTS),
    "platform": "linux",
}
RECORDING_PATH = "/videos/_incoming/2026-09-21 18-40-39.mkv"
REPEATED_ID = 913
_ids = count(100)


def _open(conn: int, subs: int) -> list[dict[str, Any]]:
    return [
        {"conn": conn, "event": "open", "subprotocol": None, "upstream": UPSTREAM},
        {
            "conn": conn,
            "dir": "obs->client",
            "msg": {
                "op": 0,
                "d": {
                    "authentication": {"challenge": "<redacted>", "salt": "<redacted>"},
                    "obsStudioVersion": "32.2.2",
                    "obsWebSocketVersion": "5.7.4",
                    "rpcVersion": 1,
                },
            },
        },
        {
            "conn": conn,
            "dir": "client->obs",
            "msg": {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": subs, "authentication": "<redacted>"}},
        },
        {"conn": conn, "dir": "obs->client", "msg": {"op": 2, "d": {"negotiatedRpcVersion": 1}}},
    ]


def _exchange(
    conn: int,
    request_type: str,
    request_data: dict[str, Any] | None = None,
    *,
    data: dict[str, Any] | None = None,
    code: int = 100,
    comment: str | None = None,
    request_id: int | None = None,
) -> list[dict[str, Any]]:
    request_id = next(_ids) if request_id is None else request_id
    request: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
    if request_data:
        request["requestData"] = request_data
    status: dict[str, Any] = {"code": code, "result": code == 100}
    if comment:
        status["comment"] = comment
    response: dict[str, Any] = {"requestId": request_id, "requestStatus": status, "requestType": request_type}
    if data is not None:
        response["responseData"] = data
    return [
        {"conn": conn, "dir": "client->obs", "msg": {"op": 6, "d": request}},
        {"conn": conn, "dir": "obs->client", "msg": {"op": 7, "d": response}},
    ]


def _event(conn: int, event_type: str, intent: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"eventIntent": intent, "eventType": event_type}
    if data is not None:
        d["eventData"] = data
    return {"conn": conn, "dir": "obs->client", "msg": {"op": 5, "d": d}}


def _close(conn: int, by: str, code: int, reason: str = "") -> dict[str, Any]:
    return {"conn": conn, "event": "close", "by": by, "code": code, "reason": reason}


def _record(state: str, active: bool, path: str | None) -> dict[str, Any]:
    return {"outputActive": active, "outputPath": path, "outputState": f"OBS_WEBSOCKET_OUTPUT_{state}"}


_SWITCH_REQUEST, _SWITCH_RESPONSE = _exchange(
    1, "SetCurrentSceneCollection", {"sceneCollectionName": "Anki Miner Game"}
)
_UNANSWERED_REQUEST, _ = _exchange(
    3, "SetCurrentProfile", {"profileName": "Untitled"}, request_id=_SWITCH_REQUEST["msg"]["d"]["requestId"]
)

SAMPLE_RECORDS: list[dict[str, Any]] = [
    *_open(1, 0),
    *_open(2, 2047),
    _event(2, "CurrentProfileChanged", 2, {"profileName": "Anki Miner Game"}),
    *_exchange(1, "GetVersion", data=SAMPLE_VERSION),
    *_exchange(1, "GetRecordStatus", data={"outputActive": False, "outputDuration": 0, "outputPaused": False}),
    *_exchange(1, "StartRecord"),
    _event(2, "RecordStateChanged", 64, _record("STARTING", False, None)),
    _event(2, "InputCreated", 8, {"inputName": "Game capture", "inputKind": "xcomposite_input"}),
    _event(2, "RecordStateChanged", 64, _record("STARTED", True, RECORDING_PATH)),
    *_open(3, 0),
    _UNANSWERED_REQUEST,
    _SWITCH_REQUEST,
    _event(2, "CurrentSceneCollectionChanging", 2, {"sceneCollectionName": "Untitled"}),
    _event(2, "CurrentSceneCollectionChanged", 2, {"sceneCollectionName": "Anki Miner Game"}),
    _SWITCH_RESPONSE,
    _close(3, "client", 1000),
    *_exchange(1, "GetReplayBufferStatus", code=604, comment="Replay buffer is not available.", request_id=REPEATED_ID),
    *_exchange(
        1, "CreateSceneCollection", {"sceneCollectionName": "Anki Miner Game"}, code=601, request_id=REPEATED_ID
    ),
    _close(1, "client", 1000),
    _close(2, "client", 1000),
    *_open(4, 0),
    *_open(5, 2047),
    *_exchange(4, "GetRecordStatus", data={"outputActive": True, "outputPaused": False, "outputDuration": 5000}),
    _event(5, "ExitStarted", 1),
    _close(4, "obs", 1001, "Server stopping."),
    _close(5, "obs", 1001, "Server stopping."),
    {"conn": 6, "event": "upstream_failed", "error": "ConnectionRefusedError: [Errno 111] Connect call failed"},
]


def write_transcript(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write ``records`` as recorder JSONL: ``t_mono`` increasing by 10 ms, each labelled synthetic."""
    lines = [
        json.dumps({"t_mono": 5000.0 + 0.01 * index, **record, "synthetic": LABEL}, ensure_ascii=False)
        for index, record in enumerate(records)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_sample_transcript(directory: Path) -> Path:
    return write_transcript(directory / "sample.jsonl", SAMPLE_RECORDS)
