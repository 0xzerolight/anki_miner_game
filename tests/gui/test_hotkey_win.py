"""Windows global hotkey (spec 16).

Parsing and the registration logic run on every platform: a fake ``HotkeyApi`` stands in for
user32, and a ``WM_HOTKEY`` MSG is handed to the application's native event filters through the
real Qt event dispatcher. The ``windows_only`` tests use the real ``RegisterHotKey``.
"""

import ctypes
import sys
import threading
from ctypes import wintypes

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QAbstractEventDispatcher

from anki_miner_game.gui.hotkey_win import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    GlobalHotkey,
    Hotkey,
    HotkeyError,
    parse_hotkey,
)
from anki_miner_game.models.config import AppConfig

VK_F9 = 0x78
VK_F10 = 0x79
WM_KEYDOWN = 0x0100
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
ERROR_ACCESS_DENIED = 5
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


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


# Registration, with a fake user32.


class FakeApi:
    """``HotkeyApi`` over a dict: hotkey id -> (modifiers, vk); ``error`` makes every register fail."""

    def __init__(self, error: int = 0) -> None:
        self.error = error
        self.registered: dict[int, tuple[int, int]] = {}
        self.unregister_calls = 0
        self.last_id: int | None = None

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> int:
        self.last_id = hotkey_id
        assert hotkey_id not in self.registered, "an id is registered twice"
        assert 0 <= hotkey_id <= 0xBFFF, "application hotkey ids are 0x0000-0xBFFF"
        if self.error:
            return self.error
        self.registered[hotkey_id] = (modifiers, vk)
        return 0

    def unregister(self, hotkey_id: int) -> None:
        self.unregister_calls += 1
        del self.registered[hotkey_id]

    @property
    def only_id(self) -> int:
        (hotkey_id,) = self.registered
        return hotkey_id


def _deliver(message: int, wparam: int, event_type: bytes = b"windows_dispatcher_MSG") -> bool:
    """Hand one MSG to the app's native event filters, as Qt's Windows event dispatcher does; True if one took it."""
    msg = wintypes.MSG()
    msg.message = message
    msg.wParam = wparam
    dispatcher = QAbstractEventDispatcher.instance()
    assert dispatcher is not None
    handled, _ = dispatcher.filterNativeEvent(event_type, sip.voidptr(ctypes.addressof(msg)))
    return handled


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def hotkey(qapp, api: FakeApi):
    hk = GlobalHotkey(api=api)
    yield hk
    hk.unregister()


@pytest.fixture
def hits(hotkey: GlobalHotkey) -> list[int]:
    got: list[int] = []
    hotkey.activated.connect(lambda: got.append(1))
    return got


def test_register_asks_for_no_autorepeat(hotkey: GlobalHotkey, api: FakeApi) -> None:
    hotkey.register("Ctrl+Shift+F9")
    assert list(api.registered.values()) == [(MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_F9)]


@pytest.mark.parametrize("event_type", [b"windows_dispatcher_MSG", b"windows_generic_MSG"])
def test_wm_hotkey_emits_activated_and_is_consumed(
    hotkey: GlobalHotkey, api: FakeApi, hits: list[int], event_type: bytes
) -> None:
    hotkey.register("Ctrl+Shift+F9")
    assert _deliver(WM_HOTKEY, api.only_id, event_type) is True
    assert hits == [1]


def test_other_native_events_pass_through(hotkey: GlobalHotkey, api: FakeApi, hits: list[int]) -> None:
    hotkey.register("Ctrl+Shift+F9")
    assert _deliver(WM_HOTKEY, api.only_id + 1) is False  # another hotkey id
    assert _deliver(WM_KEYDOWN, api.only_id) is False
    assert _deliver(WM_HOTKEY, api.only_id, b"xcb_generic_event_t") is False
    assert hits == []


def test_unregister_releases_the_keys_and_stops_filtering(hotkey: GlobalHotkey, api: FakeApi, hits: list[int]) -> None:
    hotkey.register("Ctrl+Shift+F9")
    hotkey_id = api.only_id
    hotkey.unregister()
    assert api.registered == {}
    assert _deliver(WM_HOTKEY, hotkey_id) is False
    assert hits == []
    hotkey.unregister()
    assert api.unregister_calls == 1


def test_register_replaces_the_previous_hotkey(hotkey: GlobalHotkey, api: FakeApi, hits: list[int]) -> None:
    hotkey.register("Ctrl+Shift+F9")
    hotkey.register("Alt+F10")
    assert list(api.registered.values()) == [(MOD_ALT | MOD_NOREPEAT, VK_F10)]
    assert _deliver(WM_HOTKEY, api.only_id) is True
    assert hits == [1]


def test_invalid_string_leaves_no_hotkey(hotkey: GlobalHotkey, api: FakeApi) -> None:
    hotkey.register("Ctrl+Shift+F9")
    with pytest.raises(HotkeyError, match="unknown key"):
        hotkey.register("Ctrl+Foo")
    assert api.registered == {}


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (ERROR_HOTKEY_ALREADY_REGISTERED, "Ctrl\\+Shift\\+F9 is already in use by another program"),
        (ERROR_ACCESS_DENIED, "error 5"),
    ],
)
def test_refused_registration_raises_and_filters_nothing(qapp, error: int, fragment: str) -> None:
    api = FakeApi(error=error)
    hk = GlobalHotkey(api=api)
    got: list[int] = []
    hk.activated.connect(lambda: got.append(1))
    with pytest.raises(HotkeyError, match=fragment):
        hk.register(" Ctrl+Shift+F9 ")
    assert api.last_id is not None
    assert _deliver(WM_HOTKEY, api.last_id) is False
    assert got == []
    hk.unregister()
    assert api.unregister_calls == 0


@pytest.mark.skipif(sys.platform == "win32", reason="user32 exists on Windows")
def test_without_windows_register_points_to_the_cli_verb(qapp) -> None:
    with pytest.raises(HotkeyError, match="--toggle"):
        GlobalHotkey().register("Ctrl+Shift+F9")


def test_application_quit_unregisters(qapp, hotkey: GlobalHotkey, api: FakeApi) -> None:
    hotkey.register("Ctrl+Shift+F9")
    qapp.aboutToQuit.emit()
    assert api.registered == {}


def test_register_off_the_main_thread_is_refused(hotkey: GlobalHotkey, api: FakeApi) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            hotkey.register("Ctrl+Shift+F9")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert api.registered == {}


# Real RegisterHotKey (the CI Windows runner).

_UNLIKELY = "Ctrl+Alt+Shift+F24"


@pytest.mark.windows_only
def test_real_wm_hotkey_reaches_activated_through_the_event_loop(qtbot) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hk = GlobalHotkey()
    hk.register(_UNLIKELY)
    try:
        # What Windows posts when the keys are pressed: WM_HOTKEY on this thread's queue, no window.
        with qtbot.waitSignal(hk.activated, timeout=5000):
            # The id is private; nothing but this test needs it.
            assert user32.PostThreadMessageW(kernel32.GetCurrentThreadId(), WM_HOTKEY, hk._hotkey_id, 0)
    finally:
        hk.unregister()


@pytest.mark.windows_only
def test_real_keys_held_by_one_hotkey_are_refused_to_another_until_released(qapp) -> None:
    first, second = GlobalHotkey(), GlobalHotkey()
    first.register(_UNLIKELY)
    try:
        with pytest.raises(HotkeyError, match="already in use"):
            second.register(_UNLIKELY)
    finally:
        first.unregister()
    second.register(_UNLIKELY)
    second.unregister()
