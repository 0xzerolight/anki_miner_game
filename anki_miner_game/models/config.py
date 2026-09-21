"""App-wide settings (spec 5), stored as ``<home>/config.json``."""

from dataclasses import dataclass, field
from typing import Final

from anki_miner_game.models.constants import MAX_CUE_SECONDS_MAX, MAX_CUE_SECONDS_MIN


@dataclass(frozen=True)
class ObsSettings:
    host: str = "127.0.0.1"
    port: int | None = None
    """``None``: read from OBS's websocket ``config.json`` at every connect."""
    password_override: str | None = field(default=None, repr=False)
    """Stored only when the user typed one; otherwise read from OBS at connect time and never stored."""


@dataclass(frozen=True)
class TextSourceConfig:
    id: str
    name: str
    uri: str
    """``host:port[/path]`` without the scheme; the source connects to ``ws://<uri>`` (spec 8.1)."""
    enabled: bool = True


DEFAULT_TEXT_SOURCES: Final = (
    TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677"),
    TextSourceConfig(id="agent", name="Agent", uri="localhost:9001"),
    TextSourceConfig(id="luna", name="LunaTranslator", uri="localhost:2333"),
)


@dataclass(frozen=True)
class FeedSettings:
    enabled: bool = True
    ws_port: int = 6678
    http_port: int = 6679


@dataclass(frozen=True)
class RecordingSettings:
    max_height: int = 1080
    fps: int = 30


@dataclass(frozen=True)
class CueSettings:
    max_cue_seconds: int = 15
    end_gap_ms: int = 350
    """Anki Miner's ``audio_padding`` (300 ms) + 50 ms; raise it by as much as that padding is raised."""

    def __post_init__(self) -> None:
        if not MAX_CUE_SECONDS_MIN <= self.max_cue_seconds <= MAX_CUE_SECONDS_MAX:
            raise ValueError(
                f"max_cue_seconds must be {MAX_CUE_SECONDS_MIN}-{MAX_CUE_SECONDS_MAX}, got {self.max_cue_seconds}"
            )
        if self.end_gap_ms < 0:
            raise ValueError(f"end_gap_ms cannot be negative, got {self.end_gap_ms}")


@dataclass(frozen=True)
class VadSettings:
    enabled: bool = True
    """The user's switch. The effective value is ``enabled and <VAD add-on installed>`` (spec 5: true
    when the add-on is installed); the VAD add-on enforces it, so a default config never runs a pass
    without it."""


@dataclass(frozen=True, kw_only=True)
class AppConfig:
    output_root: str = "~/Videos/Anki Miner Game"
    """May start with ``~``; ``paths.output_root`` expands it. ``_incoming/`` lives inside."""
    obs: ObsSettings = field(default_factory=ObsSettings)
    text_sources: tuple[TextSourceConfig, ...] = DEFAULT_TEXT_SOURCES
    feed: FeedSettings = field(default_factory=FeedSettings)
    hotkey: str = "Ctrl+Shift+F9"
    """Windows only (spec 16)."""
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    cue: CueSettings = field(default_factory=CueSettings)
    vad: VadSettings = field(default_factory=VadSettings)
    last_game: str | None = None
    """Slug of the last selected game."""
