"""The global Start/Stop hotkey on Windows (spec 16).

``parse_hotkey`` turns the config string (``AppConfig.hotkey``, default ``Ctrl+Shift+F9``) into
``RegisterHotKey`` modifier flags and a virtual-key code; it is pure and runs on every platform.
Key names follow ``QKeySequence``'s portable text (``Ctrl``, ``Shift``, ``Alt``, ``Meta``, ``F9``,
``PgDown``, ...), so a string from a key-sequence editor parses as it reads.
"""

import string
from dataclasses import dataclass
from typing import Final

MOD_ALT: Final = 0x0001
MOD_CONTROL: Final = 0x0002
MOD_SHIFT: Final = 0x0004
MOD_WIN: Final = 0x0008

_MODIFIERS: Final[dict[str, int]] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
}

_KEYS: Final[dict[str, int]] = {
    **{c.lower(): ord(c) for c in string.ascii_uppercase + string.digits},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # VK_F1 = 0x70 ... VK_F24 = 0x87
    "space": 0x20,
    "tab": 0x09,
    "return": 0x0D,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "ins": 0x2D,
    "insert": 0x2D,
    "del": 0x2E,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pgup": 0x21,
    "pageup": 0x21,
    "pgdown": 0x22,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pause": 0x13,
    "print": 0x2C,
}
"""Lower-cased key name -> Windows virtual-key code."""


class HotkeyError(Exception):
    """The hotkey cannot be used; the message is user-facing text for a banner."""


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    """``MOD_*`` flags."""
    vk: int
    """Windows virtual-key code."""


def parse_hotkey(text: str) -> Hotkey:
    """``Ctrl+Shift+F9`` -> ``Hotkey``: parts split on ``+``, any case, spaces and order.

    Exactly one key and at least one modifier: a bare key would be taken from every other program.
    """
    if not text.strip():
        raise HotkeyError("No hotkey set.")
    modifiers = 0
    vk: int | None = None
    for raw in text.split("+"):
        part = raw.strip()
        name = part.lower()
        if not name:
            raise HotkeyError(f"Hotkey {text!r} has an empty part.")
        if name in _MODIFIERS:
            if modifiers & _MODIFIERS[name]:
                raise HotkeyError(f"Hotkey {text!r} repeats {part}.")
            modifiers |= _MODIFIERS[name]
        elif name in _KEYS:
            if vk is not None:
                raise HotkeyError(f"Hotkey {text!r} has more than one key.")
            vk = _KEYS[name]
        else:
            raise HotkeyError(f"Hotkey {text!r} has an unknown key: {part}.")
    if vk is None:
        raise HotkeyError(f"Hotkey {text!r} has no key.")
    if not modifiers:
        raise HotkeyError(f"Hotkey {text!r} needs at least one of Ctrl, Shift, Alt or Win.")
    return Hotkey(modifiers=modifiers, vk=vk)
