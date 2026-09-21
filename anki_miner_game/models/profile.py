"""Per-game profile (spec 5), stored as ``<home>/games/<slug>.json``."""

import sys
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class TextMode(StrEnum):
    HOOK = "hook"
    """Hooker websockets and the clipboard."""
    OCR = "ocr"
    """owocr only."""


class CaptureKind(StrEnum):
    AUTO = "auto"
    GAME = "game"
    WINDOW = "window"
    PIPEWIRE = "pipewire"
    XCOMPOSITE = "xcomposite"


class AudioMode(StrEnum):
    APP = "app"
    """One application's audio; needs ``capture.window``."""
    DESKTOP = "desktop"


class OcrEngine(StrEnum):
    ONEOCR = "oneocr"
    MEIKIOCR = "meikiocr"
    GLENS = "glens"
    BING = "bing"


def default_audio_mode(platform: str | None = None) -> AudioMode:
    """``app`` on Windows, ``desktop`` elsewhere; ``platform`` defaults to ``sys.platform`` read at call time."""
    return AudioMode.APP if (platform or sys.platform) == "win32" else AudioMode.DESKTOP


def default_ocr_engine(platform: str | None = None) -> OcrEngine:
    """``oneocr`` on Windows, ``meikiocr`` elsewhere; ``platform`` defaults to ``sys.platform`` read at call time."""
    return OcrEngine.ONEOCR if (platform or sys.platform) == "win32" else OcrEngine.MEIKIOCR


_FORBIDDEN_CHARS: Final = frozenset('/\\:<>"|?*')
_WINDOWS_DEVICE_NAMES: Final = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789¹²³"]
)


def is_safe_slug(slug: str) -> bool:
    """Whether ``<slug>.json`` is a valid file name on Windows and Linux.

    Rejects the empty string, a leading dot, a trailing dot or space, path
    separators, the characters Windows forbids (``< > : " | ? *``), control
    characters, and Windows device names (``CON``, ``nul.x``, ``COM1``, ...).
    """
    if not slug or slug.startswith(".") or slug.endswith((".", " ")):
        return False
    if any(ch in _FORBIDDEN_CHARS or unicodedata.category(ch) == "Cc" for ch in slug):
        return False
    return slug.split(".", 1)[0].rstrip(" ").upper() not in _WINDOWS_DEVICE_NAMES


@dataclass(frozen=True)
class CaptureSettings:
    kind: CaptureKind = CaptureKind.AUTO
    window: str | None = None
    """The OBS window string picked in the profile dialog, stored verbatim (spec 11.3)."""


@dataclass(frozen=True)
class AudioSettings:
    mode: AudioMode = field(default_factory=default_audio_mode)


@dataclass(frozen=True)
class FilterSettings:
    speaker_strip: bool = True
    typewriter_merge: bool = False


@dataclass(frozen=True)
class OcrSettings:
    engine: OcrEngine = field(default_factory=default_ocr_engine)
    language: str = "ja"
    window_title: str | None = None
    rects: str | None = None
    """owocr's ``Selected coordinates`` / ``Selected window coordinates`` value, verbatim (spec 14)."""


@dataclass(frozen=True)
class AutoSettings:
    enabled: bool = False
    """Auto mode for this game (spec 12); the other fields apply only while it is on."""
    start_on_first_line: bool = False
    stop_idle_minutes: int = 10
    """``0`` disables the idle stop."""
    stop_on_window_close: bool = True


@dataclass(frozen=True, kw_only=True)
class GameProfile:
    slug: str
    title: str
    """What the user typed; the folder and stem use the sanitised form (spec 10.1)."""
    text_mode: TextMode = TextMode.HOOK
    source_ids: tuple[str, ...] | None = None
    """Hook mode: ids from ``AppConfig.text_sources``; ``None`` means every enabled source."""
    clipboard: bool = False
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    auto: AutoSettings = field(default_factory=AutoSettings)


def validate(profile: GameProfile) -> list[str]:
    """Problems that stop the profile from being saved or armed; empty when it is valid."""
    problems: list[str] = []
    if not is_safe_slug(profile.slug):
        problems.append(f"slug {profile.slug!r} cannot be a file name")
    if not profile.title.strip():
        problems.append("title is empty")
    if profile.text_mode is TextMode.OCR and profile.source_ids:
        problems.append("OCR mode cannot use hook sources")
    if profile.text_mode is TextMode.OCR and profile.clipboard:
        problems.append("OCR mode cannot use the clipboard")
    if profile.audio.mode is AudioMode.APP and not profile.capture.window:
        problems.append("application audio needs a pinned window")
    if profile.auto.stop_idle_minutes < 0:
        problems.append("auto stop idle minutes cannot be negative")
    return problems
