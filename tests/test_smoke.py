"""Scaffold smoke test: the package tree imports and the test harness works."""

import importlib
import os
import re
from pathlib import Path

import anki_miner_game
from anki_miner_game import paths
from anki_miner_game.models.config import AppConfig

_SUBPACKAGES = [
    "anki_miner_game.models",
    "anki_miner_game.text",
    "anki_miner_game.text.sources",
    "anki_miner_game.obs",
    "anki_miner_game.session",
    "anki_miner_game.lifecycle",
    "anki_miner_game.vad",
    "anki_miner_game.vad.worker",
    "anki_miner_game.addons",
    "anki_miner_game.feed",
    "anki_miner_game.gui",
    "anki_miner_game.interfaces",
    "anki_miner_game.runtime",
]


def test_version():
    # Plain X.Y.Z: release.yml refuses anything else, and the installer carries it as a file version.
    assert re.fullmatch(r"\d+\.\d+\.\d+", anki_miner_game.__version__)


def test_every_package_importable():
    for name in _SUBPACKAGES:
        importlib.import_module(name)


_HOME_VARS = (
    "ANKI_MINER_GAME_HOME",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def test_every_home_is_isolated_inside_the_test_tmp(tmp_path_factory):
    """The autouse fixture points the app home and the user's home dirs at a throwaway dir."""
    base = tmp_path_factory.getbasetemp()
    for var in _HOME_VARS:
        assert Path(os.environ[var]).is_relative_to(base), var
    assert Path.home().is_relative_to(base)
    assert Path(os.path.expanduser("~")).is_relative_to(base)
    assert paths.home().name == ".anki_miner_game"


def test_default_output_root_resolves_inside_the_test_tmp(tmp_path_factory):
    """``~/Videos/Anki Miner Game`` must not reach the developer's real Videos folder."""
    base = tmp_path_factory.getbasetemp()
    assert paths.output_root(AppConfig()).is_relative_to(base)
    assert paths.incoming_dir(AppConfig()).is_relative_to(base)


def test_offscreen_qt_app(qapp):
    """pytest-qt's ``qapp`` runs on the offscreen platform (later Qt tests copy this: never build a QApplication)."""
    assert qapp.platformName() == "offscreen"


def test_loopback_socket_not_blocked():
    """The network tripwire allows loopback, per the global no-network-except-loopback rule."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(("127.0.0.1", port))  # must not raise NetworkTripwireError
