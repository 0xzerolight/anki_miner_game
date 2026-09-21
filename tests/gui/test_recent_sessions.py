"""Recent sessions (spec 16 item 5, 13.3, 17 VAD row): name, duration, cue count, VAD state; Open folder;
Re-run VAD and Restore untrimmed subtitle in the row's context menu through ``VadJobs``; interrupted
passes handed to ``VadJobs.rerun`` once, at the first load."""

from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl

from anki_miner_game.gui.widgets.recent_sessions import RECENT_LIMIT, RecentSessions, scan_sessions
from anki_miner_game.models.manifest import ManifestState, VadRecord, VadState, to_json
from tests.gui.session_fakes import TITLE, Jobs, manifest, place


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def jobs() -> Jobs:
    return Jobs()


@pytest.fixture
def opened() -> list[QUrl]:
    return []


@pytest.fixture
def recent(qtbot, jobs, opened) -> RecentSessions:
    widget = RecentSessions(vad_jobs=jobs, open_url=opened.append)
    qtbot.addWidget(widget)
    return widget


# The rows ----------------------------------------------------------------------------------------


def test_a_row_shows_name_duration_cue_count_and_vad_state(recent, root):
    place(root, manifest(vad=VadRecord(VadState.DONE, model="silero_vad_v6", trimmed=2)))
    recent.load(root)
    assert recent.cells() == [(f"{TITLE} - 03", "1:27:29", "2", "VAD done")]


@pytest.mark.parametrize(
    ("changes", "text"),
    [
        ({"vad": None}, "No VAD pass"),
        ({"vad": VadRecord(VadState.QUEUED)}, "VAD queued"),
        ({"state": ManifestState.VAD_RUNNING, "vad": VadRecord(VadState.QUEUED)}, "VAD running"),
        (
            {"vad": VadRecord(VadState.FAILED, message="the worker exited with 1")},
            "VAD failed: the worker exited with 1",
        ),
        (
            {"vad": VadRecord(VadState.UNAVAILABLE, message="The VAD add-on is not installed")},
            "VAD unavailable: The VAD add-on is not installed",
        ),
        ({"vad": VadRecord(VadState.RESTORED)}, "Untrimmed subtitle"),
        ({"cues": ()}, "No subtitle"),
        ({"state": ManifestState.FINALISE_PENDING}, "Not moved yet; retried at next launch"),
    ],
)
def test_the_vad_column_says_what_the_manifest_holds(qtbot, root, changes, text):
    recent = RecentSessions()  # no VadJobs: nothing is handed on, the manifest is shown as it is
    qtbot.addWidget(recent)
    place(root, manifest(**changes))
    recent.load(root)
    assert recent.cells()[0][3] == text
    assert recent.item(0).toolTip(3) == text  # a long failure message stays readable


def test_a_session_still_recording_is_not_listed_and_an_unreadable_manifest_is_skipped(recent, root):
    place(root, manifest(1, state=ManifestState.RECORDING))
    bad = root / TITLE / f"{TITLE} - 02.session.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not json", encoding="utf-8")
    place(root, manifest(3))
    recent.load(root)
    assert [cells[0] for cells in recent.cells()] == [f"{TITLE} - 03"]


def test_newest_first_and_at_most_the_limit(root):
    for index in range(1, RECENT_LIMIT + 3):
        place(root, manifest(index, started_at=f"2026-10-{index:02d}T18:04:11Z"), age_s=100.0 - index)
    rows = scan_sessions(root)
    assert len(rows) == RECENT_LIMIT
    assert rows[0].manifest.index == RECENT_LIMIT + 2
    assert rows[-1].manifest.index == 3


def test_a_missing_output_folder_lists_nothing(recent, tmp_path):
    recent.load(tmp_path / "nowhere")
    assert recent.cells() == []


def test_open_folder_opens_the_sessions_folder(recent, root, opened):
    path = place(root, manifest())
    recent.load(root)
    recent.open_button(path).click()
    assert opened == [QUrl.fromLocalFile(str(path.parent))]


# The context menu --------------------------------------------------------------------------------


def actions(recent: RecentSessions, path: Path) -> dict[str, bool]:
    return {action.text(): action.isEnabled() for action in recent.menu_for(path).actions()}


def test_rerun_and_restore_go_to_vad_jobs(recent, root, jobs):
    path = place(root, manifest(vad=VadRecord(VadState.DONE)))
    recent.load(root)
    assert actions(recent, path) == {"Re-run VAD": True, "Restore untrimmed subtitle": True}
    recent.menu_for(path).actions()[0].trigger()
    assert jobs.calls == [("rerun", path)]
    assert recent.cells()[0][3] == "VAD queued"
    assert actions(recent, path) == {"Re-run VAD": False, "Restore untrimmed subtitle": False}  # one job at a time
    recent.vad_finished(path, VadState.DONE)
    recent.menu_for(path).actions()[1].trigger()
    assert jobs.calls[-1] == ("restore", path)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"vad": None}, {"Re-run VAD": True, "Restore untrimmed subtitle": False}),
        ({"vad": VadRecord(VadState.FAILED)}, {"Re-run VAD": True, "Restore untrimmed subtitle": False}),
        ({"vad": VadRecord(VadState.RESTORED)}, {"Re-run VAD": True, "Restore untrimmed subtitle": False}),
        ({"cues": ()}, {"Re-run VAD": False, "Restore untrimmed subtitle": False}),
        ({"state": ManifestState.FINALISE_PENDING}, {"Re-run VAD": False, "Restore untrimmed subtitle": False}),
    ],
)
def test_the_menu_offers_only_what_the_session_allows(recent, root, changes, expected):
    path = place(root, manifest(**changes))
    recent.load(root)
    assert actions(recent, path) == expected


def test_without_vad_jobs_the_menu_offers_nothing(qtbot, root):
    widget = RecentSessions()
    qtbot.addWidget(widget)
    path = place(root, manifest(vad=VadRecord(VadState.DONE)))
    widget.load(root)
    assert actions(widget, path) == {"Re-run VAD": False, "Restore untrimmed subtitle": False}


# VAD jobs while the app runs ---------------------------------------------------------------------


def test_the_first_load_hands_interrupted_passes_to_rerun_once(recent, root, jobs):
    running = place(root, manifest(1, state=ManifestState.VAD_RUNNING, vad=VadRecord(VadState.QUEUED)))
    queued = place(root, manifest(2, vad=VadRecord(VadState.QUEUED)))
    place(root, manifest(3, vad=VadRecord(VadState.DONE)))
    recent.load(root)
    assert sorted(jobs.calls) == [("rerun", running), ("rerun", queued)]
    recent.load(root)
    recent.reload()
    assert len(jobs.calls) == 2


def test_progress_shows_in_the_row_and_the_outcome_is_read_back(recent, root):
    path = place(root, manifest(vad=VadRecord(VadState.QUEUED)))
    recent.load(root)
    recent.vad_progress(path, 600_000, 2_400_000)
    assert recent.cells()[0][3] == "VAD 25%"
    recent.vad_progress(path, 600_000, None)
    assert recent.cells()[0][3] == "VAD running"
    path.write_text(to_json(replace(manifest(), vad=VadRecord(VadState.DONE))), encoding="utf-8")
    recent.vad_finished(path, VadState.DONE)
    assert recent.cells()[0][3] == "VAD done"


def test_progress_for_a_session_not_listed_is_ignored(recent, root, tmp_path):
    place(root, manifest())
    recent.load(root)
    recent.vad_progress(tmp_path / "elsewhere.session.json", 1, 2)
    assert recent.cells()[0][3] == "No VAD pass"


def test_a_finished_session_is_added_on_reload(recent, root):
    place(root, manifest(3, started_at="2026-10-02T18:04:11Z"), age_s=10.0)
    recent.load(root)
    place(root, manifest(4, started_at="2026-10-03T18:04:11Z"))
    recent.reload()
    assert [cells[0] for cells in recent.cells()] == [f"{TITLE} - 04", f"{TITLE} - 03"]
