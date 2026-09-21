"""Pins the obs-websocket transcripts recorded from a real OBS by the R2 spike (docs/m0/obs-behaviour.md).

T12's FakeObsServer replays these files, T14 replays the provisioning one and T25 runs one scripted
session per transcript. This module keeps the set honest: the files are exactly the ones the README
documents, every line is a record of ``tools/obs_transcript_recorder.py``, nothing secret or
host-identifying is left in them, requests pair with their responses, synthetic records are
labelled, and the behaviour each transcript was recorded for is actually in it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "obs_transcripts"
README = FIXTURES / "README.md"
REDACTED = "<redacted>"
TO_OBS = "client->obs"
FROM_OBS = "obs->client"
APP_DIR = "/home/user/Videos/Anki Miner Game/_incoming/"
USER_DIR = "/home/user/Videos/"
APP = "Anki Miner Game"

EXPECTED = (
    "arm_disarm.jsonl",
    "missed_pause.jsonl",
    "normal.jsonl",
    "obs_exit.jsonl",
    "obs_killed.jsonl",
    "obs_sigterm.jsonl",
    "pause_noop.jsonl",
    "pause_resume.jsonl",
    "provision.jsonl",
    "reconnect.jsonl",
    "settings_apply.jsonl",
    "split.jsonl",
    "split_off_runtime.jsonl",
    "start_failed_missing_dir.jsonl",
    "start_failed_unwritable.jsonl",
    "switch_not_ready.jsonl",
    "switch_recording.jsonl",
    "switch_refused.jsonl",
    "switch_replay_buffer.jsonl",
    "switch_restart_prompt.jsonl",
    "switch_restart_prompt_matched.jsonl",
    "switch_streaming.jsonl",
    "synthetic-arm-virtualcam-active.jsonl",
    "window_retitle.jsonl",
)

# OBS never answered a request here: a modal restart question held the answer back.
MAY_LEAVE_REQUESTS_UNANSWERED = frozenset({"switch_restart_prompt.jsonl"})


def _records(name: str) -> list[dict[str, Any]]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _messages(records: list[dict[str, Any]], direction: str | None = None, op: int | None = None) -> list[dict]:
    return [
        r
        for r in records
        if "msg" in r and (direction is None or r["dir"] == direction) and (op is None or r["msg"].get("op") == op)
    ]


def _timed(records: list[dict[str, Any]], direction: str, op: int, kind: str) -> list[tuple[float, dict[str, Any]]]:
    """``(t_mono, d)`` of every request (op 6), response (op 7) or event (op 5) of one type."""
    key = "eventType" if op == 5 else "requestType"
    return [(r["t_mono"], r["msg"]["d"]) for r in _messages(records, direction, op) if r["msg"]["d"][key] == kind]


def _requests(records: list[dict[str, Any]], request_type: str) -> list[dict[str, Any]]:
    return [d for _, d in _timed(records, TO_OBS, 6, request_type)]


def _responses(records: list[dict[str, Any]], request_type: str) -> list[dict[str, Any]]:
    return [d for _, d in _timed(records, FROM_OBS, 7, request_type)]


def _codes(records: list[dict[str, Any]], request_type: str) -> list[int]:
    return [d["requestStatus"]["code"] for d in _responses(records, request_type)]


def _events(records: list[dict[str, Any]], event_type: str | None = None) -> list[dict[str, Any]]:
    return [
        r["msg"]["d"]
        for r in _messages(records, FROM_OBS, 5)
        if event_type is None or r["msg"]["d"]["eventType"] == event_type
    ]


def _record_states(records: list[dict[str, Any]]) -> list[str]:
    return [
        e["eventData"]["outputState"].removeprefix("OBS_WEBSOCKET_OUTPUT_")
        for e in _events(records, "RecordStateChanged")
    ]


def _record_event(records: list[dict[str, Any]], state: str) -> tuple[float, dict[str, Any]]:
    (found,) = [
        (t, d["eventData"])
        for t, d in _timed(records, FROM_OBS, 5, "RecordStateChanged")
        if d["eventData"]["outputState"] == f"OBS_WEBSOCKET_OUTPUT_{state}"
    ]
    return found


def _closes(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {r["conn"]: r for r in records if r.get("event") == "close"}


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key, item
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


# --- the set --------------------------------------------------------------------------------------


def test_fixture_set_is_exactly_the_pinned_one():
    assert sorted(p.name for p in FIXTURES.glob("*.jsonl")) == sorted(EXPECTED)


def test_readme_documents_every_fixture_and_nothing_else():
    documented = set(re.findall(r"^\| `([\w-]+\.jsonl)` \|", README.read_text(encoding="utf-8"), flags=re.M))
    assert documented == set(EXPECTED)


# --- structure ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED)
def test_every_line_is_a_recorder_record_in_time_order(name):
    records = _records(name)
    assert records, name
    times = [r["t_mono"] for r in records]
    assert all(isinstance(t, float) for t in times)
    assert times == sorted(times)
    for r in records:
        assert isinstance(r["conn"], int)
        if "event" in r:
            assert r["event"] in {"open", "close", "upstream_failed"}
        else:
            assert r["dir"] in {TO_OBS, FROM_OBS}
            assert isinstance(r["msg"], dict), "text and binary frames never occur with obsws-python"


@pytest.mark.parametrize("name", EXPECTED)
def test_every_connection_opens_with_hello_and_identify(name):
    records = _records(name)
    by_conn: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_conn[r["conn"]].append(r)
    for conn, items in by_conn.items():
        if items[0].get("event") == "upstream_failed":
            assert len(items) == 1, (name, conn)
            continue
        assert items[0].get("event") == "open", (name, conn)
        frames = [r["msg"]["op"] for r in items if "msg" in r]
        assert frames[:3] == [0, 1, 2], (name, conn)


@pytest.mark.parametrize("name", EXPECTED)
def test_requests_and_responses_pair_up_per_connection(name):
    """obsws-python sends one request at a time per connection, so answers come back in order."""
    pending: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for r in _records(name):
        if "msg" not in r:
            continue
        op, d = r["msg"]["op"], r["msg"]["d"]
        if op == 6 and r["dir"] == TO_OBS:
            pending[r["conn"]].append((d["requestId"], d["requestType"]))
        elif op == 7 and r["dir"] == FROM_OBS:
            assert pending[r["conn"]], (name, r)
            assert pending[r["conn"]].pop(0) == (d["requestId"], d["requestType"]), (name, r)
    left = {conn: items for conn, items in pending.items() if items}
    if name in MAY_LEAVE_REQUESTS_UNANSWERED:
        assert left, name
    else:
        assert not left, (name, left)


# --- secrets and host identity ---------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED)
def test_authentication_is_redacted(name):
    records = _records(name)
    hellos = [r["msg"]["d"] for r in _messages(records, FROM_OBS, 0)]
    identifies = [r["msg"]["d"] for r in _messages(records, TO_OBS, 1)]
    assert hellos and len(hellos) == len(identifies)
    for hello in hellos:
        assert hello["authentication"] == {"challenge": REDACTED, "salt": REDACTED}
    for identify in identifies:
        assert identify["authentication"] == REDACTED
    for r in records:
        for where, key, value in _walk(r):
            if "password" in key.lower() or key == "authentication":
                assert value == REDACTED or (isinstance(value, dict) and set(value.values()) == {REDACTED}), where


@pytest.mark.parametrize("name", EXPECTED)
def test_the_owners_home_is_rewritten(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    assert "/home/light" not in text
    assert "\r\n" not in text.replace("\\r\\n", "")  # xcomposite values keep their escaped \r\n


@pytest.mark.parametrize("name", EXPECTED)
def test_synthetic_records_are_labelled_and_only_in_synthetic_files(name):
    labelled = [r for r in _records(name) if "synthetic" in r]
    if name.startswith("synthetic-"):
        assert labelled and all(isinstance(r["synthetic"], str) and r["synthetic"] for r in labelled)
    else:
        assert not labelled


# --- recording ------------------------------------------------------------------------------------


def test_normal_session_names_one_file_from_started_to_stopped():
    records = _records("normal.jsonl")
    assert _record_states(records) == ["STARTING", "STARTED", "STOPPING", "STOPPED"]
    _, started = _record_event(records, "STARTED")
    t_stopped, stopped = _record_event(records, "STOPPED")
    path = started["outputPath"]
    assert path.startswith(APP_DIR) and path.endswith(".mkv")
    assert stopped["outputPath"] == path
    assert _record_event(records, "STARTING")[1]["outputPath"] is None
    assert _record_event(records, "STOPPING")[1]["outputPath"] is None
    assert _responses(records, "StopRecord")[0]["responseData"]["outputPath"] == path
    assert _responses(records, "GetOutputSettings")[0]["responseData"]["outputSettings"]["path"] == path
    # GetRecordStatus still reports the output active for a while after STOPPED.
    after = [d["responseData"] for t, d in _timed(records, FROM_OBS, 7, "GetRecordStatus") if t > t_stopped]
    assert after[0]["outputActive"] is True and after[-1]["outputActive"] is False


def test_pause_and_resume_edges_on_a_pausable_profile():
    records = _records("pause_resume.jsonl")
    assert _record_states(records) == ["STARTING", "STARTED", "PAUSED", "RESUMED", "STOPPING", "STOPPED"]
    t_paused, paused = _record_event(records, "PAUSED")
    t_resumed, resumed = _record_event(records, "RESUMED")
    assert paused["outputActive"] is False and resumed["outputActive"] is True
    during = [d["responseData"] for t, d in _timed(records, FROM_OBS, 7, "GetRecordStatus") if t_paused < t < t_resumed]
    assert during and all(s["outputPaused"] for s in during)
    assert during[-1]["outputDuration"] - during[0]["outputDuration"] < 200


def test_pause_on_the_default_profile_is_a_silent_no_op():
    records = _records("pause_noop.jsonl")
    assert _codes(records, "PauseRecord") == [100]
    assert _codes(records, "ResumeRecord") == [503]
    assert _record_states(records) == ["STARTING", "STARTED", "STOPPING", "STOPPED"]
    assert not any(d["responseData"]["outputPaused"] for d in _responses(records, "GetRecordStatus"))


def test_a_pause_made_while_disconnected_shows_only_in_record_status():
    records = _records("missed_pause.jsonl")
    assert _record_states(records) == ["STARTING", "STARTED", "RESUMED", "STOPPING", "STOPPED"]
    closes = _closes(records)
    assert closes[1]["by"] == "client" and closes[2]["by"] == "client"
    first_after = next(
        d["responseData"] for t, d in _timed(records, FROM_OBS, 7, "GetRecordStatus") if t > closes[2]["t_mono"]
    )
    assert first_after["outputActive"] is True and first_after["outputPaused"] is True


def test_a_reconnect_finds_the_active_file_through_output_settings():
    records = _records("reconnect.jsonl")
    _, started = _record_event(records, "STARTED")
    t_dropped = _closes(records)[2]["t_mono"]
    status = next(d["responseData"] for t, d in _timed(records, FROM_OBS, 7, "GetRecordStatus") if t > t_dropped)
    assert status["outputActive"] is True and status["outputBytes"] > 0
    paths = [d["responseData"]["outputSettings"]["path"] for d in _responses(records, "GetOutputSettings")]
    assert paths == [started["outputPath"], started["outputPath"]]


def test_a_split_announces_the_next_file_but_stop_names_the_first():
    records = _records("split.jsonl")
    _, started = _record_event(records, "STARTED")
    (changed,) = _events(records, "RecordFileChanged")
    first, second = started["outputPath"], changed["eventData"]["newOutputPath"]
    assert first != second and second.startswith(APP_DIR)
    assert _record_event(records, "STOPPED")[1]["outputPath"] == first
    assert _responses(records, "StopRecord")[0]["responseData"]["outputPath"] == first
    assert [d["responseData"]["outputSettings"]["path"] for d in _responses(records, "GetOutputSettings")] == [
        first,
        first,
    ]


def test_split_turned_off_at_runtime_applies_to_the_next_start():
    records = _records("split_off_runtime.jsonl")
    values = [d["responseData"]["parameterValue"] for d in _responses(records, "GetProfileParameter")]
    assert values == ["true", "false"]
    assert _codes(records, "SplitRecordFile") == [702]
    assert not _events(records, "RecordFileChanged")


@pytest.mark.parametrize(
    ("name", "states"), [("start_failed_missing_dir.jsonl", []), ("start_failed_unwritable.jsonl", ["STARTING"])]
)
def test_a_failed_start_answers_success_and_never_sends_stopped(name, states):
    records = _records(name)
    assert _codes(records, "StartRecord") == [100]
    assert _record_states(records) == states
    assert all(not d["responseData"]["outputActive"] for d in _responses(records, "GetRecordStatus"))


# --- OBS going away -------------------------------------------------------------------------------


def test_a_clean_obs_exit_sends_exit_started_and_no_stopped():
    records = _records("obs_exit.jsonl")
    assert _events(records, "ExitStarted")
    assert _record_states(records) == ["STARTING", "STARTED"]
    closes = _closes(records)
    assert {(closes[c]["by"], closes[c]["code"]) for c in (1, 2)} == {("obs", 1001)}
    assert any(r.get("event") == "upstream_failed" for r in records)


def test_sigterm_while_recording_leaves_obs_serving():
    records = _records("obs_sigterm.jsonl")
    assert not _events(records, "ExitStarted")
    assert _record_states(records) == ["STARTING", "STARTED"]
    assert {_closes(records)[c]["by"] for c in (1, 2)} == {"client"}
    later = [r["msg"]["op"] for r in _messages(records) if r["conn"] in (3, 4)]
    assert later.count(2) == 2  # both reconnects were identified by the same OBS


def test_a_killed_obs_drops_every_connection_abnormally():
    records = _records("obs_killed.jsonl")
    assert not _events(records, "ExitStarted")
    assert _record_states(records) == ["STARTING", "STARTED"]
    closes = _closes(records)
    assert {(closes[c]["by"], closes[c]["code"]) for c in (1, 2)} == {("obs", 1006)}
    assert any(r.get("event") == "upstream_failed" for r in records)


# --- provisioning, profiles and collections -------------------------------------------------------


def test_create_profile_answers_before_obs_switches_to_it():
    records = _records("provision.jsonl")
    ((t_resp, _),) = _timed(records, FROM_OBS, 7, "CreateProfile")
    ((t_changed, changed),) = _timed(records, FROM_OBS, 5, "CurrentProfileChanged")
    assert t_resp < t_changed and changed["eventData"]["profileName"] == APP
    assert not _events(records, "CurrentProfileChanging")
    ((t_req, _),) = _timed(records, TO_OBS, 6, "CreateSceneCollection")
    ((t_changing, _),) = _timed(records, FROM_OBS, 5, "CurrentSceneCollectionChanging")
    ((t_coll, _),) = _timed(records, FROM_OBS, 5, "CurrentSceneCollectionChanged")
    ((t_coll_resp, _),) = _timed(records, FROM_OBS, 7, "CreateSceneCollection")
    assert t_req < t_changing < t_coll < t_coll_resp


def test_provisioning_reads_back_the_profile_keys():
    records = _records("provision.jsonl")
    got = {
        (q["requestData"]["parameterCategory"], q["requestData"]["parameterName"]): a["responseData"]["parameterValue"]
        for q, a in zip(
            _requests(records, "GetProfileParameter"), _responses(records, "GetProfileParameter"), strict=True
        )
    }
    assert got[("SimpleOutput", "RecFormat2")] == "mkv"
    assert got[("AdvOut", "RecFormat2")] == "mkv"
    assert got[("AdvOut", "RecSplitFile")] == "false"
    assert got[("Video", "AutoRemux")] == "false"
    assert got[("Output", "Mode")] == "Simple"
    assert got[("SimpleOutput", "RecQuality")] == "Stream"


def test_a_new_xcomposite_input_lists_its_placeholder_disabled_first():
    records = _records("provision.jsonl")
    first = _responses(records, "GetInputPropertiesListPropertyItems")[0]["responseData"]["propertyItems"]
    assert first[0]["itemEnabled"] is False and first[0]["itemValue"].endswith("\r\nplaceholder")
    assert any(i["itemEnabled"] and i["itemName"] == first[0]["itemName"] for i in first[1:])


def test_settings_apply_reactivates_the_profile_between_two_recordings():
    records = _records("settings_apply.jsonl")
    stopped = [
        d["eventData"]["outputPath"] for d in _events(records, "RecordStateChanged") if d["eventData"]["outputPath"]
    ]
    assert len(set(stopped)) == 2 and all(p.startswith(APP_DIR) and p.endswith(".mkv") for p in stopped)
    assert [q["requestData"]["profileName"] for q in _requests(records, "SetCurrentProfile")] == ["Untitled", APP]


def test_arm_and_disarm_switch_profile_then_collection_and_back():
    records = _records("arm_disarm.jsonl")
    assert _codes(records, "GetReplayBufferStatus") == [604]
    assert _codes(records, "GetVirtualCamStatus") == [604]
    targets = [q["requestData"]["profileName"] for q in _requests(records, "SetCurrentProfile")]
    targets += [q["requestData"]["sceneCollectionName"] for q in _requests(records, "SetCurrentSceneCollection")]
    assert targets == [APP, "Untitled", APP, "Untitled"]
    for kind, request in (("Profile", "SetCurrentProfile"), ("SceneCollection", "SetCurrentSceneCollection")):
        changing = [t for t, _ in _timed(records, FROM_OBS, 5, f"Current{kind}Changing")]
        changed = [t for t, _ in _timed(records, FROM_OBS, 5, f"Current{kind}Changed")]
        answered = [t for t, _ in _timed(records, FROM_OBS, 7, request)]
        assert len(changing) == len(changed) == len(answered) == 2
        assert all(a < b < c for a, b, c in zip(changing, changed, answered, strict=True))


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("switch_recording.jsonl", "GetRecordStatus"),
        ("switch_streaming.jsonl", "GetStreamStatus"),
        ("switch_replay_buffer.jsonl", "GetReplayBufferStatus"),
    ],
)
def test_obs_switches_under_an_active_output_and_keeps_it_running(name, status):
    records = _records(name)
    assert _codes(records, "SetCurrentProfile") == [100, 100]
    assert _codes(records, "SetCurrentSceneCollection") == [100, 100]
    assert [d["responseData"]["outputActive"] for d in _responses(records, status)] == [True] * 4


def test_a_recording_that_survives_the_switch_stays_in_the_users_folder():
    records = _records("switch_recording.jsonl")
    path = _record_event(records, "STOPPED")[1]["outputPath"]
    assert path.startswith(USER_DIR) and not path.startswith(APP_DIR)


def test_a_switch_answer_can_arrive_before_its_changed_event():
    records = _records("switch_streaming.jsonl")
    t_resp = _timed(records, FROM_OBS, 7, "SetCurrentSceneCollection")[0][0]
    t_changed = _timed(records, FROM_OBS, 5, "CurrentSceneCollectionChanged")[0][0]
    assert t_resp < t_changed


def test_refused_and_no_op_switches():
    records = _records("switch_refused.jsonl")
    assert _codes(records, "SetCurrentProfile") == [600, 100]  # unknown name, then the current one
    assert _codes(records, "SetCurrentSceneCollection") == [600, 100]
    assert _codes(records, "CreateProfile") == [601]
    assert _codes(records, "CreateSceneCollection") == [601]
    assert _codes(records, "CreateScene") == [601]
    assert not _events(records)  # switching to the current profile or collection sends no event


def test_requests_during_a_collection_change_get_207_and_events_pause():
    records = _records("switch_not_ready.jsonl")
    windows = list(
        zip(
            [t for t, _ in _timed(records, FROM_OBS, 5, "CurrentSceneCollectionChanging")],
            [t for t, _ in _timed(records, FROM_OBS, 5, "CurrentSceneCollectionChanged")],
            strict=True,
        )
    )
    not_ready = [t for t, d in _timed(records, FROM_OBS, 7, "GetRecordStatus") if d["requestStatus"]["code"] == 207]
    assert not_ready and all(any(a < t < b for a, b in windows) for t in not_ready)
    events = [r["t_mono"] for r in _messages(records, FROM_OBS, 5)]
    assert not any(a < t < b for a, b in windows for t in events)


def test_a_restart_question_holds_back_the_switch_answer():
    records = _records("switch_restart_prompt.jsonl")
    rate = _responses(records, "GetProfileParameter")[0]["responseData"]
    assert rate == {"defaultParameterValue": "48000", "parameterValue": "44100"}
    assert _requests(records, "SetCurrentProfile") and not _responses(records, "SetCurrentProfile")
    assert [e["eventData"]["profileName"] for e in _events(records, "CurrentProfileChanged")] == [APP]


def test_matching_sample_rates_switch_without_a_question():
    records = _records("switch_restart_prompt_matched.jsonl")
    assert _codes(records, "SetCurrentProfile") == [100, 100, 100, 100]


def test_synthetic_virtualcam_refusal_stops_arming_after_the_status_checks():
    records = _records("synthetic-arm-virtualcam-active.jsonl")
    (edited,) = [r for r in records if "synthetic" in r]
    assert edited["msg"]["d"]["requestType"] == "GetVirtualCamStatus"
    assert edited["msg"]["d"]["responseData"] == {"outputActive": True}
    assert not _requests(records, "SetCurrentProfile") and not _requests(records, "SetCurrentSceneCollection")


# --- window list ----------------------------------------------------------------------------------


def test_a_retitled_x11_window_disables_item_0_and_reappears_under_the_same_xid():
    records = _records("window_retitle.jsonl")
    before, retitled, closed = (
        d["responseData"]["propertyItems"] for d in _responses(records, "GetInputPropertiesListPropertyItems")
    )
    stored = before[0]["itemValue"]
    xid = stored.split("\r\n")[0]

    def enabled_with_xid(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [i for i in items[1:] if i["itemEnabled"] and i["itemValue"].split("\r\n")[0] == xid]

    assert before[0]["itemEnabled"] is True
    assert retitled[0] == {**before[0], "itemEnabled": False}
    (moved,) = enabled_with_xid(retitled)
    assert moved["itemName"] != before[0]["itemName"]
    assert closed[0]["itemEnabled"] is False and not enabled_with_xid(closed)
    settings = [d["responseData"]["inputSettings"]["capture_window"] for d in _responses(records, "GetInputSettings")]
    assert settings == [stored, stored]
