"""The status row (spec 16 item 1): one light for OBS, each enabled text source, and OCR in OCR mode.

Each light shows one of the four ``SourceStatus`` values the session actor publishes
(``Presenter.source_status``). OBS and the configured text sources always have a light. A source the
armed game adds (the clipboard, OCR) gets one on its first live status and loses it at disarm
(``clear_armed_only``); the ``DISCONNECTED`` its stop reports after that adds nothing back.
"""

from collections.abc import Sequence
from typing import Final

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from anki_miner_game.models.messages import OBS_SOURCE_ID, SourceStatus

LIGHT_COLOUR: Final = {
    SourceStatus.DISCONNECTED: "#9e9e9e",
    SourceStatus.CONNECTING: "#f9a825",
    SourceStatus.CONNECTED: "#43a047",
    SourceStatus.RECEIVING: "#1e88e5",
}
KNOWN_NAMES: Final = {OBS_SOURCE_ID: "OBS", "clipboard": "Clipboard", "ocr": "OCR"}
"""Lights for ids no ``TextSourceConfig`` names: OBS and the clipboard and OCR sources' fixed ids."""
_DOT_PX: Final = 10


class StatusLight(QWidget):
    """A coloured dot and a name; the tooltip says the state."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.status = SourceStatus.DISCONNECTED
        self.dot = QLabel()
        self.dot.setFixedSize(_DOT_PX, _DOT_PX)
        label = QLabel(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.dot)
        layout.addWidget(label)
        self.set_status(SourceStatus.DISCONNECTED)

    def set_status(self, status: SourceStatus) -> None:
        self.status = status
        self.dot.setStyleSheet(f"background-color: {LIGHT_COLOUR[status]}; border-radius: {_DOT_PX // 2}px;")
        self.setToolTip(f"{self.name}: {status.value}")


class StatusRow(QWidget):
    """``text_sources`` lists ``(id, name)`` of the enabled text sources, in the order shown."""

    def __init__(self, text_sources: Sequence[tuple[str, str]] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self._layout.addStretch(1)
        self._lights: dict[str, StatusLight] = {}
        self._configured: set[str] = set()
        self._add(OBS_SOURCE_ID, KNOWN_NAMES[OBS_SOURCE_ID])
        self.set_text_sources(text_sources)

    def set_text_sources(self, text_sources: Sequence[tuple[str, str]]) -> None:
        """Show these configured sources after OBS; a source kept keeps its state."""
        states = {source_id: light.status for source_id, light in self._lights.items()}
        for source_id in list(self._lights):
            if source_id != OBS_SOURCE_ID:
                self._remove(source_id)
        self._configured = {source_id for source_id, _name in text_sources}
        for source_id, name in text_sources:
            self._add(source_id, name).set_status(states.get(source_id, SourceStatus.DISCONNECTED))

    def set_status(self, source_id: str, status: SourceStatus) -> None:
        light = self._lights.get(source_id)
        if light is None:
            if status is SourceStatus.DISCONNECTED:
                return
            light = self._add(source_id, KNOWN_NAMES.get(source_id, source_id))
        light.set_status(status)

    def clear_armed_only(self) -> None:
        """Drop the lights of sources only the armed game used (disarm)."""
        for source_id in list(self._lights):
            if source_id != OBS_SOURCE_ID and source_id not in self._configured:
                self._remove(source_id)

    def status(self, source_id: str) -> SourceStatus | None:
        light = self._lights.get(source_id)
        return None if light is None else light.status

    def light(self, source_id: str) -> StatusLight:
        return self._lights[source_id]

    def names(self) -> list[str]:
        return [light.name for light in self._lights.values()]

    def _add(self, source_id: str, name: str) -> StatusLight:
        light = StatusLight(name, self)
        self._lights[source_id] = light
        self._layout.insertWidget(self._layout.count() - 1, light)  # before the stretch
        return light

    def _remove(self, source_id: str) -> None:
        light = self._lights.pop(source_id)
        self._layout.removeWidget(light)
        light.hide()
        light.deleteLater()
