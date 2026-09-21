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

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from anki_miner_game.models.profile import OcrSettings

OWOCR_VERSION: Final = "1.26.8"


class OcrError(RuntimeError):
    """OCR cannot run as asked; the message says why and is fit for a banner or a dialog."""


# --- command line ----------------------------------------------------------------------------------


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
    '"screen_capture_area" must be empty',  # run.py:1991,2005: no window with that title
    '"screen_capture_window_area" must be empty',  # run.py:2039
    "Window capture is only currently supported",  # run.py:2017
    "Error initializing screenshots",  # run.py:1910,2529: no screen to capture
    "Error initializing picker window",  # run.py:2442
    "No engines available!",  # run.py:3295
)
"""owocr's fatal messages (``exit_with_error``) that a restart with the same flags would hit again.
Anything else it exits on, such as a websocket port taken meanwhile, is worth a restart."""

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
    return None
