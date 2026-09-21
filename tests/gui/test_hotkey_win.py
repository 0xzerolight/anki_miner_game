"""Windows global hotkey (spec 16): hotkey-string parsing, on every platform."""

import pytest

from anki_miner_game.gui.hotkey_win import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    Hotkey,
    HotkeyError,
    parse_hotkey,
)
from anki_miner_game.models.config import AppConfig

VK_F9 = 0x78


def test_default_config_hotkey_is_ctrl_shift_f9() -> None:
    assert parse_hotkey(AppConfig().hotkey) == Hotkey(modifiers=MOD_CONTROL | MOD_SHIFT, vk=VK_F9)


@pytest.mark.parametrize("text", [" ctrl + shift + f9 ", "SHIFT+CTRL+F9", "Control+Shift+F9", "F9+Shift+Ctrl"])
def test_case_spacing_order_and_aliases_do_not_matter(text: str) -> None:
    assert parse_hotkey(text) == Hotkey(modifiers=MOD_CONTROL | MOD_SHIFT, vk=VK_F9)


@pytest.mark.parametrize(
    ("text", "modifiers"),
    [
        ("Alt+F9", MOD_ALT),
        ("Win+F9", MOD_WIN),
        ("Meta+F9", MOD_WIN),  # what QKeySequence calls the Windows key
        ("Ctrl+Alt+Shift+Win+F9", MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN),
    ],
)
def test_modifiers(text: str, modifiers: int) -> None:
    assert parse_hotkey(text) == Hotkey(modifiers=modifiers, vk=VK_F9)


@pytest.mark.parametrize(
    ("key", "vk"),
    [
        ("F1", 0x70),
        ("F12", 0x7B),
        ("F24", 0x87),
        ("A", 0x41),
        ("z", 0x5A),
        ("0", 0x30),
        ("9", 0x39),
        ("Space", 0x20),
        ("Tab", 0x09),
        ("Return", 0x0D),
        ("Enter", 0x0D),
        ("Esc", 0x1B),
        ("Escape", 0x1B),
        ("Backspace", 0x08),
        ("Ins", 0x2D),
        ("Insert", 0x2D),
        ("Del", 0x2E),
        ("Delete", 0x2E),
        ("Home", 0x24),
        ("End", 0x23),
        ("PgUp", 0x21),
        ("PageUp", 0x21),
        ("PgDown", 0x22),
        ("PageDown", 0x22),
        ("Left", 0x25),
        ("Up", 0x26),
        ("Right", 0x27),
        ("Down", 0x28),
        ("Pause", 0x13),
        ("Print", 0x2C),
    ],
)
def test_key_names_map_to_virtual_key_codes(key: str, vk: int) -> None:
    assert parse_hotkey(f"Ctrl+{key}") == Hotkey(modifiers=MOD_CONTROL, vk=vk)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "No hotkey"),
        ("   ", "No hotkey"),
        ("F9", "needs at least one of Ctrl, Shift, Alt or Win"),
        ("Ctrl+Shift", "has no key"),
        ("Ctrl+F9+F10", "more than one key"),
        ("Ctrl+Ctrl+F9", "repeats Ctrl"),
        ("Ctrl+Control+F9", "repeats Control"),
        ("Win+Meta+F9", "repeats Meta"),
        ("Ctrl+F25", "unknown key: F25"),
        ("Ctrl+F0", "unknown key: F0"),
        ("Ctrl+Foo", "unknown key: Foo"),
        ("Ctrl+AB", "unknown key: AB"),
        ("Ctrl++F9", "empty part"),
        ("Ctrl+F9+", "empty part"),
    ],
)
def test_invalid_hotkeys_raise_with_a_reason(text: str, fragment: str) -> None:
    with pytest.raises(HotkeyError, match=fragment):
        parse_hotkey(text)
