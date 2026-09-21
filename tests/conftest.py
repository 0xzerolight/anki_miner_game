"""Pytest configuration and shared fixtures."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from tests import _network_tripwire as _net

pytest_plugins = ["pytester"]


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
    asserts on. Only ``network``-marked tests genuinely need it (and the gate
    deselects them), so the wrapper is suppressed for their duration alone;
    ``e2e`` tests run in the gate and stay guarded. The stray check runs first
    for every test, so a leaked thread's connect is reported at the next test
    boundary, never carried past a ``network`` test onto a later one.
    """
    stray = _net.summarize_recorded(_net.RECORDED)
    _net.RECORDED.clear()
    if stray:
        pytest.fail(f"stray network connect(s) landed between tests: {stray}", pytrace=False)

    if request.node.get_closest_marker("network"):
        _net.SUPPRESSED = True
        try:
            yield
        finally:
            _net.SUPPRESSED = False
        return

    yield
    leaked = _net.summarize_recorded(_net.RECORDED)
    _net.RECORDED.clear()
    if leaked:
        pytest.fail(leaked, pytrace=False)


@pytest.fixture(autouse=True)
def _isolate_game_home(tmp_path_factory, monkeypatch):
    """Point the app home and every user home dir at a per-test tmp dir.

    ``paths.home()`` reads ``ANKI_MINER_GAME_HOME`` at CALL time, so setting the
    env var is enough; there is no per-module snapshot to chase. ``HOME`` and
    ``USERPROFILE`` (what ``Path.home()`` and ``~`` expand to on POSIX and on
    Windows) plus the Windows and XDG config/data dirs point inside the same
    tmp dir, so a default ``AppConfig().output_root`` (``~/Videos/...``) or an
    OBS config root found under ``~`` can never reach the real home. All are
    set on every platform; the ones a platform ignores are harmless.
    """
    fake_home = tmp_path_factory.mktemp("home")
    for var, path in (
        ("HOME", fake_home),
        ("USERPROFILE", fake_home),
        ("APPDATA", fake_home / "AppData" / "Roaming"),
        ("LOCALAPPDATA", fake_home / "AppData" / "Local"),
        ("XDG_CONFIG_HOME", fake_home / ".config"),
        ("XDG_DATA_HOME", fake_home / ".local" / "share"),
    ):
        monkeypatch.setenv(var, str(path))
    tmp_home = fake_home / ".anki_miner_game"
    tmp_home.mkdir()
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_home))
    yield tmp_home
