"""The actor's own machinery: thread-safe ``post``, marshalled status listener, ticker, finalise
worker (spec 4.2, 10.3; wave 2a contracts)."""

import asyncio
import threading
import time

from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    CommandKind,
    SourceStatus,
    SourceStatusChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.obs.recorder import ObsRecorder
from anki_miner_game.session import session as session_mod
from anki_miner_game.session.finalise import FinaliseResult
from anki_miner_game.session.session import BannerKey, FinaliseWorker, SessionActor
from tests.session.actor_harness import (
    SLUG,
    FakeClock,
    FakeDiscovery,
    FakeGateway,
    FakeObs,
    FakeProvisioner,
    Harness,
)


def test_the_actor_is_a_session_control(h: Harness):
    control: SessionControl = h.actor
    assert control.state is AppState.IDLE


async def test_launch_connects_to_a_running_obs(h: Harness):
    assert h.gateway.connects == 1
    assert SourceStatusChanged(OBS_SOURCE_ID, SourceStatus.CONNECTED) in h.events


async def test_post_from_another_thread_is_handled_on_the_loop(h: Harness):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda _event: seen.append(threading.get_ident()))
    worker = threading.Thread(target=h.actor.post, args=(UserCommand(CommandKind.ARM, slug=SLUG),))
    worker.start()
    worker.join()
    await h.settle()
    assert h.actor.state is AppState.ARMED
    assert seen and set(seen) == {loop_thread}


async def test_status_listener_from_another_thread_is_published_on_the_loop(h: Harness):
    await h.arm()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    h.actor.subscribe(lambda e: seen.append(threading.get_ident()) if isinstance(e, SourceStatusChanged) else None)
    worker = threading.Thread(target=h.sources[0].set_status, args=(SourceStatus.RECEIVING,))
    worker.start()
    worker.join()
    await h.settle()
    assert SourceStatusChanged("textractor", SourceStatus.RECEIVING) in h.events
    assert seen == [loop_thread]


async def test_sources_start_on_the_loop_thread(h: Harness):
    await h.arm()
    assert h.sources[0].start_thread == threading.get_ident()


async def test_a_failing_subscriber_does_not_stop_the_actor(h: Harness):
    def broken(_event: object) -> None:
        raise RuntimeError("subscriber bug")

    h.actor.subscribe(broken)
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_an_unexpected_error_is_a_banner_and_the_actor_goes_on(h: Harness):
    h.provisioner.error = RuntimeError("bug")
    await h.arm()
    assert BannerKey.INTERNAL in h.banners()
    h.provisioner.error = None
    await h.arm()
    assert h.actor.state is AppState.ARMED


async def test_run_posts_a_tick_every_tick_s(tmp_path):
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    rig = Harness(tmp_path, sleep=sleep)
    try:
        await rig.start()
        await asyncio.sleep(0.01)
    finally:
        await rig.stop()
    assert slept and set(slept) == {session_mod.TICK_S}


def test_a_message_posted_after_the_loop_closed_is_dropped():
    loop = asyncio.new_event_loop()
    loop.close()
    gateway = FakeGateway(FakeObs(), FakeClock())
    finaliser = FinaliseWorker()
    actor = SessionActor(
        loop=loop,
        gateway=gateway,
        discovery=FakeDiscovery(),
        provisioner=FakeProvisioner(gateway),
        recorder=ObsRecorder(gateway),
        finaliser=finaliser,
        get_config=AppConfig,
        get_profile=lambda _slug: None,
        source_factory=lambda _cfg, _game: [],
    )
    actor.post(Tick(0.0))  # the app is quitting: dropped, no RuntimeError
    finaliser.shutdown()


async def test_finalise_worker_runs_one_call_at_a_time_on_its_own_thread(monkeypatch, tmp_path):
    active = 0
    overlaps = 0
    threads: set[str] = set()

    def fake_finalise(manifest_path, cfg, *, sleep):
        nonlocal active, overlaps
        active += 1
        overlaps += active > 1
        threads.add(threading.current_thread().name)
        time.sleep(0.02)
        active -= 1
        return FinaliseResult(manifest_path, None, queue_vad=False)

    monkeypatch.setattr(session_mod, "finalise", fake_finalise)
    worker = FinaliseWorker()
    try:
        await asyncio.gather(*(worker.run(tmp_path / f"{i}.session.json", AppConfig()) for i in range(4)))
    finally:
        worker.shutdown()
    assert overlaps == 0
    assert len(threads) == 1 and next(iter(threads)).startswith("finalise")
