"""CLI verbs and the single-instance guard (spec 16 "Global control").

``anki_miner_game --arm <game> | --start | --stop | --toggle`` sends its verb to the running
instance and exits; users bind that command in their desktop's shortcut settings, which works on
Wayland, where no application can grab a global key. A launch without a verb while an instance runs
asks it to show its window instead of starting a second one; on Windows the launch first lets it
take the foreground (``allow_foreground``). ``<game>`` is the game's title or slug; the app maps a
title to its game.

The running instance listens on a ``QLocalServer`` whose name is derived from the app's home folder,
so two homes (tests, a second user profile) never reach each other, and whose socket only this user
may open. One message per connection: a JSON object on one line, ``{"verb": "arm", "slug": "..."}``,
answered with ``ok`` or ``error``; the client then closes the connection.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from PyQt6.QtCore import QObject
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from anki_miner_game.models.messages import CommandKind, UserCommand

log = logging.getLogger(__name__)

SERVER_PREFIX: Final = "anki_miner_game-"
SHOW: Final = "show"
"""The verb of a launch without one: show the running instance's window."""
VERBS: Final = frozenset({CommandKind.ARM, CommandKind.START, CommandKind.STOP, CommandKind.TOGGLE})
MAX_MESSAGE_BYTES: Final = 4096
SEND_TIMEOUT_MS: Final = 3000
"""How long ``send`` waits for the running instance to connect, read and answer, each."""
OK: Final = b"ok"
ERROR: Final = b"error"
ASFW_ANY: Final = 0xFFFFFFFF
"""``AllowSetForegroundWindow``'s "any process": this end does not know the instance's process id."""


def parse_verb(args: Sequence[str]) -> tuple[UserCommand | None, list[str]]:
    """The verb in ``args`` (without the program name) and the arguments left for Qt.

    Two verbs, or ``--arm`` without a game, exit with argparse's usage error (code 2).
    """
    parser = argparse.ArgumentParser(prog="anki_miner_game", description="Records game sessions for Anki Miner.")
    verbs = parser.add_mutually_exclusive_group()
    verbs.add_argument("--arm", metavar="GAME", help="get this game ready (its title or slug)")
    verbs.add_argument("--start", action="store_true", help="start recording (once a game is ready)")
    verbs.add_argument("--stop", action="store_true", help="stop recording")
    verbs.add_argument("--toggle", action="store_true", help="start or stop recording")
    known, rest = parser.parse_known_args(list(args))
    if known.arm is not None:
        return UserCommand(CommandKind.ARM, slug=known.arm), rest
    for kind in (CommandKind.START, CommandKind.STOP, CommandKind.TOGGLE):
        if getattr(known, kind.value):
            return UserCommand(kind), rest
    return None, rest


def server_name(home: Path) -> str:
    """The local server name of the instance that uses ``home``."""
    key = os.path.normcase(str(home.expanduser().resolve()))
    return SERVER_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def encode(command: UserCommand | None) -> bytes:
    """``None`` is ``show``."""
    message: dict[str, str] = {"verb": SHOW if command is None else command.kind.value}
    if command is not None and command.kind is CommandKind.ARM and command.slug is not None:
        message["slug"] = command.slug
    return json.dumps(message).encode("utf-8") + b"\n"


def decode(data: bytes) -> UserCommand | None:
    """The command in one message; ``None`` for ``show``. Raises ``ValueError`` for anything else."""
    message = json.loads(data.decode("utf-8"))  # JSONDecodeError and UnicodeDecodeError are ValueErrors
    if not isinstance(message, dict):
        raise ValueError("a message is a JSON object")
    verb = message.get("verb")
    if verb == SHOW:
        return None
    if verb not in VERBS:
        raise ValueError(f"unknown verb {verb!r}")
    kind = CommandKind(verb)
    if kind is not CommandKind.ARM:
        return UserCommand(kind)
    slug = message.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ValueError("arm needs a game")
    return UserCommand(kind, slug=slug)


def send(name: str, command: UserCommand | None, *, timeout_ms: int = SEND_TIMEOUT_MS) -> bool | None:
    """Send ``command`` (``None``: show the window) to the instance listening on ``name``.

    ``None`` when no instance listens; otherwise whether it accepted the message. Blocks; needs no
    event loop.
    """
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return None
    if command is None:
        allow_foreground()  # before the instance reads ``show``
    try:
        sock.write(encode(command))
        # False also when the write is done already, as a Windows pipe's can be before this runs.
        if not sock.waitForBytesWritten(timeout_ms) and sock.bytesToWrite() > 0:
            return False
        while not sock.canReadLine():
            if not sock.waitForReadyRead(timeout_ms) and not sock.canReadLine():
                return False
        return bytes(sock.readLine().data()).strip() == OK
    finally:
        sock.abort()


def allow_foreground() -> None:
    """Windows: let the running instance bring its window to the front.

    Windows gives the foreground only to a process that may take it, such as this launch from the
    Start menu or a shortcut, and not to the instance running in the background: its
    ``activateWindow`` only flashed the taskbar button. The grant lasts until the next input. A launch
    that may not take the foreground itself cannot grant it; the call then does nothing. Off Windows
    there is nothing to do.
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow(ASFW_ANY)


class CliServer(QObject):
    """The running instance's end: lives on the Qt main thread, where its callbacks run.

    ``on_command`` gets each verb as a ``UserCommand`` (the composition posts it to the session
    actor); ``on_show`` is called for a launch without a verb.
    """

    def __init__(
        self,
        name: str,
        *,
        on_command: Callable[[UserCommand], None],
        on_show: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._on_command = on_command
        self._on_show = on_show
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._clients: set[QLocalSocket] = set()
        """Accepted connections until they close. Held here because the socket and its ``readyRead``
        slot only refer to each other, and a garbage collection would end that cycle before the verb
        is read."""

    def listen(self) -> bool:
        """Listen, replacing a socket file a crashed instance left; ``False`` (logged) when it cannot.

        Call it only after ``send`` found no instance: on POSIX a running instance's socket would be
        removed too.
        """
        QLocalServer.removeServer(self._name)
        if self._server.listen(self._name):
            return True
        log.warning("CLI verbs are off: cannot listen on %s (%s)", self._name, self._server.errorString())
        return False

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                return
            self._clients.add(sock)
            sock.disconnected.connect(lambda sock=sock: self._forget(sock))
            sock.readyRead.connect(lambda sock=sock: self._read(sock))

    def _forget(self, sock: QLocalSocket) -> None:
        self._clients.discard(sock)
        sock.deleteLater()

    def _read(self, sock: QLocalSocket) -> None:
        if not sock.canReadLine():
            if sock.bytesAvailable() > MAX_MESSAGE_BYTES:
                log.warning("CLI verbs: dropped a message over %d bytes", MAX_MESSAGE_BYTES)
                sock.abort()
            return
        data = bytes(sock.readLine().data())
        try:
            command = decode(data)
        except ValueError as exc:
            log.warning("CLI verbs: refused a message: %s", exc)
            answer = ERROR
        else:
            log.info("CLI verb: %s", "show" if command is None else command.kind.value)
            if command is None:
                self._on_show()
            else:
                self._on_command(command)
            answer = OK
        # The client closes once it has read this: on Windows a server-side disconnect
        # (DisconnectNamedPipe) discards an answer the client has not read yet.
        sock.write(answer + b"\n")
