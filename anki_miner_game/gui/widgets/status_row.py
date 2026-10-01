"""The status row (spec 16 item 1 as amended by UJ-04): at most two lights.

- **OBS**: the OBS connection's ``SourceStatus`` (``OBS_SOURCE_ID``).
- **Game text: <names>**: the game's text sources taken together. Its names are the sources
  connected or receiving ("Game text: waiting" with none), its colour the best status among them
  (receiving > connected > connecting > not connected), and its tooltip lists every source with its
  state (``strings.SOURCE_STATUS_TEXT``).

The window shows the row only from a click on and while a game is ready or recording
(``set_shown``); in Idle the sources cannot run, so it shows nothing. A source counts once it reports
a status; a ``DISCONNECTED`` from a source not counted (its stop, reported after Done playing) adds
nothing. ``clear_sources`` (Idle) forgets them; OBS keeps its state. Configured sources name
themselves (``set_text_sources`` changes names only, so a live source and its state stay, B4-08);
the clipboard and OCR sources have fixed names. Not connected and connecting are rings, connected
and receiving filled dots, in ``gui.colours``' tokens.
"""

from collections.abc import Sequence
from typing import Final

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from anki_miner_game.gui import colours, strings
from anki_miner_game.models.messages import OBS_SOURCE_ID, SourceStatus

OBS_NAME: Final = "OBS"
KNOWN_NAMES: Final = {"clipboard": "Clipboard", "ocr": "OCR"}
"""Names for the clipboard and OCR sources' fixed ids, which no ``TextSourceConfig`` names."""
WAITING: Final = "waiting"
NO_SOURCE_TOOLTIP: Final = "No text source is running yet."
_RANK: Final = {
    SourceStatus.DISCONNECTED: 0,
    SourceStatus.CONNECTING: 1,
    SourceStatus.CONNECTED: 2,
    SourceStatus.RECEIVING: 3,
}
_LIVE: Final = frozenset({SourceStatus.CONNECTED, SourceStatus.RECEIVING})
_DOT_PX: Final = 10


def dot_style(status: SourceStatus) -> str:
    """A filled dot for connected and receiving, a ring otherwise, in the status's colour token."""
    colour = colours.SOURCE_COLOUR[status]
    radius = _DOT_PX // 2
    if colours.SOURCE_FILLED[status]:
        return f"background-color: {colour}; border-radius: {radius}px;"
    return f"border: 2px solid {colour}; border-radius: {radius}px;"


class StatusLight(QWidget):
    """A coloured dot and a name; the tooltip says the state."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = SourceStatus.DISCONNECTED
        self.dot = QLabel()
        self.dot.setFixedSize(_DOT_PX, _DOT_PX)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self.show_status(name, SourceStatus.DISCONNECTED, "")

    @property
    def name(self) -> str:
        return self.label.text()

    def show_status(self, name: str, status: SourceStatus, tooltip: str) -> None:
        self.status = status
        self.label.setText(name)
        self.dot.setStyleSheet(dot_style(status))
        self.setToolTip(tooltip)


class StatusRow(QWidget):
    """``text_sources`` lists ``(id, name)`` of the configured text sources (their names)."""

    def __init__(self, text_sources: Sequence[tuple[str, str]] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._names: dict[str, str] = {}
        self._states: dict[str, SourceStatus] = {}
        """The game's sources, in the order they first reported."""
        self._obs = SourceStatus.DISCONNECTED
        self._shown = True
        """``set_shown``'s word; a top-level row never shown still lists its lights."""
        self.obs_light = StatusLight(OBS_NAME, self)
        self.game_light = StatusLight(f"{strings.GAME_TEXT}: {WAITING}", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.obs_light)
        layout.addWidget(self.game_light)
        self.set_text_sources(text_sources)

    def set_text_sources(self, text_sources: Sequence[tuple[str, str]]) -> None:
        """New names for the configured sources; live sources and their states stay (B4-08)."""
        self._names = dict(text_sources)
        self._show()

    def set_status(self, source_id: str, status: SourceStatus) -> None:
        if source_id == OBS_SOURCE_ID:
            self._obs = status
        elif source_id in self._states or status is not SourceStatus.DISCONNECTED:
            self._states[source_id] = status
        else:
            return
        self._show()

    def clear_sources(self) -> None:
        """Forget the game's sources (Idle); OBS keeps its state."""
        self._states.clear()
        self._show()

    def set_shown(self, shown: bool) -> None:
        self._shown = shown
        self.setHidden(not shown)

    def status(self, source_id: str) -> SourceStatus | None:
        if source_id == OBS_SOURCE_ID:
            return self._obs
        return self._states.get(source_id)

    def game_text_status(self) -> SourceStatus:
        return max(self._states.values(), key=_RANK.__getitem__, default=SourceStatus.DISCONNECTED)

    def names(self) -> list[str]:
        """The lights shown, left to right; none while the row is hidden."""
        return [self.obs_light.name, self.game_light.name] if self._shown else []

    def _name(self, source_id: str) -> str:
        return self._names.get(source_id) or KNOWN_NAMES.get(source_id, source_id)

    def _show(self) -> None:
        self.obs_light.show_status(OBS_NAME, self._obs, f"{OBS_NAME}: {strings.SOURCE_STATUS_TEXT[self._obs]}")
        live = [self._name(source_id) for source_id, status in self._states.items() if status in _LIVE]
        label = f"{strings.GAME_TEXT}: {', '.join(live) if live else WAITING}"
        tooltip = "\n".join(
            f"{self._name(source_id)}: {strings.SOURCE_STATUS_TEXT[status]}"
            for source_id, status in self._states.items()
        )
        self.game_light.show_status(label, self.game_text_status(), tooltip or NO_SOURCE_TOOLTIP)
