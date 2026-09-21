"""Scaffold smoke test: the package tree imports and the test harness works."""

import importlib
import os

import anki_miner_game

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
    assert anki_miner_game.__version__ == "0.1.0"


def test_every_package_importable():
    for name in _SUBPACKAGES:
        importlib.import_module(name)


def test_game_home_isolated():
    """The autouse fixture points ``ANKI_MINER_GAME_HOME`` at a throwaway dir, never the real home."""
    home = os.environ["ANKI_MINER_GAME_HOME"]
    real_home = os.path.join(os.path.expanduser("~"), ".anki_miner_game")
    assert home.endswith(".anki_miner_game")
    assert home != real_home


def test_offscreen_qt_app():
    """PyQt6 constructs under the offscreen platform (proves the dev env is sane)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None


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
