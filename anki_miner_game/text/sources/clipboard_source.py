"""Text source for lines copied to the system clipboard (spec 8.1).

Replaces GameSentenceMiner's polling ``gametext.py::monitor_clipboard`` with ``QClipboard.dataChanged``.
Qt sees clipboard changes on Windows and X11; on Wayland only while one of the app's windows has focus
(the wizard says so).
"""

import logging
import time
from collections.abc import Callable
from typing import Final

from PyQt6.QtGui import QClipboard, QGuiApplication

from anki_miner_game.interfaces.text_source import LineSink, StatusListener
from anki_miner_game.models.messages import SourceStatus

logger = logging.getLogger(__name__)

CLIPBOARD_SOURCE_ID: Final = "clipboard"


class ClipboardSource:
    """A ``TextSource`` fed by the system clipboard.

    Lives on the Qt main thread, where ``QClipboard`` lives: call ``start`` and ``stop`` there, after
    the ``QGuiApplication`` exists. The sink and the status listener run there too, so each must only
    hand its value on. Only text contents count, and a change this app made itself (the clipboard
    reports it owns the data) is ignored. Status: ``CONNECTED`` once started, ``RECEIVING`` from the
    first line, ``DISCONNECTED`` before ``start`` and after ``stop``.
    """

    def __init__(self, source_id: str = CLIPBOARD_SOURCE_ID, *, now: Callable[[], float] = time.monotonic) -> None:
        self._id = source_id
        self._now = now
        self._status = SourceStatus.DISCONNECTED
        self._listener: StatusListener | None = None
        self._clipboard: QClipboard | None = None
        self._sink: LineSink | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> SourceStatus:
        return self._status

    def set_status_listener(self, cb: StatusListener) -> None:
        self._listener = cb

    def start(self, sink: LineSink) -> None:
        if self._clipboard is not None:
            raise RuntimeError(f"text source {self._id!r} is already started")
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            raise RuntimeError("the clipboard source needs a QGuiApplication")
        self._sink = sink
        self._clipboard = clipboard
        clipboard.dataChanged.connect(self._on_data_changed)
        self._set_status(SourceStatus.CONNECTED)

    def stop(self) -> None:
        clipboard, self._clipboard = self._clipboard, None
        if clipboard is None:
            return
        clipboard.dataChanged.disconnect(self._on_data_changed)
        self._sink = None
        self._set_status(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        """Nothing to wait for: ``stop`` leaves no background work."""

    def _on_data_changed(self) -> None:
        t_mono = self._now()
        clipboard, sink = self._clipboard, self._sink
        if clipboard is None or sink is None or clipboard.ownsClipboard():
            return
        mime = clipboard.mimeData()
        if mime is None or not mime.hasText():
            return
        try:
            sink(mime.text(), t_mono, self._id)
        except Exception:
            logger.exception("%s: the sink failed", self._id)
        self._set_status(SourceStatus.RECEIVING)

    def _set_status(self, status: SourceStatus) -> None:
        if status == self._status:
            return
        self._status = status
        if self._listener is not None:
            try:
                self._listener(self._id, status)
            except Exception:
                logger.exception("%s: the status listener failed", self._id)
