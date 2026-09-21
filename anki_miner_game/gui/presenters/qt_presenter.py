"""The ``Presenter`` the GUI listens to (spec 4.2): each call is re-emitted as a Qt signal.

The session actor, the VAD jobs and the composition call the presenter on their own threads (the
I/O loop, a VAD job thread). ``PresenterSignals`` lives on the Qt main thread, so a slot of a main
thread widget runs there through a queued connection, whatever thread emitted. Every argument
travels as a Python ``object``: millisecond counts can pass a C++ ``int``.
"""

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import VadState
from anki_miner_game.models.messages import AppState, Banner, SourceStatus


class PresenterSignals(QObject):
    """One signal per ``Presenter`` method, with the method's arguments. Build it on the main thread."""

    state_changed = pyqtSignal(object, object)
    """``(AppState, slug | None)``."""
    source_status = pyqtSignal(object, object)
    """``(source_id, SourceStatus)``."""
    line_accepted = pyqtSignal(object, object, object)
    """``(GameLine, offset_ms | None, replaces_previous)``."""
    banner = pyqtSignal(object)
    """``Banner``."""
    banner_cleared = pyqtSignal(object)
    """``key``."""
    session_finished = pyqtSignal(object)
    """``manifest_path``."""
    vad_progress = pyqtSignal(object, object, object)
    """``(manifest_path, done_ms, total_ms | None)``."""
    vad_finished = pyqtSignal(object, object)
    """``(manifest_path, VadState)``."""


class QtPresenter:
    """``Presenter`` over ``signals``; callable from any thread."""

    def __init__(self, signals: PresenterSignals | None = None) -> None:
        self.signals = signals if signals is not None else PresenterSignals()

    def state_changed(self, state: AppState, slug: str | None) -> None:
        self.signals.state_changed.emit(state, slug)

    def source_status(self, source_id: str, status: SourceStatus) -> None:
        self.signals.source_status.emit(source_id, status)

    def line_accepted(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None:
        self.signals.line_accepted.emit(line, offset_ms, replaces_previous)

    def banner(self, banner: Banner) -> None:
        self.signals.banner.emit(banner)

    def banner_cleared(self, key: str) -> None:
        self.signals.banner_cleared.emit(key)

    def session_finished(self, manifest_path: Path) -> None:
        self.signals.session_finished.emit(manifest_path)

    def vad_progress(self, manifest_path: Path, done_ms: int, total_ms: int | None) -> None:
        self.signals.vad_progress.emit(manifest_path, done_ms, total_ms)

    def vad_finished(self, manifest_path: Path, state: VadState) -> None:
        self.signals.vad_finished.emit(manifest_path, state)
