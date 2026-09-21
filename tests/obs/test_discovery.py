"""``obs/discovery.py``: install lookup, config roots, the websocket config, launch and readiness (spec 11.1, 17)."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from anki_miner_game.models.config import AppConfig, ObsSettings
from anki_miner_game.models.obs import ObsConfigError, ObsCredentials, ObsInstall, WsConfig
from anki_miner_game.obs import discovery
from anki_miner_game.obs.discovery import LocalObsDiscovery

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
