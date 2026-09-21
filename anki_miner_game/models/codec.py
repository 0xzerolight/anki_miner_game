"""Conversion between the frozen models and JSON documents.

Pure: builds and parses strings, never touches a file. ``store`` and
``session.manifest`` do the file I/O. Supported field types: ``bool``, ``int``,
``float``, ``str``, str-valued ``Enum``, nested dataclasses, ``tuple[X, ...]``
(a JSON list) and ``X | None``.
"""

import dataclasses
import json
import types
from enum import Enum
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

from anki_miner_game.models.constants import SCHEMA


class DecodeError(ValueError):
    """The data does not describe the expected document."""


class UnsupportedSchemaError(DecodeError):
    """The document carries a ``schema`` newer than :data:`SCHEMA`."""

    def __init__(self, found: int) -> None:
        super().__init__(f"schema {found} is newer than the supported schema {SCHEMA}")
        self.found = found


def to_dict(obj: object) -> dict[str, Any]:
    """Encode a dataclass instance: enums by value, tuples as lists, nested dataclasses as dicts."""
    if isinstance(obj, type) or not dataclasses.is_dataclass(obj):
        raise TypeError(f"expected a dataclass instance, got {type(obj).__name__}")
    return {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}


def _encode(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_dict(value)
    if isinstance(value, tuple | list):
        return [_encode(item) for item in value]
    return value


def from_dict[T](cls: type[T], data: object) -> T:
    """Decode parsed JSON into ``cls``.

    A missing key takes the field default and an unknown key is ignored. A
    missing required key, a value of the wrong JSON type, an unknown enum value,
    or a value the model's ``__post_init__`` rejects raises :class:`DecodeError`.
    """
    return cast(T, _decode(cls, data, cls.__name__))


def _decode(tp: Any, value: object, where: str) -> Any:
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        return _decode_optional(tp, value, where)
    if origin is tuple:
        return _decode_tuple(tp, value, where)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _decode_dataclass(tp, value, where)
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except (ValueError, TypeError):
            raise DecodeError(f"{where}: {value!r} is not a valid {tp.__name__}") from None
    return _decode_scalar(tp, value, where)


def _decode_optional(tp: Any, value: object, where: str) -> Any:
    args = get_args(tp)
    inner = [arg for arg in args if arg is not type(None)]
    if len(args) != 2 or len(inner) != 1:
        raise TypeError(f"{where}: the codec supports only `X | None` unions, not {tp!r}")
    return None if value is None else _decode(inner[0], value, where)


def _decode_tuple(tp: Any, value: object, where: str) -> tuple[Any, ...]:
    args = get_args(tp)
    if len(args) != 2 or args[1] is not Ellipsis:
        raise TypeError(f"{where}: the codec supports only tuple[X, ...], not {tp!r}")
    if not isinstance(value, list):
        raise DecodeError(f"{where}: expected a list, got {type(value).__name__}")
    return tuple(_decode(args[0], item, f"{where}[{i}]") for i, item in enumerate(value))


def _decode_dataclass(cls: type[Any], value: object, where: str) -> Any:
    if not isinstance(value, dict):
        raise DecodeError(f"{where}: expected an object, got {type(value).__name__}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        if f.name in value:
            kwargs[f.name] = _decode(hints[f.name], value[f.name], f"{where}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise DecodeError(f"{where}.{f.name}: missing")
    try:
        return cls(**kwargs)
    except ValueError as exc:
        raise DecodeError(f"{where}: {exc}") from exc


def _decode_scalar(tp: Any, value: object, where: str) -> Any:
    if tp is bool:
        ok = isinstance(value, bool)
    elif tp is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif tp is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif tp is str:
        ok = isinstance(value, str)
    else:
        raise TypeError(f"{where}: the codec does not support {tp!r}")
    if not ok:
        raise DecodeError(f"{where}: expected {tp.__name__}, got {type(value).__name__}")
    return float(cast(float, value)) if tp is float else value


def dump_document(obj: object) -> str:
    """JSON text for a top-level document, ``"schema"`` first, UTF-8 characters kept, trailing newline."""
    return json.dumps({"schema": SCHEMA, **to_dict(obj)}, ensure_ascii=False, indent=2) + "\n"


def load_document[T](cls: type[T], text: str) -> T:
    """Parse text written by :func:`dump_document`.

    Raises :class:`UnsupportedSchemaError` for a newer ``schema`` and
    :class:`DecodeError` for anything else that is wrong.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecodeError(f"not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise DecodeError(f"expected a JSON object, got {type(data).__name__}")
    schema = data.pop("schema", None)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise DecodeError(f"missing or invalid schema: {schema!r}")
    if schema > SCHEMA:
        raise UnsupportedSchemaError(schema)
    if schema < SCHEMA:
        raise DecodeError(f"unknown schema {schema}")
    return from_dict(cls, data)
