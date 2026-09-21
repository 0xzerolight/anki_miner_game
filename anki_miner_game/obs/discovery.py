"""Find, configure and start the user's OBS (spec 3.3, 11.1, 17 rows 1-3).

``LocalObsDiscovery`` implements ``interfaces.obs.ObsDiscovery`` for this machine:

- ``find_install``: Windows ``%ProgramFiles%\\obs-studio\\bin\\64bit\\obs64.exe``, then the folder the
  OBS installer records in the registry (64-bit view, then 32-bit view); Linux ``obs`` on ``PATH``,
  then the Flatpak ``com.obsproject.Studio``. Each call looks again (the wizard's re-check).
- ``config_root``: the config root of the install the latest ``find_install`` found: Windows
  ``%APPDATA%\\obs-studio``, native Linux ``$XDG_CONFIG_HOME/obs-studio`` (``~/.config/obs-studio``
  when unset), Flatpak ``~/.var/app/com.obsproject.Studio/config/obs-studio``.
- ``read_ws_config`` / ``credentials``: ``plugin_config/obs-websocket/config.json`` under that root,
  read at every call. A key that is missing or of the wrong JSON type takes obs-websocket's own
  default, as ``Config::Load`` does (``docs/m0/source-findings.md`` section 5).

The password is never logged; ``WsConfig`` and ``ObsCredentials`` keep it out of ``repr``.

Ported in part from GameSentenceMiner ``GameSentenceMiner/obs/launch.py``
(``get_obs_websocket_config_values``, ``_resolve_obs_launch_command``) at commit ``479747fe``:
the config file is the source of truth for port and password, read as ``utf-8-sig``, and turned on
by setting ``server_enabled``. Unlike GSM, the file is written only while OBS is closed (obs-websocket
reads it once at start) and all other keys are kept.
"""

import json
import logging
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePath
from typing import Any, Final, Protocol

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.obs import ObsConfigError, ObsCredentials, ObsInstall, WsConfig

log = logging.getLogger(__name__)

# Values that M0 runtime checks may still change live here and nowhere else.
# Provisional until R2 (Flatpak, Linux) or H5 (Windows) confirms them at runtime; the source
# evidence is docs/m0/source-findings.md sections 3-5.
FLATPAK_APP_ID: Final = "com.obsproject.Studio"
FLATPAK_CONFIG_ROOT: Final = PurePath(".var", "app", FLATPAK_APP_ID, "config", "obs-studio")
"""Relative to the home folder: Flatpak points ``XDG_CONFIG_HOME`` at ``~/.var/app/<id>/config`` [R2]."""
LAUNCH_FLAGS: Final = ("--minimize-to-tray",)
"""Appended to ``ObsInstall.argv``; ``flatpak run <id>`` passes trailing arguments to ``obs`` [R2]."""
REGISTRY_KEY: Final = r"SOFTWARE\OBS Studio"
"""Under ``HKEY_LOCAL_MACHINE``; its default value is the install folder [H5]."""
REGISTRY_VIEWS: Final = ("64", "32")
"""The installer writes the key in both views; the 64-bit one is read first [H5]."""
WINDOWS_EXE: Final = PurePath("bin", "64bit", "obs64.exe")
"""Relative to the install folder; OBS must start with its folder as the working directory."""

WS_CONFIG_PATH: Final = PurePath("plugin_config", "obs-websocket", "config.json")
"""Relative to the config root."""
DEFAULT_WS_PORT: Final = 4455
"""obs-websocket's defaults (``src/Config.h``): server off, port 4455, auth on, no password."""
DEFAULT_WS_ENABLED: Final = False
DEFAULT_WS_AUTH_REQUIRED: Final = True


class ProcessRunner(Protocol):
    """How discovery runs the helper tools it needs and starts OBS."""

    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        """Run to completion: ``(exit code, stdout)``; ``(-1, "")`` when it cannot start or times out."""
        ...

    def spawn(self, argv: Sequence[str], cwd: Path | None) -> None:
        """Start a program that outlives this app, never waited for; ``OSError`` when it cannot start."""
        ...


RegistryReader = Callable[[str], str | None]
"""The default value of ``HKLM\\SOFTWARE\\OBS Studio`` in one registry view (``"64"`` or ``"32"``)."""


def read_registry_install_dir(view: str) -> str | None:
    """``HKLM\\SOFTWARE\\OBS Studio`` (default value) in ``view``; ``None`` off Windows or when absent."""
    if sys.platform != "win32":
        return None
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if view == "64" else winreg.KEY_WOW64_32KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_KEY, 0, access) as key:
            value, kind = winreg.QueryValueEx(key, None)
    except OSError:
        return None
    if not isinstance(value, str) or not value:
        return None
    if kind == winreg.REG_EXPAND_SZ:
        return winreg.ExpandEnvironmentStrings(value)
    return value if kind == winreg.REG_SZ else None


class LocalObsDiscovery:
    """``ObsDiscovery`` for the OBS installed on this machine (spec 11.1).

    ``config`` returns the app's current settings; ``wait_ready`` reads its credentials through it.
    Everything else is injected for tests: ``platform`` (``sys.platform``), ``which`` (``PATH``
    lookup), ``registry`` (Windows install folder), ``runner`` (``flatpak``, ``tasklist``, starting
    OBS) and ``proc_root`` (Linux process table).
    """

    def __init__(
        self,
        config: Callable[[], AppConfig],
        *,
        platform: str = sys.platform,
        which: Callable[[str], str | None] = shutil.which,
        registry: RegistryReader = read_registry_install_dir,
        runner: ProcessRunner,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._config = config
        self._windows = platform == "win32"
        self._which = which
        self._registry = registry
        self._runner = runner
        self._proc_root = proc_root
        self._install: ObsInstall | None = None
        self._looked = False

    # --- install and config root -----------------------------------------------------------

    def find_install(self) -> ObsInstall | None:
        install = self._find_windows() if self._windows else self._find_linux()
        self._install, self._looked = install, True
        if install is None:
            log.info("OBS not found")
        else:
            log.info("OBS found: %s", " ".join(install.argv))
        return install

    def _find_windows(self) -> ObsInstall | None:
        for folder in self._windows_folders():
            exe = folder / WINDOWS_EXE
            if exe.is_file():
                return ObsInstall(argv=(str(exe),), cwd=exe.parent, flatpak=False)
        return None

    def _windows_folders(self) -> Iterator[Path]:
        program_files = os.environ.get("PROGRAMFILES")
        if program_files:
            yield Path(program_files) / "obs-studio"
        for view in REGISTRY_VIEWS:
            folder = self._registry(view)
            if folder:
                yield Path(folder)

    def _find_linux(self) -> ObsInstall | None:
        obs = self._which("obs")
        if obs:
            return ObsInstall(argv=(obs,), cwd=None, flatpak=False)
        flatpak = self._which("flatpak")
        if flatpak and self._runner.run([flatpak, "info", FLATPAK_APP_ID])[0] == 0:
            return ObsInstall(argv=(flatpak, "run", FLATPAK_APP_ID), cwd=None, flatpak=True)
        return None

    def config_root(self) -> Path | None:
        """The config root of the install the latest ``find_install`` found (looking once if none ran)."""
        install = self._install if self._looked else self.find_install()
        if install is None:
            return None
        if install.flatpak:
            return Path.home() / FLATPAK_CONFIG_ROOT
        if self._windows:
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
            return base / "obs-studio"
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return (Path(xdg) if xdg else Path.home() / ".config") / "obs-studio"

    # --- websocket config ------------------------------------------------------------------

    def read_ws_config(self) -> WsConfig | None:
        path = self._ws_config_path()
        if path is None:
            return None
        data = _load_json(path)
        return None if data is None else _ws_config(path, data)

    def _ws_config_path(self) -> Path | None:
        root = self.config_root()
        return None if root is None else root / WS_CONFIG_PATH

    def credentials(self, cfg: AppConfig) -> ObsCredentials:
        override = cfg.obs
        try:
            ws = self.read_ws_config()
        except ObsConfigError:
            if override.port is None:
                raise
            ws = None
        if override.port is not None:
            port = override.port
        elif ws is not None:
            port = ws.port
        else:
            where = self._ws_config_path() or "OBS's websocket config.json"
            raise ObsConfigError(f"{where}: not found, and no OBS port override is set")
        password: str | None = None
        if override.password_override is not None:
            password = override.password_override
        elif ws is not None and ws.auth_required:
            password = ws.password
        return ObsCredentials(host=override.host, port=port, password=password)


def _load_json(path: Path) -> dict[str, Any] | None:
    """The file's JSON object; ``None`` when the file does not exist; ``ObsConfigError`` otherwise."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ObsConfigError(f"{path}: not UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise ObsConfigError(f"{path}: cannot be read ({exc.strerror or type(exc).__name__})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ObsConfigError(f"{path}: not JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(data, dict):
        raise ObsConfigError(f"{path}: not a JSON object")
    return data


def _ws_config(path: Path, data: dict[str, Any]) -> WsConfig:
    """Apply ``Config::Load``'s rules: a key that is missing or of the wrong type keeps its default."""
    port = data.get("server_port", DEFAULT_WS_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        port = DEFAULT_WS_PORT
    elif not 1 <= port <= 65535:
        raise ObsConfigError(f"{path}: server_port {port} is not a TCP port")
    password = data.get("server_password")
    return WsConfig(
        server_enabled=_bool(data, "server_enabled", DEFAULT_WS_ENABLED),
        port=port,
        password=password if isinstance(password, str) and password else None,
        auth_required=_bool(data, "auth_required", DEFAULT_WS_AUTH_REQUIRED),
    )


def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    return value if isinstance(value, bool) else default
