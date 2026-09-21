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

    @property
    def note(self) -> str | None:
        """A platform limitation to show beside the status, or ``None``. The OCR add-on on Linux
        says OCR needs an X11 session (Wayland is not supported in v1)."""
        ...

    async def install(self, progress: ProgressCallback) -> None:
        """Download, verify and install the add-on; a no-op while ``status()`` is ``READY``.

        A ``MISSING`` or ``BROKEN`` add-on is (re)installed.
        ``progress(done_bytes, total_bytes)`` may be called on any thread. Raises
        ``RuntimeError`` (the add-on's own subclass) with a message fit for a banner when the
        install fails, and at once when an install of the add-on is already running. A failure
        or a cancellation stops the work (a running ``uv`` is killed, a download stops at its next
        chunk) and removes what the attempt wrote, so no partial install is left behind.
        """
        ...


class VadJobs(Protocol):
    """VAD pass jobs keyed by manifest path.

    Each call only queues. While a job runs it reports ``Presenter.vad_progress``;
    when it ends, however it ends, it writes the outcome into the manifest's
    ``vad`` record, then calls ``Presenter.vad_finished`` with that state. A job
    skipped because its manifest is missing, unreadable or not a placed session
    with a subtitle, and a job dropped at shutdown, get no call.
    """

    def queue(self, manifest_path: Path) -> None: ...

    def rerun(self, manifest_path: Path) -> None: ...

    def restore(self, manifest_path: Path) -> None:
        """Rewrite the subtitle from ``live_cues`` (state ``restored``)."""
        ...


class OcrAreaPicker(Protocol):
    async def pick(self, window_title: str | None) -> str | None:
        """Run owocr's own picker; the selected rectangles text, or ``None`` when cancelled.

        Raises ``RuntimeError`` (the add-on's ``OcrError``) with a message fit for the dialog when
        owocr cannot run or exits without an answer.
        """
        ...
