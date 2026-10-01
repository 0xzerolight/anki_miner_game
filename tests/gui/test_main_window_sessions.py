"""The main window's recent sessions and the hand-off (spec 16, Appendix C): the sessions listed at
launch, a finished session's row and hand-off text, and VAD progress on its row. Moved from
``test_main_window.py`` so the owner of ``widgets/recent_sessions.py`` and ``widgets/handoff.py``
keeps them (audit 2026-10-01 plan, section 4.10)."""

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QUrl

from anki_miner_game.gui.main_window import MainWindow
from anki_miner_game.gui.presenters.qt_presenter import QtPresenter
from anki_miner_game.models.manifest import ManifestState, VadRecord, VadState
from anki_miner_game.models.messages import AppState, SessionEvent, SessionInput
from tests.gui.session_fakes import TITLE, Jobs, manifest, place

GAMES = [("steins-gate", "Steins;Gate"), ("zero-escape", "Zero Escape")]


class Control:
    """``SessionControl`` that records what the window posts."""

    def __init__(self) -> None:
        self.posted: list[SessionInput] = []

    def post(self, msg: SessionInput) -> None:
        self.posted.append(msg)

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        raise AssertionError("the window listens to the presenter")

    @property
    def state(self) -> AppState:
        return AppState.IDLE


def session_window(qtbot, root: Path, jobs: Jobs, opened: list[QUrl]) -> tuple[MainWindow, QtPresenter]:
    presenter = QtPresenter()
    window = MainWindow(
        Control(),
        presenter.signals,
        GAMES,
        on_quit=lambda: None,
        output_root=lambda: root,
        vad_jobs=jobs,
        open_url=opened.append,
    )
    qtbot.addWidget(window)
    return window, presenter


def test_recent_sessions_load_at_launch_and_interrupted_passes_are_handed_on(qtbot, tmp_path):
    root, jobs = tmp_path / "out", Jobs()
    interrupted = place(root, manifest(1, state=ManifestState.VAD_RUNNING, vad=VadRecord(VadState.QUEUED)))
    place(root, manifest(2, vad=VadRecord(VadState.DONE), started_at="2026-10-03T18:04:11Z"), age_s=5.0)
    window, _ = session_window(qtbot, root, jobs, [])
    assert [cells[0] for cells in window.recent.cells()] == [f"{TITLE} - 02", f"{TITLE} - 01"]
    assert jobs.calls == [("rerun", interrupted)]


def test_a_finished_session_is_listed_and_its_hand_off_shown(qtbot, tmp_path):
    root, opened = tmp_path / "out", []
    window, presenter = session_window(qtbot, root, Jobs(), opened)
    assert window.handoff.isHidden()
    path = place(root, manifest(3))
    presenter.session_finished(path)
    qtbot.waitUntil(lambda: not window.handoff.isHidden())
    assert window.recent.cells()[0][0] == f"{TITLE} - 03"
    assert window.handoff.label.text().startswith(f'Saved "{TITLE} - 03".')
    window.handoff.open_button.click()
    assert opened == [QUrl.fromLocalFile(str(path.parent))]


def test_vad_progress_and_outcome_reach_the_session_row(qtbot, tmp_path):
    root = tmp_path / "out"
    path = place(root, manifest(3))
    window, presenter = session_window(qtbot, root, Jobs(), [])
    presenter.vad_progress(path, 1, 4)
    qtbot.waitUntil(lambda: window.recent.cells()[0][3] == "Trimming 25%")
    path.write_text(path.read_text(encoding="utf-8").replace('"vad": null', '"vad": {"state": "done"}'), "utf-8")
    presenter.vad_finished(path, VadState.DONE)
    qtbot.waitUntil(lambda: window.recent.cells()[0][3] == "Ready")
