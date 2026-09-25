"""The environment of every program the app starts: OBS, discovery's helpers, uv, owocr, the VAD worker.

A frozen Linux build runs with ``LD_LIBRARY_PATH`` pointing at the bundle first: the PyInstaller
bootloader puts it there and keeps the value it replaced in ``LD_LIBRARY_PATH_ORIG`` (PyInstaller
manual, "LD_LIBRARY_PATH / LIBPATH considerations"). A program started with that environment loads
the bundle's copies of glib, OpenSSL or libpython instead of its own and can fail to start. Every
spawn site passes ``child_environ()`` instead, which gives the child the original value back; the
app's own environment keeps the bundle path, since its libraries still load from there. macOS builds
are left alone (the bootloader changes no variable the child would inherit).

A frozen Windows build hands its DLL search to every program it starts instead (PyInstaller manual,
"Launching External Programs from the Frozen Application"): OBS starts through ``external_program()``.
"""

import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import PureWindowsPath
from typing import Final

LIBRARY_PATH: Final = "LD_LIBRARY_PATH"
ORIGINAL_SUFFIX: Final = "_ORIG"


def child_environ(
    environ: Mapping[str, str] | None = None, *, frozen: bool | None = None, platform: str | None = None
) -> dict[str, str]:
    """A copy of ``environ`` (default ``os.environ``) for a child process.

    In a frozen build off Windows and macOS, ``LD_LIBRARY_PATH`` becomes ``LD_LIBRARY_PATH_ORIG``, or
    is removed when the app was started without one. ``frozen`` defaults to ``sys.frozen`` and
    ``platform`` to ``sys.platform``, both read at call time.
    """
    env = dict(os.environ if environ is None else environ)
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen or (platform or sys.platform) in ("win32", "darwin"):
        return env
    original = env.get(LIBRARY_PATH + ORIGINAL_SUFFIX)
    if original is None:
        env.pop(LIBRARY_PATH, None)
    else:
        env[LIBRARY_PATH] = original
    return env


@contextmanager
def external_program(
    environ: Mapping[str, str] | None = None, *, frozen: bool | None = None, platform: str | None = None
) -> Iterator[dict[str, str]]:
    """``child_environ()`` for a program that is not part of the app, such as OBS; start it in the block.

    The PyInstaller bootloader makes the bundle folder (``sys._MEIPASS``) the DLL directory of a frozen
    Windows build (``SetDllDirectoryW``), which every program the app starts inherits, and PyQt6 puts
    the bundle's folders first on ``PATH``. OBS started that way loaded the bundle's
    ``VCRUNTIME140.dll`` instead of the system's, and Setup then closed OBS on every upgrade as a
    program using the app's files. So in a frozen Windows build the block holds the system's default
    DLL search order and the environment has no ``PATH`` entry inside the bundle; the block's end
    gives the app its DLL directory back. ``frozen`` and ``platform`` default as in ``child_environ``.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    platform = platform or sys.platform
    env = child_environ(environ, frozen=frozen, platform=platform)
    if not frozen or platform != "win32":
        yield env
        return
    bundle = PureWindowsPath(sys._MEIPASS)  # type: ignore[attr-defined]
    if "PATH" in env:
        entries = env["PATH"].split(";")
        env["PATH"] = ";".join(entry for entry in entries if not PureWindowsPath(entry).is_relative_to(bundle))
    _set_dll_directory(None)
    try:
        yield env
    finally:
        _set_dll_directory(str(bundle))


def _set_dll_directory(path: str | None) -> None:
    """``SetDllDirectoryW``; ``None`` restores the system's default DLL search order."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(path)
