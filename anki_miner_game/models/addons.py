"""Optional add-on state (spec 13.1, 14)."""

from enum import StrEnum


class AddonStatus(StrEnum):
    MISSING = "missing"
    INSTALLING = "installing"
    READY = "ready"
    BROKEN = "broken"
    """Installed files fail verification; reinstalling repairs it."""
