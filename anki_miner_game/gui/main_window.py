"""The main window (spec 16), compact and single-column:

1. Status row: OBS, each enabled text source, OCR in OCR mode (``widgets.status_row``).
2. Game dropdown, **New game…**, **Edit…**.
3. **Arm** / **Disarm**, **Start** / **Stop**, the state, the elapsed time and the cue count.
4. Banners (``widgets.banner_area``) and the last session's hand-off text (Appendix C).
5. Live list: the last 200 accepted lines (``widgets.live_list``).
6. Recent sessions with **Open folder**; VAD re-run and restore in a row's context menu.

It reaches the rest of the app only through ``SessionControl`` (commands out), the presenter's
signals (state in) and ``VadJobs``; its slots run on the main thread. The cue count is the number of
lines the session actor journalled in the running recording, counted from its ``LineAccepted``
events (``widgets.live_list.JournalCounter``); after an app restart that resumed a recording it
counts from the resume.

The dialogs and the wizard are the app's to open: the window only asks (``new_game_requested``,
``edit_game_requested(slug)``, ``settings_requested``, ``setup_requested``). Closing the window
quits, except while armed or recording with a tray icon shown (``minimise_to_tray``, set by the app
when ``Tray.show()`` succeeds): then it hides to the tray. The recent sessions load when the window
is built, before the actor runs, so interrupted VAD passes are handed on before any new job is
queued (``widgets.recent_sessions``).
"""

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QDesktopServices
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
from anki_miner_game.gui.widgets import clock_text
from anki_miner_game.gui.widgets.banner_area import BannerArea
from anki_miner_game.gui.widgets.handoff import HandoffPanel
from anki_miner_game.gui.widgets.live_list import JournalCounter, LiveList
from anki_miner_game.gui.widgets.recent_sessions import RecentSessions, UrlOpener, read_session
from anki_miner_game.gui.widgets.status_row import StatusRow
from anki_miner_game.interfaces.addons import VadJobs
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import AppState, CommandKind, SourceStatus, UserCommand

WINDOW_TITLE: Final = "Anki Miner Game"
ELAPSED_REFRESH_MS: Final = 1000
_STATE_TEXT: Final = {
    AppState.IDLE: "Idle",
    AppState.ARMED: "Armed",
    AppState.RECORDING: "Recording",
    AppState.FINALISING: "Finalising",
}


class MainWindow(QMainWindow):
    """``games`` lists ``(slug, title)`` and ``text_sources`` the enabled sources' ``(id, name)``.

    ``on_quit`` is called instead of closing (the app quits). ``output_root()`` is the recordings
    folder the recent sessions list; without it the list stays empty.
    """

    new_game_requested = pyqtSignal()
    edit_game_requested = pyqtSignal(str)
    """The slug of the selected game."""
    settings_requested = pyqtSignal()
    setup_requested = pyqtSignal()

    def __init__(
        self,
        control: SessionControl,
        signals: PresenterSignals,
        games: Sequence[tuple[str, str]],
        *,
        on_quit: Callable[[], None],
        selected: str | None = None,
        now: Callable[[], float] = time.monotonic,
        text_sources: Sequence[tuple[str, str]] = (),
        output_root: Callable[[], Path] | None = None,
        vad_jobs: VadJobs | None = None,
        open_url: UrlOpener = QDesktopServices.openUrl,
    ) -> None:
        super().__init__()
        self._control = control
        self._on_quit = on_quit
        self._now = now
        self._output_root = output_root
        self._state = AppState.IDLE
        self._recording_since: float | None = None
        self._last_elapsed = 0.0
        """The length of the last recording, shown until the next one starts."""
        self._journalled = JournalCounter()
        self.minimise_to_tray = False
        """Set by the app while a tray icon is shown."""
        self.setWindowTitle(WINDOW_TITLE)
        self._build_menu()

        self.status_row = StatusRow(text_sources)
        self.game = QComboBox()
        self.new_game_button = QPushButton("New game…")
        self.new_game_button.clicked.connect(lambda _checked=False: self.new_game_requested.emit())
        self.edit_game_button = QPushButton("Edit…")
        self.edit_game_button.clicked.connect(self._edit_clicked)
        self.arm_button = QPushButton("Arm")
        self.arm_button.clicked.connect(self._arm_clicked)
        self.record_button = QPushButton("Start")
        self.record_button.clicked.connect(self._record_clicked)
        self.state_label = QLabel()
        self.elapsed_label = QLabel()
        self.cues_label = QLabel()
        self.banners = BannerArea()
        self.handoff = HandoffPanel(open_url=open_url)
        self.live_list = LiveList()
        self.recent = RecentSessions(vad_jobs=vad_jobs, open_url=open_url)

        game_row = QHBoxLayout()
        game_row.addWidget(self.game, 1)
        game_row.addWidget(self.new_game_button)
        game_row.addWidget(self.edit_game_button)
        controls = QHBoxLayout()
        controls.addWidget(self.arm_button)
        controls.addWidget(self.record_button)
        controls.addWidget(self.state_label, 1)
        controls.addWidget(self.elapsed_label)
        controls.addWidget(self.cues_label)
        column = QVBoxLayout()
        column.addWidget(self.status_row)
        column.addLayout(game_row)
        column.addLayout(controls)
        column.addWidget(self.banners)
        column.addWidget(self.handoff)
        column.addWidget(QLabel("Lines"))
        column.addWidget(self.live_list, 3)
        column.addWidget(QLabel("Recent sessions"))
        column.addWidget(self.recent, 2)
        body = QWidget()
        body.setLayout(column)
        self.setCentralWidget(body)
        self.resize(560, 680)

        self._timer = QTimer(self)
        self._timer.setInterval(ELAPSED_REFRESH_MS)
        self._timer.timeout.connect(self._show_counts)
        signals.state_changed.connect(self._on_state)
        signals.source_status.connect(self._on_source_status)
        signals.line_accepted.connect(self._on_line)
        signals.banner.connect(self.banners.show_banner)
        signals.banner_cleared.connect(self.banners.clear)
        signals.session_finished.connect(self._on_session_finished)
        signals.vad_progress.connect(self.recent.vad_progress)
        signals.vad_finished.connect(self.recent.vad_finished)
        self.set_games(games, selected)
        self.reload_sessions()

    def _build_menu(self) -> None:
        self.settings_action = QAction("Settings…", self)
        self.settings_action.triggered.connect(lambda _checked=False: self.settings_requested.emit())
        self.setup_action = QAction("Setup wizard…", self)
        self.setup_action.triggered.connect(lambda _checked=False: self.setup_requested.emit())
        self.quit_action = QAction("Quit", self)
        self.quit_action.triggered.connect(lambda _checked=False: self._on_quit())
        bar = self.menuBar()
        menu = bar.addMenu("&File") if bar is not None else None
        if menu is not None:
            menu.addAction(self.settings_action)
            menu.addAction(self.setup_action)
            menu.addSeparator()
            menu.addAction(self.quit_action)

    # --- what the app changes -------------------------------------------------------------------

    def set_games(self, games: Sequence[tuple[str, str]], selected: str | None = None) -> None:
        """Replace the game list; ``selected`` (else the current game, while listed) stays selected."""
        keep = selected if selected is not None else self.game.currentData()
        self.game.clear()
        for slug, title in games:
            self.game.addItem(title, slug)
        if keep is not None and (index := self.game.findData(keep)) >= 0:
            self.game.setCurrentIndex(index)
        self._show_state()

    def set_text_sources(self, text_sources: Sequence[tuple[str, str]]) -> None:
        self.status_row.set_text_sources(text_sources)

    def reload_sessions(self) -> None:
        """List the recent sessions again from ``output_root()`` (after a finalise, or a new folder)."""
        if self._output_root is not None:
            self.recent.load(self._output_root())

    def banner_texts(self) -> list[str]:
        """The banners shown, oldest first."""
        return self.banners.texts()

    # --- commands out ---------------------------------------------------------------------------

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

    def _edit_clicked(self) -> None:
        slug = self.game.currentData()
        if isinstance(slug, str):
            self.edit_game_requested.emit(slug)

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 - Qt override
        if event is not None:
            event.ignore()
        if self.minimise_to_tray and self._state is not AppState.IDLE:
            self.hide()
            return
        self._on_quit()

    # --- state in -------------------------------------------------------------------------------

    def _on_state(self, state: AppState, slug: str | None) -> None:
        if state is AppState.RECORDING and self._state is not AppState.RECORDING:
            self._recording_since = self._now()
            self._journalled.reset()
            self._timer.start()
        elif state is not AppState.RECORDING and self._recording_since is not None:
            self._last_elapsed = self._now() - self._recording_since
            self._recording_since = None
            self._timer.stop()
        if state is AppState.IDLE:
            self.status_row.clear_armed_only()
        self._state = state
        if slug is not None and (index := self.game.findData(slug)) >= 0:
            self.game.setCurrentIndex(index)
        self._show_state()

    def _on_source_status(self, source_id: str, status: SourceStatus) -> None:
        self.status_row.set_status(source_id, status)

    def _on_line(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        self.live_list.add(line, offset_ms, replaces_previous)
        self._journalled.add(line, offset_ms, replaces_previous)
        self._show_counts()

    def _on_session_finished(self, manifest_path: Path) -> None:
        self.reload_sessions()
        row = read_session(manifest_path)
        if row is not None:
            self.handoff.show_session(row)

    def _show_state(self) -> None:
        idle = self._state is AppState.IDLE
        has_game = self.game.count() > 0
        self.state_label.setText(_STATE_TEXT[self._state])
        self.game.setEnabled(idle)
        self.edit_game_button.setEnabled(has_game)
        self.arm_button.setText("Arm" if idle else "Disarm")
        self.arm_button.setEnabled((idle and has_game) or self._state is AppState.ARMED)
        self.record_button.setText("Stop" if self._state is AppState.RECORDING else "Start")
        self.record_button.setEnabled(self._state in (AppState.ARMED, AppState.RECORDING))
        self._show_counts()

    def _show_counts(self) -> None:
        since = self._recording_since
        self.elapsed_label.setText(clock_text(self._last_elapsed if since is None else self._now() - since))
        cues = self._journalled.count
        self.cues_label.setText(f"{cues} cue" if cues == 1 else f"{cues} cues")
