"""Text source Protocol (spec 8.1)."""

from collections.abc import Callable
from typing import Protocol

from anki_miner_game.models.messages import SourceStatus

LineSink = Callable[[str, float, str], None]
"""Called as ``sink(raw, t_mono, source_id)``."""

StatusListener = Callable[[str, SourceStatus], None]
"""Called as ``listener(source_id, status)``."""


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

    def set_status_listener(self, cb: StatusListener) -> None:
        """Have ``cb(source_id, status)`` called on every transition of ``status``.

        Called once per change, after the ``status`` property already reports
        the new value, including the move to ``DISCONNECTED`` on ``stop``; never
        for an unchanged status. It runs on the same thread as the sink, so it
        must only hand the change on. One listener per source: a later call
        replaces it. Set it before ``start``; the status at that moment is not
        reported (read ``status``). The session actor registers it and
        publishes each change as ``SourceStatusChanged``, which the Presenter
        shows as ``source_status``.
        """
        ...
