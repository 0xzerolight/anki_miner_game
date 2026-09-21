"""The hand-off text shown after a session (spec 16, Appendix C): how to mine it in Anki Miner.

Shown for a session placed in its game folder with a subtitle. A session without one (no cues, or
not moved yet) has its own banner from the session actor, so it hides the last hand-off instead.
"""

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from anki_miner_game.gui.widgets.recent_sessions import SessionRow, UrlOpener


def handoff_text(row: SessionRow) -> str | None:
    """Appendix C for ``row``; ``None`` when it has no placed subtitle."""
    files = row.manifest.files
    if not row.has_subtitle or files is None:
        return None
    video = Path(files.video)
    return (
        f'Saved "{video.stem}". To mine it in Anki Miner: Video -> Single, choose the {video.suffix}; the subtitle '
        "fills in by itself. To mine every session of this game at once: Video -> Batch, and choose this folder "
        "for both the video and the subtitle folder."
    )


class HandoffPanel(QFrame):
    """The last session's hand-off, with its folder and an **Open folder** button; hidden until one."""

    def __init__(self, *, open_url: UrlOpener = QDesktopServices.openUrl, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open_url = open_url
        self._folder: Path | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Dismiss")
        self.close_button.setAutoRaise(True)
        self.close_button.clicked.connect(self.hide)
        buttons = QVBoxLayout()
        buttons.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignRight)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        row = QHBoxLayout(self)
        row.addWidget(self.label, 1)
        row.addLayout(buttons)
        self.hide()

    def show_session(self, row: SessionRow) -> None:
        text = handoff_text(row)
        if text is None:
            self.hide()
            return
        self._folder = row.manifest_path.parent
        self.label.setText(f"{text}\nFolder: {self._folder}")
        self.show()

    def _open_folder(self) -> None:
        if self._folder is not None:
            self._open_url(QUrl.fromLocalFile(str(self._folder)))
