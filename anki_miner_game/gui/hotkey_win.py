"""The global Start/Stop hotkey on Windows (spec 16).

``parse_hotkey`` turns the config string (``AppConfig.hotkey``, default ``Ctrl+Shift+F9``) into
``RegisterHotKey`` modifier flags and a virtual-key code; it is pure and runs on every platform.
Key names follow ``QKeySequence``'s portable text (``Ctrl``, ``Shift``, ``Alt``, ``Meta``, ``F9``,
``PgDown``, ...), so a string from a key-sequence editor parses as it reads.

``GlobalHotkey`` registers it through ``ctypes`` for the Qt main thread's message queue (no
window) and emits ``activated`` from a ``QAbstractNativeEventFilter`` that takes the ``WM_HOTKEY``
Qt's Windows event dispatcher hands it. The module imports on every platform; only the user32
calls are Windows-only. Linux has no global hotkey: the desktop binds the ``--toggle`` CLI verb.
"""

import ctypes
import itertools
import logging
import string
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Final, Protocol

from PyQt6 import sip
from PyQt6.QtCore import QAbstractNativeEventFilter, QByteArray, QCoreApplication, QObject, QThread, pyqtSignal

logger = logging.getLogger(__name__)

MOD_ALT: Final = 0x0001
MOD_CONTROL: Final = 0x0002
MOD_SHIFT: Final = 0x0004
MOD_WIN: Final = 0x0008
MOD_NOREPEAT: Final = 0x4000
"""Always added on registration: holding the keys down must not toggle Start/Stop over and over."""

WM_HOTKEY: Final = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED: Final = 1409

_MSG_EVENT_TYPES: Final = (b"windows_dispatcher_MSG", b"windows_generic_MSG")
"""Qt's Windows native event types carrying a ``MSG``: a thread-queue ``WM_HOTKEY`` arrives as the
first; the second is for messages to a window. Consuming the message in the filter means it is
seen once whichever path brings it."""

_NO_RESULT: Final = sip.voidptr(0)

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


class HotkeyApi(Protocol):
    """The two user32 calls, for the calling thread's message queue (``hWnd`` ``NULL``)."""

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> int:
        """``RegisterHotKey``: 0 on success, else the Win32 error code."""
        ...

    def unregister(self, hotkey_id: int) -> None:
        """``UnregisterHotKey``."""
        ...


if sys.platform == "win32":

    class _User32:
        def __init__(self) -> None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._register = user32.RegisterHotKey
            self._register.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
            self._register.restype = wintypes.BOOL
            self._unregister = user32.UnregisterHotKey
            self._unregister.argtypes = (wintypes.HWND, ctypes.c_int)
            self._unregister.restype = wintypes.BOOL

        def register(self, hotkey_id: int, modifiers: int, vk: int) -> int:
            if self._register(None, hotkey_id, modifiers, vk):
                return 0
            return ctypes.get_last_error()

        def unregister(self, hotkey_id: int) -> None:
            self._unregister(None, hotkey_id)


def _platform_api() -> HotkeyApi | None:
    if sys.platform == "win32":
        return _User32()
    return None


_hotkey_ids = itertools.count(1)
"""``RegisterHotKey`` ids, unique per ``GlobalHotkey`` (applications use 0x0000-0xBFFF)."""


class _WmHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int, on_hotkey: Callable[[], None]) -> None:
        super().__init__()
        self._hotkey_id = hotkey_id
        self._on_hotkey = on_hotkey

    def nativeEventFilter(
        self, event_type: QByteArray | bytes | bytearray | memoryview, message: sip.voidptr
    ) -> tuple[bool, sip.voidptr]:
        if QByteArray(event_type).data() in _MSG_EVENT_TYPES:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                self._on_hotkey()
                return True, _NO_RESULT
        return False, _NO_RESULT


class GlobalHotkey(QObject):
    """One system-wide hotkey; ``activated`` is emitted on the main thread each time it is pressed.

    Create and register on the Qt main thread: ``WM_HOTKEY`` goes to the registering thread's queue,
    and the native event filter sees only the main thread's. Unregisters when the application quits.
    """

    activated = pyqtSignal()

    def __init__(self, parent: QObject | None = None, *, api: HotkeyApi | None = None) -> None:
        super().__init__(parent)
        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("GlobalHotkey needs a QApplication")
        self._app = app
        self._api = api if api is not None else _platform_api()
        self._hotkey_id = next(_hotkey_ids)
        self._filter: _WmHotkeyFilter | None = None
        app.aboutToQuit.connect(self.unregister)

    def register(self, text: str) -> None:
        """Make *text* the hotkey, replacing the registered one.

        Raises ``HotkeyError`` (user-facing text) when the string is invalid, when another program
        holds the keys, or off Windows; no hotkey is registered then.
        """
        if QThread.currentThread() is not self._app.thread():
            raise RuntimeError("GlobalHotkey.register must run on the Qt main thread")
        self.unregister()
        hotkey = parse_hotkey(text)
        if self._api is None:
            raise HotkeyError(
                "Global hotkeys work only on Windows. Bind `anki_miner_game --toggle` in your desktop's"
                " shortcut settings instead."
            )
        shown = text.strip()
        error = self._api.register(self._hotkey_id, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk)
        if error == ERROR_HOTKEY_ALREADY_REGISTERED:
            raise HotkeyError(f"{shown} is already in use by another program. Choose another hotkey in Settings.")
        if error:
            raise HotkeyError(f"Windows refused the hotkey {shown} (error {error}).")
        self._filter = _WmHotkeyFilter(self._hotkey_id, self.activated.emit)
        self._app.installNativeEventFilter(self._filter)
        logger.info("Global hotkey %s registered", shown)

    def unregister(self) -> None:
        """Release the keys; a no-op when no hotkey is registered."""
        if self._filter is None:
            return
        self._app.removeNativeEventFilter(self._filter)
        self._filter = None
        if self._api is not None:
            self._api.unregister(self._hotkey_id)
