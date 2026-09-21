"""Fixtures for the session actor tests (``tests/session/actor_harness.py``)."""

import pytest

from anki_miner_game.session import session as session_mod
from tests.session.actor_harness import Harness


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    """A ``Harness`` that is built but not started: set the fakes up, then ``await rig.start()``.

    Teardown quits the actor; a test that ends while recording must not wait the real
    ``QUIT_STOP_TIMEOUT_S`` for a ``STOPPED`` the fake OBS only sends when ``stops_on_request``.
    """
    monkeypatch.setattr(session_mod, "QUIT_STOP_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path)
    yield harness
    await harness.stop()


@pytest.fixture
async def h(rig):
    """A running ``Harness``: OBS runs, the launch connected, the actor is idle."""
    await rig.start()
    return rig
