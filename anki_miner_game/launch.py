"""The entry point: ``anki_miner_game [--arm <slug> | --start | --stop | --toggle]`` (spec 16).

With an instance of this home already running, the verb (or, without one, "show your window") goes
to it and this process exits: 0 when it was accepted, 1 when not. Otherwise this process becomes the
instance: it logs to ``<home>/anki_miner_game.log``, starts the app, applies the verb once the
session actor runs, and quits when the window is closed. The OBS password never reaches the log:
nothing here or in the composition logs it, and ``obsws-python``'s own logger, which would, is held
at CRITICAL by the gateway.
"""

# The store reads the process umask once at import, and off Linux it has to set it briefly to read
# it: it must be imported before anything can start a thread (T16 ruling on store.write_text_atomic).
from anki_miner_game import store  # noqa: F401

# isort: split

import logging
import logging.handlers
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Final

from PyQt6.QtWidgets import QApplication

from anki_miner_game import __version__, paths
from anki_miner_game.app import App
from anki_miner_game.gui import cli_verbs

log = logging.getLogger(__name__)

LOG_NAME: Final = "anki_miner_game.log"
LOG_MAX_BYTES: Final = 5 * 1024 * 1024
LOG_BACKUPS: Final = 3
"""The log rotates at 5 MB and keeps three old files: a hooker that is not running retries every
10 s for as long as the game stays armed."""
LOG_FORMAT: Final = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(home: Path, level: int = logging.INFO) -> logging.Handler:
    """Send the app's log to ``<home>/anki_miner_game.log``; ``teardown_logging`` undoes it."""
    home.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        home / LOG_NAME, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    return handler


def teardown_logging(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()


def install_exception_hooks() -> Callable[[], None]:
    """Log an exception nothing caught, on the main thread (a Qt slot) or any other, and carry on.

    PyQt ends the process on an exception escaping a slot unless ``sys.excepthook`` is replaced.
    Returns the function that puts the previous hooks back.
    """
    previous_sys, previous_threading = sys.excepthook, threading.excepthook

    def on_main(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        log.critical("unhandled error", exc_info=(kind, exc, tb))

    def on_thread(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread is not None else "?"
        exc = args.exc_value if args.exc_value is not None else args.exc_type()
        log.critical("unhandled error in thread %s", name, exc_info=(args.exc_type, exc, args.exc_traceback))

    sys.excepthook, threading.excepthook = on_main, on_thread

    def restore() -> None:
        sys.excepthook, threading.excepthook = previous_sys, previous_threading

    return restore


def main(argv: Sequence[str] | None = None) -> int:
    command, qt_args = cli_verbs.parse_verb(sys.argv[1:] if argv is None else argv)
    program = sys.argv[0] if sys.argv else "anki_miner_game"
    qapp = QApplication.instance() or QApplication([program, *qt_args])
    home = paths.home()
    name = cli_verbs.server_name(home)
    answered = cli_verbs.send(name, command)
    if answered is not None:  # another instance of this home runs; it logs, this process does not
        return 0 if answered else 1
    handler = setup_logging(home)
    restore_hooks = install_exception_hooks()
    try:
        log.info("Anki Miner Game %s starting (home %s)", __version__, home)
        app = App(server_name=name)
        app.stopped.connect(qapp.quit)
        if isinstance(qapp, QApplication):
            qapp.setQuitOnLastWindowClosed(False)  # the window hides first; the app quits once stopped
        try:
            app.start()
            if command is not None:
                app.post(command)
            return qapp.exec()
        finally:
            app.close()
    finally:
        log.info("exited")
        restore_hooks()
        teardown_logging(handler)


if __name__ == "__main__":
    raise SystemExit(main())
