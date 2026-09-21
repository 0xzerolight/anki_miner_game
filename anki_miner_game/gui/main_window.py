"""The minimal main window (spec 16 items 2, 3 and 6): game, Arm/Disarm, Start/Stop, state, elapsed
time, cue count and the banner area. T19 replaces it with the full window.

It reaches the rest of the app only through ``SessionControl`` (commands out) and the presenter's
signals (state in); its slots run on the main thread.
"""

import time
from collections.abc import Callable, Sequence
from typing import Final

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui.presenters.qt_presenter import PresenterSignals
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import AppState, Banner, BannerLevel, CommandKind, UserCommand

WINDOW_TITLE: Final = "Anki Miner Game"
ELAPSED_REFRESH_MS: Final = 1000
_STATE_TEXT: Final = {
    AppState.IDLE: "Idle",
    AppState.ARMED: "Armed",
    AppState.RECORDING: "Recording",
    AppState.FINALISING: "Finalising",
}
_BANNER_STYLE: Final = {
    BannerLevel.INFO: "",
    BannerLevel.WARNING: "color: #b36b00;",
    BannerLevel.ERROR: "color: #c62828;",
}


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"


class MainWindow(QMainWindow):
    """``games`` lists ``(slug, title)``; ``on_quit`` is called instead of closing (the app quits)."""

    def __init__(
        self,
        control: SessionControl,
        signals: PresenterSignals,
        games: Sequence[tuple[str, str]],
        *,
        on_quit: Callable[[], None],
        selected: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._control = control
        self._on_quit = on_quit
        self._now = now
        self._state = AppState.IDLE
        self._recording_since: float | None = None
        self._last_elapsed = 0.0
        """The length of the last recording, shown until the next one starts."""
        self._cues = 0
        self._banners: dict[str, QLabel] = {}
        self.setWindowTitle(WINDOW_TITLE)

        self.game = QComboBox()
        for slug, title in games:
            self.game.addItem(title, slug)
        if selected is not None and (index := self.game.findData(selected)) >= 0:
            self.game.setCurrentIndex(index)
        self.arm_button = QPushButton("Arm")
        self.arm_button.clicked.connect(self._arm_clicked)
        self.record_button = QPushButton("Start")
        self.record_button.clicked.connect(self._record_clicked)
        self.state_label = QLabel()
        self.elapsed_label = QLabel()
        self.cues_label = QLabel()
        self._banner_box = QVBoxLayout()

        controls = QHBoxLayout()
        controls.addWidget(self.game, 1)
        controls.addWidget(self.arm_button)
        controls.addWidget(self.record_button)
        status = QHBoxLayout()
        status.addWidget(self.state_label, 1)
        status.addWidget(self.elapsed_label)
        status.addWidget(self.cues_label)
        column = QVBoxLayout()
        column.addLayout(controls)
        column.addLayout(status)
        column.addLayout(self._banner_box)
        column.addStretch(1)
        body = QWidget()
        body.setLayout(column)
        self.setCentralWidget(body)

        self._timer = QTimer(self)
        self._timer.setInterval(ELAPSED_REFRESH_MS)
        self._timer.timeout.connect(self._show_counts)
        signals.state_changed.connect(self._on_state)
        signals.line_accepted.connect(self._on_line)
        signals.banner.connect(self._on_banner)
        signals.banner_cleared.connect(self._on_banner_cleared)
        self._show_state()

    def banner_texts(self) -> list[str]:
        """The banners shown, oldest first."""
        return [label.text() for label in self._banners.values()]

    # --- commands out -----------------------------------------------------------------------

    def _arm_clicked(self) -> None:
        if self._state is AppState.IDLE:
            slug = self.game.currentData()
            if isinstance(slug, str):
                self._control.post(UserCommand(CommandKind.ARM, slug=slug))
        else:
            self._control.post(UserCommand(CommandKind.DISARM))

    def _record_clicked(self) -> None:
        kind = CommandKind.STOP if self._state is AppState.RECORDING else CommandKind.START
        self._control.post(UserCommand(kind))

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 - Qt override
        if event is not None:
            event.ignore()
        self._on_quit()

    # --- state in ---------------------------------------------------------------------------

    def _on_state(self, state: AppState, slug: str | None) -> None:
        if state is AppState.RECORDING and self._state is not AppState.RECORDING:
            self._recording_since = self._now()
            self._cues = 0
            self._timer.start()
        elif state is not AppState.RECORDING and self._recording_since is not None:
            self._last_elapsed = self._now() - self._recording_since
            self._recording_since = None
            self._timer.stop()
        self._state = state
        if slug is not None and (index := self.game.findData(slug)) >= 0:
            self.game.setCurrentIndex(index)
        self._show_state()

    def _on_line(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        if offset_ms is not None and not replaces_previous and self._state is AppState.RECORDING:
            self._cues += 1
            self._show_counts()

    def _on_banner(self, banner: Banner) -> None:
        label = self._banners.get(banner.key)
        if label is None:
            label = QLabel()
            label.setWordWrap(True)
            self._banners[banner.key] = label
            self._banner_box.addWidget(label)
        label.setText(banner.text)
        label.setStyleSheet(_BANNER_STYLE[banner.level])

    def _on_banner_cleared(self, key: str) -> None:
        label = self._banners.pop(key, None)
        if label is not None:
            self._banner_box.removeWidget(label)
            label.deleteLater()

    def _show_state(self) -> None:
        idle = self._state is AppState.IDLE
        self.state_label.setText(_STATE_TEXT[self._state])
        self.game.setEnabled(idle)
        self.arm_button.setText("Arm" if idle else "Disarm")
        self.arm_button.setEnabled((idle and self.game.count() > 0) or self._state is AppState.ARMED)
        self.record_button.setText("Stop" if self._state is AppState.RECORDING else "Start")
        self.record_button.setEnabled(self._state in (AppState.ARMED, AppState.RECORDING))
        self._show_counts()

    def _show_counts(self) -> None:
        since = self._recording_since
        self.elapsed_label.setText(_clock(self._last_elapsed if since is None else self._now() - since))
        self.cues_label.setText(f"{self._cues} cue" if self._cues == 1 else f"{self._cues} cues")
