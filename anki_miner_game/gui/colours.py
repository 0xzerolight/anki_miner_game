"""Colour tokens (UJ-04, UJ-11, UJ-12): status lights, tray dot, banner edges. Text never uses them:
text keeps the palette's colour (UJ-11). ``feed/page.html`` repeats ``GREY`` for its "Disconnected" dot."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from anki_miner_game.models.messages import AppState, BannerLevel, SourceStatus

GREY: Final = "#9e9e9e"  # off, not connected, idle
AMBER: Final = "#f9a825"  # waiting, connecting, ready, warning
GREEN: Final = "#43a047"  # connected
BLUE: Final = "#1e88e5"  # receiving, info
RED: Final = "#e53935"  # recording, error

SOURCE_COLOUR: Final[Mapping[SourceStatus, str]] = MappingProxyType(
    {
        SourceStatus.DISCONNECTED: GREY,
        SourceStatus.CONNECTING: AMBER,
        SourceStatus.CONNECTED: GREEN,
        SourceStatus.RECEIVING: BLUE,
    }
)
SOURCE_FILLED: Final[Mapping[SourceStatus, bool]] = MappingProxyType(
    {
        SourceStatus.DISCONNECTED: False,
        SourceStatus.CONNECTING: False,
        SourceStatus.CONNECTED: True,
        SourceStatus.RECEIVING: True,
    }
)
"""Rings (``False``) for not connected and connecting, filled dots once connected (UJ-04)."""
STATE_COLOUR: Final[Mapping[AppState, str]] = MappingProxyType(
    {
        AppState.IDLE: GREY,
        AppState.ARMED: AMBER,
        AppState.RECORDING: RED,
        AppState.FINALISING: AMBER,
    }
)
"""The tray icon's state dot (UJ-12)."""
BANNER_COLOUR: Final[Mapping[BannerLevel, str]] = MappingProxyType(
    {
        BannerLevel.INFO: BLUE,
        BannerLevel.WARNING: AMBER,
        BannerLevel.ERROR: RED,
    }
)
"""A banner's 3 px left edge (UJ-11)."""
