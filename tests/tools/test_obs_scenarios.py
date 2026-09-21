"""tools/m0/obs_scenarios.py: scenario steps and runner, OBS config helpers, output recipes.

No real OBS: the runner is driven through fake links, and ``ObsLink`` through a minimal
obs-websocket server on loopback port 0.
"""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from anki_miner_game.models.obs import REQUIRED_REQUESTS
from tools.m0 import obs_scenarios as sc

# --- ini editing and replay-buffer seeding -------------------------------------------------

BASIC_INI = "[General]\nName=amg\n\n[Output]\nMode=Simple\n\n[SimpleOutput]\nFilePath=/v\nRecRB=false\n\n[Video]\nFPSCommon=30\n"


def test_set_ini_value_replaces_a_key_in_its_section_only():
    text = "[A]\nRecRB=false\n\n[B]\nRecRB=false\n"
    assert sc.set_ini_value(text, "B", "RecRB", "true") == "[A]\nRecRB=false\n\n[B]\nRecRB=true\n"


def test_set_ini_value_inserts_a_missing_key_at_the_end_of_its_section():
    text = "[A]\nx=1\n\n[B]\ny=2\n"
    assert sc.set_ini_value(text, "A", "RecRB", "true") == "[A]\nx=1\nRecRB=true\n\n[B]\ny=2\n"


def test_set_ini_value_appends_a_missing_section():
    assert sc.set_ini_value("[A]\nx=1\n", "AdvOut", "RecRB", "true") == "[A]\nx=1\n\n[AdvOut]\nRecRB=true\n"
    assert sc.set_ini_value("", "AdvOut", "RecRB", "true") == "[AdvOut]\nRecRB=true\n"


def test_set_ini_value_keeps_crlf_line_ends():
    text = "[A]\r\nx=1\r\n"
    assert sc.set_ini_value(text, "A", "x", "2") == "[A]\r\nx=2\r\n"


def test_seed_replay_buffer_enables_it_for_simple_and_advanced_output(tmp_path):
    ini = tmp_path / "basic.ini"
    ini.write_bytes(b"\xef\xbb\xbf" + BASIC_INI.encode())
    sc.seed_replay_buffer(tmp_path)
    data = ini.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")  # a BOM, when present, stays
    text = data[3:].decode()
    assert "[SimpleOutput]\nFilePath=/v\nRecRB=true\n" in text
    assert "[AdvOut]\nRecRB=true\n" in text
    assert "[Video]\nFPSCommon=30\n" in text


def test_seed_replay_buffer_needs_an_existing_profile(tmp_path):
    with pytest.raises(sc.ScenarioError, match="basic.ini"):
        sc.seed_replay_buffer(tmp_path / "missing")


# --- config root: websocket settings, snapshot and restore -----------------------------------


def _config_root(tmp_path: Path) -> Path:
    root = tmp_path / "obs-studio"
    (root / "basic" / "profiles" / "Untitled").mkdir(parents=True)
    (root / "global.ini").write_text("[General]\nLastVersion=1\n", encoding="utf-8")
    (root / "basic" / "profiles" / "Untitled" / "basic.ini").write_text(BASIC_INI, encoding="utf-8")
    ws = root / "plugin_config" / "obs-websocket"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(
        json.dumps({"server_enabled": True, "server_port": 4455, "auth_required": True, "server_password": "pw"}),
        encoding="utf-8",
    )
    return root


def test_the_flatpak_config_root_is_under_the_app_data_dir(tmp_path):
    assert sc.flatpak_config_root(tmp_path) == tmp_path / ".var/app/com.obsproject.Studio/config/obs-studio"


def test_websocket_settings_come_from_the_plugin_config(tmp_path):
    settings = sc.read_ws_settings(_config_root(tmp_path))
    assert (settings.port, settings.password, settings.auth_required) == (4455, "pw", True)
    assert "pw" not in repr(settings)


def test_snapshot_and_restore_round_trip(tmp_path):
    root = _config_root(tmp_path)
    snapshot = tmp_path / "snap"
    sc.snapshot_config(root, snapshot, running=lambda: False)
    (root / "global.ini").write_text("changed", encoding="utf-8")
    (root / "basic" / "new.json").write_text("{}", encoding="utf-8")
    sc.restore_config(snapshot, root, running=lambda: False)
    assert (root / "global.ini").read_text(encoding="utf-8") == "[General]\nLastVersion=1\n"
    assert not (root / "basic" / "new.json").exists()
    assert (root / "basic" / "profiles" / "Untitled" / "basic.ini").read_text(encoding="utf-8") == BASIC_INI
    assert snapshot.is_dir()  # restoring keeps the snapshot for the next scenario


def test_a_snapshot_never_overwrites(tmp_path):
    root = _config_root(tmp_path)
    (tmp_path / "snap").mkdir()
    with pytest.raises(sc.ScenarioError, match="exists"):
        sc.snapshot_config(root, tmp_path / "snap", running=lambda: False)


def test_restore_refuses_a_root_that_is_not_an_obs_config_root(tmp_path):
    root = _config_root(tmp_path)
    snapshot = tmp_path / "snap"
    sc.snapshot_config(root, snapshot, running=lambda: False)
    with pytest.raises(sc.ScenarioError, match="obs-studio"):
        sc.restore_config(snapshot, tmp_path, running=lambda: False)


def test_restore_refuses_a_directory_that_is_not_a_snapshot(tmp_path):
    root = _config_root(tmp_path)
    (tmp_path / "junk").mkdir()
    with pytest.raises(sc.ScenarioError, match="snapshot"):
        sc.restore_config(tmp_path / "junk", root, running=lambda: False)
    assert (root / "global.ini").exists()


def test_snapshot_and_restore_refuse_while_obs_runs(tmp_path):
    root = _config_root(tmp_path)
    with pytest.raises(sc.ScenarioError, match="running"):
        sc.snapshot_config(root, tmp_path / "snap", running=lambda: True)
    with pytest.raises(sc.ScenarioError, match="running"):
        sc.restore_config(tmp_path / "snap", root, running=lambda: True)


# --- output recipes ----------------------------------------------------------------------------


def test_the_rtmp_sink_listens_on_loopback_and_discards_the_stream():
    argv = sc.rtmp_sink_argv(19350)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-listen") + 1] == "1"
    assert argv[argv.index("-i") + 1] == "rtmp://127.0.0.1:19350/live/test"
    assert argv[-3:] == ["-f", "null", "-"]


def test_stream_settings_point_obs_at_the_sink():
    step = sc.stream_to_sink(19350)
    assert step.name == "SetStreamServiceSettings"
    assert step.data == {
        "streamServiceType": "rtmp_custom",
        "streamServiceSettings": {"server": "rtmp://127.0.0.1:19350/live", "key": "test"},
    }


# --- scenarios ---------------------------------------------------------------------------------


def _args(**overrides):
    defaults = {
        "hold": 2.0,
        "profile": "amg-probe",
        "collection": "amg-probe",
        "back_profile": "Untitled",
        "back_collection": "Untitled",
        "output": "record",
        "rtmp_port": 19350,
    }
    return type("Args", (), {**defaults, **overrides})()


@pytest.mark.parametrize("name", sorted(sc.SCENARIOS))
def test_every_scenario_uses_only_known_requests_and_events(name):
    steps = sc.SCENARIOS[name](_args())
    assert steps
    for step in steps:
        if isinstance(step, sc.Request):
            assert step.name in REQUIRED_REQUESTS or step.name in sc.EXTRA_REQUESTS, step.name
        if isinstance(step, sc.WaitEvent | sc.Operator) and step.event:
            assert step.event in sc.EVENTS


def test_the_missed_pause_scenario_pauses_from_a_second_client_while_disconnected():
    steps = sc.SCENARIOS["missed_pause"](_args())
    kinds = [type(s).__name__ for s in steps]
    pause = next(s for s in steps if isinstance(s, sc.Request) and s.name == "PauseRecord")
    assert pause.via == "direct"
    assert kinds.index("Disconnect") < steps.index(pause) < kinds.index("Connect")


@pytest.mark.parametrize("output", ["none", "record", "stream", "replay_buffer"])
def test_the_switch_scenario_starts_and_stops_the_chosen_output(output):
    names = [s.name for s in sc.SCENARIOS["switch"](_args(output=output)) if isinstance(s, sc.Request)]
    starts = {"record": "StartRecord", "stream": "StartStream", "replay_buffer": "StartReplayBuffer"}
    stops = {"record": "StopRecord", "stream": "StopStream", "replay_buffer": "StopReplayBuffer"}
    if output == "none":
        assert not set(starts.values()) & set(names)
    else:
        assert names.index(starts[output]) < names.index("SetCurrentProfile") < names.index(stops[output])
    assert names.count("SetCurrentProfile") == 2  # there and back
    assert names.count("SetCurrentSceneCollection") == 2


# --- the runner, with fake links ---------------------------------------------------------------


class FakeLink:
    def __init__(self, name, script, events):
        self.name = name
        self.script = script  # request name -> response dict or sc.RequestFailedError
        self.events = events  # shared list of (t_mono, event, data) the fake OBS emits
        self.requests = []
        self.closed = False

    def request(self, name, data=None):
        self.requests.append((name, data))
        outcome = self.script.get(name, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def next_event(self, timeout_s):
        return self.events.pop(0) if self.events else None

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, name, script=None, events=None):
        self.name, self.script, self.events = name, script or {}, events if events is not None else []
        self.links = []

    def __call__(self):
        link = FakeLink(self.name, self.script, self.events)
        self.links.append(link)
        return link


def _run(steps, proxy, direct=None):
    log = []
    runner = sc.ScenarioRunner(proxy, direct, log.append, now=iter(range(1000)).__next__, sleep=lambda s: None)
    return runner.run("t", steps), log


def _started(t=5):
    return (t, "RecordStateChanged", {"output_active": True, "output_state": "OBS_WEBSOCKET_OUTPUT_STARTED"})


def test_the_runner_logs_every_step_with_its_times_and_result():
    proxy = Factory("proxy", {"GetRecordStatus": {"outputActive": False}}, [_started()])
    ok, log = _run([sc.Request("GetRecordStatus"), sc.WaitEvent("RecordStateChanged", "STARTED"), sc.Sleep(1)], proxy)
    assert ok
    steps = [r for r in log if r["kind"] == "step"]
    assert [s["step"]["type"] for s in steps] == ["Request", "WaitEvent", "Sleep"]
    assert steps[0]["result"] == {"response": {"outputActive": False}}
    assert steps[1]["result"]["event"] == "RecordStateChanged"
    assert steps[1]["result"]["t_mono"] == 5
    assert all(s["t_start"] <= s["t_end"] for s in steps)
    assert log[-1] == {"kind": "end", "scenario": "t", "ok": True, "t_mono": log[-1]["t_mono"]}


def test_wait_event_skips_other_events_and_matches_the_state():
    starting = (3, "RecordStateChanged", {"output_state": "OBS_WEBSOCKET_OUTPUT_STARTING"})
    proxy = Factory("proxy", events=[starting, (4, "CurrentProfileChanged", {}), _started()])
    ok, log = _run([sc.WaitEvent("RecordStateChanged", "STARTED")], proxy)
    assert ok
    assert [r for r in log if r["kind"] == "step"][0]["result"]["t_mono"] == 5


def test_a_missing_required_event_fails_the_scenario():
    ok, log = _run([sc.WaitEvent("RecordStateChanged", "STARTED", timeout_s=0.1), sc.Sleep(1)], Factory("proxy"))
    assert not ok
    assert log[-2]["kind"] == "error" and "RecordStateChanged" in log[-2]["error"]
    assert not [r for r in log if r["kind"] == "step" and r["step"]["type"] == "Sleep"]


def test_an_optional_event_that_never_comes_is_logged_and_the_run_goes_on():
    ok, log = _run([sc.WaitEvent("CurrentProfileChanged", timeout_s=0.1, required=False), sc.Sleep(1)], Factory("p"))
    assert ok
    assert [r for r in log if r["kind"] == "step"][0]["result"] == {"timeout": True}


def test_a_refused_request_is_recorded_when_allowed_and_fatal_otherwise():
    refused = sc.RequestFailedError("SetCurrentProfile", 500, "outputs active")
    proxy = Factory("proxy", {"SetCurrentProfile": refused})
    ok, log = _run([sc.Request("SetCurrentProfile", {"profileName": "x"}, allow_error=True)], proxy)
    assert ok
    assert [r for r in log if r["kind"] == "step"][0]["result"] == {"error": {"code": 500, "comment": "outputs active"}}
    ok, _ = _run([sc.Request("SetCurrentProfile", {"profileName": "x"})], proxy)
    assert not ok


def test_disconnect_and_connect_replace_the_proxied_link_and_direct_requests_bypass_it():
    proxy, direct = Factory("proxy"), Factory("direct")
    ok, _ = _run(
        [sc.Request("GetVersion"), sc.Disconnect(), sc.Request("PauseRecord", via="direct"), sc.Connect()],
        proxy,
        direct,
    )
    assert ok
    assert len(proxy.links) == 2 and proxy.links[0].closed
    assert direct.links[0].requests == [("PauseRecord", None)]
    assert proxy.links[0].requests == [("GetVersion", None)]
    assert all(link.closed for link in proxy.links + direct.links)  # everything closed at the end


def test_an_operator_step_prints_its_instruction_and_waits_for_the_event(capsys):
    proxy = Factory("proxy", events=[(9, "ExitStarted", {})])
    ok, _ = _run([sc.Operator("Quit OBS now", event="ExitStarted")], proxy)
    assert ok
    assert "Quit OBS now" in capsys.readouterr().err


# --- ObsLink against a minimal obs-websocket server ------------------------------------------


class MiniObs:
    """No auth; answers every request; the event client (eventSubscriptions != 0) gets one event."""

    def __init__(self):
        self.port = 0
        self.requests = []
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _handler(self, ws):
        await ws.send(json.dumps({"op": 0, "d": {"obsWebSocketVersion": "5.7.4", "rpcVersion": 1}}))
        identify = json.loads(await ws.recv())
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        if identify["d"].get("eventSubscriptions"):
            # obsws-python starts its event thread before callbacks can be registered: give
            # ObsLink a moment, as a real OBS event would.
            await asyncio.sleep(0.3)
            data = {"outputActive": True, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED", "outputPath": "/v/a.mkv"}
            await ws.send(json.dumps({"op": 5, "d": {"eventType": "RecordStateChanged", "eventData": data}}))
        async for message in ws:
            d = json.loads(message)["d"]
            self.requests.append(d["requestType"])
            status = {"result": True, "code": 100}
            response = {"requestType": d["requestType"], "requestId": d["requestId"], "requestStatus": status}
            if d["requestType"] == "GetRecordStatus":
                response["responseData"] = {"outputActive": True, "outputPaused": False}
            await ws.send(json.dumps({"op": 7, "d": response}))

    def _run(self):
        asyncio.set_event_loop(self._loop)

        async def open_server():
            return await serve(self._handler, "127.0.0.1", 0)

        server = self._loop.run_until_complete(open_server())
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()
        server.close()
        self._loop.run_until_complete(server.wait_closed())

    def __enter__(self):
        self._thread.start()
        self._ready.wait(5)
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


def test_obs_link_sends_requests_and_queues_events_with_t_mono():
    with MiniObs() as obs:
        link = sc.ObsLink("127.0.0.1", obs.port, "", timeout_s=5, now=lambda: 42.0)
        try:
            assert link.request("GetRecordStatus") == {"outputActive": True, "outputPaused": False}
            assert link.request("StartRecord") == {}
            t_mono, event, data = link.next_event(timeout_s=5)
        finally:
            link.close()
    assert (t_mono, event) == (42.0, "RecordStateChanged")
    assert data["output_state"] == "OBS_WEBSOCKET_OUTPUT_STARTED"
    assert obs.requests == ["GetRecordStatus", "StartRecord"]


def test_event_handlers_are_named_for_obsws_python_dispatch():
    sink = []
    handler = sc.event_handler("RecordStateChanged", sink.append, now=lambda: 1.0)
    assert handler.__name__ == "on_record_state_changed"


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_lists_the_scenarios(capsys):
    assert sc.main(["list"]) == 0
    out = capsys.readouterr().out
    for name in sc.SCENARIOS:
        assert name in out
