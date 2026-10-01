"""The hand-off shown after a session (spec 16, Appendix C as amended by UJ-08)."""

from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QDir, QMargins, QUrl

from anki_miner_game.gui.widgets.handoff import HandoffPanel, handoff_text
from anki_miner_game.gui.widgets.recent_sessions import SessionRow, read_session
from anki_miner_game.models.manifest import FilesRecord, ManifestState
from tests.gui.session_fakes import TITLE, manifest, place

PATH = Path("/out") / TITLE / f"{TITLE} - 03.session.json"
SINGLE = (
    'Saved "Steins;Gate - 03". In Anki Miner, choose Video -> Single and pick this video; the subtitle fills in '
    "by itself."
)


def test_one_session_gets_the_single_video_sentence():
    assert handoff_text(SessionRow(PATH, manifest())) == SINGLE


def test_two_or_more_sessions_add_the_batch_sentence():
    assert handoff_text(SessionRow(PATH, manifest()), sessions=3) == (
        f"{SINGLE} To mine all 3 sessions at once: Video -> Batch, with this folder as both the video and the "
        "subtitle folder."
    )


@pytest.mark.parametrize(
    "m",
    [
        manifest(cues=()),  # no lines: no subtitle to fill in (the no_cues banner says so)
        manifest(state=ManifestState.FINALISE_PENDING),  # not in the game folder yet (its banner says so)
    ],
)
def test_no_hand_off_without_a_placed_subtitle(m):
    assert handoff_text(SessionRow(PATH, m)) is None


def test_the_name_is_the_video_as_placed():
    m = replace(manifest(), files=FilesRecord("Steins;Gate - 04.mkv", "Steins;Gate - 04.srt"))
    assert (handoff_text(SessionRow(PATH, m)) or "").startswith('Saved "Steins;Gate - 04".')


@pytest.fixture
def panel(qtbot):
    opened: list[QUrl] = []
    widget = HandoffPanel(open_url=opened.append)
    qtbot.addWidget(widget)
    return widget, opened


def test_the_panel_shows_the_text_and_opens_the_folder_named_in_its_tooltip(panel):
    widget, opened = panel
    assert widget.isHidden()
    widget.show_session(SessionRow(PATH, manifest()))
    assert not widget.isHidden()
    assert widget.label.text() == SINGLE  # the path left the text for the button's tooltip
    assert widget.open_button.text() == "Open folder"
    assert widget.open_button.toolTip() == QDir.toNativeSeparators(str(PATH.parent))
    widget.open_button.click()
    assert opened == [QUrl.fromLocalFile(str(PATH.parent))]


def test_the_panel_counts_the_sessions_in_the_game_folder(panel, tmp_path):
    widget, _ = panel
    place(tmp_path, manifest(1))
    row = read_session(place(tmp_path, manifest(2)))
    assert row is not None
    widget.show_session(row)
    assert widget.label.text().endswith(
        "To mine all 2 sessions at once: Video -> Batch, with this folder as " "both the video and the subtitle folder."
    )


def test_the_batch_count_leaves_out_sessions_without_a_subtitle(panel, tmp_path):
    """Batch mines a video with its subtitle: a "No lines" session is not one of them (P8 saw "all 21"
    with 9 of them empty)."""
    widget, _ = panel
    place(tmp_path, manifest(1))
    place(tmp_path, manifest(2, cues=()))
    row = read_session(place(tmp_path, manifest(3)))
    assert row is not None
    widget.show_session(row)
    assert "To mine all 2 sessions at once" in widget.label.text()
    place(tmp_path, manifest(1, cues=()))  # the only other one has no subtitle either now
    widget.show_session(row)
    assert widget.label.text() == SINGLE


def test_the_panel_lines_up_with_the_banners(panel):
    widget, _ = panel
    assert widget.layout().contentsMargins() == QMargins(6, 4, 4, 4)


def test_a_session_without_a_hand_off_hides_the_last_one_and_the_user_can_dismiss_it(panel):
    widget, _ = panel
    widget.show_session(SessionRow(PATH, manifest()))
    widget.show_session(SessionRow(PATH, manifest(cues=())))
    assert widget.isHidden()
    widget.show_session(SessionRow(PATH, manifest()))
    widget.close_button.click()
    assert widget.isHidden()
