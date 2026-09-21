"""OBS behaviour scenarios for the M0 spikes (R1 clock and sync, R2 OBS behaviour).

A scenario is a list of steps (requests, waits for events, disconnects, operator actions) run
through obsws-python, the client library the app uses. The "proxy" link normally points at
``tools/obs_transcript_recorder.py``, so every frame lands in the transcript; the "direct" link
talks to OBS itself and plays the second client that R2's missed-pause case needs. Each step is
logged to a steps JSONL with the ``time.monotonic()`` at its start and end::

    python -m tools.m0.obs_scenarios list
    python -m tools.m0.obs_scenarios run pause_resume --port 4456 --steps STEPS.jsonl
    python -m tools.m0.obs_scenarios run switch --output stream --rtmp-port 19350 --port 4456 --steps S.jsonl

Helpers for the Flatpak OBS config root (``~/.var/app/com.obsproject.Studio/config/obs-studio``),
which the spikes use directly and restore between scenarios, with OBS closed::

    python -m tools.m0.obs_scenarios snapshot --dest DIR
    python -m tools.m0.obs_scenarios restore --snapshot DIR
    python -m tools.m0.obs_scenarios seed-replay-buffer --profile NAME   # before launching OBS
    python -m tools.m0.obs_scenarios rtmp-sink --port 19350              # a local stream target

The OBS password is read from the root's ``plugin_config/obs-websocket/config.json`` and never
printed; the ``obsws_python`` logger, which logs it at INFO, is held at WARNING.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import obsws_python
from obsws_python.error import OBSSDKRequestError
from obsws_python.util import to_snake_case

logging.getLogger("obsws_python").setLevel(logging.WARNING)  # it logs the password at INFO

APP_ID = "com.obsproject.Studio"
EVENTS = (
    "RecordStateChanged",
    "RecordFileChanged",
    "StreamStateChanged",
    "ReplayBufferStateChanged",
    "VirtualcamStateChanged",
    "CurrentProfileChanging",
    "CurrentProfileChanged",
    "CurrentSceneCollectionChanging",
    "CurrentSceneCollectionChanged",
    "ExitStarted",
)
# Requests the scenarios send beyond the app's own list (models.obs.REQUIRED_REQUESTS).
EXTRA_REQUESTS = (
    "PauseRecord",
    "ResumeRecord",
    "StartStream",
    "StopStream",
    "SetStreamServiceSettings",
    "StartReplayBuffer",
    "StopReplayBuffer",
)
_STATE_PREFIX = "OBS_WEBSOCKET_OUTPUT_"


class ScenarioError(Exception):
    """A scenario helper was used on the wrong thing (not an OBS root, OBS still running, ...)."""


class RequestFailedError(Exception):
    """OBS answered a request with a failure status."""

    def __init__(self, name: str, code: int, comment: str | None) -> None:
        super().__init__(f"{name} failed with code {code}: {comment}")
        self.name, self.code, self.comment = name, code, comment


# --- steps -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    name: str
    data: dict[str, Any] | None = None
    via: str = "proxy"  # or "direct": a second client that bypasses the proxy
    allow_error: bool = False  # record a refusal instead of failing the scenario


@dataclass(frozen=True)
class WaitEvent:
    event: str
    state: str | None = None  # outputState without the OBS_WEBSOCKET_OUTPUT_ prefix
    timeout_s: float = 15.0
    required: bool = True


@dataclass(frozen=True)
class Sleep:
    seconds: float


@dataclass(frozen=True)
class Disconnect:
    """Drop the proxied links, as a lost connection would."""


@dataclass(frozen=True)
class Connect:
    """Reconnect the proxied links."""


@dataclass(frozen=True)
class Operator:
    """Something done outside the websocket (quit OBS); optionally wait for its event."""

    instruction: str
    event: str | None = None
    timeout_s: float = 300.0


Step = Request | WaitEvent | Sleep | Disconnect | Connect | Operator


def stream_to_sink(rtmp_port: int) -> Request:
    """Point OBS's stream output at the local ``rtmp-sink``."""
    settings = {"server": f"rtmp://127.0.0.1:{rtmp_port}/live", "key": "test"}
    return Request("SetStreamServiceSettings", {"streamServiceType": "rtmp_custom", "streamServiceSettings": settings})


def _start_stop(request: str, event: str) -> tuple[list[Step], list[Step]]:
    return (
        [Request(f"Start{request}"), WaitEvent(event, "STARTED")],
        [Request(f"Stop{request}"), WaitEvent(event, "STOPPED")],
    )


def _output(args: Any) -> tuple[list[Step], list[Step]]:
    if args.output == "none":
        return [], []
    if args.output == "record":
        return _start_stop("Record", "RecordStateChanged")
    if args.output == "stream":
        start, stop = _start_stop("Stream", "StreamStateChanged")
        return [stream_to_sink(args.rtmp_port), *start], stop
    if args.output == "replay_buffer":
        return _start_stop("ReplayBuffer", "ReplayBufferStateChanged")
    raise ScenarioError(f"unknown output {args.output!r}")


def _record() -> tuple[list[Step], list[Step]]:
    return _start_stop("Record", "RecordStateChanged")


def normal(args: Any) -> list[Step]:
    start, stop = _record()
    return [
        Request("GetVersion"),
        Request("GetRecordStatus"),
        *start,
        Sleep(args.hold),
        Request("GetRecordStatus"),
        *stop,
    ]


def pause_resume(args: Any) -> list[Step]:
    start, stop = _record()
    return [
        *start,
        Sleep(args.hold),
        Request("PauseRecord"),
        WaitEvent("RecordStateChanged", "PAUSED"),
        Sleep(args.hold),
        Request("GetRecordStatus"),
        Request("ResumeRecord"),
        WaitEvent("RecordStateChanged", "RESUMED"),
        Sleep(args.hold),
        Request("GetRecordStatus"),
        *stop,
    ]


def missed_pause(args: Any) -> list[Step]:
    """The proxied client is away while a second client pauses; it reconnects to a paused OBS."""
    start, stop = _record()
    return [
        *start,
        Sleep(args.hold),
        Disconnect(),
        Request("PauseRecord", via="direct"),
        Sleep(args.hold),
        Connect(),
        Request("GetRecordStatus"),
        Request("ResumeRecord"),
        WaitEvent("RecordStateChanged", "RESUMED"),
        Sleep(args.hold),
        Request("GetRecordStatus"),
        *stop,
    ]


def reconnect(args: Any) -> list[Step]:
    start, stop = _record()
    return [
        *start,
        Sleep(args.hold),
        Disconnect(),
        Sleep(args.hold),
        Connect(),
        Request("GetRecordStatus"),
        Sleep(args.hold),
        *stop,
    ]


def obs_exit(args: Any) -> list[Step]:
    start, _ = _record()
    # SIGINT closes the main window, whose closeWindow() emits the frontend event behind
    # ExitStarted; SIGTERM only saves and quits, and ``flatpak kill`` sends neither (README).
    quit_obs = "Quit OBS now (pkill -INT -x obs); waiting for ExitStarted"
    return [*start, Sleep(args.hold), Operator(quit_obs, event="ExitStarted")]


def prepare_switch_targets(args: Any) -> list[Step]:
    """Create the switch targets (creating one also switches to it), then switch back."""
    return [
        Request("CreateProfile", {"profileName": args.profile}, allow_error=True),
        Request("CreateSceneCollection", {"sceneCollectionName": args.collection}, allow_error=True),
        *_switch_to(args.back_profile, args.back_collection),
    ]


def _switch_to(profile: str, collection: str) -> list[Step]:
    return [
        Request("SetCurrentProfile", {"profileName": profile}, allow_error=True),
        WaitEvent("CurrentProfileChanged", timeout_s=10, required=False),
        Request("SetCurrentSceneCollection", {"sceneCollectionName": collection}, allow_error=True),
        WaitEvent("CurrentSceneCollectionChanged", timeout_s=10, required=False),
    ]


def switch(args: Any) -> list[Step]:
    """Profile and collection switch and back, with ``--output`` active throughout (or none)."""
    start, stop = _output(args)
    status = [Request("GetRecordStatus"), Request("GetStreamStatus"), Request("GetReplayBufferStatus")]
    return [
        Request("GetProfileList"),
        Request("GetSceneCollectionList"),
        *start,
        *_switch_to(args.profile, args.collection),
        *status,
        Sleep(args.hold),
        *_switch_to(args.back_profile, args.back_collection),
        *status,
        *stop,
    ]


SCENARIOS: dict[str, Callable[[Any], list[Step]]] = {
    "normal": normal,
    "pause_resume": pause_resume,
    "missed_pause": missed_pause,
    "reconnect": reconnect,
    "obs_exit": obs_exit,
    "prepare_switch_targets": prepare_switch_targets,
    "switch": switch,
}


# --- links -------------------------------------------------------------------------------------


class Link(Protocol):
    def request(self, name: str, data: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def next_event(self, timeout_s: float) -> tuple[float, str, dict[str, Any]] | None: ...

    def close(self) -> None: ...


def event_handler(
    event: str, put: Callable[[tuple[float, str, dict[str, Any]]], None], now: Callable[[], float]
) -> Callable[[Any], None]:
    """An obsws-python callback (dispatched by function name) that queues ``(t_mono, event, data)``."""

    def handler(data: Any) -> None:
        put((now(), event, {key: getattr(data, key) for key in data.attrs()}))

    handler.__name__ = f"on_{to_snake_case(event)}"
    return handler


class ObsLink:
    """A ReqClient + EventClient pair, like the app's gateway; events queue with their ``t_mono``."""

    def __init__(
        self, host: str, port: int, password: str, *, timeout_s: float = 10.0, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._events: queue.Queue[tuple[float, str, dict[str, Any]]] = queue.Queue()
        self._req = obsws_python.ReqClient(host=host, port=port, password=password, timeout=timeout_s)
        try:
            self._ev = obsws_python.EventClient(host=host, port=port, password=password, timeout=timeout_s)
        except Exception:
            self._req.disconnect()
            raise
        self._ev.callback.register([event_handler(name, self._events.put, now) for name in EVENTS])

    def request(self, name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._req.send(name, data, raw=True)
        except OBSSDKRequestError as exc:
            raise RequestFailedError(name, exc.code, str(exc)) from exc
        return dict(response or {})

    def next_event(self, timeout_s: float) -> tuple[float, str, dict[str, Any]] | None:
        try:
            return self._events.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._req.disconnect()
        self._ev.disconnect()


# --- runner ------------------------------------------------------------------------------------


def _describe(step: Step) -> dict[str, Any]:
    return {"type": type(step).__name__, **asdict(step)}


def _matches(event: tuple[float, str, dict[str, Any]], name: str, state: str | None) -> bool:
    _, got, data = event
    if got != name:
        return False
    return state is None or str(data.get("output_state", "")).removeprefix(_STATE_PREFIX) == state


@dataclass
class ScenarioRunner:
    connect_proxy: Callable[[], Link]
    connect_direct: Callable[[], Link] | None
    write: Callable[[dict[str, Any]], None]
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = field(default=lambda text: print(text, file=sys.stderr, flush=True))
    _proxy: Link | None = None
    _direct: Link | None = None

    def run(self, name: str, steps: Sequence[Step]) -> bool:
        self.write({"kind": "scenario", "name": name, "t_mono": self.now()})
        ok = True
        try:
            self._proxy = self.connect_proxy()
            for index, step in enumerate(steps):
                t_start = self.now()
                try:
                    result = self._do(step)
                except Exception as exc:  # any failure ends the scenario, logged with its step
                    self.write(
                        {
                            "kind": "error",
                            "index": index,
                            "step": _describe(step),
                            "error": f"{type(exc).__name__}: {exc}",
                            "t_mono": self.now(),
                        }
                    )
                    ok = False
                    break
                self.write(
                    {
                        "kind": "step",
                        "index": index,
                        "step": _describe(step),
                        "t_start": t_start,
                        "t_end": self.now(),
                        "result": result,
                    }
                )
        finally:
            for link in (self._proxy, self._direct):
                if link is not None:
                    try:
                        link.close()
                    except Exception as exc:  # OBS may already be gone (obs_exit)
                        self.say(f"closing a link failed: {exc}")
            self._proxy = self._direct = None
        self.write({"kind": "end", "scenario": name, "ok": ok, "t_mono": self.now()})
        return ok

    def _link(self, via: str) -> Link:
        if via == "direct":
            if self.connect_direct is None:
                raise ScenarioError("this scenario needs a direct link (--direct-port)")
            if self._direct is None:
                self._direct = self.connect_direct()
            return self._direct
        if self._proxy is None:
            raise ScenarioError("the proxied link is disconnected")
        return self._proxy

    def _wait(self, name: str, state: str | None, timeout_s: float, required: bool) -> dict[str, Any]:
        link = self._link("proxy")
        deadline = self.now() + timeout_s
        while True:
            remaining = deadline - self.now()
            if remaining <= 0:
                if required:
                    raise TimeoutError(f"no {name}{f' {state}' if state else ''} within {timeout_s:g} s")
                return {"timeout": True}
            event = link.next_event(min(remaining, 0.5))
            if event is not None and _matches(event, name, state):
                return {"event": event[1], "t_mono": event[0], "data": event[2]}

    def _do(self, step: Step) -> dict[str, Any]:
        if isinstance(step, Request):
            try:
                return {"response": self._link(step.via).request(step.name, step.data)}
            except RequestFailedError as exc:
                if not step.allow_error:
                    raise
                return {"error": {"code": exc.code, "comment": exc.comment}}
        if isinstance(step, WaitEvent):
            return self._wait(step.event, step.state, step.timeout_s, step.required)
        if isinstance(step, Sleep):
            self.sleep(step.seconds)
            return {}
        if isinstance(step, Disconnect):
            if self._proxy is not None:
                self._proxy.close()
            self._proxy = None
            return {}
        if isinstance(step, Connect):
            if self._proxy is None:
                self._proxy = self.connect_proxy()
            return {}
        self.say(step.instruction)
        if step.event is None:
            return {}
        return self._wait(step.event, None, step.timeout_s, True)


# --- the Flatpak OBS config root ---------------------------------------------------------------


def flatpak_config_root(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / ".var" / "app" / APP_ID / "config" / "obs-studio"


@dataclass(frozen=True)
class WsSettings:
    port: int
    auth_required: bool
    password: str = field(repr=False)


def read_ws_settings(root: Path) -> WsSettings:
    data = json.loads((root / "plugin_config" / "obs-websocket" / "config.json").read_text(encoding="utf-8"))
    return WsSettings(
        int(data.get("server_port", 4455)), bool(data.get("auth_required")), data.get("server_password", "")
    )


def obs_running() -> bool:
    proc = subprocess.run(
        ["flatpak", "ps", "--columns=application"], capture_output=True, text=True, check=False, timeout=10
    )
    return APP_ID in proc.stdout.split()


def _require_closed(running: Callable[[], bool]) -> None:
    if running():
        raise ScenarioError("OBS is running; quit it first (it rewrites its config on exit)")


def snapshot_config(root: Path, dest: Path, *, running: Callable[[], bool] = obs_running) -> None:
    _require_closed(running)
    if dest.exists():
        raise ScenarioError(f"{dest} exists; snapshots never overwrite")
    shutil.copytree(root, dest, symlinks=True)


def restore_config(snapshot: Path, root: Path, *, running: Callable[[], bool] = obs_running) -> None:
    """Replace ``root`` with a copy of ``snapshot`` (kept for the next scenario)."""
    _require_closed(running)
    if root.name != "obs-studio":
        raise ScenarioError(f"refusing to replace {root}: not an obs-studio config root")
    if not (snapshot / "global.ini").is_file() and not (snapshot / "user.ini").is_file():
        raise ScenarioError(f"{snapshot} is not a config snapshot (no global.ini or user.ini)")
    staging = root.with_name(root.name + ".restoring")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(snapshot, staging, symlinks=True)
    shutil.rmtree(root)
    os.replace(staging, root)


# --- basic.ini --------------------------------------------------------------------------------


def set_ini_value(text: str, section: str, key: str, value: str) -> str:
    """``key=value`` in ``[section]``: replaced in place, else added at the section's end."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        block = [header, f"{key}={value}"]
        lines = [*lines, "", *block] if lines else block
        return newline.join(lines) + newline
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if lines[i].split("=", 1)[0].strip() == key and "=" in lines[i]:
            lines[i] = f"{key}={value}"
            return newline.join(lines) + newline
    last = max((i for i in range(start, end) if lines[i].strip()), default=start)
    lines.insert(last + 1, f"{key}={value}")
    return newline.join(lines) + newline


def seed_replay_buffer(profile_dir: Path) -> None:
    """Enable the replay buffer for Simple and Advanced output; OBS must read it at launch."""
    ini = profile_dir / "basic.ini"
    if not ini.is_file():
        raise ScenarioError(f"no basic.ini in {profile_dir}")
    raw = ini.read_bytes()
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    text = raw[len(bom) :].decode("utf-8")
    for section in ("SimpleOutput", "AdvOut"):
        text = set_ini_value(text, section, "RecRB", "true")
    tmp = ini.with_name(ini.name + ".tmp")
    tmp.write_bytes(bom + text.encode("utf-8"))
    os.replace(tmp, ini)


# --- RTMP sink ---------------------------------------------------------------------------------


def rtmp_sink_argv(port: int) -> list[str]:
    """ffmpeg as an RTMP server on loopback that accepts one publisher and discards the stream."""
    url = f"rtmp://127.0.0.1:{port}/live/test"
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-listen", "1", "-i", url, "-c", "copy"] + [
        "-f",
        "null",
        "-",
    ]


def run_rtmp_sink(port: int) -> int:
    """Serve one publisher after another (ffmpeg exits when a publisher leaves) until Ctrl-C."""
    try:
        while True:
            print(f"rtmp-sink: listening on rtmp://127.0.0.1:{port}/live (key: test)", file=sys.stderr, flush=True)
            subprocess.run(rtmp_sink_argv(port), check=False)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


# --- CLI ---------------------------------------------------------------------------------------


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="obs_scenarios", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--config-root", type=Path, default=None, help="OBS config root (default: Flatpak)")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="list the scenarios")
    run = sub.add_parser("run", help="run a scenario")
    run.add_argument("name", choices=sorted(SCENARIOS))
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, required=True, help="the transcript recorder's port")
    run.add_argument("--direct-port", type=int, default=None, help="OBS itself (default: its config)")
    run.add_argument("--steps", type=Path, required=True, help="steps JSONL (appended)")
    run.add_argument("--hold", type=float, default=5.0, help="seconds between actions")
    run.add_argument("--profile", default="amg-probe", help="switch target profile")
    run.add_argument("--collection", default="amg-probe", help="switch target scene collection")
    run.add_argument("--back-profile", default="Untitled")
    run.add_argument("--back-collection", default="Untitled")
    run.add_argument("--output", choices=("none", "record", "stream", "replay_buffer"), default="record")
    run.add_argument("--rtmp-port", type=int, default=19350)
    snap = sub.add_parser("snapshot", help="copy the config root (OBS closed)")
    snap.add_argument("--dest", type=Path, required=True)
    restore = sub.add_parser("restore", help="replace the config root with a snapshot (OBS closed)")
    restore.add_argument("--snapshot", type=Path, required=True)
    seed = sub.add_parser("seed-replay-buffer", help="enable the replay buffer in a profile (OBS closed)")
    seed.add_argument("--profile", required=True, help="profile directory name under basic/profiles")
    sink = sub.add_parser("rtmp-sink", help="local RTMP server that discards what OBS streams")
    sink.add_argument("--port", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    root = args.config_root or flatpak_config_root()
    try:
        if args.verb == "list":
            for name, build in SCENARIOS.items():
                print(f"{name:<24} {(build.__doc__ or '').strip().splitlines()[0] if build.__doc__ else ''}")
            return 0
        if args.verb == "snapshot":
            snapshot_config(root, args.dest)
            return 0
        if args.verb == "restore":
            restore_config(args.snapshot, root)
            return 0
        if args.verb == "seed-replay-buffer":
            _require_closed(obs_running)
            seed_replay_buffer(root / "basic" / "profiles" / args.profile)
            return 0
        if args.verb == "rtmp-sink":
            return run_rtmp_sink(args.port)
        ws = read_ws_settings(root)
        direct_port = args.direct_port or ws.port
        with open(args.steps, "a", encoding="utf-8") as log:

            def write(record: dict[str, Any]) -> None:
                log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                log.flush()

            runner = ScenarioRunner(
                lambda: ObsLink(args.host, args.port, ws.password),
                lambda: ObsLink(args.host, direct_port, ws.password),
                write,
            )
            return 0 if runner.run(args.name, SCENARIOS[args.name](args)) else 1
    except (ScenarioError, OSError, ValueError) as exc:
        print(f"obs_scenarios: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
