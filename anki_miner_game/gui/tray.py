"""The tray icon (spec 16): Arm, Start/Stop, Open text feed, Show window, Quit.

Like the window, it posts commands through ``SessionControl`` and follows the state through the
presenter's signals; the icon's colour and tooltip say the state. Show window, a click on the icon
and Quit are requests the app answers (``show_requested``, ``quit_requested``). ``game()`` is the
game Arm arms (the window's selection) and ``feed_url()`` the text feed's page, ``None`` while the
feed is off; both are read when the menu opens.

The menu has no parent widget (the tray icon is no widget), so the tray keeps it.
``show()`` shows the icon when the desktop has a tray; the window minimises to it only then.
"""

from collections.abc import Callable
from typing import Final

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from anki_miner_game.gui.presenters.qt_presenter import PresenterSignals
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.messages import AppState, CommandKind, UserCommand

APP_NAME: Final = "Anki Miner Game"
STATE_COLOUR: Final = {
    AppState.IDLE: "#9e9e9e",
    AppState.ARMED: "#f9a825",
    AppState.RECORDING: "#e53935",
    AppState.FINALISING: "#f9a825",
}
_ICON_PX: Final = 32


def state_icon(state: AppState) -> QIcon:
    """A filled circle in the state's colour."""
    pixmap = QPixmap(_ICON_PX, _ICON_PX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(STATE_COLOUR[state]))
    painter.drawEllipse(2, 2, _ICON_PX - 4, _ICON_PX - 4)
    painter.end()
    return QIcon(pixmap)


class Tray(QObject):
    show_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(
        self,
        control: SessionControl,
        signals: PresenterSignals,
        *,
        game: Callable[[], str | None],
        feed_url: Callable[[], str | None],
        open_url: Callable[[QUrl], object] = QDesktopServices.openUrl,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._control = control
        self._game = game
        self._feed_url = feed_url
        self._open_url = open_url
        self.state = AppState.IDLE
        self.menu = QMenu()
        self.arm_action = self._action("Arm", self._arm)
        self.record_action = self._action("Start", self._record)
        self.menu.addSeparator()
        self.feed_action = self._action("Open text feed", self._open_feed)
        self.show_action = self._action("Show window", self.show_requested.emit)
        self.menu.addSeparator()
        self.quit_action = self._action("Quit", self.quit_requested.emit)
        self.menu.aboutToShow.connect(self._refresh)
        self.icon = QSystemTrayIcon(self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        signals.state_changed.connect(self._on_state)
        self._refresh()

    def show(self) -> bool:
        """Show the icon; ``False`` when the desktop has no tray."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.icon.show()
        return True

    def _action(self, text: str, slot: Callable[[], object]) -> QAction:
        action = QAction(text, self.menu)
        action.triggered.connect(lambda _checked=False: slot())
        self.menu.addAction(action)
        return action

    def _arm(self) -> None:
        if self.state is AppState.IDLE:
            if (slug := self._game()) is not None:
                self._control.post(UserCommand(CommandKind.ARM, slug=slug))
        else:
            self._control.post(UserCommand(CommandKind.DISARM))

    def _record(self) -> None:
        self._control.post(UserCommand(CommandKind.STOP if self.state is AppState.RECORDING else CommandKind.START))

    def _open_feed(self) -> None:
        if (url := self._feed_url()) is not None:
            self._open_url(QUrl(url))

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_requested.emit()

    def _on_state(self, state: AppState, _slug: str | None) -> None:
        self.state = state
        self._refresh()

    def _refresh(self) -> None:
        idle = self.state is AppState.IDLE
        self.arm_action.setText("Arm" if idle else "Disarm")
        self.arm_action.setEnabled((idle and self._game() is not None) or self.state is AppState.ARMED)
        self.record_action.setText("Stop" if self.state is AppState.RECORDING else "Start")
        self.record_action.setEnabled(self.state in (AppState.ARMED, AppState.RECORDING))
        self.feed_action.setEnabled(self._feed_url() is not None)
        self.icon.setIcon(state_icon(self.state))
        self.icon.setToolTip(f"{APP_NAME}: {self.state.value}")
