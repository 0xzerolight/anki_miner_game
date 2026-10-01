"""The actor's banner keys the GUI acts on (UJ-02 pending ends, UJ-10 Set up OBS…). The GUI may not
import ``session``; ``tests/gui/test_gui_shared.py`` pins these to ``session.session.BannerKey``."""

from typing import Final

from anki_miner_game.models.messages import START_FAILED_BANNER_KEY, STOP_FAILED_BANNER_KEY

OBS_BANNER_KEY: Final = "obs"
ARM_BANNER_KEY: Final = "arm"
INTERNAL_BANNER_KEY: Final = "internal_error"
FOREIGN_RECORDING_BANNER_KEY: Final = "foreign_recording"
SESSION_FILES_BANNER_KEY: Final = "session_files"

ARM_FAILED_KEYS: Final = frozenset({ARM_BANNER_KEY, OBS_BANNER_KEY, INTERNAL_BANNER_KEY})
"""A Get ready (ARM) that does not reach ``ARMED`` raises one of these (P1 pins it)."""
START_FAILED_KEYS: Final = frozenset(
    {START_FAILED_BANNER_KEY, FOREIGN_RECORDING_BANNER_KEY, SESSION_FILES_BANNER_KEY, INTERNAL_BANNER_KEY}
)
"""A Start that does not reach ``RECORDING`` raises one of these (P1 pins it). A recording that starts
but cannot become a session raises ``foreign_recording`` or ``session_files``; for a Start both come
from ``_on_started`` (OBS keeps recording, the state stays Ready, the banner says what to do). The Idle
one-click (ARM then START) ends at ``ARM_FAILED_KEYS | START_FAILED_KEYS``."""
STOP_FAILED_KEYS: Final = frozenset({STOP_FAILED_BANNER_KEY, INTERNAL_BANNER_KEY})
"""A Stop that does not reach ``FINALISING`` raises one of these (P1 pins it)."""
DONE_FAILED_KEYS: Final = frozenset({ARM_BANNER_KEY, INTERNAL_BANNER_KEY})
"""A Done playing (DISARM) that does not reach ``IDLE`` raises one of these (P1 pins it)."""
SET_UP_OBS_KEYS: Final = frozenset({OBS_BANNER_KEY})
"""Banners that carry the Set up OBS… button while idle (UJ-10)."""
