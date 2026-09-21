"""Add-on Protocols: install services, VAD jobs and the OCR area picker (spec 13, 14)."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from anki_miner_game.models.addons import AddonStatus

ProgressCallback = Callable[[int, int], None]
"""Called as ``progress(done_bytes, total_bytes)``."""


class AddonService(Protocol):
    def status(self) -> AddonStatus: ...

    @property
    def size_bytes(self) -> int:
        """Approximate download size, shown by the wizard."""
        ...

    async def install(self, progress: ProgressCallback) -> None:
        """Download and verify; raises on failure and leaves no partial install behind."""
        ...


class VadJobs(Protocol):
    """VAD pass jobs keyed by manifest path.

    Each call only queues. While a job runs it reports ``Presenter.vad_progress``;
    when it ends it writes the outcome into the manifest's ``vad`` record, then
    calls ``Presenter.vad_finished`` with that state.
    """

    def queue(self, manifest_path: Path) -> None: ...

    def rerun(self, manifest_path: Path) -> None: ...

    def restore(self, manifest_path: Path) -> None:
        """Rewrite the subtitle from ``live_cues`` (state ``restored``)."""
        ...


class OcrAreaPicker(Protocol):
    async def pick(self, window_title: str | None) -> str | None:
        """Run owocr's own picker; the selected rectangles text, or ``None`` when cancelled."""
        ...
