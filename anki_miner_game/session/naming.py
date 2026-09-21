"""Session names (spec 10.1): the sanitised title, ``<title> - NN`` stems, and profile slugs.

Anki Miner pairs a session like an anime episode and reads NN back out of the stem with its
``EpisodeNumberExtractor``. Every stem built here reads as exactly NN with no season;
``tests/contract/test_naming_contract.py`` holds that against a vendored copy of the extractor.
"""

import re
import unicodedata
from typing import Final

from anki_miner_game.models.profile import is_safe_slug

MIN_INDEX: Final = 1
MAX_INDEX: Final = 9999
"""Anki Miner's extractor reads at most four digits."""

DEFAULT_TITLE: Final = "Game"
DEFAULT_SLUG: Final = "game"

_FORBIDDEN_CHARS: Final = frozenset('<>:"/\\|?*')
_SPACED_HYPHEN: Final = re.compile(r"(?<=\s)-(?=\s)")
_SEASON_EPISODE: Final = re.compile(r"([Ss]\d+)[\s._-]*([Ee]\d+)")
"""Anki Miner's first pattern, which outranks the `` - NN`` release slot."""
_INDEX_SUFFIX: Final = re.compile(r" - ([0-9]{1,4})\Z")

# Ported from Anki Miner anki_miner/utils/episode_matcher.py, _strip_technical_tokens, at commit
# ea4a30ce2be4f57f30379ca3fe1ec438ff7fb8a5: the tokens it deletes from a stem before matching, in
# its order. tests/contract/test_naming_contract.py checks this copy against the vendored original.
_TECHNICAL_TOKENS: Final = (
    re.compile(r"\d{3,4}[xX]\d{3,4}"),
    re.compile(r"(?<![0-9A-Za-z])\d{3,4}[pi](?![0-9A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![0-9A-Za-z])(?:[xh]\.?26[45]|av1|vp9)(?![0-9A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![0-9A-Za-z])\d{1,2}[\s._-]?bit(?![0-9A-Za-z])", re.IGNORECASE),
    re.compile(r"[\[(][0-9A-Fa-f]{8}[\])]"),
    re.compile(r"(?<=\d)[\s._-]*[vV]\d{1,2}(?![0-9A-Za-z])"),
)
_PROBE_SUFFIX: Final = " - 01"
"""Stands in for any NN: no token reaches into `` - NN``, and NN is never part of a hazard."""


def sanitise_title(title: str) -> str:
    """The folder name and stem prefix for ``title`` (spec 10.1, amended at M0: docs/m0/sanitiser.md).

    1. Forbidden file-name characters and control characters become spaces.
    2. Whitespace collapses to single spaces; both ends are trimmed.
    3. A hyphen between spaces becomes ``~``.
    4. ``S<digits>`` + separators + ``E<digits>`` becomes ``S<digits>~E<digits>``.
    5. Trailing dots and spaces go; empty becomes ``Game``.
    6. Steps 3 and 4 also hold for the stem as Anki Miner reads it, after it deletes technical tokens.

    Idempotent, so an already sanitised title passes through unchanged.
    """
    text = "".join(" " if ch in _FORBIDDEN_CHARS or unicodedata.category(ch) == "Cc" else ch for ch in title)
    text = " ".join(text.split())
    text = _SPACED_HYPHEN.sub("~", text)
    text = _SEASON_EPISODE.sub(r"\1~\2", text)
    return _guard_token_deletion(text.rstrip(". ") or DEFAULT_TITLE)


def _guard_token_deletion(title: str) -> str:
    """Step 6: fix, one character at a time, what Anki Miner's token deletion would expose.

    Deleting ``1080p``, ``x264``, ``10bit``, ``[1A2B3C4D]`` or ``v2`` can leave a hyphen between
    spaces (read as the `` - NN`` slot) or join an ``S<digits>`` to an ``E<digits>`` (read as a
    season). The hyphen becomes ``~``; a ``~`` goes in before the ``E``. Both edits only ever remove
    a hazard, so the loop ends; nothing the user typed is deleted.
    """
    while True:
        read, where = _as_anki_miner_reads(title + _PROBE_SUFFIX)
        joined = _SEASON_EPISODE.search(read)
        if joined is not None:
            at = where[joined.start(2)]
            title = f"{title[:at]}~{title[at:]}"
            continue
        hyphen = _SPACED_HYPHEN.search(read)
        if hyphen is not None and where[hyphen.start()] < len(title):  # not the hyphen before NN
            at = where[hyphen.start()]
            title = f"{title[:at]}~{title[at + 1:]}"
            continue
        return title


def _as_anki_miner_reads(stem: str) -> tuple[str, list[int]]:
    """``stem`` with Anki Miner's technical tokens deleted, and each kept character's index in ``stem``."""
    text, where = stem, list(range(len(stem)))
    for pattern in _TECHNICAL_TOKENS:
        gone = {i for match in pattern.finditer(text) for i in range(match.start(), match.end())}
        if gone:
            text = "".join(ch for i, ch in enumerate(text) if i not in gone)
            where = [w for i, w in enumerate(where) if i not in gone]
    return text, where


def session_stem(title: str, index: int) -> str:
    """``<sanitised title> - NN``, NN zero-padded to two digits; ``title`` may already be sanitised."""
    if not MIN_INDEX <= index <= MAX_INDEX:
        raise ValueError(f"session index {index} is outside {MIN_INDEX}-{MAX_INDEX}")
    return f"{sanitise_title(title)} - {index:02d}"


def parse_index(stem: str) -> int | None:
    """NN from a stem ending in `` - NN`` (1-4 ASCII digits, 1-9999), else ``None``."""
    match = _INDEX_SUFFIX.search(stem)
    if match is None:
        return None
    index = int(match.group(1))
    return index if index >= MIN_INDEX else None


def slugify(title: str) -> str:
    """The profile slug for ``title`` (``games/<slug>.json``): NFKC, lower case, ``-`` between words.

    Letters and digits of any script are kept. Never empty, and never a Windows device name.
    """
    folded = unicodedata.normalize("NFKC", title).lower()
    kept = "".join(ch if ch.isalnum() or unicodedata.category(ch).startswith("M") else " " for ch in folded)
    slug = "-".join(kept.split()) or DEFAULT_SLUG
    return slug if is_safe_slug(slug) else f"{slug}-{DEFAULT_SLUG}"
