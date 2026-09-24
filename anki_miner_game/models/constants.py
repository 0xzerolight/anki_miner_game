"""Shared constants (spec 5, 8.2, 9). Module constants, never settings."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

SCHEMA: Final = 1
"""Value of the ``"schema"`` key in every JSON document the app writes."""

SKIP_MS: Final = 300
MIN_CUE_MS: Final = 500
START_SHIFT_MS: Final[Mapping[str, int]] = MappingProxyType({"hook": -400, "ocr": -1000})
"""Keyed by ``TextMode`` value; a ``TextMode`` member works as the key. A hooker delivers a line after
its voice has started (the game voices it before the first glyph is drawn, and the hooker sends it
later still), and OCR only once the text stops changing, so each cue starts earlier than its line."""

MAX_LINE_CHARS: Final = 300
TYPEWRITER_WINDOW_S: Final = 2.0

MAX_CUE_SECONDS_MIN: Final = 5
MAX_CUE_SECONDS_MAX: Final = 60

OBS_PROFILE_NAME: Final = "Anki Miner Game"
OBS_COLLECTION_NAME: Final = "Anki Miner Game"
OBS_SCENE_NAME: Final = "Game"
INCOMING_DIRNAME: Final = "_incoming"
