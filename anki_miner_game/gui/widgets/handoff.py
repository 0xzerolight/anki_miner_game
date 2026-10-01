"""The hand-off shown after a session (spec 16, Appendix C as amended by UJ-08): how to mine it in Anki Miner.

Shown for a session placed in its game folder with a subtitle; a session without one (no lines, or
not filed yet) has its own banner from the session actor, so it hides the last hand-off instead.
One sentence for this session (Video -> Single), and one for Video -> Batch once the game folder
holds two or more sessions. The folder's path is the **Open folder** button's tooltip; the margins
are the banners' (``widgets.banner_area``). The main window hides the panel when the next recording
starts.
"""

from pathlib import Path
from typing import Final

from PyQt6.QtCore import QDir, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QToolButton, QWidget

from anki_miner_game.gui import strings
from anki_miner_game.gui.widgets.recent_sessions import MANIFEST_SUFFIX, SessionRow, UrlOpener

MARGINS: Final = (6, 4, 4, 4)


def handoff_text(row: SessionRow, sessions: int = 1) -> str | None:
    """The hand-off for ``row``, ``sessions`` being how many its game folder holds; ``None`` when it
    has no placed subtitle."""
    files = row.manifest.files
    if not row.has_subtitle or files is None:
        return None
    text = (
        f'Saved "{Path(files.video).stem}". In Anki Miner, choose Video -> Single and pick this video; '
        "the subtitle fills in by itself."
    )
    if sessions >= 2:
        text += (
            f" To mine all {sessions} sessions at once: Video -> Batch, with this folder as both the video and "
            "the subtitle folder."
        )
    return text


def sessions_in(folder: Path) -> int:
    """How many session manifests ``folder`` holds (0 when it cannot be read)."""
    try:
        return sum(1 for _ in folder.glob(f"*{MANIFEST_SUFFIX}"))
    except OSError:
        return 0


class HandoffPanel(QFrame):
    """The last session's hand-off with **Open folder**; hidden until one."""

    def __init__(self, *, open_url: UrlOpener = QDesktopServices.openUrl, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open_url = open_url
        self._folder: Path | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_button = QPushButton(strings.OPEN_FOLDER)
        self.open_button.clicked.connect(self._open_folder)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Dismiss")
        self.close_button.setAutoRaise(True)
        self.close_button.clicked.connect(self.hide)
        grid = QGridLayout(self)
        grid.setContentsMargins(*MARGINS)
        grid.addWidget(self.label, 0, 0)
        grid.addWidget(self.close_button, 0, 1, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.open_button, 1, 0, Qt.AlignmentFlag.AlignRight)
        grid.setColumnStretch(0, 1)
        self.hide()

    def show_session(self, row: SessionRow) -> None:
        folder = row.manifest_path.parent
        text = handoff_text(row, sessions_in(folder))
        if text is None:
            self.hide()
            return
        self._folder = folder
        self.label.setText(text)
        self.open_button.setToolTip(QDir.toNativeSeparators(str(folder)))
        self.show()

    def _open_folder(self) -> None:
        if self._folder is not None:
            self._open_url(QUrl.fromLocalFile(str(self._folder)))
