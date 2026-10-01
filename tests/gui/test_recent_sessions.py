"""Recent sessions (spec 16 item 5 as amended by UJ-09; 13.3; 17 VAD row): Session, Length, Lines and a
plain Status with the reason in its tooltip; a double-click or the row menu's Open folder opens the
folder; Trim again and Undo trimming in the menu only while the voice-trimming add-on is ready, through
``VadJobs``; interrupted passes handed to ``VadJobs.rerun`` once, at the first load."""

from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QUrl

from anki_miner_game.gui.widgets.recent_sessions import RECENT_LIMIT, RecentSessions, scan_sessions
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.manifest import ManifestState, VadRecord, VadState, to_json
from tests.gui.session_fakes import TITLE, Jobs, manifest, place

ROW_TOOLTIP = "Double-click to open the folder"


class Addon:
    """The voice-trimming ``AddonService``; the list reads only its status."""

    size_bytes = 96_000_000
    note = None

    def __init__(self, status: AddonStatus = AddonStatus.READY) -> None:
        self.state = status

    def status(self) -> AddonStatus:
        return self.state

    async def install(self, progress) -> None:
        raise AssertionError("the list never installs")


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
    widget = RecentSessions(vad_jobs=jobs, vad_addon=Addon(), open_url=opened.append)
    qtbot.addWidget(widget)
    return widget


# The rows ----------------------------------------------------------------------------------------


def test_four_columns_and_a_row_per_session(recent, root):
    place(root, manifest(vad=VadRecord(VadState.DONE, model="silero_vad_v6", trimmed=2)))
    recent.load(root)
    header = recent.tree.headerItem()
    assert [header.text(i) for i in range(recent.tree.columnCount())] == ["Session", "Length", "Lines", "Status"]
    assert recent.cells() == [(f"{TITLE} - 03", "1:27:29", "2", "Ready")]
    assert recent.tree.itemWidget(recent.item(0), 3) is None  # no button per row


UNAVAILABLE = VadRecord(VadState.UNAVAILABLE, message="The voice-trimming add-on is not installed.")
BROKEN = VadRecord(
    VadState.UNAVAILABLE, message="The voice-trimming add-on is damaged or out of date; repair it in Settings."
)


@pytest.mark.parametrize(
    ("changes", "addon", "status", "why"),
    [
        ({"vad": None}, AddonStatus.READY, "Ready", ""),
        ({"vad": VadRecord(VadState.DONE)}, AddonStatus.READY, "Ready", ""),
        ({"vad": VadRecord(VadState.QUEUED)}, AddonStatus.READY, "Waiting to trim", ""),
        ({"state": ManifestState.VAD_RUNNING, "vad": VadRecord(VadState.QUEUED)}, AddonStatus.READY, "Trimming", ""),
        (
            {"vad": VadRecord(VadState.FAILED, message="The voice-trimming worker exited with code 3")},
            AddonStatus.READY,
            "Trim failed",
            "The voice-trimming worker exited with code 3",
        ),
        ({"vad": UNAVAILABLE}, AddonStatus.MISSING, "Ready", ""),
        ({"vad": UNAVAILABLE}, None, "Ready", ""),
        ({"vad": BROKEN}, AddonStatus.BROKEN, "Ready (not trimmed)", BROKEN.message),
        ({"vad": UNAVAILABLE}, AddonStatus.READY, "Ready (not trimmed)", UNAVAILABLE.message),
        (
            {"vad": VadRecord(VadState.RESTORED)},
            AddonStatus.READY,
            "Ready (not trimmed)",
            "Trimming was undone; choose Trim again to trim it.",
        ),
        ({"cues": ()}, AddonStatus.READY, "No lines", "No lines were recorded, so this session has no subtitle."),
        (
            {"state": ManifestState.FINALISE_PENDING},
            AddonStatus.READY,
            "Not filed yet",
            "Moved to its game folder at the next launch.",
        ),
    ],
)
def test_the_status_is_a_plain_word_and_the_reason_is_its_tooltip(qtbot, root, changes, addon, status, why):
    widget = RecentSessions(vad_addon=None if addon is None else Addon(addon))  # no VadJobs: shown as stored
    qtbot.addWidget(widget)
    place(root, manifest(**changes))
    widget.load(root)
    assert widget.cells()[0][3] == status
    assert widget.item(0).toolTip(3) == (why or ROW_TOOLTIP)
    assert widget.item(0).toolTip(0) == ROW_TOOLTIP


def test_long_names_keep_their_number(recent):
    assert recent.tree.textElideMode() is Qt.TextElideMode.ElideMiddle


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


def test_a_double_click_opens_the_sessions_folder(recent, root, opened):
    path = place(root, manifest())
    recent.load(root)
    recent.tree.itemDoubleClicked.emit(recent.item(0), 0)
    assert opened == [QUrl.fromLocalFile(str(path.parent))]


# The row menu ------------------------------------------------------------------------------------


def actions(recent: RecentSessions, path: Path) -> dict[str, bool]:
    return {action.text(): action.isEnabled() for action in recent.menu_for(path).actions()}


def test_open_folder_comes_first_for_every_row(recent, root, opened):
    path = place(root, manifest(state=ManifestState.FINALISE_PENDING))
    recent.load(root)
    menu = recent.menu_for(path)
    assert menu.actions()[0].text() == "Open folder"
    menu.actions()[0].trigger()
    assert opened == [QUrl.fromLocalFile(str(path.parent))]


def test_trim_again_and_undo_trimming_go_to_vad_jobs(recent, root, jobs):
    path = place(root, manifest(vad=VadRecord(VadState.DONE)))
    recent.load(root)
    assert actions(recent, path) == {"Open folder": True, "Trim again": True, "Undo trimming": True}
    recent.menu_for(path).actions()[1].trigger()
    assert jobs.calls == [("rerun", path)]
    assert recent.cells()[0][3] == "Waiting to trim"
    assert actions(recent, path) == {
        "Open folder": True,
        "Trim again": False,
        "Undo trimming": False,
    }  # one job at a time
    recent.vad_finished(path, VadState.DONE)
    recent.menu_for(path).actions()[2].trigger()
    assert jobs.calls[-1] == ("restore", path)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"vad": None}, {"Open folder": True, "Trim again": True, "Undo trimming": False}),
        ({"vad": VadRecord(VadState.FAILED)}, {"Open folder": True, "Trim again": True, "Undo trimming": False}),
        ({"vad": VadRecord(VadState.RESTORED)}, {"Open folder": True, "Trim again": True, "Undo trimming": False}),
        ({"cues": ()}, {"Open folder": True, "Trim again": False, "Undo trimming": False}),
        ({"state": ManifestState.FINALISE_PENDING}, {"Open folder": True, "Trim again": False, "Undo trimming": False}),
    ],
)
def test_the_menu_offers_only_what_the_session_allows(recent, root, changes, expected):
    path = place(root, manifest(**changes))
    recent.load(root)
    assert actions(recent, path) == expected


@pytest.mark.parametrize("status", [AddonStatus.MISSING, AddonStatus.INSTALLING, AddonStatus.BROKEN, None])
def test_without_a_ready_add_on_the_menu_offers_only_open_folder(qtbot, root, jobs, status):
    """UJ-09 / D-08 (F#33): Trim again was offered with no add-on, and reverted a trimmed subtitle."""
    widget = RecentSessions(vad_jobs=jobs, vad_addon=None if status is None else Addon(status))
    qtbot.addWidget(widget)
    path = place(root, manifest(vad=VadRecord(VadState.DONE)))
    widget.load(root)
    assert actions(widget, path) == {"Open folder": True}


def test_without_vad_jobs_trimming_is_offered_but_cannot_run(qtbot, root):
    widget = RecentSessions(vad_addon=Addon())
    qtbot.addWidget(widget)
    path = place(root, manifest(vad=VadRecord(VadState.DONE)))
    widget.load(root)
    assert actions(widget, path) == {"Open folder": True, "Trim again": False, "Undo trimming": False}


@pytest.mark.parametrize("change", ["deleted", "no subtitle"])
def test_a_row_gone_stale_reloads_the_list_instead_of_waiting_forever(recent, root, jobs, change):
    """B4-04: a session moved or deleted outside the app stayed "queued" with its menu disabled."""
    stale = place(root, manifest(3, vad=VadRecord(VadState.DONE)), age_s=5.0)
    place(root, manifest(4, started_at="2026-10-03T18:04:11Z"))
    recent.load(root)
    menu = recent.menu_for(stale)
    if change == "deleted":
        stale.unlink()
    else:
        stale.write_text(to_json(replace(manifest(3), live_cues=())), encoding="utf-8")
    menu.actions()[1].trigger()
    assert jobs.calls == []
    expected = [f"{TITLE} - 04"] if change == "deleted" else [f"{TITLE} - 04", f"{TITLE} - 03"]
    assert [cells[0] for cells in recent.cells()] == expected
    assert "Waiting to trim" not in [cells[3] for cells in recent.cells()]


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
    assert recent.cells()[0][3] == "Trimming 25%"
    recent.vad_progress(path, 600_000, None)
    assert recent.cells()[0][3] == "Trimming"
    path.write_text(to_json(replace(manifest(), vad=VadRecord(VadState.DONE))), encoding="utf-8")
    recent.vad_finished(path, VadState.DONE)
    assert recent.cells()[0][3] == "Ready"


def test_progress_for_a_session_not_listed_is_ignored(recent, root, tmp_path):
    place(root, manifest())
    recent.load(root)
    recent.vad_progress(tmp_path / "elsewhere.session.json", 1, 2)
    assert recent.cells()[0][3] == "Ready"


def test_a_finished_session_is_added_on_reload(recent, root):
    place(root, manifest(3, started_at="2026-10-02T18:04:11Z"), age_s=10.0)
    recent.load(root)
    place(root, manifest(4, started_at="2026-10-03T18:04:11Z"))
    recent.reload()
    assert [cells[0] for cells in recent.cells()] == [f"{TITLE} - 04", f"{TITLE} - 03"]
