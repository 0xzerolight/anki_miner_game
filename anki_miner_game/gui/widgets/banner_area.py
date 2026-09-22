"""The inline banner area (spec 16 item 6, 17): recoverable failures are banners, never modal dialogs.

A banner is keyed (``Banner.key``): one with a shown key replaces that banner's text and level in
place, ``clear(key)`` removes it. The user can also dismiss one; a later banner with its key shows
again. Text is plain: OBS messages and file names are data, never markup.
"""

from typing import Final

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from anki_miner_game.models.messages import Banner, BannerLevel

BANNER_STYLE: Final = {
    BannerLevel.INFO: "",
    BannerLevel.WARNING: "color: #b36b00;",
    BannerLevel.ERROR: "color: #c62828;",
}


class _BannerFrame(QFrame):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.level = BannerLevel.INFO
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Dismiss")
        self.close_button.setAutoRaise(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 4, 4)
        row.addWidget(self.label, 1)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

    def show_banner(self, banner: Banner) -> None:
        self.level = banner.level
        self.label.setText(banner.text)
        self.label.setStyleSheet(BANNER_STYLE[banner.level])


class BannerArea(QWidget):
    """The banners shown, oldest first."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._banners: dict[str, _BannerFrame] = {}

    def show_banner(self, banner: Banner) -> None:
        frame = self._banners.get(banner.key)
        if frame is None:
            frame = _BannerFrame(self)
            frame.close_button.clicked.connect(lambda _checked=False, key=banner.key: self.clear(key))
            self._banners[banner.key] = frame
            self._column.addWidget(frame)
        frame.show_banner(banner)

    def clear(self, key: str) -> None:
        frame = self._banners.pop(key, None)
        if frame is not None:
            self._column.removeWidget(frame)
            frame.hide()
            frame.deleteLater()

    def texts(self) -> list[str]:
        return [frame.label.text() for frame in self._banners.values()]

    def level(self, key: str) -> BannerLevel:
        return self._banners[key].level

    def label(self, key: str) -> QLabel:
        return self._banners[key].label

    def close_button(self, key: str) -> QToolButton:
        return self._banners[key].close_button
