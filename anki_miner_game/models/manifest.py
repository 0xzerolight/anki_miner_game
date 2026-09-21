"""Session manifest ``<stem>.session.json`` (spec 5, 10). Field names are the JSON keys."""

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Self

from anki_miner_game.models.codec import dump_document, load_document
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.profile import TextMode


class ManifestState(StrEnum):
    RECORDING = "recording"
    FINALISE_PENDING = "finalise_pending"
    READY = "ready"
    VAD_RUNNING = "vad_running"


class Flag(StrEnum):
    SPLIT_UNSUPPORTED = "split_unsupported"
    CLOCK_DEGRADED = "clock_degraded"
    OBS_EXITED = "obs_exited"
    NO_CUES = "no_cues"


class ClockKind(StrEnum):
    EVENT = "event"
    OUTPUT_DURATION = "output_duration"


class VadState(StrEnum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    """The add-on is not installed; the live subtitle stands."""
    RESTORED = "restored"
    """The user restored the untrimmed subtitle from ``live_cues``."""


@dataclass(frozen=True)
class GameRef:
    slug: str
    title: str


@dataclass(frozen=True)
class ObsRecord:
    version: str
    websocket: str
    profile: str
    collection: str
    output_path: str
    """``RecordStateChanged.outputPath`` at STARTED, verbatim."""


@dataclass(frozen=True)
class DriftSample:
    at_ms: int
    """Record-clock offset when sampled."""
    output_duration_ms: int
    """``GetRecordStatus.outputDuration`` at that moment; a check, never an input (spec 7)."""


@dataclass(frozen=True)
class ClockRecord:
    kind: ClockKind = ClockKind.EVENT
    zero_event: str = "STARTED"
    """The moment that is offset 0; M0 measures which one (spec 7)."""
    capture_latency_ms: int = 0
    degraded: bool = False
    drift_samples: tuple[DriftSample, ...] = ()


@dataclass(frozen=True)
class Counts:
    received: int = 0
    accepted: int = 0
    duplicate: int = 0
    no_letters: int = 0
    junk: int = 0
    paused: int = 0
    skip: int = 0

    def incremented(self, name: str, by: int = 1) -> Self:
        """A copy with counter ``name`` raised by ``by``; ``KeyError`` for a name that is not a counter."""
        if name not in COUNT_NAMES:
            raise KeyError(name)
        return replace(self, **{name: getattr(self, name) + by})


COUNT_NAMES: frozenset[str] = frozenset(f.name for f in fields(Counts))


@dataclass(frozen=True)
class LiveCue:
    """A cue as computed before the VAD pass, with the manifest's key names (spec 5)."""

    i: int
    start_ms: int
    end_ms: int
    text: str
    source: str

    @classmethod
    def from_cue(cls, cue: Cue) -> Self:
        return cls(i=cue.index, start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text, source=cue.source_id)

    def to_cue(self) -> Cue:
        return Cue(index=self.i, start_ms=self.start_ms, end_ms=self.end_ms, text=self.text, source_id=self.source)


@dataclass(frozen=True)
class VadRecord:
    state: VadState
    model: str | None = None
    trimmed: int = 0
    no_speech: int = 0
    message: str | None = None
    """Why the pass failed or is unavailable; shown in the session row (spec 13, 17)."""


@dataclass(frozen=True)
class FilesRecord:
    """Final names inside the game folder, set by finalise step 4 (spec 10.3)."""

    video: str
    subtitle: str | None
    """``None`` when the session has no cues (flag ``no_cues``)."""


@dataclass(frozen=True, kw_only=True)
class SessionManifest:
    app_version: str
    game: GameRef
    index: int
    """The reserved session number NN (spec 10.2)."""
    state: ManifestState
    started_at: str
    """UTC, ISO 8601 with a ``Z`` suffix."""
    stopped_at: str | None = None
    obs: ObsRecord
    clock: ClockRecord = field(default_factory=ClockRecord)
    text_mode: TextMode
    sources_used: tuple[str, ...] = ()
    counts: Counts = field(default_factory=Counts)
    flags: tuple[Flag, ...] = ()
    live_cues: tuple[LiveCue, ...] = ()
    vad: VadRecord | None = None
    files: FilesRecord | None = None


def to_json(manifest: SessionManifest) -> str:
    """The manifest as JSON text with ``"schema": 1`` first."""
    return dump_document(manifest)


def from_json(text: str) -> SessionManifest:
    """Parse manifest text; raises ``codec.DecodeError`` (``UnsupportedSchemaError`` for a newer schema)."""
    return load_document(SessionManifest, text)
