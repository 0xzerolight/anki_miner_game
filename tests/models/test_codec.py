import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

import pytest

from anki_miner_game.models.codec import (
    DecodeError,
    UnsupportedSchemaError,
    dump_document,
    from_dict,
    load_document,
    to_dict,
)


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class Inner:
    n: int
    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class Outer:
    name: str
    ratio: float = 0.5
    flag: bool = False
    colour: Colour = Colour.RED
    items: tuple[Inner, ...] = ()
    tags: tuple[str, ...] = ()
    maybe: Inner | None = None
    inner: Inner = field(default_factory=lambda: Inner(n=1))


@dataclass(frozen=True)
class Positive:
    n: int

    def __post_init__(self) -> None:
        if self.n <= 0:
            raise ValueError("n must be positive")


@dataclass(frozen=True)
class WithDict:
    d: dict[str, int]


SAMPLE = Outer(
    name="名前",
    ratio=1.25,
    flag=True,
    colour=Colour.BLUE,
    items=(Inner(1, "a"), Inner(2)),
    tags=("x", "y"),
    maybe=Inner(3),
    inner=Inner(4, "b"),
)


def test_to_dict_encodes_enums_tuples_and_nesting():
    assert to_dict(SAMPLE) == {
        "name": "名前",
        "ratio": 1.25,
        "flag": True,
        "colour": "blue",
        "items": [{"n": 1, "label": "a"}, {"n": 2, "label": None}],
        "tags": ["x", "y"],
        "maybe": {"n": 3, "label": None},
        "inner": {"n": 4, "label": "b"},
    }


def test_from_dict_inverts_to_dict():
    assert from_dict(Outer, to_dict(SAMPLE)) == SAMPLE


def test_missing_keys_take_defaults_and_unknown_keys_are_ignored():
    assert from_dict(Outer, {"name": "n", "added_later": 1}) == Outer(name="n")


def test_int_is_accepted_for_float():
    decoded = from_dict(Outer, {"name": "n", "ratio": 2})
    assert decoded.ratio == 2.0
    assert isinstance(decoded.ratio, float)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "Outer.name: missing"),
        ({"name": 5}, "Outer.name: expected str, got int"),
        ({"name": "n", "flag": 1}, "Outer.flag: expected bool, got int"),
        ({"name": "n", "ratio": True}, "Outer.ratio: expected float, got bool"),
        ({"name": "n", "colour": "green"}, "Outer.colour: 'green' is not a valid Colour"),
        ({"name": "n", "tags": "xy"}, "Outer.tags: expected a list, got str"),
        ({"name": "n", "items": [{"n": "1"}]}, "Outer.items[0].n: expected int, got str"),
        ({"name": "n", "inner": None}, "Outer.inner: expected an object, got NoneType"),
        ([], "Outer: expected an object, got list"),
    ],
)
def test_bad_data_raises_decode_error_naming_the_field(data, message):
    with pytest.raises(DecodeError, match=re.escape(message)):
        from_dict(Outer, data)


def test_post_init_rejection_becomes_decode_error():
    with pytest.raises(DecodeError, match="n must be positive"):
        from_dict(Positive, {"n": 0})


def test_unsupported_field_type_is_a_programming_error():
    with pytest.raises(TypeError, match="does not support"):
        from_dict(WithDict, {"d": {}})


def test_to_dict_rejects_non_dataclasses():
    with pytest.raises(TypeError):
        to_dict({"n": 1})
    with pytest.raises(TypeError):
        to_dict(Inner)


def test_dump_document_puts_schema_first_and_keeps_unicode():
    text = dump_document(Inner(7, "名"))
    assert text.endswith("\n")
    assert "名" in text
    assert list(json.loads(text)) == ["schema", "n", "label"]
    assert json.loads(text)["schema"] == 1


def test_load_document_inverts_dump_document():
    assert load_document(Outer, dump_document(SAMPLE)) == SAMPLE


def test_load_document_rejects_a_newer_schema():
    with pytest.raises(UnsupportedSchemaError) as info:
        load_document(Inner, '{"schema": 2, "n": 1}')
    assert info.value.found == 2
    assert isinstance(info.value, DecodeError)


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[1]",
        '{"n": 1}',
        '{"schema": "1", "n": 1}',
        '{"schema": true, "n": 1}',
        '{"schema": 0, "n": 1}',
        '{"schema": 1}',
    ],
)
def test_load_document_rejects_bad_text(text):
    with pytest.raises(DecodeError) as info:
        load_document(Inner, text)
    assert not isinstance(info.value, UnsupportedSchemaError)
