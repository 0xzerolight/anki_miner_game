"""Find, configure and start the user's OBS (spec 3.3, 11.1, 17 rows 1-3).

``LocalObsDiscovery`` implements ``interfaces.obs.ObsDiscovery`` for this machine:

- ``find_install``: Windows ``%ProgramFiles%\\obs-studio\\bin\\64bit\\obs64.exe``, then the folder the
  OBS installer records in the registry, then the folder of Steam's OBS build from Steam's Uninstall
  entry (each in the 64-bit view, then the 32-bit view); Linux ``obs`` on ``PATH``, then the Flatpak
  ``com.obsproject.Studio``. Each call looks again (the wizard's re-check).
- ``config_root``: the config root of the install the latest ``find_install`` found: Windows
  ``%APPDATA%\\obs-studio``, native Linux ``$XDG_CONFIG_HOME/obs-studio`` (``~/.config/obs-studio``
  when unset), Flatpak ``~/.var/app/com.obsproject.Studio/config/obs-studio``.
- ``read_ws_config`` / ``credentials``: ``plugin_config/obs-websocket/config.json`` under that root,
  read at every call. A key that is missing or of the wrong JSON type takes obs-websocket's own
  default, as ``Config::Load`` does (``docs/m0/source-findings.md`` section 5).
- ``ensure_server_enabled``: only while OBS is closed, since obs-websocket reads the file once at
  start. Sets ``server_enabled``, keeps every other key, and adds a password only when auth is
  required and none exists. A missing file is created that way.
- ``is_running``: an ``obs`` process of this user in ``/proc`` (native and Flatpak alike), or
  ``obs64.exe`` in ``tasklist`` on Windows; ``True`` when the process list cannot be read.
- ``launch``: the install's command plus ``--minimize-to-tray``, detached, in the folder it needs;
  nothing while OBS already runs.
- ``wait_ready``: ready once ``GetVersion`` succeeds on a fresh connection. Until OBS has loaded,
  obs-websocket accepts connections and answers every request with 207 ``NotReady``
  (``docs/m0/source-findings.md`` summary 11), so a refused connection, a failed handshake and a
  non-success answer all mean "not yet", with the credentials read again before every try. A
  rejected password is not "not yet": after one re-read it raises ``ObsAuthError`` (spec 17).

The probe uses obsws-python's shared connection class (``obsws_python.baseclient.ObsClient``) rather
than ``ReqClient``, which logs every failed identify or request at ERROR: a probe fails many times
by design. obsws-python logs the password at INFO when it connects, so its logger is held at
WARNING (``docs/m0/source-findings.md`` section 11).

The password is never logged; ``WsConfig`` and ``ObsCredentials`` keep it out of ``repr``.

Ported in part from GameSentenceMiner ``GameSentenceMiner/obs/launch.py`` at commit ``479747fe``:
``get_obs_websocket_config_values`` (the config file is the source of truth for port and password,
read as ``utf-8-sig``, turned on by setting ``server_enabled``) and ``_resolve_obs_launch_command``
(OBS starts in its executable's folder). Unlike GSM, the file is written only while OBS is closed
(obs-websocket reads it once at start), every other key is kept, and nothing from it is copied into
the app's own config.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path, PurePath
from typing import Any, Final, Protocol

from obsws_python.baseclient import ObsClient
from obsws_python.error import OBSSDKError
from websocket import WebSocketException

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsCredentials,
    ObsInstall,
    WsConfig,
)
from anki_miner_game.runtime.child_env import child_environ
from anki_miner_game.store import write_text_atomic

log = logging.getLogger(__name__)

_obsws_log = logging.getLogger("obsws_python")
if _obsws_log.level < logging.WARNING:  # NOTSET too: it logs the password at INFO when it connects
    _obsws_log.setLevel(logging.WARNING)

FLATPAK_APP_ID: Final = "com.obsproject.Studio"

# Values that M0 runtime checks may still change live here and nowhere else.
# PROVISIONAL until R2 (Flatpak, Linux) or H5 (Windows) confirms them at runtime; the source
# evidence is docs/m0/source-findings.md sections 3-5.
FLATPAK_CONFIG_ROOT: Final = PurePath(".var", "app", FLATPAK_APP_ID, "config", "obs-studio")
"""Relative to the home folder: Flatpak points ``XDG_CONFIG_HOME`` at ``~/.var/app/<id>/config`` [R2]."""
LAUNCH_FLAGS: Final = ("--minimize-to-tray",)
"""Appended to ``ObsInstall.argv``; ``flatpak run <id>`` passes trailing arguments to ``obs`` [R2]."""
REGISTRY_KEY: Final = r"SOFTWARE\OBS Studio"
"""Under ``HKEY_LOCAL_MACHINE``; its default value is the install folder [H5]."""
REGISTRY_VIEWS: Final = ("64", "32")
"""The installer writes the key in both views; the 64-bit one is read first [H5]."""
STEAM_APP_ID: Final = 1905180
"""OBS Studio on Steam."""
STEAM_UNINSTALL_KEY: Final = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {STEAM_APP_ID}"
"""Under ``HKEY_LOCAL_MACHINE``; its ``InstallLocation`` value is the folder of Steam's OBS build. That
build writes ``REGISTRY_KEY`` only from its Steam install script, which the install itself does not
run: H5 found no key after a Steam install, only this entry [H5]."""
REGISTRY_INSTALL_DIRS: Final = ((REGISTRY_KEY, ""), (STEAM_UNINSTALL_KEY, "InstallLocation"))
"""``(key, value name)`` pairs whose value is an install folder, read in this order; ``""`` is the
default value."""
WINDOWS_EXE: Final = PurePath("bin", "64bit", "obs64.exe")
"""Relative to the install folder; OBS must start with its folder as the working directory [H5]."""
WINDOWS_PROCESS: Final = "obs64.exe"
TASKLIST: Final = ("tasklist", "/FI", f"IMAGENAME eq {WINDOWS_PROCESS}", "/FO", "CSV", "/NH")
"""Lists a running ``obs64.exe`` as a CSV row that starts with the quoted image name [H5]."""
# End of the provisional values.

WS_CONFIG_PATH: Final = PurePath("plugin_config", "obs-websocket", "config.json")
"""Relative to the config root."""
DEFAULT_WS_PORT: Final = 4455
"""obs-websocket's defaults (``src/Config.h``): server off, port 4455, auth on, no password."""
DEFAULT_WS_ENABLED: Final = False
DEFAULT_WS_AUTH_REQUIRED: Final = True
PASSWORD_ALPHABET: Final = string.ascii_letters + string.digits
PASSWORD_LENGTH: Final = 16
"""What obs-websocket's own ``Utils::Crypto::GeneratePassword`` makes."""

LINUX_PROCESS: Final = "obs"
"""``comm`` of the OBS main process, native or Flatpak (the Flatpak's command is ``obs``)."""
RUN_TIMEOUT_S: Final = 10.0
READY_POLL_S: Final = 0.5
"""Pause between two ``GetVersion`` tries in ``wait_ready``."""
PROBE_TIMEOUT_S: Final = 2.0
"""Connect and answer timeout of one ``GetVersion`` try."""

_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED: Final[int] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ProcessRunner(Protocol):
    """How discovery runs the helper tools it needs and starts OBS."""

    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        """Run to completion: ``(exit code, stdout)``; ``(-1, "")`` when it cannot start or times out."""
        ...

    def spawn(self, argv: Sequence[str], cwd: Path | None) -> None:
        """Start a program that outlives this app, never waited for; ``OSError`` when it cannot start."""
        ...


class SubprocessRunner:
    """The real ``ProcessRunner``: no console window on Windows, OBS in its own session on POSIX, and
    the environment of ``runtime.child_env`` (a frozen build's library path is not the child's)."""

    def run(self, argv: Sequence[str]) -> tuple[int, str]:
        try:
            done = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=RUN_TIMEOUT_S,
                creationflags=_NO_WINDOW,
                env=child_environ(),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return -1, ""
        return done.returncode, done.stdout

    def spawn(self, argv: Sequence[str], cwd: Path | None) -> None:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED,
            env=child_environ(),
            start_new_session=True,  # POSIX: a signal to this app's process group never reaches OBS
        )
        # Reap OBS when it exits before this app does, so it leaves no zombie behind.
        threading.Thread(target=proc.wait, name="obs-reaper", daemon=True).start()


RegistryReader = Callable[[str, str, str], str | None]
"""A string value under ``HKEY_LOCAL_MACHINE``: key, value name (``""`` for the default value) and
registry view (``"64"`` or ``"32"``)."""
Probe = Callable[[ObsCredentials, float], bool]
"""One ``GetVersion`` with a timeout in seconds; ``True`` when it succeeds, ``ObsAuthError`` when OBS
rejects the password. Runs on a worker thread."""


def get_version_succeeds(creds: ObsCredentials, timeout_s: float) -> bool:
    """One ``GetVersion`` on a fresh connection; ``False`` for any failure but a rejected password.

    Raises ``ObsAuthError`` when OBS asked for authentication and the handshake failed: obs-websocket
    closes the connection (4009) on a wrong password, and obsws-python refuses to send none.
    """
    try:
        # obsws-python logs a refused connection at ERROR with a traceback: look quietly first.
        socket.create_connection((creds.host, creds.port), timeout=timeout_s).close()
        client = ObsClient(host=creds.host, port=creds.port, password=creds.password or "", timeout=timeout_s)
        try:
            try:
                client.authenticate()
            except OBSSDKError as exc:
                if "authentication" in client.server_hello["d"]:
                    raise ObsAuthError("OBS rejected the websocket password") from exc
                raise
            status = client.req("GetVersion")["requestStatus"]
        finally:
            client.ws.close()
            client.ws.shutdown()  # close() leaves the socket open once the server has sent its close frame
    except (OSError, ValueError, LookupError, TypeError, OBSSDKError, WebSocketException) as exc:
        log.debug("OBS not ready: %s: %s", type(exc).__name__, exc)
        return False
    if not isinstance(status, dict) or status.get("result") is not True:
        log.debug("OBS not ready: GetVersion answered %s", status)
        return False
    return True


def read_registry_value(key: str, name: str, view: str) -> str | None:
    """``HKLM\\<key>`` value ``name`` in ``view``; ``None`` off Windows, when absent or not a string."""
    if sys.platform != "win32":
        return None
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WOW64_64KEY if view == "64" else winreg.KEY_WOW64_32KEY)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, access) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    if not isinstance(value, str) or not value:
        return None
    if kind == winreg.REG_EXPAND_SZ:
        return winreg.ExpandEnvironmentStrings(value)
    return value if kind == winreg.REG_SZ else None


class LocalObsDiscovery:
    """``ObsDiscovery`` for the OBS installed on this machine (spec 11.1).

    ``config`` returns the app's current settings; ``wait_ready`` reads its credentials through it,
    on a worker thread.
    Everything else is injected for tests: ``platform`` (``sys.platform``), ``which`` (``PATH``
    lookup), ``registry`` (Windows install folder), ``runner`` (``flatpak``, ``tasklist``, starting
    OBS), ``proc_root`` (Linux process table), ``probe`` (one ``GetVersion``), ``now`` and ``sleep``.
    """

    def __init__(
        self,
        config: Callable[[], AppConfig],
        *,
        platform: str = sys.platform,
        which: Callable[[str], str | None] = shutil.which,
        registry: RegistryReader = read_registry_value,
        runner: ProcessRunner | None = None,
        proc_root: Path = Path("/proc"),
        probe: Probe = get_version_succeeds,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._windows = platform == "win32"
        self._which = which
        self._registry = registry
        self._runner = runner or SubprocessRunner()
        self._proc_root = proc_root
        self._probe = probe
        self._now = now
        self._sleep = sleep
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
        for key, name in REGISTRY_INSTALL_DIRS:
            for view in REGISTRY_VIEWS:
                folder = self._registry(key, name, view)
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

    def _found(self) -> ObsInstall | None:
        """What the latest ``find_install`` found, looking once if none ran."""
        return self._install if self._looked else self.find_install()

    def config_root(self) -> Path | None:
        """The config root of the install the latest ``find_install`` found (looking once if none ran)."""
        install = self._found()
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

    def ensure_server_enabled(self) -> bool:
        """Turn the websocket server on while OBS is closed; ``False`` while it is off and OBS runs.

        Also ``False`` without an install. Raises ``ObsConfigError`` when the file cannot be read,
        parsed or written; an unusable file is never overwritten.
        """
        path = self._ws_config_path()
        if path is None:
            return False
        data = _load_json(path)
        if data is not None and _ws_config(path, data).server_enabled:
            return True
        if self.is_running():
            log.info("OBS websocket server is off while OBS runs; %s left alone", path)
            return False
        if data is None:
            data = {
                "first_load": False,  # as obs-websocket leaves it after its own first load
                "server_enabled": DEFAULT_WS_ENABLED,
                "server_port": DEFAULT_WS_PORT,
                "alerts_enabled": False,
                "auth_required": DEFAULT_WS_AUTH_REQUIRED,
            }
        current = _ws_config(path, data)
        data["server_enabled"] = True
        if current.auth_required and current.password is None:
            data["server_password"] = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))
        try:
            write_text_atomic(path, json.dumps(data, indent=4) + "\n", mode=0o600)
        except OSError as exc:
            raise ObsConfigError(f"{path}: cannot be written ({exc.strerror or type(exc).__name__})") from exc
        log.info("OBS websocket server turned on in %s", path)
        return True

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

    # --- process ---------------------------------------------------------------------------

    def is_running(self) -> bool:
        """Whether an OBS of this user runs; ``True`` when the process list cannot be read.

        "Cannot tell" never reads as "not running": a caller would end a recording OBS is still
        writing, or ``launch`` would start a second OBS.
        """
        if self._windows:
            code, out = self._runner.run(TASKLIST)
            if code != 0:
                log.warning("tasklist failed (exit code %s); assuming OBS runs", code)
                return True
            return f'"{WINDOWS_PROCESS}"' in out.lower()
        uid = _own_uid()
        try:
            entries = list(self._proc_root.iterdir())
        except OSError as exc:
            log.warning("cannot list %s (%s); assuming OBS runs", self._proc_root, exc.strerror or type(exc).__name__)
            return True
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if uid is not None and entry.stat().st_uid != uid:
                    continue  # another user's OBS has its own config
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue  # exited meanwhile
            if name == LINUX_PROCESS:
                return True
        return False

    def launch(self) -> None:
        """Start OBS minimised to the tray, in the folder it needs; nothing while OBS already runs.

        A second instance would stop at OBS's modal "already running" question. Raises
        ``ObsConnectError`` when no install is found or the program cannot be started.
        """
        if self.is_running():
            log.info("OBS already runs; not starting another")
            return
        install = self._found()
        if install is None:
            raise ObsConnectError("OBS is not installed")
        argv = [*install.argv, *LAUNCH_FLAGS]
        try:
            self._runner.spawn(argv, install.cwd)
        except OSError as exc:
            raise ObsConnectError(f"OBS could not be started ({exc.strerror or type(exc).__name__})") from exc
        log.info("OBS started: %s", " ".join(argv))

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        """``True`` once ``GetVersion`` succeeds; ``False`` after ``timeout_s``.

        Tries at once, then every ``READY_POLL_S``, the last time at the deadline. Each try reads
        the credentials again (OBS may still be writing its config) and runs on a worker thread.
        Raises ``ObsAuthError`` when OBS rejects the password on two tries in a row, so the
        credentials are read again once before giving up (spec 17).
        """
        deadline = self._now() + timeout_s
        rejected = False
        while True:
            try:
                if await asyncio.to_thread(self._ready_once):
                    return True
                rejected = False
            except ObsAuthError:
                if rejected:
                    log.info("OBS rejected the websocket password again")
                    raise
                rejected = True
                log.info("OBS rejected the websocket password; reading it again")
            remaining = deadline - self._now()
            if remaining <= 0:
                log.info("OBS not ready after %.0f s", timeout_s)
                return False
            await self._sleep(min(READY_POLL_S, remaining))

    def _ready_once(self) -> bool:
        try:
            creds = self.credentials(self._config())
        except ObsConfigError as exc:
            log.debug("OBS not ready: %s", exc)
            return False
        return self._probe(creds, PROBE_TIMEOUT_S)


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


def _own_uid() -> int | None:
    """This process's user id; ``None`` where there is none (Windows)."""
    getuid = getattr(os, "getuid", None)
    return None if getuid is None else int(getuid())
