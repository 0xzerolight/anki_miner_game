"""``obs/discovery.py``: install lookup, config roots, the websocket config, launch and readiness (spec 11.1, 17)."""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import socket
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from websockets.asyncio.server import Server, ServerConnection, serve

from anki_miner_game.models.config import AppConfig, ObsSettings
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsCredentials,
    ObsInstall,
    WsConfig,
)
from anki_miner_game.obs import discovery
from anki_miner_game.obs.discovery import LocalObsDiscovery
from tests.test_contracts import _assert_conforms

WS_CONFIG = Path("plugin_config", "obs-websocket", "config.json")


class FakeRunner:
    """Answers ``run`` from ``respond`` and records every ``run`` and ``spawn``."""

    def __init__(
        self,
        respond: Callable[[tuple[str, ...]], tuple[int, str]] = lambda argv: (1, ""),
        spawn_error: OSError | None = None,
    ) -> None:
        self.respond = respond
        self.spawn_error = spawn_error
        self.ran: list[tuple[str, ...]] = []
        self.spawned: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        self.ran.append(tuple(argv))
        return self.respond(tuple(argv))

    def spawn(self, argv: Sequence[str], cwd: Path | None) -> None:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawned.append((tuple(argv), cwd))


def flatpak_installed(argv: tuple[str, ...]) -> tuple[int, str]:
    return (
        (0, "Ref: app/com.obsproject.Studio/x86_64/stable\n")
        if argv[1:] == ("info", discovery.FLATPAK_APP_ID)
        else (1, "")
    )


def which_from(found: dict[str, str]) -> Callable[[str], str | None]:
    return found.get


def fake_proc(root: Path, names: Sequence[str]) -> Path:
    """A ``/proc`` look-alike with one numbered folder per process name."""
    root.mkdir(parents=True, exist_ok=True)
    for pid, name in enumerate(names, start=100):
        (root / str(pid)).mkdir()
        (root / str(pid) / "comm").write_text(name + "\n", encoding="utf-8")
    return root


def make(
    tmp_path: Path,
    *,
    platform: str = "linux",
    which: dict[str, str] | None = None,
    registry: Callable[[str], str | None] = lambda view: None,
    runner: FakeRunner | None = None,
    running: Sequence[str] = (),
    cfg: AppConfig | None = None,
    **kwargs: object,
) -> LocalObsDiscovery:
    return LocalObsDiscovery(
        lambda: cfg or AppConfig(),
        platform=platform,
        which=which_from(which or {}),
        registry=registry,
        runner=runner or FakeRunner(),
        proc_root=fake_proc(tmp_path / "proc", running),
        **kwargs,
    )


def native_linux(tmp_path: Path, **kwargs: object) -> LocalObsDiscovery:
    return make(tmp_path, which={"obs": "/usr/bin/obs"}, **kwargs)


def native_root() -> Path:
    return Path.home() / ".config" / "obs-studio"  # conftest points XDG_CONFIG_HOME here


def write_ws(root: Path, data: object, *, raw: str | bytes | None = None) -> Path:
    path = root / WS_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
    return path


def windows_exe(folder: Path) -> Path:
    exe = folder / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def test_conforms_to_the_obs_discovery_protocol():
    from anki_miner_game.interfaces.obs import ObsDiscovery

    _assert_conforms(ObsDiscovery, LocalObsDiscovery)
    assert inspect.signature(LocalObsDiscovery.wait_ready).parameters["timeout_s"].default == 30.0


# --- find_install ------------------------------------------------------------------------------


def test_windows_finds_obs_under_program_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    exe = windows_exe(tmp_path / "pf" / "obs-studio")
    looked_up: list[str] = []

    def registry(view: str) -> str | None:
        looked_up.append(view)
        return None

    found = make(tmp_path, platform="win32", registry=registry).find_install()

    assert found == ObsInstall(argv=(str(exe),), cwd=exe.parent, flatpak=False)
    assert looked_up == []  # the registry is the fallback only


def test_windows_falls_back_to_the_registry_64_bit_view_first(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    exe = windows_exe(tmp_path / "D" / "OBS")
    looked_up: list[str] = []

    def registry(view: str) -> str | None:
        looked_up.append(view)
        return str(tmp_path / "D" / "OBS") if view == "64" else None

    found = make(tmp_path, platform="win32", registry=registry).find_install()

    assert found == ObsInstall(argv=(str(exe),), cwd=exe.parent, flatpak=False)
    assert looked_up == ["64"]


def test_windows_reads_the_32_bit_view_when_the_64_bit_one_has_no_obs(tmp_path, monkeypatch):
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    exe = windows_exe(tmp_path / "x86" / "obs-studio")
    folders = {"64": str(tmp_path / "gone"), "32": str(tmp_path / "x86" / "obs-studio")}

    found = make(tmp_path, platform="win32", registry=folders.get).find_install()

    assert found is not None and found.argv == (str(exe),)


def test_windows_without_obs_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    (tmp_path / "pf" / "obs-studio").mkdir(parents=True)  # a leftover folder without the exe

    assert make(tmp_path, platform="win32").find_install() is None


def test_linux_prefers_obs_on_path(tmp_path):
    runner = FakeRunner(flatpak_installed)
    found = make(tmp_path, which={"obs": "/usr/bin/obs", "flatpak": "/usr/bin/flatpak"}, runner=runner).find_install()

    assert found == ObsInstall(argv=("/usr/bin/obs",), cwd=None, flatpak=False)
    assert runner.ran == []


def test_linux_falls_back_to_the_flatpak(tmp_path):
    runner = FakeRunner(flatpak_installed)
    found = make(tmp_path, which={"flatpak": "/usr/bin/flatpak"}, runner=runner).find_install()

    assert found == ObsInstall(argv=("/usr/bin/flatpak", "run", "com.obsproject.Studio"), cwd=None, flatpak=True)
    assert runner.ran == [("/usr/bin/flatpak", "info", "com.obsproject.Studio")]


def test_linux_without_the_flatpak_app_finds_nothing(tmp_path):
    runner = FakeRunner(lambda argv: (1, "error: com.obsproject.Studio not installed"))

    assert make(tmp_path, which={"flatpak": "/usr/bin/flatpak"}, runner=runner).find_install() is None


def test_linux_without_flatpak_finds_nothing(tmp_path):
    runner = FakeRunner(flatpak_installed)

    assert make(tmp_path, runner=runner).find_install() is None
    assert runner.ran == []


def test_find_install_checks_again_on_every_call(tmp_path):
    """The wizard's re-check button: an install made after the first check is found."""
    found: dict[str, str] = {}
    obs = LocalObsDiscovery(AppConfig, which=found.get, runner=FakeRunner(), proc_root=tmp_path, platform="linux")
    assert obs.find_install() is None

    found["obs"] = "/usr/bin/obs"

    assert obs.find_install() == ObsInstall(argv=("/usr/bin/obs",), cwd=None, flatpak=False)


# --- config_root -------------------------------------------------------------------------------


def test_windows_config_root_is_under_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    windows_exe(tmp_path / "pf" / "obs-studio")

    assert make(tmp_path, platform="win32").config_root() == tmp_path / "Roaming" / "obs-studio"


def test_native_linux_config_root_follows_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert native_linux(tmp_path).config_root() == tmp_path / "xdg" / "obs-studio"


@pytest.mark.parametrize("value", [None, ""])
def test_native_linux_config_root_defaults_to_dot_config(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", value)

    assert native_linux(tmp_path).config_root() == Path.home() / ".config" / "obs-studio"


def test_flatpak_config_root_ignores_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    obs = make(tmp_path, which={"flatpak": "/usr/bin/flatpak"}, runner=FakeRunner(flatpak_installed))

    assert obs.config_root() == Path.home() / ".var" / "app" / "com.obsproject.Studio" / "config" / "obs-studio"


def test_no_install_has_no_config_root(tmp_path):
    assert make(tmp_path).config_root() is None


def test_config_root_follows_the_latest_check(tmp_path):
    found: dict[str, str] = {}
    obs = LocalObsDiscovery(AppConfig, which=found.get, runner=FakeRunner(flatpak_installed), proc_root=tmp_path)
    assert obs.config_root() is None

    found["flatpak"] = "/usr/bin/flatpak"
    assert obs.config_root() is None  # no new check yet
    obs.find_install()

    assert obs.config_root() == Path.home() / ".var" / "app" / "com.obsproject.Studio" / "config" / "obs-studio"


# --- read_ws_config ----------------------------------------------------------------------------

FULL = {
    "alerts_enabled": False,
    "auth_required": True,
    "first_load": False,
    "server_enabled": True,
    "server_password": "s3cretPassw0rd12",
    "server_port": 4466,
}


def test_reads_the_websocket_config(tmp_path):
    write_ws(native_root(), FULL)

    ws = native_linux(tmp_path).read_ws_config()

    assert ws == WsConfig(server_enabled=True, port=4466, password="s3cretPassw0rd12", auth_required=True)


def test_reads_a_config_with_a_byte_order_mark(tmp_path):
    write_ws(native_root(), None, raw=b"\xef\xbb\xbf" + json.dumps(FULL).encode())

    ws = native_linux(tmp_path).read_ws_config()

    assert ws is not None and ws.port == 4466


def test_missing_keys_take_obs_websocket_defaults(tmp_path):
    """obs-websocket ``Config.h``: server off, port 4455, auth on, no password."""
    write_ws(native_root(), {})

    assert native_linux(tmp_path).read_ws_config() == WsConfig(
        server_enabled=False, port=4455, password=None, auth_required=True
    )


def test_keys_of_the_wrong_type_take_the_defaults_as_obs_does(tmp_path):
    """``Config::Load`` keeps its default for a key whose JSON type is wrong."""
    write_ws(
        native_root(),
        {"server_enabled": 1, "server_port": "4466", "auth_required": "no", "server_password": 5},
    )

    assert native_linux(tmp_path).read_ws_config() == WsConfig(
        server_enabled=False, port=4455, password=None, auth_required=True
    )


def test_a_boolean_port_is_not_a_port(tmp_path):
    write_ws(native_root(), {"server_port": True})

    ws = native_linux(tmp_path).read_ws_config()

    assert ws is not None and ws.port == 4455


def test_an_empty_password_is_no_password(tmp_path):
    write_ws(native_root(), {**FULL, "server_password": ""})

    ws = native_linux(tmp_path).read_ws_config()

    assert ws is not None and ws.password is None


def test_missing_config_file_reads_as_none(tmp_path):
    assert native_linux(tmp_path).read_ws_config() is None


def test_no_install_reads_as_none(tmp_path):
    write_ws(native_root(), FULL)

    assert make(tmp_path).read_ws_config() is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2]",
        '"text"',
        json.dumps({**FULL, "server_port": 70000}),
        json.dumps({**FULL, "server_port": 0}),
        b"\xff\xfe{}",
    ],
    ids=["not-json", "array", "string", "port-too-big", "port-zero", "not-utf8"],
)
def test_an_unusable_config_file_raises_obs_config_error(tmp_path, raw):
    path = write_ws(native_root(), None, raw=raw)

    with pytest.raises(ObsConfigError) as caught:
        native_linux(tmp_path).read_ws_config()

    assert str(path) in str(caught.value)


def test_an_unreadable_config_path_raises_obs_config_error(tmp_path):
    (native_root() / WS_CONFIG).mkdir(parents=True)  # a folder where the file should be

    with pytest.raises(ObsConfigError):
        native_linux(tmp_path).read_ws_config()


def test_config_errors_never_quote_the_password(tmp_path):
    write_ws(native_root(), None, raw='{"server_password": "s3cretPassw0rd12", "server_port": 99999}')

    with pytest.raises(ObsConfigError) as caught:
        native_linux(tmp_path).read_ws_config()

    assert "s3cretPassw0rd12" not in str(caught.value)


# --- credentials -------------------------------------------------------------------------------


def test_credentials_come_from_obs_config(tmp_path):
    write_ws(native_root(), FULL)
    cfg = AppConfig(obs=ObsSettings(host="127.0.0.2"))

    creds = native_linux(tmp_path).credentials(cfg)

    assert creds == ObsCredentials(host="127.0.0.2", port=4466, password="s3cretPassw0rd12")


def test_credentials_are_read_again_at_every_call(tmp_path):
    obs = native_linux(tmp_path)
    write_ws(native_root(), FULL)
    assert obs.credentials(AppConfig()).port == 4466

    write_ws(native_root(), {**FULL, "server_port": 4477, "server_password": "an0therPassw0rd1"})

    assert obs.credentials(AppConfig()) == ObsCredentials(host="127.0.0.1", port=4477, password="an0therPassw0rd1")


def test_overrides_win_over_obs_config(tmp_path):
    write_ws(native_root(), FULL)
    cfg = AppConfig(obs=ObsSettings(port=5000, password_override="typed"))

    assert native_linux(tmp_path).credentials(cfg) == ObsCredentials(host="127.0.0.1", port=5000, password="typed")


def test_a_port_override_keeps_the_password_from_obs_config(tmp_path):
    write_ws(native_root(), FULL)

    creds = native_linux(tmp_path).credentials(AppConfig(obs=ObsSettings(port=5000)))

    assert creds == ObsCredentials(host="127.0.0.1", port=5000, password="s3cretPassw0rd12")


def test_no_password_when_obs_requires_no_auth(tmp_path):
    write_ws(native_root(), {**FULL, "auth_required": False})

    assert native_linux(tmp_path).credentials(AppConfig()).password is None


def test_missing_config_without_a_port_override_raises(tmp_path):
    with pytest.raises(ObsConfigError):
        native_linux(tmp_path).credentials(AppConfig())


def test_no_install_without_a_port_override_raises(tmp_path):
    with pytest.raises(ObsConfigError):
        make(tmp_path).credentials(AppConfig())


def test_unreadable_config_without_a_port_override_raises(tmp_path):
    write_ws(native_root(), None, raw="{not json")

    with pytest.raises(ObsConfigError):
        native_linux(tmp_path).credentials(AppConfig())


@pytest.mark.parametrize("override", [None, "typed"])
def test_unreadable_config_with_a_port_override_uses_the_password_override(tmp_path, override):
    write_ws(native_root(), None, raw="{not json")
    cfg = AppConfig(obs=ObsSettings(port=5000, password_override=override))

    assert native_linux(tmp_path).credentials(cfg) == ObsCredentials(host="127.0.0.1", port=5000, password=override)


def test_missing_config_with_a_port_override_connects_without_a_file(tmp_path):
    cfg = AppConfig(obs=ObsSettings(port=5000))

    assert make(tmp_path).credentials(cfg) == ObsCredentials(host="127.0.0.1", port=5000, password=None)


# --- is_running --------------------------------------------------------------------------------


def test_linux_sees_an_obs_process(tmp_path):
    assert native_linux(tmp_path, running=["bash", "kwin_wayland", "obs"]).is_running()


def test_linux_ignores_processes_that_only_look_like_obs(tmp_path):
    assert not native_linux(tmp_path, running=["bash", "obs-ffmpeg-mux", "obsidian", "xdg-dbus-proxy"]).is_running()


def test_linux_skips_entries_that_are_not_processes_or_vanish(tmp_path):
    proc = fake_proc(tmp_path / "proc", [])
    (proc / "self").mkdir()
    (proc / "self" / "comm").write_text("obs\n", encoding="utf-8")  # not a pid folder
    (proc / "123").mkdir()  # exited between the listing and the read: no comm
    obs = LocalObsDiscovery(AppConfig, which=which_from({"obs": "/usr/bin/obs"}), runner=FakeRunner(), proc_root=proc)

    assert not obs.is_running()


def test_linux_ignores_another_users_obs(tmp_path, monkeypatch):
    obs = native_linux(tmp_path, running=["obs"])
    monkeypatch.setattr(discovery, "_own_uid", lambda: (tmp_path / "proc").stat().st_uid + 1)

    assert not obs.is_running()


def test_windows_asks_tasklist_for_obs64(tmp_path):
    runner = FakeRunner(lambda argv: (0, '"obs64.exe","4242","Console","1","250,000 K"\r\n'))
    obs = make(tmp_path, platform="win32", runner=runner)

    assert obs.is_running()
    assert runner.ran == [("tasklist", "/FI", "IMAGENAME eq obs64.exe", "/FO", "CSV", "/NH")]


@pytest.mark.parametrize(
    "answer", [(0, "INFO: No tasks are running which match the specified criteria.\r\n"), (-1, "")]
)
def test_windows_without_obs64_is_not_running(tmp_path, answer):
    assert not make(tmp_path, platform="win32", runner=FakeRunner(lambda argv: answer)).is_running()


# --- ensure_server_enabled ---------------------------------------------------------------------


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_an_enabled_server_is_left_alone(tmp_path):
    path = write_ws(native_root(), FULL)
    before = path.read_bytes()

    assert native_linux(tmp_path, running=["obs"]).ensure_server_enabled()
    assert path.read_bytes() == before


def test_enables_the_server_while_obs_is_closed_and_keeps_everything_else(tmp_path):
    settings = {**FULL, "server_enabled": False, "some_future_key": [1, 2]}
    path = write_ws(native_root(), settings)

    assert native_linux(tmp_path).ensure_server_enabled()
    assert read_json(path) == {**settings, "server_enabled": True}


def test_enabling_generates_a_password_when_auth_is_required_and_none_exists(tmp_path):
    path = write_ws(native_root(), {**FULL, "server_enabled": False, "server_password": ""})

    assert native_linux(tmp_path).ensure_server_enabled()

    written = read_json(path)
    assert written["server_enabled"] is True and written["auth_required"] is True
    assert len(written["server_password"]) == 16 and written["server_password"].isalnum()
    assert written["server_password"].isascii()


def test_enabling_generates_no_password_when_auth_is_off(tmp_path):
    path = write_ws(native_root(), {**FULL, "server_enabled": False, "auth_required": False, "server_password": ""})

    assert native_linux(tmp_path).ensure_server_enabled()
    assert read_json(path) == {**FULL, "server_enabled": True, "auth_required": False, "server_password": ""}


def test_generated_passwords_differ(tmp_path):
    passwords = set()
    for _ in range(3):
        path = write_ws(native_root(), {"server_enabled": False})
        assert native_linux(tmp_path).ensure_server_enabled()
        passwords.add(read_json(path)["server_password"])

    assert len(passwords) == 3


def test_a_disabled_server_is_left_alone_while_obs_runs(tmp_path):
    path = write_ws(native_root(), {**FULL, "server_enabled": False})
    before = path.read_bytes()

    assert not native_linux(tmp_path, running=["obs"]).ensure_server_enabled()
    assert path.read_bytes() == before


def test_a_missing_config_is_created_enabled_while_obs_is_closed(tmp_path):
    obs = native_linux(tmp_path)

    assert obs.ensure_server_enabled()

    ws = obs.read_ws_config()
    assert ws is not None
    assert (ws.server_enabled, ws.port, ws.auth_required) == (True, 4455, True)
    assert ws.password is not None and len(ws.password) == 16
    assert read_json(native_root() / WS_CONFIG)["first_load"] is False


def test_a_missing_config_is_not_created_while_obs_runs(tmp_path):
    assert not native_linux(tmp_path, running=["obs"]).ensure_server_enabled()
    assert not (native_root() / WS_CONFIG).exists()


def test_nothing_to_enable_without_an_install(tmp_path):
    assert not make(tmp_path).ensure_server_enabled()


def test_an_unreadable_config_is_not_overwritten(tmp_path):
    path = write_ws(native_root(), None, raw="{not json")

    with pytest.raises(ObsConfigError):
        native_linux(tmp_path).ensure_server_enabled()
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_config_that_cannot_be_written_raises_obs_config_error(tmp_path, monkeypatch):
    write_ws(native_root(), {**FULL, "server_enabled": False})

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(discovery, "write_text_atomic", refuse)

    with pytest.raises(ObsConfigError):
        native_linux(tmp_path).ensure_server_enabled()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_written_config_is_private(tmp_path):
    path = write_ws(native_root(), {"server_enabled": False})

    assert native_linux(tmp_path).ensure_server_enabled()
    assert path.stat().st_mode & 0o077 == 0


def test_enabling_never_logs_the_password(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    path = write_ws(native_root(), {"server_enabled": False})

    assert native_linux(tmp_path).ensure_server_enabled()

    password = read_json(path)["server_password"]
    assert password not in caplog.text


# --- launch ------------------------------------------------------------------------------------


def test_windows_launches_obs64_minimised_from_its_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
    exe = windows_exe(tmp_path / "pf" / "obs-studio")
    runner = FakeRunner(lambda argv: (0, "INFO: No tasks are running which match the specified criteria.\r\n"))

    make(tmp_path, platform="win32", runner=runner).launch()

    assert runner.spawned == [((str(exe), "--minimize-to-tray"), exe.parent)]


def test_linux_launches_native_obs_minimised(tmp_path):
    runner = FakeRunner()

    native_linux(tmp_path, runner=runner).launch()

    assert runner.spawned == [(("/usr/bin/obs", "--minimize-to-tray"), None)]


def test_linux_launches_the_flatpak_minimised(tmp_path):
    runner = FakeRunner(flatpak_installed)

    make(tmp_path, which={"flatpak": "/usr/bin/flatpak"}, runner=runner).launch()

    assert runner.spawned == [(("/usr/bin/flatpak", "run", "com.obsproject.Studio", "--minimize-to-tray"), None)]


def test_launch_does_not_start_a_second_obs(tmp_path):
    """A second instance would stop at OBS's modal "already running" question (S1 section 4)."""
    runner = FakeRunner()

    native_linux(tmp_path, runner=runner, running=["obs"]).launch()

    assert runner.spawned == []


def test_launch_without_an_install_raises(tmp_path):
    with pytest.raises(ObsConnectError):
        make(tmp_path).launch()


def test_launch_reports_a_program_that_cannot_start(tmp_path):
    runner = FakeRunner(spawn_error=FileNotFoundError(2, "No such file or directory"))

    with pytest.raises(ObsConnectError):
        native_linux(tmp_path, runner=runner).launch()


# --- the default process runner ----------------------------------------------------------------


def test_the_default_runner_returns_exit_code_and_stdout():
    code, out = discovery.SubprocessRunner().run([sys.executable, "-c", "print('hi'); raise SystemExit(3)"])

    assert (code, out.strip()) == (3, "hi")


def test_the_default_runner_reports_a_missing_program_as_minus_one(tmp_path):
    assert discovery.SubprocessRunner().run([str(tmp_path / "no-such-program")]) == (-1, "")


def test_the_default_runner_spawns_in_the_given_folder(tmp_path):
    marker = tmp_path / "started"
    discovery.SubprocessRunner().spawn([sys.executable, "-c", "open('started', 'w').close()"], tmp_path)

    deadline = time.monotonic() + 10
    while not marker.exists():
        assert time.monotonic() < deadline, "the spawned program never ran"
        time.sleep(0.02)


def test_the_default_runner_raises_when_a_program_cannot_start(tmp_path):
    with pytest.raises(OSError):
        discovery.SubprocessRunner().spawn([str(tmp_path / "no-such-program")], None)


# --- wait_ready --------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[], None] = lambda: None

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        self.on_sleep()


class ScriptedProbe:
    """Answers each ``GetVersion`` probe from ``answers``, raising an exception answer; the last answer repeats."""

    def __init__(self, answers: Sequence[bool | Exception]) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[ObsCredentials, float]] = []

    def __call__(self, creds: ObsCredentials, timeout_s: float) -> bool:
        self.seen.append((creds, timeout_s))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


REJECTED = ObsAuthError("OBS rejected the websocket password")


def ready_obs(tmp_path: Path, probe: Callable[[ObsCredentials, float], bool], clock: FakeClock, **kwargs: object):
    return native_linux(tmp_path, probe=probe, now=clock.now, sleep=clock.sleep, **kwargs)


async def test_ready_at_once_when_get_version_succeeds(tmp_path):
    write_ws(native_root(), FULL)
    clock, probe = FakeClock(), ScriptedProbe([True])

    assert await ready_obs(tmp_path, probe, clock).wait_ready()

    assert clock.sleeps == []
    assert probe.seen == [
        (ObsCredentials(host="127.0.0.1", port=4466, password="s3cretPassw0rd12"), discovery.PROBE_TIMEOUT_S)
    ]


async def test_not_ready_answers_are_retried_until_get_version_succeeds(tmp_path):
    """207 ``NotReady`` while OBS loads: the probe answers False until OBS has loaded."""
    write_ws(native_root(), FULL)
    clock, probe = FakeClock(), ScriptedProbe([False, False, True])

    assert await ready_obs(tmp_path, probe, clock).wait_ready()

    assert clock.sleeps == [discovery.READY_POLL_S, discovery.READY_POLL_S]


async def test_gives_up_after_the_timeout(tmp_path):
    write_ws(native_root(), FULL)
    clock, probe = FakeClock(), ScriptedProbe([False])

    assert not await ready_obs(tmp_path, probe, clock).wait_ready(timeout_s=2.0)

    assert clock.t == 1002.0
    assert len(probe.seen) == 1 + round(2.0 / discovery.READY_POLL_S)  # the last probe comes at the deadline


async def test_the_last_sleep_ends_at_the_deadline(tmp_path):
    write_ws(native_root(), FULL)
    clock = FakeClock()

    assert not await ready_obs(tmp_path, ScriptedProbe([False]), clock).wait_ready(timeout_s=0.7)

    assert clock.t == pytest.approx(1000.7)
    assert clock.sleeps[-1] == pytest.approx(0.7 - discovery.READY_POLL_S)


async def test_the_timeout_defaults_to_30_seconds(tmp_path):
    write_ws(native_root(), FULL)
    clock = FakeClock()

    assert not await ready_obs(tmp_path, ScriptedProbe([False]), clock).wait_ready()

    assert clock.t == 1030.0


async def test_waits_while_obs_has_not_written_its_websocket_config_yet(tmp_path):
    clock, probe = FakeClock(), ScriptedProbe([True])
    clock.on_sleep = lambda: write_ws(native_root(), FULL)

    assert await ready_obs(tmp_path, probe, clock).wait_ready()

    assert len(clock.sleeps) == 1 and len(probe.seen) == 1


async def test_reads_the_current_settings_at_every_probe(tmp_path):
    write_ws(native_root(), FULL)
    settings = [AppConfig(), AppConfig(obs=ObsSettings(password_override="typed"))]
    clock, probe = FakeClock(), ScriptedProbe([False, True])
    obs = LocalObsDiscovery(
        lambda: settings[min(len(probe.seen), 1)],
        which=which_from({"obs": "/usr/bin/obs"}),
        runner=FakeRunner(),
        probe=probe,
        now=clock.now,
        sleep=clock.sleep,
    )

    assert await obs.wait_ready()

    assert [creds.password for creds, _ in probe.seen] == ["s3cretPassw0rd12", "typed"]


async def test_a_rejected_password_is_read_again_once_then_raised(tmp_path):
    """Spec 17: a failed authentication re-reads ``config.json`` once, then asks for the override."""
    write_ws(native_root(), FULL)
    clock, probe = FakeClock(), ScriptedProbe([REJECTED])

    with pytest.raises(ObsAuthError):
        await ready_obs(tmp_path, probe, clock).wait_ready()

    assert len(probe.seen) == 2
    assert clock.sleeps == [discovery.READY_POLL_S]


async def test_a_password_accepted_after_the_re_read_is_ready(tmp_path):
    write_ws(native_root(), {**FULL, "server_password": "old"})
    clock, probe = FakeClock(), ScriptedProbe([REJECTED, True])
    clock.on_sleep = lambda: write_ws(native_root(), FULL)  # OBS wrote its config after the first read

    assert await ready_obs(tmp_path, probe, clock).wait_ready()

    assert [creds.password for creds, _ in probe.seen] == ["old", "s3cretPassw0rd12"]


async def test_only_consecutive_rejections_raise(tmp_path):
    write_ws(native_root(), FULL)
    clock, probe = FakeClock(), ScriptedProbe([REJECTED, False, REJECTED, True])

    assert await ready_obs(tmp_path, probe, clock).wait_ready()

    assert len(probe.seen) == 4


# --- the default GetVersion probe, against a minimal obs-websocket server on loopback ----------

SALT = "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI="
CHALLENGE = "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY="


def auth_string(password: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + SALT).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + CHALLENGE.encode()).digest()).decode()


class MiniObs:
    """Just enough obs-websocket v5 for a ``GetVersion`` probe.

    Hello (with auth when ``password`` is set), Identify (closing with 4009 on a wrong answer, as
    obs-websocket does, or with 1001 on every Identify when ``drop_identify``), then one
    ``requestStatus`` per request from ``codes``: 100 succeeds, 207 is ``NotReady``. The last code
    repeats.
    """

    def __init__(self, codes: Sequence[int], password: str | None = None, *, drop_identify: bool = False) -> None:
        self.codes = list(codes)
        self.password = password
        self.drop_identify = drop_identify
        self.identifies = 0
        self.requests: list[str] = []
        self._server: Server | None = None

    async def __aenter__(self) -> "MiniObs":
        self._server = await serve(self._handle, "127.0.0.1", 0, ping_interval=None)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(self, ws: ServerConnection) -> None:
        hello: dict[str, object] = {"obsWebSocketVersion": "5.7.4", "rpcVersion": 1}
        if self.password is not None:
            hello["authentication"] = {"challenge": CHALLENGE, "salt": SALT}
        await ws.send(json.dumps({"op": 0, "d": hello}))
        identify = json.loads(await ws.recv())["d"]
        self.identifies += 1
        if self.drop_identify:
            await ws.close(1001, "Server stopping.")
            return
        if self.password is not None and identify.get("authentication") != auth_string(self.password):
            await ws.close(4009, "Authentication failed.")
            return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for message in ws:
            request = json.loads(message)["d"]
            self.requests.append(request["requestType"])
            code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
            response: dict[str, object] = {
                "requestType": request["requestType"],
                "requestId": request["requestId"],
                "requestStatus": {"result": code == 100, "code": code},
            }
            if code == 100:
                response["responseData"] = {"obsVersion": "32.2.2", "availableRequests": ["GetVersion"]}
            else:
                response["requestStatus"]["comment"] = "OBS is not ready to perform the request."  # type: ignore[index]
            await ws.send(json.dumps({"op": 7, "d": response}))


async def probe(creds: ObsCredentials, timeout_s: float = 2.0) -> bool:
    return await asyncio.to_thread(discovery.get_version_succeeds, creds, timeout_s)


async def test_the_probe_succeeds_when_get_version_does():
    async with MiniObs([100]) as obs:
        assert await probe(ObsCredentials("127.0.0.1", obs.port))

    assert obs.requests == ["GetVersion"]


async def test_the_probe_fails_on_not_ready():
    async with MiniObs([207]) as obs:
        assert not await probe(ObsCredentials("127.0.0.1", obs.port))

    assert obs.requests == ["GetVersion"]


async def test_the_probe_authenticates_with_the_password():
    async with MiniObs([100], password="s3cretPassw0rd12") as obs:
        assert await probe(ObsCredentials("127.0.0.1", obs.port, "s3cretPassw0rd12"))


@pytest.mark.parametrize("password", ["wrong", None])
async def test_the_probe_raises_when_obs_rejects_the_password(password):
    started = time.monotonic()
    async with MiniObs([100], password="s3cretPassw0rd12") as obs:
        with pytest.raises(ObsAuthError):
            await probe(ObsCredentials("127.0.0.1", obs.port, password))

    assert obs.requests == []
    # The probe closed its socket: the server did not wait out its 10 s close timeout.
    assert time.monotonic() - started < 5


async def test_a_dropped_identify_without_auth_is_not_an_auth_failure():
    async with MiniObs([100], drop_identify=True) as obs:
        assert not await probe(ObsCredentials("127.0.0.1", obs.port))

    assert obs.identifies == 1


async def test_the_probe_fails_when_nothing_listens():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again before the probe: nothing listens there

    assert not await probe(ObsCredentials("127.0.0.1", port))


async def test_the_probe_gives_up_on_a_server_that_never_answers():
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # never replies to the websocket handshake
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        assert not await probe(ObsCredentials("127.0.0.1", port), timeout_s=0.3)
        assert time.monotonic() - started < 5
    finally:
        server.close()
        await server.wait_closed()


async def test_the_probe_logs_no_password_and_no_errors(caplog):
    caplog.set_level(logging.DEBUG)
    async with MiniObs([207, 100], password="s3cretPassw0rd12") as obs:
        creds = ObsCredentials("127.0.0.1", obs.port, "s3cretPassw0rd12")
        assert not await probe(creds)
        assert await probe(creds)
        with pytest.raises(ObsAuthError):
            await probe(ObsCredentials("127.0.0.1", obs.port, "wrong"))

    assert "s3cretPassw0rd12" not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_wait_ready_polls_obs_until_it_has_loaded(tmp_path):
    async with MiniObs([207, 207, 100], password="s3cretPassw0rd12") as server:
        write_ws(native_root(), {**FULL, "server_port": server.port})
        obs = native_linux(tmp_path, sleep=lambda seconds: asyncio.sleep(0))

        assert await obs.wait_ready(timeout_s=10)

    assert server.requests == ["GetVersion"] * 3


async def test_wait_ready_raises_when_obs_rejects_a_stale_password_override(tmp_path):
    """Spec 17 row 4: the password-override banner after one re-read, not the 30 s timeout."""
    async with MiniObs([100], password="s3cretPassw0rd12") as server:
        write_ws(native_root(), {**FULL, "server_port": server.port})
        stale = AppConfig(obs=ObsSettings(password_override="stale"))
        obs = native_linux(tmp_path, cfg=stale, sleep=lambda seconds: asyncio.sleep(0))

        with pytest.raises(ObsAuthError):
            await obs.wait_ready(timeout_s=10)

    assert server.identifies == 2
    assert server.requests == []
