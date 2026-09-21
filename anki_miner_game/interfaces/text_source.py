"""Text source Protocol (spec 8.1)."""

from collections.abc import Callable
from typing import Protocol

from anki_miner_game.models.messages import SourceStatus

LineSink = Callable[[str, float, str], None]
"""Called as ``sink(raw, t_mono, source_id)``."""


class TextSource(Protocol):
    """A producer of raw text lines.

    Implementations take ``now: Callable[[], float] = time.monotonic`` in their
    constructor and read ``t_mono = now()`` at frame receipt, before any
    queueing; nothing downstream re-stamps it. ``start`` returns at once. The
    sink is called on the source's own thread (the I/O loop for websocket
    sources, the Qt main thread for the clipboard), so the composition passes
    one that only posts ``LineReceived`` through ``SessionControl.post``.
    """

    @property
    def id(self) -> str: ...

    @property
    def status(self) -> SourceStatus: ...

    def start(self, sink: LineSink) -> None: ...

    def stop(self) -> None: ...
