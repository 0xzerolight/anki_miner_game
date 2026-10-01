"""The inline banner area (spec 16 item 6, 17): recoverable failures are banners, never modal dialogs.

A banner is keyed (``Banner.key``): one with a shown key replaces that banner's text and level in
place, ``clear(key)`` removes it. The user can also dismiss one; a later banner with its key shows
again. Text is plain (OBS messages and file names are data, never markup) and keeps the palette's
text colour, so it reads in a light and a dark theme; the level shows as the style's 16 px
information, warning or critical icon at the left and a 3 px left edge in the level's colour token
(``gui.colours.BANNER_COLOUR``, UJ-11). A banner whose key is in ``actions`` carries a button with
that text under its text (``action_clicked(key)``), shown while ``set_actions_shown(True)`` (UJ-10).
"""

from collections.abc import Mapping

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from anki_miner_game.gui import colours
from anki_miner_game.gui.widgets.layout import level_pixmap
from anki_miner_game.models.messages import Banner, BannerLevel

_ICON_PX = 16


class _BannerFrame(QFrame):
    def __init__(self, parent: QWidget, action_text: str | None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self.level = BannerLevel.INFO
        self.icon = QLabel()
        self.icon.setFixedSize(_ICON_PX, _ICON_PX)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("Dismiss")
        self.close_button.setAutoRaise(True)
        self.action_button = QPushButton(action_text) if action_text is not None else None
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.addWidget(self.label)
        if self.action_button is not None:
            text.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignRight)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 4, 4)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

    def show_banner(self, banner: Banner) -> None:
        self.level = banner.level
        self.label.setText(banner.text)
        self.icon.setPixmap(level_pixmap(banner.level, self, _ICON_PX))
        edge = colours.BANNER_COLOUR[banner.level]
        self.setStyleSheet(f"QFrame#banner {{ border: 1px solid palette(mid); border-left: 3px solid {edge}; }}")


class BannerArea(QWidget):
    """The banners shown, oldest first. ``actions`` maps a banner key to its button's text."""

    action_clicked = pyqtSignal(str)

    def __init__(self, actions: Mapping[str, str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._actions = dict(actions or {})
        self._actions_shown = True
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._banners: dict[str, _BannerFrame] = {}

    def show_banner(self, banner: Banner) -> None:
        frame = self._banners.get(banner.key)
        if frame is None:
            frame = _BannerFrame(self, self._actions.get(banner.key))
            frame.close_button.clicked.connect(lambda _checked=False, key=banner.key: self.clear(key))
            if frame.action_button is not None:
                frame.action_button.clicked.connect(
                    lambda _checked=False, key=banner.key: self.action_clicked.emit(key)
                )
                frame.action_button.setHidden(not self._actions_shown)
            self._banners[banner.key] = frame
            self._column.addWidget(frame)
        frame.show_banner(banner)

    def clear(self, key: str) -> None:
        frame = self._banners.pop(key, None)
        if frame is not None:
            self._column.removeWidget(frame)
            frame.hide()
            frame.deleteLater()

    def set_actions_shown(self, shown: bool) -> None:
        """Show or hide every action button (the window shows Set up OBS… in Idle and Ready)."""
        self._actions_shown = shown
        for frame in self._banners.values():
            if frame.action_button is not None:
                frame.action_button.setHidden(not shown)

    def texts(self) -> list[str]:
        return [frame.label.text() for frame in self._banners.values()]

    def level(self, key: str) -> BannerLevel:
        return self._banners[key].level

    def label(self, key: str) -> QLabel:
        return self._banners[key].label

    def icon(self, key: str) -> QLabel:
        return self._banners[key].icon

    def frame(self, key: str) -> QFrame:
        return self._banners[key]

    def close_button(self, key: str) -> QToolButton:
        return self._banners[key].close_button

    def action_button(self, key: str) -> QPushButton | None:
        return self._banners[key].action_button
