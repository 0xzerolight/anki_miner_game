"""The tray icon (spec 16 as amended by UJ-05, UJ-12): it mirrors the window's buttons.

Its session items run the window's ``RecordingControls``, so a click from the tray is pending in the
window too, and they are hidden, never disabled, when they cannot run: Idle "Start recording:
<game>" (with "Get ready: <game>" for a game that starts at the first line; none without a game);
Ready "Start recording" and "Done playing"; Recording "Stop recording"; nothing while saving or
while a click is pending. Then "Open text feed" (hidden while the feed is off), "Show window" and
"Quit". The icon is the app icon with a dot in the state's colour; the tooltip names the state, the
game and the time recorded (``strings.tray_tooltip``), refreshed every second while recording.

Show window, a click on the icon and Quit are requests the app answers (``show_requested``,
``quit_requested``). ``game_title()`` is the window's game and ``feed_url()`` the text feed's page,
``None`` while the feed is off; both are read when the menu opens. ``tell_still_running`` says once
per run, where the desktop shows tray messages, that the window went to the tray (UJ-12).

The menu has no parent widget (the tray icon is no widget), so the tray keeps it.
``show()`` shows the icon when the desktop has a tray; the window minimises to it only then.
"""

from collections.abc import Callable
from typing import Final

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from anki_miner_game.gui import colours, strings
from anki_miner_game.gui.icon import app_icon
from anki_miner_game.gui.main_window import RecordingControls
from anki_miner_game.gui.widgets import clock_text
from anki_miner_game.models.messages import AppState

STILL_RUNNING_TITLE: Final = f"{strings.APP_NAME} is still running"
TOOLTIP_REFRESH_MS: Final = 1000
_ICON_PX: Final = 32
_DOT_PX: Final = 14


def state_icon(state: AppState, base: QIcon | None = None) -> QIcon:
    """``base`` (the app icon) with a dot in the state's colour at its lower right; the dot alone
    when ``base`` is null."""
    pixmap = QPixmap(_ICON_PX, _ICON_PX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if base is not None and not base.isNull():
        painter.drawPixmap(0, 0, base.pixmap(_ICON_PX, _ICON_PX))
        corner, size = _ICON_PX - _DOT_PX, _DOT_PX
        painter.setPen(QPen(QColor("#ffffff"), 1.5))
    else:
        corner, size = 2, _ICON_PX - 4
        painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(colours.STATE_COLOUR[state]))
    painter.drawEllipse(corner, corner, size - 1, size - 1)
    painter.end()
    return QIcon(pixmap)


class Tray(QObject):
    show_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(
        self,
        controls: RecordingControls,
        *,
        game_title: Callable[[], str | None],
        feed_url: Callable[[], str | None],
        open_url: Callable[[QUrl], object] = QDesktopServices.openUrl,
        supports_messages: Callable[[], bool] = QSystemTrayIcon.supportsMessages,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controls = controls
        self._game_title = game_title
        self._feed_url = feed_url
        self._open_url = open_url
        self._supports_messages = supports_messages
        self._base = app_icon()
        self._told = False
        self.menu = QMenu()
        self.record_action = self._action(strings.START_RECORDING, controls.record)
        self.get_ready_action = self._action(strings.GET_READY, controls.get_ready)
        self.done_action = self._action(strings.DONE_PLAYING, controls.done_playing)
        self.session_separator = self.menu.addSeparator()
        self.feed_action = self._action(strings.OPEN_TEXT_FEED, self._open_feed)
        self.show_action = self._action(strings.SHOW_WINDOW, self.show_requested.emit)
        self.menu.addSeparator()
        self.quit_action = self._action(strings.QUIT, self.quit_requested.emit)
        self.menu.aboutToShow.connect(self._refresh)
        self.icon = QSystemTrayIcon(self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._timer = QTimer(self)
        self._timer.setInterval(TOOLTIP_REFRESH_MS)
        self._timer.timeout.connect(self._show_tooltip)
        controls.changed.connect(self._refresh)
        self._refresh()

    def show(self) -> bool:
        """Show the icon; ``False`` when the desktop has no tray."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.icon.show()
        return True

    def tell_still_running(self) -> None:
        """The first hide to the tray in a run: say where the app went (UJ-12)."""
        if self._told or not self._supports_messages():
            return
        self._told = True
        doing = "is recording" if self._controls.state is AppState.RECORDING else "is ready"
        game = self._game_title() or strings.APP_NAME
        self.icon.showMessage(
            STILL_RUNNING_TITLE, f"{game} {doing}. Click this icon to open the window; right-click it to quit."
        )

    def _action(self, text: str, slot: Callable[[], object]) -> QAction:
        action = QAction(text, self.menu)
        action.triggered.connect(lambda _checked=False: slot())
        self.menu.addAction(action)
        return action

    def _open_feed(self) -> None:
        if (url := self._feed_url()) is not None:
            self._open_url(QUrl(url))

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_requested.emit()

    def _refresh(self) -> None:
        controls = self._controls
        state, title = controls.state, self._game_title()
        if state is AppState.IDLE:
            self.record_action.setText(f"{strings.START_RECORDING}: {title}")
            self.get_ready_action.setText(f"{strings.GET_READY}: {title}")
        else:
            self.record_action.setText(
                strings.STOP_RECORDING if state is AppState.RECORDING else strings.START_RECORDING
            )
        self.record_action.setVisible(controls.can_record())
        self.get_ready_action.setVisible(controls.offers_get_ready())
        self.done_action.setVisible(state is AppState.ARMED and controls.pending is None)
        if self.session_separator is not None:
            self.session_separator.setVisible(
                any(action.isVisible() for action in (self.record_action, self.get_ready_action, self.done_action))
            )
        self.feed_action.setVisible(self._feed_url() is not None)
        self.icon.setIcon(state_icon(state, self._base))
        self._show_tooltip()
        if state is AppState.RECORDING:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _show_tooltip(self) -> None:
        controls = self._controls
        self.icon.setToolTip(strings.tray_tooltip(controls.state, self._game_title(), clock_text(controls.elapsed())))
