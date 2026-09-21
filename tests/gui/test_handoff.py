"""The hand-off text shown after a session (spec 16, Appendix C)."""

from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl

from anki_miner_game.gui.widgets.handoff import HandoffPanel, handoff_text
from anki_miner_game.gui.widgets.recent_sessions import SessionRow
from anki_miner_game.models.manifest import FilesRecord, ManifestState
from tests.gui.session_fakes import TITLE, manifest

PATH = Path("/out") / TITLE / f"{TITLE} - 03.session.json"
APPENDIX_C = (
    'Saved "Steins;Gate - 03". To mine it in Anki Miner: Video -> Single, choose the .mkv; the subtitle '
    "fills in by itself. To mine every session of this game at once: Video -> Batch, and choose this folder "
    "for both the video and the subtitle folder."
)


def test_a_placed_session_with_a_subtitle_gets_the_appendix_c_text():
    assert handoff_text(SessionRow(PATH, manifest())) == APPENDIX_C


@pytest.mark.parametrize(
    "m",
    [
        manifest(cues=()),  # no cues: no subtitle to fill in (the no_cues banner says so)
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


def test_the_panel_shows_the_text_and_the_folder_and_opens_it(panel):
    widget, opened = panel
    assert widget.isHidden()
    widget.show_session(SessionRow(PATH, manifest()))
    assert not widget.isHidden()
    assert widget.label.text() == f"{APPENDIX_C}\nFolder: {PATH.parent}"
    widget.open_button.click()
    assert opened == [QUrl.fromLocalFile(str(PATH.parent))]


def test_a_session_without_a_hand_off_hides_the_last_one_and_the_user_can_dismiss_it(panel):
    widget, _ = panel
    widget.show_session(SessionRow(PATH, manifest()))
    widget.show_session(SessionRow(PATH, manifest(cues=())))
    assert widget.isHidden()
    widget.show_session(SessionRow(PATH, manifest()))
    widget.close_button.click()
    assert widget.isHidden()
