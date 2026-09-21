"""Presenter Protocol: everything the GUI is told (spec 4.2, 16)."""

from pathlib import Path
from typing import Protocol

from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import VadState
from anki_miner_game.models.messages import AppState, Banner, SourceStatus


class Presenter(Protocol):
    """GUI output. Callable from any thread; the Qt implementation re-emits each call as a signal.

    The composition forwards the session actor's events one to one:
    ``StateChanged`` -> ``state_changed``, ``SourceStatusChanged`` ->
    ``source_status``, ``LineAccepted`` -> ``line_accepted``, ``BannerRaised``
    -> ``banner``, ``BannerCleared`` -> ``banner_cleared``,
    ``SessionFinalised`` -> ``session_finished``. ``RecordingStarted`` and
    ``RecordingStopped`` have no method here; they serve ``SessionControl``
    subscribers such as auto mode. ``VadJobs`` calls ``vad_progress`` while a
    pass runs and ``vad_finished`` once per job.
    """

    def state_changed(self, state: AppState) -> None: ...

    def source_status(self, source_id: str, status: SourceStatus) -> None: ...

    def line_accepted(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None: ...

    def banner(self, banner: Banner) -> None: ...

    def banner_cleared(self, key: str) -> None: ...

    def session_finished(self, manifest_path: Path) -> None: ...

    def vad_progress(self, manifest_path: Path, done_ms: int, total_ms: int) -> None: ...

    def vad_finished(self, manifest_path: Path, state: VadState) -> None:
        """A VAD job ended (done, failed, unavailable or restored); the manifest already holds the outcome.

        The session row re-reads the manifest for ``vad.message`` (spec 17: the row shows why).
        """
        ...
