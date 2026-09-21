"""Results of the text pipeline (spec 8.2)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from anki_miner_game.models.lines import GameLine


class DropReason(StrEnum):
    EMPTY = "empty"
    NO_LETTERS = "no_letters"
    JUNK = "junk"
    DUPLICATE = "duplicate"


DROP_COUNTER: Final[Mapping[DropReason, str]] = MappingProxyType(
    {
        DropReason.EMPTY: "no_letters",  # the manifest has no ``empty`` counter (master plan section 1)
        DropReason.NO_LETTERS: "no_letters",
        DropReason.JUNK: "junk",
        DropReason.DUPLICATE: "duplicate",
    }
)
"""The ``Counts`` field each drop reason increments."""


@dataclass(frozen=True)
class Accepted:
    line: GameLine


@dataclass(frozen=True)
class Replaced:
    """Typewriter merge: ``line`` replaces the previous accepted line and keeps its offset (step 9)."""

    line: GameLine


@dataclass(frozen=True)
class Dropped:
    reason: DropReason


PipelineResult = Accepted | Replaced | Dropped
