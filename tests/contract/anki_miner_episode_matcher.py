"""Anki Miner's episode-number extractor, vendored for the naming contract test (spec 18.1).

Source: Anki Miner ``anki_miner/utils/episode_matcher.py`` at commit
``ea4a30ce2be4f57f30379ca3fe1ec438ff7fb8a5`` (GPL-3.0-or-later). ``EpisodeInfo``,
``_strip_technical_tokens`` and ``EpisodeNumberExtractor`` are copied verbatim; never edit them by
hand. ``scripts/diff_vendored_matcher.py <anki_miner checkout>`` reports drift; to refresh, copy the
three definitions again and update ``VENDORED_COMMIT``.
"""

import re
from dataclasses import dataclass
from pathlib import Path

VENDORED_COMMIT = "ea4a30ce2be4f57f30379ca3fe1ec438ff7fb8a5"


@dataclass
class EpisodeInfo:
    """Information extracted from episode filename."""

    file_path: Path
    episode_number: int
    season_number: int | None = None

    @property
    def filename(self) -> str:
        """Get filename."""
        return self.file_path.name


def _strip_technical_tokens(name: str) -> str:
    """Strip technical metadata that confuses episode-number regexes.

    Resolution tokens like "1280x720" otherwise get parsed as
    season=1280, episode=720 by the NxN pattern (Issue #36).
    """
    name = re.sub(r"\d{3,4}[xX]\d{3,4}", "", name)
    # Use explicit non-alphanumeric boundaries rather than \b: \b does not
    # fire between an underscore and a digit (both are word chars), so
    # "Show_03_720p" kept "720p" — which the old consuming BARE_NUMBER regex
    # silently skipped but the lookahead form would mine as episode 720.
    name = re.sub(r"(?<![0-9A-Za-z])\d{3,4}[pi](?![0-9A-Za-z])", "", name, flags=re.IGNORECASE)
    # Strip release-encoding tags whose embedded digits otherwise win the
    # trailing-number fallback (Issue #80): video codec (x264/x265/h264/
    # h265/av1/vp9), color bit-depth (10-bit/8bit), and the 8-hex CRC32
    # checksum fansub groups append, e.g. "[3EEAABE6]". In the standard
    # "[Group] Title - 03 - EpTitle [BD 1080p x265 10-bit][3EEAABE6]" format
    # the real episode ("- 03 -") otherwise loses to "265", "10", or a
    # checksum digit, collapsing distinct episodes onto one number and
    # mispairing them. All three strips are required: any one left in leaves
    # a different trailing junk number that re-triggers the collision.
    name = re.sub(r"(?<![0-9A-Za-z])(?:[xh]\.?26[45]|av1|vp9)(?![0-9A-Za-z])", "", name, flags=re.IGNORECASE)
    name = re.sub(r"(?<![0-9A-Za-z])\d{1,2}[\s._-]?bit(?![0-9A-Za-z])", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[\[(][0-9A-Fa-f]{8}[\])]", "", name)
    # A re-upload's version marker ("24 V2", "24_v2", "S01E02v5") sits
    # directly against the episode digits, so an unstripped name lets the
    # bare-number fallback's LAST-run pick (BARE_NUMBER, below) grab the
    # version digit instead of the real episode number — "Show_24_v2.mkv"
    # used to mine as episode 2. The lookbehind requires a digit
    # immediately (past only separator chars) before the marker, so a title
    # token with no digit in front — "Show V2 - 03" — is left alone.
    # MUST run after the codec/bit-depth strips above, not before: a codec
    # or bit-depth token can sit between the episode digits and the marker
    # ("05 x265 10bit v2"), and those tokens are letters, not separator
    # characters, so the lookbehind cannot cross them until that token is
    # already gone. Run this strip first and the marker survives untouched
    # (it gets exactly one `re.sub` pass, never a second chance), the codec/
    # bit-depth strips then expose "05   v2" too late for any pattern to
    # notice, and the bare-number fallback picks the "2" from "v2" instead
    # of the real episode number. Locked by
    # test_bit_depth_and_version_marker_together and the "x265 10bit" row of
    # test_version_marker_resolves_real_episode, both in
    # test_episode_matcher.py.
    name = re.sub(r"(?<=\d)[\s._-]*[vV]\d{1,2}(?![0-9A-Za-z])", "", name)
    return name


class EpisodeNumberExtractor:
    """Extract episode numbers from filenames using regex patterns."""

    # Just numbers: 01, 001, 1 (at boundaries or after non-digits). Last-resort
    # fallback handled separately (findall + LAST match) so numeric show titles
    # like "86", "Mob Psycho 100", "Steins;Gate 0", "3-gatsu" don't steal the
    # episode slot from the trailing episode number. See extract_episode_info.
    #
    # The trailing boundary is a LOOKAHEAD, not a consuming class: consuming the
    # separator made non-overlapping findall skip a number that immediately
    # followed a single-char-separated number ("Title 1 2" -> ["1"], "5-6-7" ->
    # ["5","7"]), so bare[-1] picked the wrong episode. The lookahead keeps the
    # separator available to start the next match, so every run is captured.
    # Four digits, not three: a long-running show goes past 999 (One Piece is
    # past 1100, Detective Conan past 1000), and a name outside the fansub
    # release slot — "One Piece 1085.mkv", "One.Piece.1085.mkv" — has only this
    # fallback, so a 3-digit cap extracted nothing at all and the file was
    # dropped from pairing. Five digits and up stay invisible: both boundaries
    # require a non-digit, so a date stamp or an id matches nothing.
    BARE_NUMBER = r"(?:^|[^\d])(\d{1,4})(?=[^\d]|$)"

    # The one 4-digit run that is never an episode number. A release year sits
    # after the episode as often as before it ("Show_05_(2019)"), so the
    # LAST-run pick below would take it in preference to the real episode; it
    # is skipped rather than stripped from the name, so an explicit marker
    # ("S01E2019", "Show - 2019 -") still resolves through PATTERNS. The band
    # ends at 2099, well above any episode count a show will reach.
    YEAR_LIKE = r"(?:19|20)\d{2}"

    # Regex patterns for common episode naming conventions (in priority order).
    # Each is tried with re.search (FIRST match) before falling back to
    # BARE_NUMBER. The bare-number fallback is intentionally excluded here.
    PATTERNS = [
        # S01E01, s1e1, S01 E01, S01.E01, S01-E01 (season + episode). The
        # separator class lets "Show.S02 E05" be read as season 2 / episode 5
        # instead of falling through to BARE_NUMBER and mining the season.
        (r"[Ss](\d+)[\s._-]*[Ee](\d+)", lambda m: (int(m.group(1)), int(m.group(2)))),
        # Fansub release slot: "Title - 01v2 [1080p]" or "Title - 01 - Name".
        # The following delimiter is required so an internal title fragment such
        # as "- 5 Centimeters" cannot steal the episode slot.
        (r"\s+-\s+(\d{1,4})(?:[vV]\d+)?(?=\s*(?:-|[\[(]|$))", lambda m: (None, int(m.group(1)))),
        # 1x01, 1X01 (season x episode)
        (r"(\d+)[xX](\d+)", lambda m: (int(m.group(1)), int(m.group(2)))),
        # Episode 01, Ep01, ep.01, episode_01 (no season)
        (
            r"(?<![0-9A-Za-z])[Ee][Pp](?:isode)?[\s._-]*(\d+)(?![0-9A-Za-z])",
            lambda m: (None, int(m.group(1))),
        ),
    ]

    @classmethod
    def extract_episode_info(cls, file_path: Path) -> EpisodeInfo | None:
        """Extract episode number from filename.

        Args:
            file_path: Path to video or subtitle file

        Returns:
            EpisodeInfo if episode number found, None otherwise
        """
        filename = _strip_technical_tokens(file_path.stem)

        for pattern, extractor in cls.PATTERNS:
            match = re.search(pattern, filename)
            if match:
                season, episode = extractor(match)
                return EpisodeInfo(file_path, episode, season)

        # A number attached to an embedded "ep" token belongs to the ordinary
        # word, not the episode marker (for example, "Step 3"). Prefer the
        # remaining candidates over it — but when it holds the ONLY number in
        # the name ("Step_03.mkv"), it is also the only episode candidate, so
        # fall back to the unsuppressed name rather than extracting nothing.
        suppressed = re.sub(
            r"(?<=[0-9A-Za-z])[Ee][Pp](?:isode)?[\s._-]*\d+(?![0-9A-Za-z])",
            "",
            filename,
        )

        # Last resort: bare number. Take the LAST run, not the first — numeric
        # show titles ("86 - 03", "Mob Psycho 100 - 05") put the title number
        # first and the episode number last; taking the first collapsed every
        # file in the folder onto the title number (T-04).
        bare = cls._bare_candidates(suppressed) or cls._bare_candidates(filename)
        if bare:
            return EpisodeInfo(file_path, bare[-1], None)

        return None

    @classmethod
    def _bare_candidates(cls, name: str) -> list[int]:
        """Bare numbers in *name* that could be an episode number, in order.

        Year-shaped runs are dropped here rather than in the suppressed/
        unsuppressed pair above, so a name whose only suppressed candidate is a
        year still falls back to the unsuppressed reading instead of resolving
        to nothing.
        """
        return [int(run) for run in re.findall(cls.BARE_NUMBER, name) if not re.fullmatch(cls.YEAR_LIKE, run)]
