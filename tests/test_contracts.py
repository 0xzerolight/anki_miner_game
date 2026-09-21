"""Every contract name from master-plan section 4 exists, and the contract packages keep the dependency rule."""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

import anki_miner_game

SECTION_4_NAMES = {
    "anki_miner_game.models.constants": [
        "SKIP_MS",
        "MIN_CUE_MS",
        "START_SHIFT_MS",
        "MAX_LINE_CHARS",
        "TYPEWRITER_WINDOW_S",
        "SCHEMA",
    ],
    "anki_miner_game.models.lines": ["GameLine", "TimedLine"],
    "anki_miner_game.models.cue": ["Cue", "Region"],
    "anki_miner_game.models.config": [
        "AppConfig",
        "ObsSettings",
        "TextSourceConfig",
        "FeedSettings",
        "RecordingSettings",
        "CueSettings",
        "VadSettings",
    ],
    "anki_miner_game.models.profile": [
        "GameProfile",
        "CaptureSettings",
        "AudioSettings",
        "FilterSettings",
        "OcrSettings",
        "AutoSettings",
        "TextMode",
        "validate",
    ],
    "anki_miner_game.models.manifest": [
        "SessionManifest",
        "ObsRecord",
        "ClockRecord",
        "DriftSample",
        "Counts",
        "LiveCue",
        "VadRecord",
        "FilesRecord",
        "ManifestState",
        "Flag",
        "to_json",
        "from_json",
    ],
    "anki_miner_game.models.pipeline": ["DropReason", "DROP_COUNTER", "Accepted", "Replaced", "Dropped"],
    "anki_miner_game.models.messages": [
        "LineReceived",
        "ObsEvent",
        "UserCommand",
        "Tick",
        "SessionEvent",
        "StateChanged",
        "LineAccepted",
        "RecordingStarted",
        "RecordingStopped",
        "SessionFinalised",
        "AppState",
        "SourceStatus",
        "Banner",
    ],
    "anki_miner_game.models.obs": [
        "ObsInfo",
        "REQUIRED_REQUESTS",
        "ObsCredentials",
        "WsConfig",
        "WindowItem",
        "ProvisionResult",
    ],
    "anki_miner_game.interfaces.text_source": ["TextSource"],
    "anki_miner_game.interfaces.record_clock": ["RecordClock"],
    "anki_miner_game.interfaces.obs": ["ObsGateway", "ObsDiscovery", "Provisioner", "Recorder"],
    "anki_miner_game.interfaces.presenter": ["Presenter"],
    "anki_miner_game.interfaces.session": ["SessionControl"],
    "anki_miner_game.interfaces.addons": ["AddonService", "VadJobs", "OcrAreaPicker"],
    "anki_miner_game.paths": ["home"],
    "anki_miner_game.store": ["load_config", "save_config", "load_profiles", "save_profile"],
}

PROPERTY = "property"


def sync(*params: str) -> tuple[bool, tuple[str, ...]]:
    """A plain method taking ``params`` after ``self``."""
    return (False, params)


def coro(*params: str) -> tuple[bool, tuple[str, ...]]:
    """An ``async def`` method taking ``params`` after ``self``."""
    return (True, params)


PROTOCOL_MEMBERS = {
    ("anki_miner_game.interfaces.text_source", "TextSource"): {
        "id": PROPERTY,
        "status": PROPERTY,
        "start": sync("sink"),
        "stop": sync(),
    },
    ("anki_miner_game.interfaces.record_clock", "RecordClock"): {
        "start": sync("zero_mono"),
        "pause": sync("at_mono"),
        "resume": sync("at_mono"),
        "offset_ms": sync("t_mono"),
    },
    ("anki_miner_game.interfaces.obs", "ObsGateway"): {
        "connect": coro(),
        "request": coro("name", "fields"),
        "subscribe": sync("handler"),
        "collection_changing": PROPERTY,
        "close": coro(),
    },
    ("anki_miner_game.interfaces.obs", "ObsDiscovery"): {
        "find_install": sync(),
        "config_root": sync(),
        "read_ws_config": sync(),
        "ensure_server_enabled": sync(),
        "is_running": sync(),
        "launch": sync(),
        "wait_ready": coro("timeout_s"),
        "credentials": sync("cfg"),
    },
    ("anki_miner_game.interfaces.obs", "Provisioner"): {
        "ensure_profile": coro("cfg"),
        "ensure_collection": coro("profile"),
        "list_windows": coro(),
    },
    ("anki_miner_game.interfaces.obs", "Recorder"): {"start": coro(), "stop": coro()},
    ("anki_miner_game.interfaces.presenter", "Presenter"): {
        "state_changed": sync("state"),
        "source_status": sync("source_id", "status"),
        "line_accepted": sync("line", "offset_ms", "replaces_previous"),
        "banner": sync("banner"),
        "banner_cleared": sync("key"),
        "session_finished": sync("manifest_path"),
        "vad_progress": sync("manifest_path", "done_ms", "total_ms"),
        "vad_finished": sync("manifest_path", "state"),
    },
    ("anki_miner_game.interfaces.session", "SessionControl"): {
        "post": sync("msg"),
        "subscribe": sync("cb"),
        "state": PROPERTY,
    },
    ("anki_miner_game.interfaces.addons", "AddonService"): {
        "status": sync(),
        "size_bytes": PROPERTY,
        "install": coro("progress"),
    },
    ("anki_miner_game.interfaces.addons", "VadJobs"): {
        "queue": sync("manifest_path"),
        "rerun": sync("manifest_path"),
        "restore": sync("manifest_path"),
    },
    ("anki_miner_game.interfaces.addons", "OcrAreaPicker"): {"pick": coro("window_title")},
}

PACKAGE_ROOT = Path(anki_miner_game.__file__).parent


@pytest.mark.parametrize(("module", "names"), sorted(SECTION_4_NAMES.items()))
def test_section_4_names_are_importable(module, names):
    mod = importlib.import_module(module)
    missing = [name for name in names if not hasattr(mod, name)]
    assert missing == []


@pytest.mark.parametrize(("key", "members"), sorted(PROTOCOL_MEMBERS.items()))
def test_protocols_declare_exactly_their_members(key, members):
    module, name = key
    protocol = getattr(importlib.import_module(module), name)
    assert {attr for attr in vars(protocol) if not attr.startswith("_")} == set(members)
    for member, expected in members.items():
        attr = inspect.getattr_static(protocol, member)
        if expected == PROPERTY:
            assert isinstance(attr, property), member
        else:
            is_async, params = expected
            assert inspect.iscoroutinefunction(attr) is is_async, member
            assert tuple(inspect.signature(attr).parameters)[1:] == params, member


def test_wait_ready_defaults_to_30_seconds():
    from anki_miner_game.interfaces.obs import ObsDiscovery

    assert inspect.signature(ObsDiscovery.wait_ready).parameters["timeout_s"].default == 30.0


def _app_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: use absolute imports"
            found.append(node.module or "")
    return [name for name in found if name == "anki_miner_game" or name.startswith("anki_miner_game.")]


@pytest.mark.parametrize(
    ("package", "allowed"),
    [
        ("models", ("anki_miner_game.models.",)),
        ("interfaces", ("anki_miner_game.models.", "anki_miner_game.interfaces.")),
    ],
)
def test_contract_packages_keep_the_dependency_rule(package, allowed):
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((PACKAGE_ROOT / package).glob("*.py"))
        for name in _app_imports(path)
        if not name.startswith(allowed)
    ]
    assert offenders == []
