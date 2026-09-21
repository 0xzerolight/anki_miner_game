"""The environment of every program the app starts: OBS, discovery's helpers, uv, owocr, the VAD worker.

A frozen Linux build runs with ``LD_LIBRARY_PATH`` pointing at the bundle first: the PyInstaller
bootloader puts it there and keeps the value it replaced in ``LD_LIBRARY_PATH_ORIG`` (PyInstaller
manual, "LD_LIBRARY_PATH / LIBPATH considerations"). A program started with that environment loads
the bundle's copies of glib, OpenSSL or libpython instead of its own and can fail to start. Every
spawn site passes ``child_environ()`` instead, which gives the child the original value back; the
app's own environment keeps the bundle path, since its libraries still load from there. Windows and
macOS builds are left alone (the bootloader changes no variable the child would inherit).
"""

import os
import sys
from collections.abc import Mapping
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
