"""The live list (spec 16 item 4) and the cue count shown beside Start/Stop (item 3).

Both read the session actor's ``LineAccepted`` events (``Presenter.line_accepted``). A line with an
offset is one the actor journalled at that offset; one without was shown only (no recording, paused,
or past the stop). The count is the actor's ``LineRecord``s for the running recording, so it follows
the actor's journal rules (``session.session`` docstring):

- A typewriter merge (``replaces_previous``) is a ``ReplaceRecord``, no new line, when its base is
  the last line journalled; otherwise the actor journals it as a new line.
- Lines held for an auto start are published again with their offsets at ``STARTED``.

A merge keeps its base line's ``t_mono`` and ``source_id`` (``TextPipeline``), which is how both find
the line a merge replaces.
"""

from typing import Final

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QPalette
from PyQt6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QWidget

from anki_miner_game.gui.widgets import clock_text
from anki_miner_game.models.lines import GameLine

LIVE_LINES: Final = 200
"""Spec 16: the last 200 accepted lines."""

_LINE_ROLE: Final = Qt.ItemDataRole.UserRole
_OFFSET_ROLE: Final = Qt.ItemDataRole.UserRole + 1


def same_line(a: GameLine, b: GameLine) -> bool:
    """``b`` is ``a`` or a typewriter merge of it."""
    return a.t_mono == b.t_mono and a.source_id == b.source_id


class JournalCounter:
    """The lines the actor has journalled in the running recording; ``reset`` when one starts."""

    def __init__(self) -> None:
        self.count = 0
        self._tail: GameLine | None = None
        """The line behind the journal's last ``LineRecord``, as the actor's ``_Session.tail``."""

    def reset(self) -> None:
        self.count = 0
        self._tail = None

    def add(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        if offset_ms is None:
            return
        if not (replaces_previous and self._tail is not None and same_line(self._tail, line)):
            self.count += 1
        self._tail = line


class LiveList(QListWidget):
    """The last ``LIVE_LINES`` accepted lines, read-only; lines outside a recording are dimmed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)

    def add(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        bar = self.verticalScrollBar()
        at_end = bar is None or bar.value() >= bar.maximum()  # follow new lines unless scrolled back
        item = self._listed(line)
        if item is not None and (replaces_previous or (offset_ms is not None and item.data(_LINE_ROLE) == line)):
            self._show(item, line, offset_ms)  # a merge, or a held line journalled at STARTED
        else:
            item = QListWidgetItem()
            self._show(item, line, offset_ms)
            self.addItem(item)
            while self.count() > LIVE_LINES:
                self.takeItem(0)
        if at_end:
            self.scrollToBottom()

    def entries(self) -> list[tuple[str, int | None]]:
        """``(text, offset_ms)`` per listed line, oldest first."""
        rows: list[tuple[str, int | None]] = []
        for row in range(self.count()):
            item = self.item(row)
            if item is not None:
                rows.append((item.text(), item.data(_OFFSET_ROLE)))
        return rows

    def _listed(self, line: GameLine) -> QListWidgetItem | None:
        for row in range(self.count() - 1, -1, -1):
            item = self.item(row)
            if item is not None and same_line(item.data(_LINE_ROLE), line):
                return item
        return None

    def _show(self, item: QListWidgetItem, line: GameLine, offset_ms: int | None) -> None:
        item.setText(line.text)
        item.setData(_LINE_ROLE, line)
        item.setData(_OFFSET_ROLE, offset_ms)
        if offset_ms is None:
            item.setToolTip("Not in a recording")
            item.setForeground(QBrush(self.palette().color(QPalette.ColorRole.PlaceholderText)))
        else:
            item.setToolTip(f"In the recording at {clock_text(offset_ms / 1000)}")
            item.setForeground(QBrush(self.palette().color(QPalette.ColorRole.Text)))
