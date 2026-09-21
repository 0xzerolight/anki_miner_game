"""The Qt presenter (spec 4.2): every ``Presenter`` call becomes a signal whose slots run on the main thread."""

import threading
from pathlib import Path

import pytest

from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import VadState
from anki_miner_game.models.messages import AppState, Banner, BannerLevel, SourceStatus

LINE = GameLine(text="はい", raw="はい", t_mono=1.0, source_id="textractor")
MANIFEST = Path("/out/Game/Game - 01.session.json")

CALLS = [
    ("state_changed", (AppState.ARMED, "steins-gate")),
    ("state_changed", (AppState.IDLE, None)),
    ("source_status", ("textractor", SourceStatus.RECEIVING)),
    ("line_accepted", (LINE, 5230, False)),
    ("line_accepted", (LINE, None, True)),
    ("banner", (Banner("feed", BannerLevel.WARNING, "port 6678 is in use"),)),
    ("banner_cleared", ("feed",)),
    ("session_finished", (MANIFEST,)),
    ("vad_progress", (MANIFEST, 600_000, 5_248_120)),
    ("vad_progress", (MANIFEST, 600_000, None)),
    ("vad_progress", (MANIFEST, 3_000_000_000, 3_000_000_001)),  # past a C++ int: a 35-day recording
    ("vad_finished", (MANIFEST, VadState.DONE)),
]


@pytest.mark.parametrize(("name", "args"), CALLS)
def test_each_call_emits_its_signal_with_the_same_arguments(qtbot, name, args):
    presenter = QtPresenter()
    with qtbot.waitSignal(getattr(presenter.signals, name), timeout=1000) as blocker:
        getattr(presenter, name)(*args)
    assert tuple(blocker.args) == args


def test_a_call_from_another_thread_reaches_its_slot_on_the_main_thread(qtbot):
    presenter = QtPresenter()
    seen: list[int] = []
    presenter.signals.banner_cleared.connect(lambda _key: seen.append(threading.get_ident()))
    worker = threading.Thread(target=presenter.banner_cleared, args=("obs",))
    worker.start()
    worker.join()
    qtbot.waitUntil(lambda: seen != [], timeout=1000)
    assert seen == [threading.get_ident()]
