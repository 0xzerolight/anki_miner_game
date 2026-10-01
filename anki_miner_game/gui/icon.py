"""The app icon (UJ-12): the window, the taskbar and the tray's base image."""

from pathlib import Path
from typing import Final

from PyQt6.QtGui import QIcon

APP_ICON_PATH: Final = Path(__file__).with_name("anki-miner-game.png")
"""256 px app icon; ``anki_miner_game.spec`` bundles it beside this module (P3)."""
DESKTOP_FILE_NAME: Final = "anki-miner-game"
"""The packaged ``.desktop`` file's name (``packaging/deb``, ``packaging/appimage``)."""


def app_icon() -> QIcon:
    """The app icon (window, taskbar, tray base). A null ``QIcon`` when the file is missing (never raises)."""
    if not APP_ICON_PATH.is_file():
        return QIcon()
    return QIcon(str(APP_ICON_PATH))
