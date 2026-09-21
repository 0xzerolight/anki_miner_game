"""SessionControl Protocol: the session actor as the rest of the app sees it (spec 4.2, 6)."""

from collections.abc import Callable
from typing import Protocol

from anki_miner_game.models.messages import AppState, SessionEvent, SessionInput


class SessionControl(Protocol):
    """The single-threaded session actor.

    The actor takes ``now: Callable[[], float] = time.monotonic`` and consumes
    one queue of ``SessionInput`` messages on the I/O loop.
    """

    def post(self, msg: SessionInput) -> None:
        """Enqueue a message; safe to call from any thread."""
        ...

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        """Callbacks run on the actor's thread and must return quickly (post, do not block)."""
        ...

    @property
    def state(self) -> AppState: ...
