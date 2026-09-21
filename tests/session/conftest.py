"""Fixtures for the session actor tests (``tests/session/actor_harness.py``)."""

import pytest

from tests.session.actor_harness import Harness


@pytest.fixture
async def rig(tmp_path):
    """A ``Harness`` that is built but not started: set the fakes up, then ``await rig.start()``."""
    harness = Harness(tmp_path)
    yield harness
    await harness.stop()


@pytest.fixture
async def h(rig):
    """A running ``Harness``: OBS runs, the launch connected, the actor is idle."""
    await rig.start()
    return rig
