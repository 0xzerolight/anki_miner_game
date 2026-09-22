"""The OBS lock the composition shares with the window picker and the wizard's OBS step (W2b cross review).

Arming and the restore read and switch OBS's profile and scene collection; so do the picker's idle
listing and wizard step 1. Each holds one ``asyncio.Lock`` while it does, so an arm never saves the
app's collection (put there by a listing) as the user's, and a restore never lands mid-provisioning.
"""

import asyncio

import pytest

from anki_miner_game.models.constants import OBS_COLLECTION_NAME, OBS_PROFILE_NAME
from anki_miner_game.models.messages import AppState, CommandKind, Tick, UserCommand
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.restore import ObsRestore, load_restore, restore_path, save_restore
from tests.session.actor_harness import SLUG, Harness

SWITCHES = ("SetCurrentProfile", "SetCurrentSceneCollection")


@pytest.fixture
async def locked(tmp_path, monkeypatch):
    """A running ``Harness`` whose actor shares ``lock`` with the test, which holds it at first."""
    monkeypatch.setattr(session_mod, "QUIT_STOP_TIMEOUT_S", 0.05)
    lock = asyncio.Lock()
    harness = Harness(tmp_path, obs_lock=lock)
    await harness.start()
    await lock.acquire()
    yield harness, lock
    if lock.locked():
        lock.release()
    await harness.stop()


async def test_an_arm_waits_for_the_lock_and_reads_the_users_names_once_it_has_it(locked):
    h, lock = locked
    h.obs.collection = OBS_COLLECTION_NAME  # a window listing holds the lock and put OBS on the app's collection
    sent = len(h.gateway.sent)
    h.actor.post(UserCommand(CommandKind.ARM, slug=SLUG))
    await asyncio.sleep(0.05)
    assert h.gateway.sent[sent:] == []  # nothing read or switched while the listing runs
    assert h.actor.state is AppState.IDLE
    h.obs.collection = "Untitled"  # the listing switched back
    lock.release()
    await h.settle()
    assert h.actor.state is AppState.ARMED
    assert load_restore(restore_path()) == ObsRestore(profile="Untitled", collection="Untitled")
    assert not lock.locked()


async def test_the_idle_restore_waits_for_the_lock(locked):
    h, lock = locked
    save_restore(restore_path(), ObsRestore(profile="Untitled", collection="Untitled"))
    h.obs.profile, h.obs.collection = OBS_PROFILE_NAME, OBS_COLLECTION_NAME
    h.clock.t += session_mod.RESTORE_RETRY_S
    h.actor.post(Tick(h.clock.t))
    await asyncio.sleep(0.05)
    assert not any(name in SWITCHES for name in h.gateway.names())
    lock.release()
    await h.settle()
    assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")
    assert not restore_path().exists()
    assert not lock.locked()


async def test_a_failed_arm_restores_under_its_own_hold(tmp_path, monkeypatch):
    """The undo of a failed arm restores OBS while the arm still holds the lock: no second acquire."""
    monkeypatch.setattr(session_mod, "SWITCH_TIMEOUT_S", 0.05)
    lock = asyncio.Lock()
    h = Harness(tmp_path, obs_lock=lock)
    await h.start()
    try:
        h.provisioner.switch_timeout_s = 0.05
        h.obs.lost_switch_events = 1  # OBS switches and answers, but the ...Changed event never comes
        async with asyncio.timeout(5):
            await h.arm()
        assert h.actor.state is AppState.IDLE
        assert "arm" in h.banners()
        assert (h.obs.profile, h.obs.collection) == ("Untitled", "Untitled")  # restored by the undo
        assert not restore_path().exists()
        assert not lock.locked()
    finally:
        await h.stop()
