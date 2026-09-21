"""Isolated nested X11 display for the M0 spikes (Linux, KDE Plasma host).

Starts ``kwin_wayland --virtual --xwayland``: a compositor that renders to an off-screen
framebuffer, with a rootless Xwayland in it (optionally a rootful Xwayland on top). OBS, the
sync-probe flasher and owocr run inside as X11 clients, so nothing they open reaches the owner's
desktop and ``xcomposite_input`` can capture their windows.

Isolation, each rule learned from an incident on the owner's machine (2026-09-21):

- An unisolated nested kwin rewrote the owner's ``~/.config/kwinrc`` and
  ``kwinoutputconfig.json``, and KDE then regenerated ``gtkrc``, ``Trolltech.conf`` and the GTK
  ``settings.ini`` files. So every nested process gets private ``XDG_CONFIG_HOME``,
  ``XDG_CACHE_HOME``, ``XDG_DATA_HOME`` and ``XDG_STATE_HOME`` under
  ``.orchestration/m0/data/<caller>/xdg`` (the only place the orchestrator's watchdog accepts),
  and a private ``XDG_RUNTIME_DIR``, ``/tmp/amg-<random>``: Flatpak's bus-proxy socket path does
  not fit in ``sun_path`` under the ``.orchestration`` prefix. It is removed at teardown.
- A plain ``dbus-run-session`` auto-activated a second xdg-document-portal, which unmounted the
  owner's ``/run/user/<uid>/doc``. So the tool starts its own ``dbus-daemon`` from a config with no
  ``<servicedir>``: nothing can be activated on it. Its address is the only bus any nested process
  sees.
- Environments are built from scratch (``env -i`` style): ``PATH``, ``LANG`` and ``HOME`` come from
  the owner, everything else is set here. ``SESSION_MANAGER``, ``AT_SPI_BUS_ADDRESS``,
  ``WAYLAND_DISPLAY`` and the owner's ``XAUTHORITY`` never reach a child. X11 clients are also told
  outright to use X11 (``QT_QPA_PLATFORM=xcb`` and friends).
- An owner guard fingerprints the KWin/toolkit config files and the doc-portal mount before the
  display starts, re-checks it while the display runs and after teardown, and fails loudly
  (``IsolationBreachError``, exit code 3) on any change.
- Teardown signals the whole process group (dbus-daemon, kwin, Xwayland, every child started
  through the tool) with SIGTERM, waits, then SIGKILL. Children also carry a marker variable, so
  a descendant that left the group with ``setsid`` is found in ``/proc`` and killed too.

Usage, from the repo root with the project interpreter (see ``tools/m0/README.md``)::

    python tools/nested_display.py run --caller r1-clock -- CMD ARGS...    # display lives as long as CMD
    python tools/nested_display.py serve --caller r1-clock --env-file F     # until SIGTERM/SIGINT
    python tools/nested_display.py exec --env-file F -- CMD ARGS...         # CMD inside a served display
    python tools/nested_display.py guard [--save F | --baseline F]          # owner-state fingerprint
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Any

KWIN = "kwin_wayland"
DBUS_DAEMON = "dbus-daemon"
XWAYLAND = "Xwayland"
WAYLAND_SOCKET = "wayland-amg"
MARKER = "AMG_NESTED_RUN"
EXIT_BREACH = 3
TEARDOWN_SIGNALS = (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM))

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH_DATA = Path(".orchestration") / "m0" / "data"
RUNTIME_BASE = Path("/tmp")  # short on purpose: see RUNTIME_SOCKETS
# The longest socket paths created in XDG_RUNTIME_DIR. Flatpak binds its bus proxy at
# realpath($XDG_RUNTIME_DIR)/.dbus-proxy/session-bus-proxy-XXXXXX (flatpak-run.c,
# create_proxy_socket), so a symlink cannot shorten it.
RUNTIME_SOCKETS = (f"{WAYLAND_SOCKET}.lock", ".dbus-proxy/session-bus-proxy-XXXXXX")

# Owner config files the incidents touched (kwin wrote the first three; KDE regenerated the rest).
GUARDED_CONFIG_FILES = (
    "kwinrc",
    "kwinoutputconfig.json",
    "kglobalshortcutsrc",
    "gtkrc",
    "gtkrc-2.0",
    "Trolltech.conf",
    "xsettingsd/xsettingsd.conf",
    "gtk-3.0/settings.ini",
    "gtk-4.0/settings.ini",
)
PORTAL_FSTYPE = "fuse.portal"

_OWNER_KEEP = ("PATH", "LANG", "HOME")
_X11_CLIENT = {"XDG_SESSION_TYPE": "x11", "QT_QPA_PLATFORM": "xcb", "GDK_BACKEND": "x11", "SDL_VIDEODRIVER": "x11"}
# Variables a caller's --setenv may never set: each one could reconnect a child to the owner session.
PROTECTED_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "WAYLAND_DISPLAY",
        "WAYLAND_SOCKET",
        "DISPLAY",
        "XAUTHORITY",
        "SESSION_MANAGER",
        "AT_SPI_BUS_ADDRESS",
        MARKER,
    }
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SAFE_PATH_RE = re.compile(r"^[\w./-]+$")
_DISPLAY_RE = re.compile(r"^:\d+(\.\d+)?$")
_SUN_PATH_MAX = 107  # sockaddr_un.sun_path is 108 bytes including the NUL


class NestedDisplayError(RuntimeError):
    """The nested display could not be started, used or stopped."""


class IsolationBreachError(RuntimeError):
    """Owner session state changed: stop everything and report it as critical."""

    def __init__(self, changes: Sequence[str]) -> None:
        self.changes = list(changes)
        super().__init__("CRITICAL: owner session state changed:\n  " + "\n  ".join(self.changes))


# --- paths -------------------------------------------------------------------------------------


def main_checkout(checkout: Path = REPO_ROOT) -> Path:
    """The main checkout, also when ``checkout`` is a worktree under ``<main>/.worktrees/``."""
    return checkout.parent.parent if checkout.parent.name == ".worktrees" else checkout


def default_xdg_root(caller: str, checkout: Path = REPO_ROOT) -> Path:
    """``<main>/.orchestration/m0/data/<caller>/xdg``: the per-caller home of the private dirs."""
    if not _SLUG_RE.match(caller):
        raise NestedDisplayError(f"caller must be a lowercase slug such as r1-clock, got {caller!r}")
    return main_checkout(checkout) / ORCH_DATA / caller / "xdg"


@dataclass(frozen=True)
class XdgDirs:
    """The private XDG dirs of one nested display.

    ``root`` holds config, cache, data and state, plus the bus config, logs and reporter.
    ``runtime`` is a separate short dir for the sockets, which must fit in ``sun_path``.
    """

    root: Path
    runtime: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def bus_socket(self) -> Path:
        return self.runtime / "bus"

    def create(self) -> None:
        for path in (self.config, self.cache, self.data, self.state):
            path.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(mode=0o700)  # new, never reused: a name planted in /tmp fails here
        self.runtime.chmod(0o700)  # whatever the umask: kwin and libwayland insist on owner-only

    def env(self) -> dict[str, str]:
        return {
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_DATA_HOME": str(self.data),
            "XDG_STATE_HOME": str(self.state),
            "XDG_RUNTIME_DIR": str(self.runtime),
        }


def check_xdg_root(root: Path, data_base: Path) -> None:
    """Refuse a root the watchdog would flag or kwin would mis-split."""
    if not _SAFE_PATH_RE.match(str(root)):
        raise NestedDisplayError(f"xdg root must not contain whitespace or shell metacharacters: {root}")
    resolved, base = root.resolve(), data_base.resolve()
    if not resolved.is_relative_to(base):
        raise NestedDisplayError(f"xdg root {resolved} is outside {base}, where every nested kwin must keep its dirs")


def check_runtime_dir(runtime: Path) -> None:
    """Refuse a runtime dir whose sockets (kwin's, Flatpak's bus proxy) could not bind."""
    for name in RUNTIME_SOCKETS:
        path = Path(os.path.realpath(runtime)) / name
        if len(os.fsencode(str(path))) > _SUN_PATH_MAX:
            raise NestedDisplayError(f"socket path too long for sun_path ({_SUN_PATH_MAX} bytes): {path}")


def mounts_under(path: Path, mountinfo: str) -> list[str]:
    """Mount points at or below ``path``, from ``/proc/self/mountinfo`` text."""
    points = [_unescape_mount(line.split()[4]) for line in mountinfo.splitlines() if len(line.split()) > 4]
    return [point for point in points if point == str(path) or point.startswith(f"{path}/")]


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def remove_runtime_dir(runtime: Path, mountinfo: str | None = None) -> None:
    """Delete the runtime dir and what the session left in it, unless something is mounted inside.

    A document portal mounts at ``$XDG_RUNTIME_DIR/doc``; deleting through it would delete the
    files it exposes.
    """
    if not runtime.exists():
        return
    if mountinfo is None:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    mounted = mounts_under(runtime, mountinfo)
    if mounted:
        raise NestedDisplayError(f"left {runtime} in place: something is mounted inside it: {', '.join(mounted)}")
    shutil.rmtree(runtime)


# --- the private bus ---------------------------------------------------------------------------


def bus_config(socket_path: Path) -> str:
    """A session-bus config with no ``<servicedir>``: no service can be activated on it."""
    return f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <keep_umask/>
  <listen>unix:path={socket_path}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def dbus_argv(config_path: Path, address_fd: int) -> list[str]:
    return [
        DBUS_DAEMON,
        f"--config-file={config_path}",
        "--nofork",
        "--nopidfile",
        "--nosyslog",
        f"--print-address={address_fd}",
    ]


# --- environments ------------------------------------------------------------------------------


def _owner_basics(owner: Mapping[str, str]) -> dict[str, str]:
    env = {key: owner[key] for key in _OWNER_KEEP if owner.get(key)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("HOME", str(owner_home()))
    return env


def kwin_env(owner: Mapping[str, str], xdg: XdgDirs, bus_address: str, token: str) -> dict[str, str]:
    """Environment for the nested kwin (and the bus): owner basics, private dirs and bus, marker."""
    return {**_owner_basics(owner), **xdg.env(), "DBUS_SESSION_BUS_ADDRESS": bus_address, MARKER: token}


def child_env(
    owner: Mapping[str, str],
    xdg: XdgDirs,
    bus_address: str,
    token: str,
    *,
    display: str,
    xauthority: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for an X11 client of the nested display, built from scratch."""
    for key in extra or {}:
        if key in PROTECTED_KEYS:
            raise NestedDisplayError(f"{key} may not be set by the caller: it belongs to the isolation")
    env = kwin_env(owner, xdg, bus_address, token)
    env["DISPLAY"] = display
    if xauthority:
        env["XAUTHORITY"] = xauthority
    env.update(_X11_CLIENT)
    # Flatpak finds the user installation through XDG_DATA_HOME, which is private here.
    owner_data = owner.get("XDG_DATA_HOME") or str(Path(env["HOME"]) / ".local" / "share")
    env["FLATPAK_USER_DIR"] = str(Path(owner_data) / "flatpak")
    env.update(extra or {})
    return env


# --- kwin, reporter, rootful Xwayland, flatpak ----------------------------------------------


def kwin_argv(socket_name: str, width: int, height: int, reporter: str) -> list[str]:
    """kwin_wayland command line. ``reporter`` (a path without spaces) runs once Xwayland is up."""
    return [
        KWIN,
        "--virtual",
        "--xwayland",
        "--socket",
        socket_name,
        "--width",
        str(width),
        "--height",
        str(height),
        "--no-lockscreen",
        "--no-global-shortcuts",
        "--no-kactivities",
        reporter,
    ]


def reporter_script(report_path: Path) -> str:
    """Script kwin starts after Xwayland is ready: records the DISPLAY and XAUTHORITY it got."""
    tmp = f"{report_path}.tmp"
    return (
        "#!/bin/sh\n"
        f'printf \'DISPLAY=%s\\nXAUTHORITY=%s\\n\' "$DISPLAY" "${{XAUTHORITY-}}" > "{tmp}"'
        f' && mv "{tmp}" "{report_path}"\n'
    )


def parse_report(text: str) -> tuple[str, str | None]:
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    display = values.get("DISPLAY", "")
    if not _DISPLAY_RE.match(display):
        raise NestedDisplayError(f"unexpected DISPLAY from the nested session: {display!r}")
    return display, values.get("XAUTHORITY") or None


def wait_for_report(
    report: Path, *, alive: Callable[[], bool], timeout_s: float, poll_s: float = 0.1
) -> tuple[str, str | None]:
    """Wait for the reporter's file; fail if kwin dies or time runs out."""
    deadline = time.monotonic() + timeout_s
    while True:
        if report.exists():
            return parse_report(report.read_text(encoding="utf-8"))
        if not alive():
            raise NestedDisplayError("kwin_wayland exited before reporting a display")
        if time.monotonic() >= deadline:
            raise NestedDisplayError(f"no display reported within {timeout_s:g} s")
        time.sleep(poll_s)


def rootful_xwayland_argv(width: int, height: int, display_fd: int) -> list[str]:
    """A rootful Xwayland inside the nested kwin: needed for root-window grabs (mss, owocr)."""
    return [XWAYLAND, "-displayfd", str(display_fd), "-geometry", f"{width}x{height}", "-noreset"]


def flatpak_x11_argv(app_id: str, *args: str) -> list[str]:
    """``flatpak run`` as an X11 client of the nested display, never Wayland, no owner portals."""
    return [
        "flatpak",
        "run",
        "--die-with-parent",
        "--nosocket=wayland",
        "--socket=x11",
        "--no-documents-portal",
        "--no-a11y-bus",
        "--env=QT_QPA_PLATFORM=xcb",
        app_id,
        *args,
    ]


# --- owner guard -------------------------------------------------------------------------------


def owner_home() -> Path:
    """The real home from the password database, whatever ``HOME`` says."""
    import pwd  # POSIX only; the module stays importable elsewhere

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def default_doc_path() -> Path:
    return Path(f"/run/user/{os.getuid()}/doc")


def _findmnt(argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)
    return proc.returncode, proc.stdout


@dataclass(frozen=True)
class OwnerFingerprint:
    mtimes: tuple[tuple[str, int | None], ...]  # (path, st_mtime_ns or None when absent)
    doc_path: str
    doc_fstype: str | None

    def to_json(self) -> str:
        data = {"mtimes": dict(self.mtimes), "doc_path": self.doc_path, "doc_fstype": self.doc_fstype}
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> OwnerFingerprint:
        data = json.loads(text)
        return cls(tuple(data["mtimes"].items()), data["doc_path"], data["doc_fstype"])


def compare_fingerprints(before: OwnerFingerprint, after: OwnerFingerprint) -> list[str]:
    def show(ns: int | None) -> str:
        return "absent" if ns is None else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ns / 1e9))

    old = dict(before.mtimes)
    changes = [f"{path}: mtime {show(old.get(path))} -> {show(ns)}" for path, ns in after.mtimes if old.get(path) != ns]
    if after.doc_fstype != before.doc_fstype or after.doc_fstype != PORTAL_FSTYPE:
        changes.append(f"{after.doc_path}: mount {before.doc_fstype or 'none'} -> {after.doc_fstype or 'none'}")
    return changes


class OwnerGuard:
    """Fingerprint of owner state that a nested display must never change."""

    def __init__(
        self,
        home: Path | None = None,
        doc_path: Path | None = None,
        run: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> None:
        self.home = home if home is not None else owner_home()
        self.doc_path = doc_path if doc_path is not None else default_doc_path()
        self._run = run
        self.baseline: OwnerFingerprint | None = None

    def fingerprint(self) -> OwnerFingerprint:
        mtimes: list[tuple[str, int | None]] = []
        for rel in GUARDED_CONFIG_FILES:
            path = self.home / ".config" / rel
            try:
                mtimes.append((str(path), path.stat().st_mtime_ns))
            except FileNotFoundError:
                mtimes.append((str(path), None))
        runner = self._run if self._run is not None else _findmnt
        code, out = runner(["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", str(self.doc_path)])
        fstype = (out.strip() or None) if code == 0 else None
        return OwnerFingerprint(tuple(mtimes), str(self.doc_path), fstype)

    def arm(self) -> OwnerFingerprint:
        fp = self.fingerprint()
        if fp.doc_fstype != PORTAL_FSTYPE:
            raise IsolationBreachError(
                [f"{fp.doc_path} is not mounted as {PORTAL_FSTYPE} before the run ({fp.doc_fstype or 'none'})"]
            )
        self.baseline = fp
        return fp

    def changes(self) -> list[str]:
        if self.baseline is None:
            raise NestedDisplayError("arm the guard before checking it")
        return compare_fingerprints(self.baseline, self.fingerprint())

    def check(self) -> None:
        changes = self.changes()
        if changes:
            raise IsolationBreachError(changes)


# --- process group -----------------------------------------------------------------------------


def _proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, pgrp) of a process, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    return fields[0], int(fields[2])


def _pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes in process group ``pgid``."""
    members = []
    for pid in _pids():
        stat = _proc_stat(pid)
        if stat is not None and stat[0] != "Z" and stat[1] == pgid:
            members.append(pid)
    return members


def marker_holders(token: str) -> list[int]:
    """Live processes whose environment carries ``AMG_NESTED_RUN=<token>``, this one excluded."""
    entry = f"{MARKER}={token}".encode()
    holders = []
    for pid in _pids():
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                environ = fh.read()
        except OSError:
            continue
        if entry in environ.split(b"\0"):
            stat = _proc_stat(pid)
            if stat is not None and stat[0] != "Z":
                holders.append(pid)
    return holders


class ProcessGroup:
    """Processes started in one process group; the first one spawned leads it."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.pgid: int | None = None
        self._procs: list[subprocess.Popen[bytes]] = []

    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], **kwargs: Any) -> subprocess.Popen[bytes]:
        for key in ("process_group", "start_new_session", "preexec_fn"):
            if key in kwargs:
                raise NestedDisplayError(f"{key} is owned by ProcessGroup")
        group = 0 if self.pgid is None else self.pgid
        proc: subprocess.Popen[bytes] = subprocess.Popen(list(argv), env=dict(env), process_group=group, **kwargs)
        if self.pgid is None:
            self.pgid = proc.pid
        self._procs.append(proc)
        return proc

    def _reap(self) -> None:
        for proc in self._procs:
            proc.poll()

    def _targets(self) -> tuple[list[int], list[int]]:
        self._reap()
        members = group_members(self.pgid) if self.pgid is not None else []
        strays = [pid for pid in marker_holders(self.token) if pid not in members]
        return members, strays

    def terminate(self, term_wait_s: float = 10.0, kill_wait_s: float = 5.0) -> list[int]:
        """SIGTERM the group and every marked stray, wait, SIGKILL what is left. Returns survivors."""
        for sig, wait_s in zip(TEARDOWN_SIGNALS, (term_wait_s, kill_wait_s), strict=True):
            members, strays = self._targets()
            if not members and not strays:
                break
            if members and self.pgid is not None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.pgid, sig)
            for pid in strays:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
            deadline = time.monotonic() + wait_s
            while any(self._targets()) and time.monotonic() < deadline:
                time.sleep(0.05)
        members, strays = self._targets()
        return sorted(members + strays)


# --- the display -------------------------------------------------------------------------------


def _read_line(fd: int, *, timeout_s: float, alive: Callable[[], bool], what: str) -> str:
    """One line from a pipe the child writes when ready (dbus address, Xwayland display number)."""
    deadline = time.monotonic() + timeout_s
    data = b""
    while b"\n" not in data:
        if time.monotonic() >= deadline:
            raise NestedDisplayError(f"{what} not ready within {timeout_s:g} s")
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                raise NestedDisplayError(f"{what} closed its pipe without reporting")
            data += chunk
        elif not alive():
            raise NestedDisplayError(f"{what} exited before reporting")
    return data.split(b"\n", 1)[0].decode().strip()


def _tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


class NestedDisplay:
    """A running isolated nested display. Use as a context manager or call start/stop."""

    def __init__(
        self,
        *,
        caller: str | None = None,
        xdg_root: Path | None = None,
        width: int = 1280,
        height: int = 720,
        rootful: bool = False,
        timeout_s: float = 30.0,
        owner_env: Mapping[str, str] | None = None,
        guard: OwnerGuard | None = None,
        data_base: Path | None = None,
        runtime_base: Path = RUNTIME_BASE,
    ) -> None:
        if xdg_root is None:
            if caller is None:
                raise NestedDisplayError("give a caller slug or an xdg root")
            xdg_root = default_xdg_root(caller)
        runtime = Path(os.path.abspath(runtime_base)) / f"amg-{secrets.token_hex(4)}"
        self.xdg = XdgDirs(Path(os.path.abspath(xdg_root)), runtime)  # env values must be absolute
        self.data_base = data_base if data_base is not None else main_checkout() / ORCH_DATA
        self.width, self.height, self.rootful, self.timeout_s = width, height, rootful, timeout_s
        self.token = f"{caller or 'nested'}-{os.getpid()}-{secrets.token_hex(4)}"
        self.group = ProcessGroup(self.token)
        self.guard = guard if guard is not None else OwnerGuard()
        self.owner_env = dict(os.environ if owner_env is None else owner_env)
        self.bus_address: str | None = None
        self.display: str | None = None
        self.xauthority: str | None = None
        self._core: list[subprocess.Popen[bytes]] = []

    @property
    def pgid(self) -> int:
        if self.group.pgid is None:
            raise NestedDisplayError("not started")
        return self.group.pgid

    def start(self) -> str:
        check_xdg_root(self.xdg.root, self.data_base)
        check_runtime_dir(self.xdg.runtime)
        self.guard.arm()
        self.xdg.create()  # outside the try: stop() deletes the runtime dir, so it must be ours
        try:
            self._start_bus()
            self._start_kwin()
            if self.rootful:
                self._start_rootful()
        except BaseException:
            self.stop()
            raise
        assert self.display is not None
        return self.display

    def _log(self, name: str) -> IO[bytes]:
        return open(self.xdg.root / f"{name}.log", "wb")

    def _start_bus(self) -> None:
        conf = self.xdg.root / "bus.conf"
        conf.write_text(bus_config(self.xdg.bus_socket), encoding="utf-8")
        env = {**_owner_basics(self.owner_env), **self.xdg.env(), MARKER: self.token}
        read_fd, write_fd = os.pipe()
        try:
            with self._log("dbus") as log:
                proc = self.group.spawn(
                    dbus_argv(conf, write_fd),
                    env=env,
                    pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            self._core.append(proc)
            os.close(write_fd)
            write_fd = -1
            address = _read_line(read_fd, timeout_s=10, alive=lambda: proc.poll() is None, what="dbus-daemon")
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        if not address.startswith(f"unix:path={self.xdg.bus_socket}"):
            raise NestedDisplayError(f"dbus-daemon listens on an unexpected address: {address}")
        self.bus_address = address

    def _start_kwin(self) -> None:
        assert self.bus_address is not None
        report = self.xdg.root / "display.env"
        report.unlink(missing_ok=True)
        reporter = self.xdg.root / "report.sh"
        reporter.write_text(reporter_script(report), encoding="utf-8")
        reporter.chmod(0o755)
        with self._log("kwin") as log:
            proc = self.group.spawn(
                kwin_argv(WAYLAND_SOCKET, self.width, self.height, str(reporter)),
                env=kwin_env(self.owner_env, self.xdg, self.bus_address, self.token),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self._core.append(proc)
        try:
            display, xauth = wait_for_report(report, alive=lambda: proc.poll() is None, timeout_s=self.timeout_s)
        except NestedDisplayError as exc:
            raise NestedDisplayError(f"{exc}\n--- kwin.log tail ---\n{_tail(self.xdg.root / 'kwin.log')}") from exc
        self.display, self.xauthority = display, xauth

    def _start_rootful(self) -> None:
        env = self.env()
        env["WAYLAND_DISPLAY"] = WAYLAND_SOCKET  # a client of the nested kwin, in the private runtime dir
        read_fd, write_fd = os.pipe()
        try:
            with self._log("xwayland-rootful") as log:
                proc = self.group.spawn(
                    rootful_xwayland_argv(self.width, self.height, write_fd),
                    env=env,
                    pass_fds=(write_fd,),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            self._core.append(proc)
            os.close(write_fd)
            write_fd = -1
            number = _read_line(
                read_fd, timeout_s=self.timeout_s, alive=lambda: proc.poll() is None, what="rootful Xwayland"
            )
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        self.display, self.xauthority = parse_report(f"DISPLAY=:{number}\n")

    def alive(self) -> bool:
        return bool(self._core) and all(proc.poll() is None for proc in self._core)

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        if self.display is None or self.bus_address is None:
            raise NestedDisplayError("not started")
        return child_env(
            self.owner_env,
            self.xdg,
            self.bus_address,
            self.token,
            display=self.display,
            xauthority=self.xauthority,
            extra=extra,
        )

    def popen(
        self, argv: Sequence[str], *, extra_env: Mapping[str, str] | None = None, **kwargs: Any
    ) -> subprocess.Popen[bytes]:
        """Start ``argv`` as an X11 client of the nested display, inside its process group."""
        return self.group.spawn(argv, env=self.env(extra_env), **kwargs)

    def check(self) -> None:
        """Raise ``IsolationBreachError`` if owner state changed since start."""
        self.guard.check()

    def stop(self, term_wait_s: float = 10.0) -> None:
        """Tear down the whole group and the runtime dir, then re-check the owner guard.

        Raises ``IsolationBreachError`` on a breach, else ``NestedDisplayError`` when a process
        survived (the runtime dir is then kept: it may still be in use) or the dir stayed.
        """
        survivors = self.group.terminate(term_wait_s=term_wait_s)
        self._core.clear()
        self.display = self.xauthority = None
        problem = f"processes survived teardown: {survivors}" if survivors else None
        if not survivors:
            try:
                remove_runtime_dir(self.xdg.runtime)
            except (NestedDisplayError, OSError) as exc:
                problem = str(exc)
        if self.guard.baseline is not None:
            self.guard.check()
        if problem:
            raise NestedDisplayError(problem)

    def __enter__(self) -> NestedDisplay:
        self.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.stop()


# --- CLI ---------------------------------------------------------------------------------------


class _StopFlag:
    requested = False

    def __call__(self, signum: int, frame: object) -> None:
        self.requested = True


_STOP = _StopFlag()


def _key_value(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key, value


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="nested_display.py", description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    for name, help_text in (
        ("run", "start the display, run CMD inside it, tear everything down when CMD exits"),
        ("serve", "start the display and keep it until SIGTERM/SIGINT; write an env file for exec"),
    ):
        p = sub.add_parser(name, help=help_text)
        where = p.add_mutually_exclusive_group(required=True)
        where.add_argument("--caller", help="task slug; private dirs go to .orchestration/m0/data/<caller>/xdg")
        where.add_argument("--xdg-root", type=Path, help="private dirs here (must be under .orchestration/m0/data)")
        p.add_argument("--width", type=int, default=1280)
        p.add_argument("--height", type=int, default=720)
        p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for Xwayland")
        p.add_argument("--rootful", action="store_true", help="add a rootful Xwayland (root-window grabs)")
        p.add_argument("--setenv", type=_key_value, action="append", default=[], metavar="KEY=VALUE")
        if name == "run":
            p.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD ARGS...")
        else:
            p.add_argument("--env-file", type=Path, required=True, help="JSON env file written when ready")
    p = sub.add_parser("exec", help="run CMD inside a display started by serve")
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--setenv", type=_key_value, action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- CMD ARGS...")
    p = sub.add_parser("guard", help="print the owner-state fingerprint; compare with a saved baseline")
    p.add_argument("--home", type=Path, help="owner home (default: from the password database)")
    p.add_argument("--save", type=Path, help="write the fingerprint here")
    p.add_argument("--baseline", type=Path, help="compare with this saved fingerprint")
    args = parser.parse_args(argv)
    if args.verb in ("run", "exec"):
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error(f"{args.verb} needs a command after --")
        args.command = command
    return args


def _critical(exc: IsolationBreachError) -> None:
    print(f"nested_display.py: {exc}", file=sys.stderr, flush=True)
    print("nested_display.py: every nested process was stopped; do not restore owner files", file=sys.stderr)


def _supervise(nested: NestedDisplay, done: Callable[[], bool]) -> int:
    """Wait until ``done``, a stop signal, the session dying, or a guard breach (raised)."""
    next_check = 0.0
    while not done():
        if _STOP.requested:
            return 130
        if not nested.alive():
            print("nested_display.py: the nested session died", file=sys.stderr)
            return 1
        if time.monotonic() >= next_check:
            nested.check()
            next_check = time.monotonic() + 2.0
        time.sleep(0.1)
    return 0


def _inside(nested: NestedDisplay, args: argparse.Namespace, extra: Mapping[str, str]) -> int:
    """Run the command, or serve until stopped. Returns the exit code."""
    if args.verb == "run":
        child = nested.popen(args.command, extra_env=extra)
        rc = _supervise(nested, lambda: child.poll() is not None)
        return child.returncode if child.returncode is not None else rc
    env_file: Path = args.env_file
    data = {"display": nested.display, "pgid": nested.pgid, "token": nested.token, "env": nested.env(extra)}
    tmp = env_file.with_name(env_file.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, env_file)
    print(f"nested_display.py: env file {env_file}", file=sys.stderr, flush=True)
    try:
        rc = _supervise(nested, lambda: False)
        return 0 if rc == 130 else rc
    finally:
        env_file.unlink(missing_ok=True)


def _stop(nested: NestedDisplay) -> tuple[IsolationBreachError | None, bool]:
    """Stop the display. Returns the breach it found, and whether teardown fell short otherwise."""
    try:
        nested.stop()
    except IsolationBreachError as exc:
        return exc, False
    except NestedDisplayError as exc:
        print(f"nested_display.py: {exc}", file=sys.stderr)
        return None, True
    return None, False


def _display_verb(args: argparse.Namespace) -> int:
    missing = [tool for tool in (KWIN, DBUS_DAEMON) + ((XWAYLAND,) if args.rootful else ()) if not shutil.which(tool)]
    if missing:
        print(f"nested_display.py: not on PATH: {', '.join(missing)}", file=sys.stderr)
        return 2
    extra = dict(args.setenv)
    protected = sorted(PROTECTED_KEYS & extra.keys())
    if protected:
        print(f"nested_display.py: --setenv may not set {', '.join(protected)}", file=sys.stderr)
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _STOP)
    try:
        nested = NestedDisplay(
            caller=args.caller,
            xdg_root=args.xdg_root,
            width=args.width,
            height=args.height,
            rootful=args.rootful,
            timeout_s=args.timeout,
        )
        nested.start()
    except IsolationBreachError as exc:
        _critical(exc)
        return EXIT_BREACH
    except (NestedDisplayError, OSError) as exc:
        print(f"nested_display.py: {exc}", file=sys.stderr)
        return 1
    print(
        f"nested display {nested.display} (process group {nested.pgid}, dirs {nested.xdg.root},"
        f" runtime {nested.xdg.runtime})",
        file=sys.stderr,
        flush=True,
    )
    rc, breach = 1, None
    try:
        rc = _inside(nested, args, extra)
    except IsolationBreachError as exc:
        breach = exc
    except (NestedDisplayError, OSError) as exc:
        print(f"nested_display.py: {exc}", file=sys.stderr)
        rc = 1
    except BaseException:
        stop_breach, _ = _stop(nested)  # anything else still tears the display down
        if stop_breach is not None:
            _critical(stop_breach)
        raise
    stop_breach, stop_failed = _stop(nested)
    breach = breach or stop_breach
    rc = rc or int(stop_failed)
    if breach is not None:
        _critical(breach)
        return EXIT_BREACH
    print("nested_display.py: stopped; owner guard unchanged", file=sys.stderr)
    return rc


def _exec_verb(args: argparse.Namespace) -> int:
    data = json.loads(args.env_file.read_text(encoding="utf-8"))
    env: dict[str, str] = dict(data["env"])
    for key, value in args.setenv:
        if key in PROTECTED_KEYS:
            print(f"nested_display.py: {key} belongs to the isolation", file=sys.stderr)
            return 2
        env[key] = value
    try:
        proc = subprocess.Popen(args.command, env=env, process_group=int(data["pgid"]))
    except PermissionError:
        # Another session cannot join the group; the marker still lets serve's teardown find it.
        proc = subprocess.Popen(args.command, env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return proc.wait()


def _guard_verb(args: argparse.Namespace) -> int:
    guard = OwnerGuard(home=args.home)
    fp = guard.fingerprint()
    if args.save:
        args.save.write_text(fp.to_json(), encoding="utf-8")
    if args.baseline:
        changes = compare_fingerprints(OwnerFingerprint.from_json(args.baseline.read_text(encoding="utf-8")), fp)
    else:
        print(fp.to_json())
        changes = [] if fp.doc_fstype == PORTAL_FSTYPE else [f"{fp.doc_path}: not mounted as {PORTAL_FSTYPE}"]
    if changes:
        _critical(IsolationBreachError(changes))
        return EXIT_BREACH
    if args.baseline:
        print(f"owner state unchanged since {args.baseline}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    if not sys.platform.startswith("linux"):
        print("nested_display.py: Linux only", file=sys.stderr)
        return 2
    if args.verb == "guard":
        return _guard_verb(args)
    if args.verb == "exec":
        return _exec_verb(args)
    return _display_verb(args)


if __name__ == "__main__":
    sys.exit(main())
