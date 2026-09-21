"""Every contract name from master-plan section 4 exists, and the contract packages keep the dependency rule."""

import ast
import importlib
import inspect
import typing
from collections.abc import Callable
from pathlib import Path

import pytest

import anki_miner_game
from anki_miner_game.models.messages import SourceStatus

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
        "wait_closed": coro(),
        "set_status_listener": sync("cb"),
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
        "state_changed": sync("state", "slug"),
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
        "note": PROPERTY,
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


def _assert_conforms(protocol: type, impl: type) -> None:
    """``impl`` has every member of ``protocol``, of the same kind, taking the same parameters."""
    for member in (attr for attr in vars(protocol) if not attr.startswith("_")):
        expected = inspect.getattr_static(protocol, member)
        actual = inspect.getattr_static(impl, member)
        if isinstance(expected, property):
            assert isinstance(actual, property), member
        else:
            assert inspect.iscoroutinefunction(actual) is inspect.iscoroutinefunction(expected), member
            assert tuple(inspect.signature(actual).parameters) == tuple(inspect.signature(expected).parameters), member


class _FakeTextSource:
    """The smallest ``TextSource``: a transition sets ``status`` first, then tells the listener."""

    def __init__(self, source_id: str) -> None:
        self._id = source_id
        self._status = SourceStatus.DISCONNECTED
        self._listener: Callable[[str, SourceStatus], None] | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return self._status

    def start(self, sink: Callable[[str, float, str], None]) -> None:
        self._move(SourceStatus.CONNECTING)

    def stop(self) -> None:
        self._move(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        pass

    def set_status_listener(self, cb: Callable[[str, SourceStatus], None]) -> None:
        self._listener = cb

    def _move(self, status: SourceStatus) -> None:
        if status is self._status:
            return
        self._status = status
        if self._listener is not None:
            self._listener(self._id, status)


def test_a_text_source_reports_every_status_transition_to_its_listener():
    from anki_miner_game.interfaces.text_source import TextSource

    _assert_conforms(TextSource, _FakeTextSource)
    source = _FakeTextSource("textractor")
    heard: list[tuple[str, SourceStatus, SourceStatus]] = []
    source.set_status_listener(lambda source_id, status: heard.append((source_id, status, source.status)))
    source.start(lambda raw, t_mono, source_id: None)
    source._move(SourceStatus.CONNECTED)
    source._move(SourceStatus.CONNECTED)
    source._move(SourceStatus.RECEIVING)
    source.stop()
    assert [(source_id, status) for source_id, status, _ in heard] == [
        ("textractor", SourceStatus.CONNECTING),
        ("textractor", SourceStatus.CONNECTED),
        ("textractor", SourceStatus.RECEIVING),
        ("textractor", SourceStatus.DISCONNECTED),
    ]
    assert all(status is seen for _, status, seen in heard)


def test_the_text_source_status_listener_is_documented():
    from anki_miner_game.interfaces.text_source import TextSource

    doc = TextSource.set_status_listener.__doc__ or ""
    assert "every" in doc and "SourceStatusChanged" in doc


def test_text_sources_are_started_on_the_actors_thread_and_awaited_after_stop():
    from anki_miner_game.interfaces.text_source import TextSource

    doc = TextSource.__doc__ or ""
    assert "actor" in doc and "thread" in doc
    assert "stop" in (TextSource.wait_closed.__doc__ or "")


def test_list_windows_names_the_input_it_reads():
    """Amendment 10: ``window_capture`` drops minimized windows, so Windows reads ``game_capture``."""
    from anki_miner_game.interfaces.obs import Provisioner

    doc = Provisioner.list_windows.__doc__ or ""
    for word in ("game_capture", "capture_window", "xcomposite_input", "``[]``", "ObsError"):
        assert word in doc, word


def test_obs_config_failures_are_typed():
    """``credentials`` and ``read_ws_config`` name the error an unusable OBS websocket config raises."""
    from anki_miner_game.interfaces.obs import ObsDiscovery

    for method in (ObsDiscovery.credentials, ObsDiscovery.read_ws_config):
        assert "ObsConfigError" in (method.__doc__ or ""), method.__name__


def test_ocr_area_picker_names_the_error_pick_raises():
    from anki_miner_game.interfaces.addons import OcrAreaPicker

    assert "RuntimeError" in (OcrAreaPicker.pick.__doc__ or "")


def test_the_addon_note_is_optional_text_shown_beside_the_status():
    from anki_miner_game.interfaces.addons import AddonService

    note = inspect.getattr_static(AddonService, "note")
    assert typing.get_type_hints(note.fget)["return"] == str | None
    assert "status" in (note.__doc__ or "")


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


def test_vad_progress_total_may_be_unknown():
    from typing import get_type_hints

    from anki_miner_game.interfaces.presenter import Presenter

    assert get_type_hints(Presenter.vad_progress)["total_ms"] == int | None


def test_obs_ready_and_requests_document_207_not_ready():
    from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway

    assert "GetVersion" in (ObsDiscovery.wait_ready.__doc__ or "")
    for method in (ObsDiscovery.wait_ready, ObsGateway.connect, ObsGateway.request):
        assert "207 ``NotReady``" in (method.__doc__ or ""), method.__name__


def test_replaced_documents_its_merge_base():
    from anki_miner_game.models.pipeline import Replaced

    doc = Replaced.__doc__ or ""
    assert "TextPipeline.reset()" in doc
    assert "ReplaceRecord" in doc
