"""Pytest configuration and shared fixtures."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from tests import _network_tripwire as _net


@pytest.fixture(scope="session", autouse=True)
def _install_network_tripwire():
    """Wrap ``socket.connect``/``connect_ex`` for the whole session (per xdist worker).

    Installed once and removed only at session end, mirroring Anki Miner's own
    tripwire fixture: unpatching mid-session would open a between-test window
    a leaked worker thread's connect could slip through unrecorded.
    """
    _net.install()
    yield
    _net.uninstall()


@pytest.fixture(autouse=True)
def _network_guard(request):
    """Fail any test whose code attempted a real non-loopback TCP connect.

    See ``tests/_network_tripwire.py`` for the record-and-block mechanism this
    asserts on. Tests marked ``network`` or ``e2e`` genuinely need it, so the
    wrapper is suppressed for their duration.
    """
    if any(request.node.get_closest_marker(m) for m in ("network", "e2e")):
        _net.SUPPRESSED = True
        try:
            yield
        finally:
            _net.SUPPRESSED = False
        return

    stray = _net.summarize_recorded(_net.RECORDED)
    _net.RECORDED.clear()
    if stray:
        pytest.fail(f"stray network connect(s) landed between tests: {stray}", pytrace=False)
    yield
    leaked = _net.summarize_recorded(_net.RECORDED)
    _net.RECORDED.clear()
    if leaked:
        pytest.fail(leaked, pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_game_home(tmp_path_factory, monkeypatch):
    """Point ``ANKI_MINER_GAME_HOME`` at a per-test tmp dir.

    ``paths.home()`` (T01) reads this env var at CALL time, not at import time
    (contract: it is a function, not a module-level constant), so setting the
    env var here is sufficient — there is no per-module snapshot to chase, the
    trap Anki Miner's ``tests/_home_isolation.py`` documents at length.
    """
    tmp_home = tmp_path_factory.mktemp("anki_miner_game_home") / ".anki_miner_game"
    tmp_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_home))
    yield tmp_home


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test files."""
    return tmp_path
