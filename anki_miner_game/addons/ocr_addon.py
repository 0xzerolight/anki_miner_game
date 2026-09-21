"""The OCR add-on: owocr in its own uv tool environment, run as a managed subprocess (spec 3.4, 14).

owocr is a CLI with no library API, so none of it is imported here: the app builds its command line
from flags, reads its log and takes its text from its websocket. Cites are to owocr 1.26.8
(``owocr/run.py``, ``owocr/config.py`` at 3b9706b1) and to the M0 R3 spike (``docs/m0/owocr.md``).

M0 rulings applied here:

- ``-el <engine>`` next to ``-e <engine>``: without it owocr constructs every installed engine at
  start, including Chrome Screen AI, which downloads a client (``config.py:27``, ``run.py:3250-3266``).
- owocr reads ``~/.config/owocr_config.ini`` on every start and downloads one from GitHub when it
  is missing (``config.py:110,186-195``), and a value in it overrides any flag the app does not
  pass. So the managed owocr runs with ``HOME`` (and ``USERPROFILE`` on Windows) pointing at
  ``<home>/addons/ocr/home/``, pre-seeded with a config holding only ``[general]``: owocr parses it,
  downloads nothing and takes everything else from its flags and built-in defaults. The user's own
  file is never read or written, by the app or by its owocr.
- On Linux the install overrides PyGObject out (``uv tool install --overrides``, a file holding
  ``pygobject; sys_platform == "never"``): owocr lists it as a base Linux dependency, it ships only
  an sdist whose build needs the cairo and GObject-introspection development packages, and owocr
  uses it only for Wayland capture. Linux OCR is therefore X11-only; Wayland is not supported in v1.
- The process tree is killed as a whole: a process group on POSIX (SIGTERM to the group, a grace
  period, then SIGKILL to the group, because ``multiprocessing``'s resource tracker ignores SIGTERM
  and a parent-only kill orphans the picker's children) and a job object with kill-on-close on
  Windows, where ``uv``'s trampoline starts ``python.exe`` as a child.
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import socket
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from anki_miner_game.addons import bootstrap
from anki_miner_game.interfaces.addons import ProgressCallback
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.profile import OcrSettings, default_ocr_engine

logger = logging.getLogger(__name__)

OWOCR_VERSION: Final = "1.26.8"
PYTHON_VERSION: Final = "3.12"
"""The managed CPython the tool environment is built on; the one the M0 R3 install used."""

LINUX_OVERRIDES: Final = 'pygobject; sys_platform == "never"\n'
"""``uv --overrides`` file content on Linux: PyGObject is never installed (see the module docstring)."""

OWOCR_CONFIG: Final = "[general]\n"
"""The whole ``owocr_config.ini`` in the private home: enough for owocr to parse a file and download
none; every setting then comes from the command line or owocr's defaults (``config.py:186-217``)."""

SIZE_BYTES: Final = {"win32": 100_000_000, "linux": 210_000_000}
"""Rough download per platform: uv, a managed CPython 3.12 and the wheels of owocr's dependencies,
plus on Linux meikiocr's model (45 MB, fetched by owocr's first run) and opencv (58 MB)."""

NOT_INSTALLED: Final = "The OCR add-on is not installed."
X11_ONLY: Final = "OCR on Linux needs an X11 session; Wayland sessions are not supported."

KILL_GRACE_S: Final = 2.0
"""How long the process group gets after SIGTERM before SIGKILL (M0 R3 amendment 4)."""
LOG_DRAIN_S: Final = 2.0
"""After owocr exits, how long its log may stay open (held by a surviving child) before reading stops."""
LINE_LIMIT: Final = 1 << 20
"""Longest log line read; a longer one is skipped."""
_POLL_S: Final = 0.02
_EXIT_POLL_S: Final = 0.1
_UV_ERROR_LINES: Final = 12


class OcrError(RuntimeError):
    """OCR cannot run as asked; the message says why and is fit for a banner or a dialog."""


# --- command lines ---------------------------------------------------------------------------------


def owocr_extra(platform: str) -> str:
    """The owocr extra installed: ``oneocr`` on Windows, ``meikiocr`` elsewhere (spec 14)."""
    return "oneocr" if platform == "win32" else "meikiocr"


def owocr_requirement(platform: str) -> str:
    return f"owocr[{owocr_extra(platform)}]=={OWOCR_VERSION}"


def uv_install_args(platform: str, overrides: Path | None) -> list[str]:
    """``uv`` arguments that install the pinned owocr as a tool, with ``overrides`` when given."""
    args = ["tool", "install", "--python", PYTHON_VERSION]
    if overrides is not None:
        args += ["--overrides", str(overrides)]
    return args + [owocr_requirement(platform)]


def owocr_args(ocr: OcrSettings, port: int, *, platform: str, pick: bool = False) -> list[str]:
    """owocr's arguments (without the executable) for a websocket OCR run on ``port`` (spec 14).

    Windows with ``ocr.window_title`` captures that window: ``-sa=<title>`` plus ``-swa=<rects>``
    (window-relative), or ``-swa=window`` for the whole window. Otherwise ``-sa=<rects>`` are screen
    rectangles; Linux always takes this form, since owocr has no window capture on X11 (it exits,
    ``run.py:2017``). ``pick`` leaves the area empty, which opens owocr's own picker. The area flags
    are joined with ``=`` so a window title starting with ``-`` stays a value. Raises ``OcrError``
    when a run has no area to capture.
    """
    args = ["-r", "screencapture", "-w", "websocket", "-wp", str(port), "-t", "False"]
    args += ["-l", ocr.language, "-e", str(ocr.engine), "-el", str(ocr.engine)]
    window = ocr.window_title if platform == "win32" else None
    if window:
        area = "" if pick else (ocr.rects or "window")
        return args + [f"-sa={window}", f"-swa={area}"]
    if pick:
        return args + ["-sa="]
    if not ocr.rects:
        raise OcrError("No OCR area is selected: use Select OCR area in the game profile")
    return args + [f"-sa={ocr.rects}"]


# --- log -----------------------------------------------------------------------------------------------


class LogKind(StrEnum):
    COORDINATES = "coordinates"
    """``Selected coordinates: <rects>``: screen rectangles (``run.py:1956,2480``)."""
    WINDOW_COORDINATES = "window_coordinates"
    """``Selected window coordinates: <rects>``: window-relative, Windows only (``run.py:2035,2517``)."""
    EMPTY_SELECTION = "empty_selection"
    """The picker returned no rectangle: owocr takes the whole screen or window (``run.py:2483,2488,2521``)."""
    PICKER_CLOSED = "picker_closed"
    """The picker window was closed (``run.py:2448,2502``); fatal for the screen picker."""
    CONFIG_ERROR = "config_error"
    """A fatal error that the same command line would hit again, so a restart cannot help."""
    WINDOW_MISSING = "window_missing"
    """No window has the title owocr was given (``run.py:1991,2005``). Worth a restart: the game may
    not be open yet, or is being restarted (owocr first exits with "The window was closed",
    ``run.py:2544``). Kept apart only so the error can name the window."""


@dataclass(frozen=True)
class LogEvent:
    kind: LogKind
    text: str
    """The rectangles for the two coordinate kinds, else owocr's message."""


CONFIG_ERRORS: Final = (
    "Invalid screen_capture_area",  # run.py:1917
    "Invalid monitor number in screen_capture_area",  # run.py:1937
    "Invalid coordinate set(s) in screen_capture_area",  # run.py:1947
    "Invalid coordinate set(s) in screen_capture_window_area",  # run.py:2026
    '"screen_capture_window_area" must be empty',  # run.py:2039
    "Window capture is only currently supported",  # run.py:2017
    "Error initializing screenshots",  # run.py:1910,2529: no screen to capture
    "Error initializing picker window",  # run.py:2442
    "No engines available!",  # run.py:3295
)
"""owocr's fatal messages (``exit_with_error``) that a restart with the same flags would hit again.
Anything else it exits on, such as a websocket port taken meanwhile, is worth a restart."""
WINDOW_MISSING_ERROR: Final = '"screen_capture_area" must be empty'
"""How owocr's message starts when no window has the title (``LogKind.WINDOW_MISSING``)."""

_ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")
_LOG_LINE: Final = re.compile(r"^\d{2}:\d{2}:\d{2} \| (.*)$")
"""loguru's ``{time:HH:mm:ss} | {message}`` on stderr (``run.py:3097,3100``); tracebacks lack the prefix."""
_RECTS: Final = r"(-?\d+,-?\d+,-?\d+,-?\d+(?:_-?\d+,-?\d+,-?\d+,-?\d+)*)"
_COORDINATES: Final = re.compile(rf"^Selected (window )?coordinates: {_RECTS}$")


def log_message(line: str) -> str | None:
    """The message of one owocr log line, or ``None`` for a line its logger did not write."""
    match = _LOG_LINE.match(_ANSI.sub("", line).rstrip("\r\n"))
    return match.group(1) if match else None


def parse_log_line(line: str) -> LogEvent | None:
    """The event one owocr log line reports, or ``None`` when it reports none of ``LogKind``."""
    message = log_message(line)
    if message is None:
        return None
    if match := _COORDINATES.match(message):
        return LogEvent(LogKind.WINDOW_COORDINATES if match.group(1) else LogKind.COORDINATES, match.group(2))
    if message.startswith("Selection is empty") or message == "Window is minimized, selecting whole window":
        return LogEvent(LogKind.EMPTY_SELECTION, message)
    if message.startswith("Picker window was closed or an error occurred"):
        return LogEvent(LogKind.PICKER_CLOSED, message)
    if message.startswith(CONFIG_ERRORS):
        return LogEvent(LogKind.CONFIG_ERROR, message)
    if message.startswith(WINDOW_MISSING_ERROR):
        return LogEvent(LogKind.WINDOW_MISSING, message)
    return None


def window_missing_text(window_title: str | None) -> str:
    """Why owocr stopped when it reported ``LogKind.WINDOW_MISSING``, naming the window."""
    return f'the window "{window_title}" is not open' if window_title else "the game window is not open"


# --- process tree ------------------------------------------------------------------------------------


class OwocrProcess:
    """One owocr process and every process it started, with its log read as it arrives.

    Made by ``spawn_owocr`` on a running asyncio loop, and used on that loop only. The log is read
    from owocr's stderr for as long as it runs: every line is logged at debug level, the last
    timestamped message is kept in ``last_message``, the first fatal one (``LogKind.CONFIG_ERROR``
    or ``PICKER_CLOSED``) in ``fatal``, ``window_missing`` says whether owocr found no window with
    its title, and every ``LogEvent`` is queued for ``next_event``. Always end with ``kill_tree``,
    also after owocr exited on its own: its children may have outlived it.
    """

    def __init__(self, proc: asyncio.subprocess.Process, job: int | None) -> None:
        self._proc = proc
        self._job = job
        """Windows job object handle; ``None`` on POSIX and once closed."""
        self._tree_dead = False
        self._events: asyncio.Queue[LogEvent | None] = asyncio.Queue()
        self._log_ended = False
        self.fatal: str | None = None
        self.window_missing = False
        self.last_message: str | None = None
        loop = asyncio.get_running_loop()
        self._reader = loop.create_task(self._read(), name=f"owocr-log-{proc.pid}")
        self._watcher = loop.create_task(self._end_log_after_exit(), name=f"owocr-exit-{proc.pid}")

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait(self) -> int:
        """Wait for owocr itself to exit, not for children that outlive it; its exit code.

        Polls: ``asyncio``'s own ``Process.wait`` returns only once every pipe has closed, and a
        surviving child can hold the log pipe open (Python 3.12 ``base_subprocess._try_finish``).
        """
        while (code := self._proc.returncode) is None:
            await asyncio.sleep(_EXIT_POLL_S)
        return code

    async def next_event(self) -> LogEvent | None:
        """The next event the log reports; ``None`` once the log is over.

        The log is over at end of file, and at the latest ``LOG_DRAIN_S`` after owocr exits, even
        while a surviving child still holds the pipe open.
        """
        if self._log_ended:
            return None
        event = await self._events.get()
        if event is None:
            self._log_ended = True
        return event

    async def kill_tree(self, grace_s: float = KILL_GRACE_S) -> None:
        """Kill owocr and everything it started, then wait for owocr and the end of its log.

        POSIX: SIGTERM to the process group, up to ``grace_s`` for the group to empty, then SIGKILL
        to the group; the SIGKILL is sent even when this call is cancelled while waiting. Windows:
        terminate and close the job object. Safe to call again and after owocr exited.
        """
        if sys.platform == "win32":
            if self._job is not None:
                job, self._job = self._job, None
                _close_job(job)
        elif not self._tree_dead:
            await self._kill_group(grace_s)
        await self.wait()
        await asyncio.wait({self._watcher})

    async def _kill_group(self, grace_s: float) -> None:
        pgid = self._proc.pid
        _signal_group(pgid, signal.SIGTERM)
        deadline = asyncio.get_running_loop().time() + grace_s
        try:
            while asyncio.get_running_loop().time() < deadline:
                if self._proc.returncode is not None and not _group_exists(pgid):
                    self._tree_dead = True
                    return
                await asyncio.sleep(_POLL_S)
        finally:
            if not self._tree_dead:
                _signal_group(pgid, _SIGKILL)
                self._tree_dead = True

    async def _read(self) -> None:
        stream = self._proc.stderr
        assert stream is not None
        try:
            while True:
                try:
                    raw = await stream.readline()
                except ValueError:  # longer than LINE_LIMIT; readline has dropped it
                    logger.debug("owocr %d: skipped a log line over %d bytes", self._proc.pid, LINE_LIMIT)
                    continue
                if not raw:
                    return
                self._take(raw.decode("utf-8", errors="replace"))
        finally:
            self._events.put_nowait(None)

    def _take(self, line: str) -> None:
        logger.debug("owocr %d: %s", self._proc.pid, line.rstrip())
        message = log_message(line)
        if message is not None:
            self.last_message = message
        event = parse_log_line(line)
        if event is None:
            return
        if self.fatal is None and event.kind in (LogKind.CONFIG_ERROR, LogKind.PICKER_CLOSED):
            self.fatal = event.text
        if event.kind is LogKind.WINDOW_MISSING:
            self.window_missing = True
        self._events.put_nowait(event)

    async def _end_log_after_exit(self) -> None:
        await self.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._reader, LOG_DRAIN_S)  # cancels the reader on timeout


async def spawn_owocr(argv: Sequence[str], env: Mapping[str, str]) -> OwocrProcess:
    """Start ``argv`` as a process tree the app can kill whole; call it on the loop that will use it.

    stdin and stdout are the null device (owocr writes its log to stderr, and a terminal stdin would
    let it change the app's terminal settings). POSIX: a new session, so owocr leads its own process
    group and every child it starts stays in it. Windows: created suspended and without a console
    window, put in a job object that kills every member when its handle closes (so also when the
    app dies), then resumed; a child cannot start before owocr is in the job. Raises ``OSError``.
    """
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            limit=LINE_LIMIT,
            creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
        )
        try:
            job = _job_with(proc.pid)
        except OSError:
            proc.kill()  # still suspended, so it has started nothing
            await proc.wait()
            raise
        return OwocrProcess(proc, job)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        limit=LINE_LIMIT,
        start_new_session=True,
    )
    return OwocrProcess(proc, None)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED: Final = 0x00000004
    _CREATE_NO_WINDOW: Final = 0x08000000
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
    _PROCESS_TERMINATE: Final = 0x0001
    _PROCESS_SET_QUOTA: Final = 0x0100
    _PROCESS_SUSPEND_RESUME: Final = 0x0800

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("Read", "Write", "Other", "ReadTransfer", "WriteTransfer", "OtherTransfer")
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _check(ok: object) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _job_with(pid: int) -> int:
        """A new kill-on-close job holding the suspended process ``pid``, which is then resumed."""
        job = _kernel32.CreateJobObjectW(None, None)
        _check(job)
        try:
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(
                _kernel32.SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
                )
            )
            process = _kernel32.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME, False, pid
            )
            _check(process)
            try:
                _check(_kernel32.AssignProcessToJobObject(job, process))
                # The documented way to resume needs the main thread's handle, which subprocess
                # closes; NtResumeProcess (also what psutil uses) resumes by process handle.
                status = _ntdll.NtResumeProcess(process)
                if status < 0:
                    raise OSError(f"NtResumeProcess failed with NTSTATUS {status & 0xFFFFFFFF:#010x}")
            finally:
                _kernel32.CloseHandle(process)
        except BaseException:
            _kernel32.CloseHandle(job)
            raise
        return int(job)

    def _close_job(job: int) -> None:
        _kernel32.TerminateJobObject(job, 1)
        _kernel32.CloseHandle(job)

    def _signal_group(pgid: int, sig: int) -> None:
        raise OSError("process groups are POSIX only")

    def _group_exists(pgid: int) -> bool:
        raise OSError("process groups are POSIX only")

    _SIGKILL: Final = 9

else:
    _CREATE_SUSPENDED: Final = 0
    _CREATE_NO_WINDOW: Final = 0
    _SIGKILL: Final = signal.SIGKILL

    def _job_with(pid: int) -> int:
        raise OSError("job objects are Windows only")

    def _close_job(job: int) -> None:
        raise OSError("job objects are Windows only")

    def _signal_group(pgid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)

    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # a member we may not signal still counts
            return True
        return True


# --- the add-on ----------------------------------------------------------------------------------------


def free_port() -> int:
    """A TCP port nothing listens on now, for owocr's websocket server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class OcrAddon:
    """The OCR add-on: an ``AddonService`` and the ``OcrAreaPicker`` (spec 14).

    Everything lives under ``<home>/addons/ocr/``: uv's managed Python, cache, tool environment and
    entry point (``bootstrap.uv_environment``), the ``--overrides`` file, and ``home/``, the private
    home owocr runs with. ``install``, ``launch`` and ``pick`` run on an asyncio loop (the I/O loop);
    ``status`` may be called from any thread. ``platform``, ``environ``, ``ensure_uv`` and ``argv0``
    (the command that starts owocr, before its arguments) exist for tests.
    """

    def __init__(
        self,
        home: Path,
        *,
        platform: str = sys.platform,
        environ: Mapping[str, str] | None = None,
        ensure_uv: Callable[[Path], Path] = bootstrap.ensure_uv,
        argv0: Sequence[str] | None = None,
    ) -> None:
        self._home = home
        self._root = home / "addons" / "ocr"
        self._platform = platform
        self._environ = environ
        self._ensure_uv = ensure_uv
        self._argv0 = list(argv0) if argv0 is not None else [str(self.executable)]
        self._installing = False

    @property
    def executable(self) -> Path:
        """owocr's entry point in the add-on's tool bin directory."""
        return self._root / "bin" / ("owocr.exe" if self._platform == "win32" else "owocr")

    @property
    def size_bytes(self) -> int:
        return SIZE_BYTES.get(self._platform, SIZE_BYTES["linux"])

    @property
    def note(self) -> str | None:
        """A platform limitation to show beside the status: Linux OCR is X11-only (M0 ruling)."""
        return X11_ONLY if self._platform == "linux" else None

    def status(self) -> AddonStatus:
        """``ready`` when uv's receipt records the pinned owocr and extra and the entry point exists;
        ``broken`` when some of it is there but not that; otherwise ``missing``."""
        if self._installing:
            return AddonStatus.INSTALLING
        if self._verified():
            return AddonStatus.READY
        if (self._root / "tools" / "owocr").exists() or (self._root / "bin").exists():
            return AddonStatus.BROKEN
        return AddonStatus.MISSING

    def unavailable_reason(self) -> str | None:
        """Why owocr cannot run now, fit for a banner, or ``None``: not installed, or a Wayland session."""
        if self.status() is not AddonStatus.READY:
            return NOT_INSTALLED
        if self._platform == "linux" and self._env().get("XDG_SESSION_TYPE", "").lower() == "wayland":
            return X11_ONLY
        return None

    async def install(self, progress: ProgressCallback) -> None:
        """``uv tool install`` the pinned owocr into the add-on folder, replacing what was there.

        ``progress`` hears ``(0, size_bytes)`` at the start and ``(size_bytes, size_bytes)`` at the
        end; uv reports nothing finer. A failure after the old install was removed, cancellation
        included, removes whatever the attempt left, so ``status`` is ``missing`` again. Raises
        ``BootstrapError`` (no uv) or ``OcrError`` (uv failed, the message ends with its output).
        """
        if self._installing:
            raise OcrError("The OCR add-on is already being installed.")
        self._installing = True
        total = self.size_bytes
        touched = False
        try:
            progress(0, total)
            uv = await asyncio.to_thread(self._ensure_uv, self._home)
            touched = True
            self._remove_install()
            self._root.mkdir(parents=True, exist_ok=True)
            overrides = None
            if self._platform != "win32":
                overrides = self._root / "overrides.txt"
                overrides.write_text(LINUX_OVERRIDES, encoding="utf-8")
            env = {**self._env(), **bootstrap.uv_environment(self._home, "ocr")}
            await _run_uv([str(uv), *uv_install_args(self._platform, overrides)], env)
            if not self._verified():
                raise OcrError("owocr was installed but its files do not check out; install it again.")
        except BaseException:
            if touched:
                self._remove_install()
            raise
        finally:
            self._installing = False
        progress(total, total)

    def owocr_environment(self) -> dict[str, str]:
        """The environment owocr runs with: the app's, with ``HOME`` (and ``USERPROFILE`` on Windows)
        pointing at the private home, whose ``.config/owocr_config.ini`` is (re)written to
        ``OWOCR_CONFIG`` first. On X11 without ``XAUTHORITY``, it points at the user's
        ``~/.Xauthority``, which Xlib would otherwise look for under the private home."""
        private = self._root / "home"
        config = private / ".config" / "owocr_config.ini"
        config.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = config.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != OWOCR_CONFIG:
            staged = config.with_name(config.name + ".tmp")
            staged.write_text(OWOCR_CONFIG, encoding="utf-8")
            os.replace(staged, config)
        env = self._env()
        if self._platform != "win32" and not env.get("XAUTHORITY") and env.get("HOME"):
            xauthority = Path(env["HOME"]) / ".Xauthority"
            if xauthority.is_file():
                env["XAUTHORITY"] = str(xauthority)
        env["HOME"] = str(private)
        if self._platform == "win32":
            env["USERPROFILE"] = str(private)
        return env

    async def launch(self, ocr: OcrSettings, port: int, *, pick: bool = False) -> OwocrProcess:
        """Start owocr for ``ocr`` with its websocket on ``port``; ``pick`` opens its picker instead
        of capturing. Raises ``OcrError`` (no area to capture) or ``OSError`` (cannot start)."""
        argv = [*self._argv0, *owocr_args(ocr, port, platform=self._platform, pick=pick)]
        return await spawn_owocr(argv, self.owocr_environment())

    async def pick(self, window_title: str | None) -> str | None:
        """Run owocr's own picker once and return the rectangles it reports (for ``OcrSettings.rects``).

        With ``window_title`` on Windows it is the window picker (window-relative rectangles);
        otherwise the screen picker (screen rectangles; Linux ignores the title). ``None`` when the
        picker is closed or returns no rectangle. owocr is killed once it has answered, and when
        this call is cancelled. Raises ``OcrError`` when owocr cannot run or exits without an answer.
        """
        reason = self.unavailable_reason()
        if reason is not None:
            raise OcrError(reason)
        settings = OcrSettings(engine=default_ocr_engine(self._platform), window_title=window_title)
        try:
            proc = await self.launch(settings, free_port(), pick=True)
        except OSError as exc:
            raise OcrError(f"owocr could not start: {exc}") from exc
        try:
            while (event := await proc.next_event()) is not None:
                if event.kind in (LogKind.COORDINATES, LogKind.WINDOW_COORDINATES):
                    return event.text
                if event.kind in (LogKind.EMPTY_SELECTION, LogKind.PICKER_CLOSED):
                    return None
                if event.kind is LogKind.CONFIG_ERROR:
                    raise OcrError(f"owocr could not open its picker: {event.text}")
                if event.kind is LogKind.WINDOW_MISSING:
                    raise OcrError(f"owocr could not open its picker: {window_missing_text(window_title)}")
            raise OcrError(f"owocr exited before an area was selected; its last message: {proc.last_message}")
        finally:
            await proc.kill_tree()

    def _env(self) -> dict[str, str]:
        return dict(os.environ if self._environ is None else self._environ)

    def _verified(self) -> bool:
        receipt = self._root / "tools" / "owocr" / "uv-receipt.toml"
        wanted = {"name": "owocr", "extras": [owocr_extra(self._platform)], "specifier": f"=={OWOCR_VERSION}"}
        try:
            requirements = tomllib.loads(receipt.read_text(encoding="utf-8"))["tool"]["requirements"]
            pinned = any(wanted.items() <= req.items() for req in requirements)  # uv may add keys
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError):
            return False
        return pinned and self.executable.is_file()

    def _remove_install(self) -> None:
        for part in ("tools", "bin"):
            shutil.rmtree(self._root / part, ignore_errors=True)


async def _run_uv(argv: Sequence[str], env: Mapping[str, str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=dict(env),
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        out, _ = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    text = out.decode("utf-8", errors="replace").strip()
    logger.debug("uv %s exited %s:\n%s", " ".join(argv[1:]), proc.returncode, text)
    if proc.returncode != 0:
        tail = "\n".join(text.splitlines()[-_UV_ERROR_LINES:])
        raise OcrError(f"Installing owocr failed (uv exit code {proc.returncode}):\n{tail}")
