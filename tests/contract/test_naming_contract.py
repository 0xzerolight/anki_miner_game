"""Naming contract (spec 18.1): Anki Miner reads every session stem as exactly NN with no season.

Runs the vendored ``EpisodeNumberExtractor`` (``anki_miner_episode_matcher.py``) on the ``.mkv`` a
session would leave, for arbitrary titles and NN 1-9999. The adversarial strategy mixes the
technical tokens Anki Miner deletes before matching with S/E fragments, spaced hyphens, digits and
arbitrary Unicode: that deletion is what defeated the spec's first sanitiser (docs/m0/sanitiser.md).
"""

from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from anki_miner_game.session.naming import (
    DEFAULT_TITLE,
    MAX_INDEX,
    MIN_INDEX,
    _as_anki_miner_reads,
    sanitise_title,
    session_stem,
)
from tests.contract.anki_miner_episode_matcher import EpisodeNumberExtractor, _strip_technical_tokens
from tests.session.test_naming import COUNTEREXAMPLES, SPEC_TABLE

KNOWN_COUNTEREXAMPLE_STEMS = ["S1 1080p E2 - 03", "S1 x264 E2 - 03", "S1 v2 E3 - 04"]


def read(stem: str) -> tuple[int | None, int] | None:
    """(season, episode) as Anki Miner pairs the session's video."""
    info = EpisodeNumberExtractor.extract_episode_info(Path(f"{stem}.mkv"))
    return None if info is None else (info.season_number, info.episode_number)


# --- the adversarial title strategy -------------------------------------------------------------

_ascii_digits = st.text("0123456789", min_size=1, max_size=5)
_any_digits = st.text(st.characters(categories=["Nd"]), min_size=1, max_size=4)
_digits = st.one_of(_ascii_digits, _ascii_digits, _any_digits)


def _digit_run(low: int, high: int) -> st.SearchStrategy[str]:
    return st.one_of(
        st.text("0123456789", min_size=low, max_size=high),
        st.text(st.characters(categories=["Nd"]), min_size=low, max_size=high),
    )


_sep = st.sampled_from(["", " ", ".", "_", "-", " - ", "\t", "\u3000", "\xa0"])
_hex8 = st.text("0123456789ABCDEFabcdef", min_size=8, max_size=8)

# One strategy per deletion in Anki Miner's _strip_technical_tokens, in its order.
_technical_token = st.one_of(
    st.tuples(_digit_run(3, 4), st.sampled_from("xX"), _digit_run(3, 4)).map("".join),
    st.tuples(_digit_run(3, 4), st.sampled_from(["p", "P", "i", "I", "İ", "ı"])).map("".join),
    st.sampled_from(["x264", "X265", "h264", "H.265", "x.264", "av1", "AV1", "vp9", "Vp9"]),
    st.tuples(_digit_run(1, 2), st.sampled_from(["", " ", ".", "_", "-"]), st.sampled_from(["bit", "BIT", "Bit"])).map(
        "".join
    ),
    st.tuples(st.sampled_from("[("), _hex8, st.sampled_from("])")).map("".join),
    st.tuples(_sep, st.sampled_from("vV"), _digit_run(1, 2)).map("".join),
)
_season_episode = st.one_of(
    st.sampled_from(["S", "s", "E", "e", "Ep", "EP", "Episode", "ep.", "x", "X", "v", "V"]),
    st.tuples(st.sampled_from("SsEe"), _digits).map("".join),
    st.tuples(st.sampled_from("Ss"), _digits, _sep, st.sampled_from("Ee"), _digits).map("".join),
)
_fragment = st.one_of(
    _technical_token,
    _technical_token,
    _season_episode,
    _season_episode,
    _sep,
    _sep,
    st.sampled_from([" -", "- ", "--", " ~ ", "~", "[", "]", "(", ")", "..."]),
    _digits,
    st.text(max_size=3),
)
adversarial_titles = st.lists(_fragment, max_size=12).map("".join)
indices = st.integers(MIN_INDEX, MAX_INDEX)


# --- the contract --------------------------------------------------------------------------------


@pytest.mark.parametrize("stem", KNOWN_COUNTEREXAMPLE_STEMS)
def test_the_vendored_extractor_reproduces_the_known_counterexamples(stem):
    """The spec's first sanitiser left these stems alone; Anki Miner reads a season into each."""
    assert read(stem)[0] == 1


@pytest.mark.parametrize(("title", "stem"), SPEC_TABLE)
def test_spec_table_reads_as_its_index(title, stem):
    assert read(session_stem(title, int(stem.rsplit(" - ", 1)[1]))) == (None, int(stem.rsplit(" - ", 1)[1]))


@pytest.mark.parametrize(("title", "_sanitised"), COUNTEREXAMPLES)
def test_counterexamples_read_as_their_index(title, _sanitised):
    for index in (1, 3, 42, 999, MAX_INDEX):
        assert read(session_stem(title, index)) == (None, index)


@settings(max_examples=2000, deadline=None)
@given(adversarial_titles, indices)
@example("S1 1080p E2", 3)
@example("S1 x264 E2", 3)
@example("S1 v2 E3", 4)
def test_adversarial_titles_read_as_exactly_their_index(title, index):
    assert read(session_stem(title, index)) == (None, index)


@settings(max_examples=1000, deadline=None)
@given(st.text(), indices)
def test_arbitrary_unicode_titles_read_as_exactly_their_index(title, index):
    assert read(session_stem(title, index)) == (None, index)


@settings(max_examples=1000, deadline=None)
@given(st.one_of(adversarial_titles, st.text()))
def test_the_ported_token_deletion_matches_the_vendored_one(stem):
    """The sanitiser's copy of Anki Miner's token deletion, and its index map, agree with the original."""
    kept, where = _as_anki_miner_reads(stem)
    assert kept == _strip_technical_tokens(stem)
    assert "".join(stem[i] for i in where) == kept
    assert where == sorted(set(where))


@settings(max_examples=1000, deadline=None)
@given(adversarial_titles)
def test_the_sanitiser_deletes_no_letter_or_digit(title):
    """The guard only replaces a hyphen or inserts a ``~``: every letter and digit typed survives, in order."""
    sanitised = sanitise_title(title)
    if sanitised != DEFAULT_TITLE:
        assert _letters_and_digits(sanitised) == _letters_and_digits(title)


def _letters_and_digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())
