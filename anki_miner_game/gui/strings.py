"""The user-facing words the windows share (UJ-32 glossary, D-01): one table per concept.

The UI never shows an enum's ``.value``. The word "arm" never reaches the user (D-01). "Line(s)",
never "cue(s)"; "WebSocket"; "voice trimming" (VAD) and "screen reading (OCR)"; "game window" (never
"pinned" or "Pick"). A button that opens a window ends in "…".
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.messages import AppState, SourceStatus

APP_NAME: Final = "Anki Miner Game"

START_RECORDING: Final = "Start recording"
GET_READY: Final = "Get ready"
STOP_RECORDING: Final = "Stop recording"
DONE_PLAYING: Final = "Done playing"
GETTING_OBS_READY: Final = "Getting OBS ready…"
STARTING: Final = "Starting…"
STOPPING: Final = "Stopping…"
ADD_YOUR_GAME: Final = "Add your game…"
NEW_GAME: Final = "New game…"
EDIT_GAME: Final = "Edit…"
SETTINGS: Final = "Settings…"
SET_UP_OBS: Final = "Set up OBS…"
OPEN_FOLDER: Final = "Open folder"
OPEN_TEXT_FEED: Final = "Open text feed"
SHOW_WINDOW: Final = "Show window"
QUIT: Final = "Quit"

READY: Final = "Ready"
READY_AUTO_START: Final = "Ready: recording starts at the first line"
SAVING_SESSION: Final = "Saving the session…"


def lines_text(count: int) -> str:
    """``1 line`` / ``N lines``."""
    return "1 line" if count == 1 else f"{count} lines"


def status_text(state: AppState, *, auto_start_pending: bool, elapsed: str, lines: int) -> str:
    """The main window's one status text (UJ-03); the red dot while recording is drawn beside it."""
    match state:
        case AppState.IDLE:
            return ""
        case AppState.ARMED:
            return READY_AUTO_START if auto_start_pending else READY
        case AppState.RECORDING:
            return f"Recording {elapsed} · {lines_text(lines)}"
        case AppState.FINALISING:
            return SAVING_SESSION


def tray_tooltip(state: AppState, game: str | None, elapsed: str) -> str:
    """UJ-05: ``Anki Miner Game - Not recording`` / ``- Ready: <game>`` / ``- Recording <game> 0:22:05``."""
    match state:
        case AppState.IDLE:
            return f"{APP_NAME} - Not recording"
        case AppState.ARMED:
            return f"{APP_NAME} - Ready: {game}"
        case AppState.RECORDING:
            return f"{APP_NAME} - Recording {game} {elapsed}"
        case AppState.FINALISING:
            return f"{APP_NAME} - {SAVING_SESSION}"


GAME_TEXT: Final = "Game text"
SOURCE_STATUS_TEXT: Final[Mapping[SourceStatus, str]] = MappingProxyType(
    {
        SourceStatus.DISCONNECTED: "Not connected",
        SourceStatus.CONNECTING: "Connecting",
        SourceStatus.CONNECTED: "Connected",
        SourceStatus.RECEIVING: "Receiving",
    }
)

VOICE_TRIMMING: Final = "Voice trimming"
SCREEN_READING: Final = "Screen reading (OCR)"
ADDON_STATUS_TEXT: Final[Mapping[AddonStatus, str]] = MappingProxyType(
    {
        AddonStatus.MISSING: "Not installed",
        AddonStatus.INSTALLING: "Installing…",
        AddonStatus.READY: "Installed",
        AddonStatus.BROKEN: "Damaged",
    }
)


def size_mb(size_bytes: int) -> int:
    return round(size_bytes / 1_000_000)


def install_now_text(status: AddonStatus, size_bytes: int) -> str:
    """The wizard's own install button (UJ-18): ``Install (96 MB)``, or ``Repair (96 MB)`` when damaged."""
    verb = "Repair" if status is AddonStatus.BROKEN else "Install"
    return f"{verb} ({size_mb(size_bytes)} MB)"


def install_elsewhere_text(status: AddonStatus) -> str:
    """A button that opens the wizard's add-ons page (UJ-22): ``Install…``, or ``Repair…`` when damaged."""
    return "Repair…" if status is AddonStatus.BROKEN else "Install…"


COPIED_TEXT: Final = "Copied text (clipboard)"
WAYLAND_CLIPBOARD_TEXT: Final = (
    "On Wayland the app sees copied text only while one of its windows has focus, so text copied "
    "while you play is missed. Use a text hooker instead."
)
