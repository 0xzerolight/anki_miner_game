"""The main window (spec 16 as amended by the 2026-10-01 audit: D-01, D-05, UJ-01..UJ-14), one column:

1. Status row: from a click on, and while a game is ready or recording, one light for OBS and one
   **Game text** light (``widgets.status_row``); nothing in Idle. A flat **Settings…** button at its
   right; there is no menu bar (UJ-07).
2. Game dropdown, **New game…**, **Edit…** (Edit… and the dropdown only in Idle, D-05).
3. The primary button (**Start recording** / **Stop recording**), **Get ready** (Idle, for a game that
   starts at the first line), **Done playing** (Ready), and one status text (UJ-03).
4. Banners (``widgets.banner_area``; ``obs`` banners carry **Set up OBS…** while Idle) and the last
   session's hand-off, hidden when the next recording starts.
5. Lines: the last 200 accepted lines (``widgets.live_list``), with a hint while empty.
6. Recent sessions (``widgets.recent_sessions``), hidden while there is none.

With no game yet, rows 2-6 give way to one **Add your game…** button and a hint (UJ-06).

Commands go out through ``RecordingControls`` (shared with the tray), state comes in through the
presenter's signals, VAD jobs through ``VadJobs``; slots run on the main thread. The line count is
the lines the session actor journalled in the running recording, counted from its ``LineAccepted``
events (``widgets.live_list.JournalCounter``); after an app restart that resumed a recording it
counts from the resume.

The dialogs and the wizard are the app's to open: the window only asks (``new_game_requested``,
``edit_game_requested(slug)``, ``settings_requested``, ``setup_requested``). Closing the window
quits, except while ready or recording with a tray icon shown (``minimise_to_tray``, set by the app
when ``Tray.show()`` succeeds): then it hides to the tray and says so (``hidden_to_tray``). The
recent sessions load when the window is built, before the actor runs, so interrupted VAD passes are
handed on before any new job is queued (``widgets.recent_sessions``).
"""

import time
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QPalette
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui import banner_keys, colours, strings
from anki_miner_game.gui.presenters.qt_presenter import PresenterSignals
from anki_miner_game.gui.widgets import clock_text
from anki_miner_game.gui.widgets.banner_area import BannerArea
from anki_miner_game.gui.widgets.handoff import HandoffPanel
from anki_miner_game.gui.widgets.layout import screen_bounded
from anki_miner_game.gui.widgets.live_list import JournalCounter, LiveList
from anki_miner_game.gui.widgets.recent_sessions import RecentSessions, UrlOpener, read_session
from anki_miner_game.gui.widgets.status_row import StatusRow
from anki_miner_game.interfaces.addons import AddonService, VadJobs
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    Banner,
    CommandKind,
    SourceStatus,
    UserCommand,
)

WINDOW_TITLE: Final = strings.APP_NAME
WINDOW_SIZE: Final = QSize(560, 680)
"""Wanted at launch; bounded by the screen (UJ-13)."""
ELAPSED_REFRESH_MS: Final = 1000
RECORDING_DOT: Final = "●"
LINES_TITLE: Final = "Lines"
RECENT_TITLE: Final = "Recent sessions"
FIRST_RUN_HINT: Final = (
    "Add the game you want to play. You do this once per game; after that, pick it here and press Start recording."
)
IDLE_HINT: Final = "Start the game and your text hooker, then press Start recording. Lines from the game show here."
STARTING_OBS_HINT: Final = "Starting OBS. The first time can take up to 30 s."
WAITING_HINT: Final = "Waiting for the first line from your text hooker…"
AFTER_SESSION_HINT: Final = "Press Start recording for the next session, or Done playing when you stop."
_PRIMARY_PADDING_PX: Final = 32


class Pending(StrEnum):
    """What a click asked for while the window waits for the actor (UJ-02)."""

    GET_READY = "get_ready"
    START = "start"
    STOP = "stop"
    DONE = "done"


PENDING_TEXT: Final[Mapping[Pending, str]] = MappingProxyType(
    {Pending.GET_READY: strings.GETTING_OBS_READY, Pending.START: strings.STARTING, Pending.STOP: strings.STOPPING}
)
"""The primary button's text while that action is pending; Done playing keeps its own text."""


class RecordingControls(QObject):
    """The core loop's commands (D-01) and what is pending after one (UJ-02), for the window and the tray.

    ``record`` is the one primary action: in Idle it posts ``ARM(game())`` then ``START`` (the actor
    handles one message at a time, so the start runs after the arm and is ignored when the arm
    failed); while Ready ``START``; while recording ``STOP``. ``get_ready`` (``ARM`` only) runs in
    Idle for a game whose profile starts at the first line (``auto_start_game``); ``done_playing``
    (``DISARM``) while Ready. Each leaves ``pending`` set until the state that completes it
    (``RECORDING`` for the Idle click, not the ``ARMED`` on the way), a return to ``IDLE`` (nothing
    pending can complete from there), or a banner whose key is a failure of it
    (``gui.banner_keys``). There is no timeout: the actor ends each of these commands in one of
    those (P1 pins it). ``changed`` follows every command, state change and pending end.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        control: SessionControl,
        signals: PresenterSignals,
        *,
        game: Callable[[], str | None],
        auto_start_game: Callable[[str], bool] = lambda _slug: False,
        now: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._control = control
        self._game = game
        self._auto_start_game = auto_start_game
        self._now = now
        self.state = AppState.IDLE
        self.pending: Pending | None = None
        self._target: AppState | None = None
        self._failures: frozenset[str] = frozenset()
        self._recording_since: float | None = None
        signals.state_changed.connect(self._on_state)
        signals.banner.connect(self._on_banner)

    def game(self) -> str | None:
        """The selected game's slug."""
        return self._game()

    def can_record(self) -> bool:
        """The primary action can run: nothing pending, and Idle with a game, Ready or Recording."""
        if self.pending is not None:
            return False
        if self.state is AppState.IDLE:
            return self._game() is not None
        return self.state in (AppState.ARMED, AppState.RECORDING)

    def offers_get_ready(self) -> bool:
        slug = self._game()
        return self.state is AppState.IDLE and self.pending is None and slug is not None and self._auto_start_game(slug)

    def elapsed(self) -> float:
        """Seconds since the running recording started; 0 outside one."""
        since = self._recording_since
        return 0.0 if since is None else self._now() - since

    def record(self) -> None:
        if not self.can_record():
            return
        if self.state is AppState.IDLE:
            slug = self._game()
            assert slug is not None  # can_record
            self._post(UserCommand(CommandKind.ARM, slug=slug), UserCommand(CommandKind.START))
            self._wait(Pending.START, AppState.RECORDING, banner_keys.ARM_FAILED_KEYS | banner_keys.START_FAILED_KEYS)
        elif self.state is AppState.ARMED:
            self._post(UserCommand(CommandKind.START))
            self._wait(Pending.START, AppState.RECORDING, banner_keys.START_FAILED_KEYS)
        else:
            self._post(UserCommand(CommandKind.STOP))
            self._wait(Pending.STOP, AppState.FINALISING, banner_keys.STOP_FAILED_KEYS)

    def get_ready(self) -> None:
        slug = self._game()
        if slug is None or not self.offers_get_ready():
            return
        self._post(UserCommand(CommandKind.ARM, slug=slug))
        self._wait(Pending.GET_READY, AppState.ARMED, banner_keys.ARM_FAILED_KEYS)

    def done_playing(self) -> None:
        if self.state is not AppState.ARMED or self.pending is not None:
            return
        self._post(UserCommand(CommandKind.DISARM))
        self._wait(Pending.DONE, AppState.IDLE, banner_keys.DONE_FAILED_KEYS)

    def _post(self, *commands: UserCommand) -> None:
        for command in commands:
            self._control.post(command)

    def _wait(self, pending: Pending, target: AppState, failures: frozenset[str]) -> None:
        self.pending, self._target, self._failures = pending, target, failures
        self.changed.emit()

    def _end(self) -> None:
        self.pending, self._target, self._failures = None, None, frozenset()

    def _on_state(self, state: AppState, _slug: str | None) -> None:
        if state is AppState.RECORDING and self.state is not AppState.RECORDING:
            self._recording_since = self._now()
        elif state is not AppState.RECORDING:
            self._recording_since = None
        self.state = state
        if self.pending is not None:
            if state is self._target or state is AppState.IDLE:
                self._end()
            elif self.pending is Pending.START and state is AppState.ARMED:
                self._failures = banner_keys.START_FAILED_KEYS  # the Idle click's arm is done; its start is not
        self.changed.emit()

    def _on_banner(self, banner: Banner) -> None:
        if self.pending is not None and banner.key in self._failures:
            self._end()
            self.changed.emit()


def _hint_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


def _row(*widgets: QWidget, stretch: QWidget | None = None) -> QWidget:
    """A margin-less row of ``widgets``; ``stretch`` takes the spare width."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        row.addWidget(widget, 1 if widget is stretch else 0)
    return holder


class MainWindow(QMainWindow):
    """``games`` lists ``(slug, title)`` and ``text_sources`` the configured sources' ``(id, name)``.

    ``on_quit`` is called instead of closing (the app quits). ``output_root()`` is the recordings
    folder the recent sessions list; without it the list stays empty. ``vad_addon`` is the
    voice-trimming add-on: the recent sessions offer Trim again and Undo trimming only while it is
    installed (UJ-09); without it they never do. ``auto_start_game(slug)`` says
    whether that game's profile has auto mode with start at the first line (Get ready);
    ``auto_start_pending()`` whether, while Ready, the next line starts a recording (D-06).
    """

    new_game_requested = pyqtSignal()
    edit_game_requested = pyqtSignal(str)
    """The slug of the selected game."""
    settings_requested = pyqtSignal()
    setup_requested = pyqtSignal()
    """The Set up OBS… button of an OBS banner (UJ-10)."""
    hidden_to_tray = pyqtSignal()

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
        vad_addon: AddonService | None = None,
        open_url: UrlOpener = QDesktopServices.openUrl,
        auto_start_game: Callable[[str], bool] = lambda _slug: False,
        auto_start_pending: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__()
        self._on_quit = on_quit
        self._output_root = output_root
        self._auto_start_pending = auto_start_pending
        self._journalled = JournalCounter()
        self._session_this_time = False
        """A recording ended since the game got ready: the Lines hint says what comes next."""
        self.minimise_to_tray = False
        """Set by the app while a tray icon is shown."""
        self.setWindowTitle(WINDOW_TITLE)

        self.status_row = StatusRow(text_sources)
        self.settings_button = QPushButton(strings.SETTINGS)
        self.settings_button.setFlat(True)
        self.settings_button.clicked.connect(lambda _checked=False: self.settings_requested.emit())
        self.add_game_button = QPushButton(strings.ADD_YOUR_GAME)
        self.add_game_button.clicked.connect(lambda _checked=False: self.new_game_requested.emit())
        self.first_run_hint = _hint_label(FIRST_RUN_HINT)
        self.game = QComboBox()
        self.new_game_button = QPushButton(strings.NEW_GAME)
        self.new_game_button.clicked.connect(lambda _checked=False: self.new_game_requested.emit())
        self.edit_game_button = QPushButton(strings.EDIT_GAME)
        self.edit_game_button.clicked.connect(self._edit_clicked)
        self.primary_button = QPushButton(strings.START_RECORDING)
        self.primary_button.clicked.connect(lambda _checked=False: self.controls.record())
        metrics = self.primary_button.fontMetrics()
        widest = max(
            metrics.horizontalAdvance(text)
            for text in (strings.START_RECORDING, strings.STOP_RECORDING, *PENDING_TEXT.values())
        )
        self.primary_button.setMinimumWidth(widest + _PRIMARY_PADDING_PX)
        self.get_ready_button = QPushButton(strings.GET_READY)
        self.get_ready_button.clicked.connect(lambda _checked=False: self.controls.get_ready())
        self.done_button = QPushButton(strings.DONE_PLAYING)
        self.done_button.clicked.connect(lambda _checked=False: self.controls.done_playing())
        self.status_dot = QLabel(RECORDING_DOT)
        self.status_dot.setStyleSheet(f"color: {colours.RED};")
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.banners = BannerArea(actions=dict.fromkeys(banner_keys.SET_UP_OBS_KEYS, strings.SET_UP_OBS))
        self.banners.action_clicked.connect(lambda _key: self.setup_requested.emit())
        self.handoff = HandoffPanel(open_url=open_url)
        self.lines_label = QLabel(LINES_TITLE)
        self.live_list = LiveList()
        self.recent_label = QLabel(RECENT_TITLE)
        self.recent = RecentSessions(vad_jobs=vad_jobs, vad_addon=vad_addon, open_url=open_url)

        top = QHBoxLayout()
        top.addWidget(self.status_row)
        top.addStretch(1)
        top.addWidget(self.settings_button)
        self.first_run = QWidget()
        first = QVBoxLayout(self.first_run)
        first.setContentsMargins(0, 0, 0, 0)
        first.addWidget(self.add_game_button)
        first.addWidget(self.first_run_hint)
        self.game_row = _row(self.game, self.new_game_button, self.edit_game_button, stretch=self.game)
        self.control_row = _row(
            self.primary_button,
            self.get_ready_button,
            self.done_button,
            self.status_dot,
            self.status_label,
            stretch=self.status_label,
        )
        column = QVBoxLayout()
        column.addLayout(top)
        column.addWidget(self.first_run)
        column.addWidget(self.game_row)
        column.addWidget(self.control_row)
        column.addWidget(self.banners)
        column.addWidget(self.handoff)
        column.addWidget(self.lines_label)
        column.addWidget(self.live_list, 1)
        column.addWidget(self.recent_label)
        column.addWidget(self.recent, 1)
        column.addStretch(0)  # keeps the first-run rows at the top while the lists are hidden
        body = QWidget()
        body.setLayout(column)
        self.setCentralWidget(body)
        self.resize(screen_bounded(self, WINDOW_SIZE))

        self._timer = QTimer(self)
        self._timer.setInterval(ELAPSED_REFRESH_MS)
        self._timer.timeout.connect(self._show_status)
        # Before the controls: _on_state reads the state the controls still hold as the previous one.
        signals.state_changed.connect(self._on_state)
        self.controls = RecordingControls(
            control, signals, game=self._selected_slug, auto_start_game=auto_start_game, now=now, parent=self
        )
        self.controls.changed.connect(self._show_state)
        signals.source_status.connect(self._on_source_status)
        signals.line_accepted.connect(self._on_line)
        signals.banner.connect(self.banners.show_banner)
        signals.banner.connect(self._on_banner)
        signals.banner_cleared.connect(self.banners.clear)
        signals.session_finished.connect(self._on_session_finished)
        signals.vad_progress.connect(self.recent.vad_progress)
        signals.vad_finished.connect(self.recent.vad_finished)
        self.game.currentIndexChanged.connect(lambda _index: self._show_state())
        self.set_games(games, selected)
        self.reload_sessions()

    # --- what the app changes -------------------------------------------------------------------

    def set_games(self, games: Sequence[tuple[str, str]], selected: str | None = None) -> None:
        """Replace the game list; ``selected`` (else the current game, while listed) stays selected."""
        keep = selected if selected is not None else self.game.currentData()
        self.game.blockSignals(True)
        self.game.clear()
        for slug, title in games:
            self.game.addItem(title, slug)
        if keep is not None and (index := self.game.findData(keep)) >= 0:
            self.game.setCurrentIndex(index)
        self.game.blockSignals(False)
        self._show_state()

    def set_text_sources(self, text_sources: Sequence[tuple[str, str]]) -> None:
        self.status_row.set_text_sources(text_sources)

    def reload_sessions(self) -> None:
        """List the recent sessions again from ``output_root()`` (after a finalise, or a new folder)."""
        if self._output_root is not None:
            self.recent.load(self._output_root())
        self._show_recent()

    def banner_texts(self) -> list[str]:
        """The banners shown, oldest first."""
        return self.banners.texts()

    def selected_title(self) -> str | None:
        """The selected game's title (the tray names it)."""
        return self.game.currentText() if self.game.count() else None

    # --- commands out ---------------------------------------------------------------------------

    def _selected_slug(self) -> str | None:
        slug = self.game.currentData()
        return slug if isinstance(slug, str) else None

    def _edit_clicked(self) -> None:
        if (slug := self._selected_slug()) is not None:
            self.edit_game_requested.emit(slug)

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 - Qt override
        if event is not None:
            event.ignore()
        if self.minimise_to_tray and self.controls.state is not AppState.IDLE:
            self.hide()
            self.hidden_to_tray.emit()
            return
        self._on_quit()

    # --- state in -------------------------------------------------------------------------------

    def _on_state(self, state: AppState, slug: str | None) -> None:
        """Runs before ``RecordingControls`` hears the state: ``controls.state`` is still the previous one."""
        was = self.controls.state
        if state is AppState.RECORDING and was is not AppState.RECORDING:
            self._journalled.reset()
            self._timer.start()
            self.handoff.hide()  # UJ-08: the last hand-off goes once the next recording runs
        elif state is not AppState.RECORDING and was is AppState.RECORDING:
            self._timer.stop()
            self._session_this_time = True
        if state is AppState.IDLE:
            self.status_row.clear_sources()
            self._session_this_time = False
        if slug is not None and (index := self.game.findData(slug)) >= 0:
            self.game.setCurrentIndex(index)

    def _on_source_status(self, source_id: str, status: SourceStatus) -> None:
        self.status_row.set_status(source_id, status)
        self._show_placeholder()

    def _on_line(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        self.live_list.add(line, offset_ms, replaces_previous)
        self._journalled.add(line, offset_ms, replaces_previous)
        self._show_status()

    def _on_banner(self, _banner: Banner) -> None:
        """``auto_start_pending()`` can change on a banner alone (a late ``obs_exited`` lifts the D-06 pause)."""
        self._show_status()

    def _on_session_finished(self, manifest_path: Path) -> None:
        self.reload_sessions()
        row = read_session(manifest_path)
        if row is not None:
            self.handoff.show_session(row)

    # --- what the window shows ------------------------------------------------------------------

    def _show_state(self) -> None:
        controls = self.controls
        state, pending = controls.state, controls.pending
        idle = state is AppState.IDLE
        has_game = self.game.count() > 0
        self.first_run.setHidden(has_game)
        for part in (self.game_row, self.control_row, self.lines_label, self.live_list):
            part.setHidden(not has_game)
        self.game.setEnabled(idle and pending is None)
        self.edit_game_button.setEnabled(idle and has_game and pending is None)
        pending_text = PENDING_TEXT.get(pending) if pending is not None else None
        if pending_text is not None:
            self.primary_button.setText(pending_text)
        else:
            self.primary_button.setText(
                strings.STOP_RECORDING if state is AppState.RECORDING else strings.START_RECORDING
            )
        self.primary_button.setEnabled(controls.can_record())
        self.get_ready_button.setHidden(not controls.offers_get_ready())
        self.done_button.setHidden(state is not AppState.ARMED)
        self.done_button.setEnabled(pending is None)
        self.status_row.set_shown(not idle or pending is not None)
        self.banners.set_actions_shown(idle and pending is None)
        self._show_status()
        self._show_placeholder()
        self._show_recent()

    def _show_status(self) -> None:
        state = self.controls.state
        self.status_label.setText(
            strings.status_text(
                state,
                auto_start_pending=state is AppState.ARMED and self._auto_start_pending(),
                elapsed=clock_text(self.controls.elapsed()),
                lines=self._journalled.count,
            )
        )
        self.status_dot.setHidden(state is not AppState.RECORDING)

    def _show_placeholder(self) -> None:
        controls = self.controls
        if controls.pending is not None and self.status_row.status(OBS_SOURCE_ID) is SourceStatus.CONNECTING:
            text = STARTING_OBS_HINT
        elif controls.state is AppState.IDLE:
            text = IDLE_HINT
        elif controls.state is AppState.ARMED:
            text = AFTER_SESSION_HINT if self._session_this_time else WAITING_HINT
        elif controls.state is AppState.RECORDING:
            text = WAITING_HINT
        else:
            text = ""
        self.live_list.set_placeholder(text)

    def _show_recent(self) -> None:
        hidden = self.game.count() == 0 or not self.recent.rows()
        self.recent_label.setHidden(hidden)
        self.recent.setHidden(hidden)
