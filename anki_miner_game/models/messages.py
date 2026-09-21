"""Messages into and events out of the session actor, plus the states the GUI shows (spec 4.2, 6, 16)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from anki_miner_game.models.lines import GameLine


class AppState(StrEnum):
    IDLE = "idle"
    ARMED = "armed"
    RECORDING = "recording"
    FINALISING = "finalising"


class SourceStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECEIVING = "receiving"


class BannerLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CommandKind(StrEnum):
    ARM = "arm"
    DISARM = "disarm"
    START = "start"
    STOP = "stop"
    TOGGLE = "toggle"


OBS_SOURCE_ID: Final = "obs"
"""``SourceStatusChanged.source_id`` of the OBS light in the status row; no text source may use it."""


START_FAILED_BANNER_KEY: Final = "start_failed"
"""``Banner.key`` of the banner the actor raises when ``StartRecord`` fails (spec 17: the state stays
``armed``). Auto mode listens for it, so the next line may try to start again."""


@dataclass(frozen=True)
class Banner:
    key: str
    """Stable id: a banner with the same key replaces the shown one; ``BannerCleared(key)`` removes it."""
    level: BannerLevel
    text: str


# Actor inputs.


@dataclass(frozen=True)
class LineReceived:
    raw: str
    t_mono: float
    source_id: str


@dataclass(frozen=True)
class ObsEvent:
    name: str
    """obs-websocket ``eventType``, or one of the gateway's own ``ObsEventName`` connection events."""
    data: Mapping[str, Any]
    """``eventData`` as OBS sent it (empty for the gateway's own events)."""
    t_mono: float
    """``now()`` read on the library's event thread when the event arrived."""


@dataclass(frozen=True)
class UserCommand:
    kind: CommandKind
    slug: str | None = None
    """The game to arm; used only with ``CommandKind.ARM``."""
    line: GameLine | None = None
    """``START`` from auto mode only: the accepted line that triggered it (spec 12).

    The actor holds it and journals it at offset 0 on ``STARTED``. A ``START``
    without a line (button, tray, hotkey, CLI) holds nothing.
    """


@dataclass(frozen=True)
class Tick:
    t_mono: float


SessionInput = LineReceived | ObsEvent | UserCommand | Tick


# Actor outputs.


@dataclass(frozen=True)
class StateChanged:
    state: AppState
    slug: str | None
    """The armed game's ``GameProfile.slug`` in every state but ``idle``, where it is ``None``.

    Arming another game while armed publishes ``StateChanged(ARMED, <new slug>)``.
    """


@dataclass(frozen=True)
class LineAccepted:
    line: GameLine
    offset_ms: int | None
    """Record-clock offset; ``None`` when no recording is running."""
    replaces_previous: bool = False
    """Typewriter merge: the text replaces the previous accepted line (spec 8.2 step 9)."""


@dataclass(frozen=True)
class RecordingStarted:
    stem: str
    """OBS's file stem in ``_incoming/``."""


@dataclass(frozen=True)
class RecordingStopped:
    stem: str


@dataclass(frozen=True)
class SessionFinalised:
    manifest_path: Path
    """Where the manifest ended up: the game folder, or ``_incoming/`` when ``finalise_pending``."""


@dataclass(frozen=True)
class SourceStatusChanged:
    source_id: str
    status: SourceStatus


@dataclass(frozen=True)
class BannerRaised:
    banner: Banner


@dataclass(frozen=True)
class BannerCleared:
    key: str


SessionEvent = (
    StateChanged
    | LineAccepted
    | RecordingStarted
    | RecordingStopped
    | SessionFinalised
    | SourceStatusChanged
    | BannerRaised
    | BannerCleared
)
