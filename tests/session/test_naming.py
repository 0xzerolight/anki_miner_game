"""Session names (spec 10.1): sanitiser, stems, indices, slugs.

What Anki Miner makes of these stems is ``tests/contract/test_naming_contract.py``.
"""

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from anki_miner_game.models.profile import is_safe_slug
from anki_miner_game.session.naming import (
    DEFAULT_TITLE,
    MAX_INDEX,
    MIN_INDEX,
    parse_index,
    sanitise_title,
    session_stem,
    slugify,
)

# Spec 10.1, verbatim.
SPEC_TABLE = [
    ("Persona 5", "Persona 5 - 01"),
    ("Steins;Gate 2010", "Steins;Gate 2010 - 03"),
    ("Zero - 3", "Zero ~ 3 - 01"),
    ("Zero Escape S2E1", "Zero Escape S2~E1 - 05"),
    ("S01E05 The Game", "S01~E05 The Game - 02"),
    ("s1 e2 spaced", "s1~e2 spaced - 08"),
    ("Fate/stay night", "Fate stay night - 12"),
    ("2x04 Edition", "2x04 Edition - 06"),
    ("Ep 5 Simulator", "Ep 5 Simulator - 04"),
    ("NieR:Automata 1.1a", "NieR Automata 1.1a - 9999"),
]

# Titles the spec's four steps turned into stems Anki Miner misreads (docs/m0/sanitiser.md),
# and what the amended sanitiser makes of them.
COUNTEREXAMPLES = [
    # Anki Miner deletes technical tokens before matching, which joins an S and an E.
    ("S1 1080p E2", "S1 1080p ~E2"),
    ("S1 x264 E2", "S1 x264 ~E2"),
    ("S1 v2 E3", "S1 v2 ~E3"),
    ("S1[0123ABCD]E2", "S1[0123ABCD]~E2"),
    ("S1 E[0123ABCD]2", "S1 ~E[0123ABCD]2"),
    ("S[0123ABCD]1 v2 E2", "S[0123ABCD]1 v2 ~E2"),
    # ... or leaves a hyphen between spaces.
    ("A 1080p- 5", "A 1080p~ 5"),
    ("A -1080p 5", "A ~1080p 5"),
    ("A 1080p-x264 5", "A 1080p~x264 5"),
    # Whitespace that is not a control character, around a hyphen.
    ("A\u3000-\u30005", "A ~ 5"),
    ("A\xa0-\xa05", "A ~ 5"),
    # Two spaced hyphens sharing a space.
    ("A - - 5", "A ~ ~ 5"),
    # A trailing spaced hyphen meets the one before NN.
    ("A -", "A ~"),
]


@pytest.mark.parametrize(("title", "stem"), SPEC_TABLE)
def test_spec_table(title, stem):
    index = int(stem.rsplit(" - ", 1)[1])
    assert session_stem(title, index) == stem
    assert sanitise_title(title) == stem.rsplit(" - ", 1)[0]


@pytest.mark.parametrize(("title", "sanitised"), COUNTEREXAMPLES)
def test_counterexamples_of_the_spec_rule(title, sanitised):
    assert sanitise_title(title) == sanitised


@pytest.mark.parametrize("char", list('<>:"/\\|?*') + ["\x00", "\x1f", "\x7f", "\x85", "\t", "\n"])
def test_forbidden_and_control_characters_become_spaces(char):
    assert sanitise_title(f"A{char}B") == "A B"


def test_whitespace_collapses_and_both_ends_are_trimmed():
    assert sanitise_title("  Persona \t\u3000 5  ") == "Persona 5"


@pytest.mark.parametrize(
    ("title", "sanitised"),
    [("Ys VIII...", "Ys VIII"), ("Ys. . .", "Ys"), ("Half-Life", "Half-Life"), ("Re-Start - 2", "Re-Start ~ 2")],
)
def test_trailing_dots_go_and_in_word_hyphens_stay(title, sanitised):
    assert sanitise_title(title) == sanitised


@pytest.mark.parametrize("title", ["", "   ", "...", " . ", "???", "\x00\x01"])
def test_empty_becomes_the_default(title):
    assert sanitise_title(title) == DEFAULT_TITLE == "Game"


@pytest.mark.parametrize(
    "title",
    [
        "魔法少女まどか☆マギカ ポータブル",
        "Tsukihime -A piece of blue glass moon-",
        "8-Bit Armies",
        "Mega Man X4",
        "Ever17 -the out of infinity-",
        "Steins;Gate 0",
    ],
)
def test_titles_without_hazards_pass_unchanged(title):
    assert sanitise_title(title) == title


@given(st.text())
def test_sanitised_titles_are_file_names_and_stable(title):
    sanitised = sanitise_title(title)
    assert sanitised
    assert sanitised == sanitised.strip()
    assert not sanitised.endswith(".")
    assert not any(ch in '<>:"/\\|?*' or unicodedata.category(ch) == "Cc" for ch in sanitised)
    assert " - " not in f"{sanitised} "
    assert sanitise_title(sanitised) == sanitised


@pytest.mark.parametrize(
    ("index", "suffix"), [(1, " - 01"), (9, " - 09"), (10, " - 10"), (99, " - 99"), (100, " - 100"), (9999, " - 9999")]
)
def test_index_is_zero_padded_to_two_digits_and_grows(index, suffix):
    assert session_stem("Persona 5", index) == "Persona 5" + suffix


@pytest.mark.parametrize("index", [0, -1, 10000])
def test_index_outside_the_extractor_range_is_refused(index):
    assert (MIN_INDEX, MAX_INDEX) == (1, 9999)
    with pytest.raises(ValueError, match="index"):
        session_stem("Persona 5", index)


@given(st.text(), st.integers(MIN_INDEX, MAX_INDEX))
def test_parse_index_inverts_session_stem(title, index):
    assert parse_index(session_stem(title, index)) == index


@pytest.mark.parametrize(
    ("stem", "index"),
    [
        ("Persona 5 - 01", 1),
        ("Persona 5 - 1", 1),
        ("Persona 5 - 123", 123),
        ("Zero ~ 3 - 01", 1),
        ("Persona 5", None),
        ("Persona 5 - 00", None),
        ("Persona 5 - 10000", None),
        ("Persona 5 -01", None),
        ("Persona 5 - 01 (copy)", None),
        ("Persona 5 - ٠١", None),
        ("- 01", None),
    ],
)
def test_parse_index(stem, index):
    assert parse_index(stem) == index


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Steins;Gate", "steins-gate"),
        ("  Persona 5 Royal  ", "persona-5-royal"),
        ("NieR:Automata", "nier-automata"),
        ("ペルソナ５ ザ・ロイヤル", "ペルソナ5-ザ-ロイヤル"),
        ("Pokémon", "pokémon"),
        ("snake_case", "snake-case"),
        ("", "game"),
        ("☆☆☆", "game"),
        ("CON", "con-game"),
        ("com¹", "com1-game"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug


@given(st.text())
def test_slugs_are_safe_file_names_and_stable(title):
    slug = slugify(title)
    assert is_safe_slug(slug)
    assert slugify(slug) == slug
