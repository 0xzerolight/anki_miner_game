"""The I/O thread (spec 4.2): one asyncio loop in a ``QThread``.

It runs the session actor, the websocket text sources, the feed's websocket server, the owocr
supervisor, and every blocking ``obsws-python`` request through ``run_in_executor``. Code on the Qt
main thread hands it work with ``submit`` (``asyncio.run_coroutine_threadsafe``) and never touches
the loop's objects directly. The loop is the platform's default, which on Windows is the Proactor
loop that asyncio subprocesses (owocr, uv) need.
"""

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Coroutine
from typing import Any, Final, cast

from PyQt6.QtCore import QThread

log = logging.getLogger(__name__)

THREAD_NAME: Final = "io"
EXECUTOR_JOIN_TIMEOUT_S: Final = 10.0
"""How long stopping waits for worker threads the loop started (``asyncio.to_thread``)."""


class IoThread(QThread):
    """Owns the loop from ``start_loop`` to ``stop_loop``; both are called on the thread that made it."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName(THREAD_NAME)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("the I/O loop is not running")
        return self._loop

    def start_loop(self) -> asyncio.AbstractEventLoop:
        """Start the thread and return its loop once it runs."""
        self.start()
        self._ready.wait()
        return self.loop

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        """Run ``coro`` on the loop; safe from any thread. The future completes on the loop's thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop_loop(self) -> None:
        """Stop the loop, cancel the tasks still on it, and wait for the thread to end."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        self.wait()

    def run(self) -> None:
        threading.current_thread().name = THREAD_NAME
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                _cancel_all(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
                # Every concrete loop is a BaseEventLoop; only its stub takes the timeout (3.12).
                base = cast(asyncio.BaseEventLoop, loop)
                loop.run_until_complete(base.shutdown_default_executor(EXECUTOR_JOIN_TIMEOUT_S))
            finally:
                asyncio.set_event_loop(None)
                loop.close()


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel every task left on ``loop`` and let each run its cleanup, as ``asyncio.run`` does."""
    tasks = asyncio.all_tasks(loop)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    for task in tasks:
        if not task.cancelled() and task.exception() is not None:
            log.error("I/O task %s failed while stopping", task.get_name(), exc_info=task.exception())
