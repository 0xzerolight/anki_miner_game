"""CLI verbs and the single-instance guard (spec 16 "Global control")."""

import gc
import socket
import sys
import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QDir
from PyQt6.QtNetwork import QLocalSocket

from anki_miner_game.gui import cli_verbs
from anki_miner_game.gui.cli_verbs import CliServer, parse_verb, send, server_name
from anki_miner_game.models.messages import CommandKind, UserCommand


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["--arm", "steins-gate"], UserCommand(CommandKind.ARM, slug="steins-gate")),
        (["--start"], UserCommand(CommandKind.START)),
        (["--stop"], UserCommand(CommandKind.STOP)),
        (["--toggle"], UserCommand(CommandKind.TOGGLE)),
        ([], None),
    ],
)
def test_each_verb_is_a_user_command(args, command):
    assert parse_verb(args) == (command, [])


def test_arguments_that_are_not_verbs_are_left_for_qt():
    assert parse_verb(["-platform", "offscreen", "--toggle"]) == (
        UserCommand(CommandKind.TOGGLE),
        ["-platform", "offscreen"],
    )


def test_two_verbs_at_once_are_refused():
    with pytest.raises(SystemExit) as exc:
        parse_verb(["--start", "--stop"])
    assert exc.value.code == 2


def test_the_help_names_the_game_by_title_or_slug_and_never_says_armed(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # one help line per option
    with pytest.raises(SystemExit):
        parse_verb(["--help"])
    out = capsys.readouterr().out
    assert "--arm GAME" in out and "get this game ready (its title or slug)" in out
    assert "start recording (once a game is ready)" in out
    assert "armed" not in out


def test_arm_needs_a_game():
    with pytest.raises(SystemExit):
        parse_verb(["--arm"])


@pytest.mark.parametrize(
    "command",
    [UserCommand(CommandKind.ARM, slug="zero-escape"), UserCommand(CommandKind.STOP), None],
)
def test_a_message_decodes_to_what_was_sent(command):
    assert cli_verbs.decode(cli_verbs.encode(command)) == command


@pytest.mark.parametrize(
    "data",
    [b"", b"not json\n", b"[]\n", b'{"verb": "quit"}\n', b'{"verb": "arm"}\n', b'{"verb": "arm", "slug": 3}\n'],
)
def test_anything_else_is_refused(data):
    with pytest.raises(ValueError):
        cli_verbs.decode(data)


def test_the_server_name_follows_the_home_folder(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert server_name(first) == server_name(first)
    assert server_name(first) != server_name(second)
    assert server_name(first).startswith("anki_miner_game-")


# The running instance ------------------------------------------------------------------------------


class Instance:
    """A listening ``CliServer`` that records what reaches it."""

    def __init__(self, name: str) -> None:
        self.commands: list[UserCommand] = []
        self.shows = 0
        self.server = CliServer(name, on_command=self.commands.append, on_show=self._show)

    def _show(self) -> None:
        self.shows += 1


@pytest.fixture
def name(tmp_path) -> str:
    return server_name(tmp_path)


@pytest.fixture
def instance(qapp, name):
    running = Instance(name)
    assert running.server.listen()
    yield running
    running.server.close()


def send_from_another_process(qtbot, name: str, command: UserCommand | None) -> bool | None:
    """``send`` blocks until the answer, which needs this thread's event loop: send from a worker."""
    answers: list[bool | None] = []
    worker = threading.Thread(target=lambda: answers.append(send(name, command)))
    worker.start()
    qtbot.waitUntil(lambda: not worker.is_alive(), timeout=5000)
    return answers[0]


def test_a_verb_reaches_the_running_instance(qtbot, instance, name):
    assert send_from_another_process(qtbot, name, UserCommand(CommandKind.ARM, slug="steins-gate")) is True
    assert instance.commands == [UserCommand(CommandKind.ARM, slug="steins-gate")]


def test_a_second_launch_without_a_verb_shows_the_running_window(qtbot, instance, name):
    assert send_from_another_process(qtbot, name, None) is True
    assert (instance.commands, instance.shows) == ([], 1)


class EagerSocket(QLocalSocket):
    """A socket whose write is done before ``waitForBytesWritten`` runs, as a Windows pipe's can be."""

    def write(self, data: bytes) -> int:
        written = super().write(data)
        self.flush()
        return written


def test_a_write_done_at_once_still_gets_its_answer(qtbot, instance, name, monkeypatch):
    monkeypatch.setattr(cli_verbs, "QLocalSocket", EagerSocket)
    assert send_from_another_process(qtbot, name, UserCommand(CommandKind.START)) is True
    assert instance.commands == [UserCommand(CommandKind.START)]


def test_the_instance_leaves_the_connection_open_for_the_client_to_close(qtbot, instance, name):
    # On Windows a server-side disconnect discards the answer the client has not read yet.
    client = QLocalSocket()
    client.connectToServer(name)
    assert client.waitForConnected(1000)
    client.write(cli_verbs.encode(UserCommand(CommandKind.START)))
    qtbot.waitUntil(client.canReadLine)
    assert bytes(client.readLine().data()).strip() == cli_verbs.OK
    qtbot.wait(200)  # time for a server-side close to arrive
    assert client.state() is QLocalSocket.LocalSocketState.ConnectedState
    client.abort()


def test_a_garbage_collection_before_the_verb_arrives_does_not_lose_it(qtbot, instance, name):
    """The accepted connection outlives a cyclic garbage collection: its ``readyRead`` slot holds the
    socket, and that cycle alone did not keep it (a ``--toggle`` from a desktop shortcut went unanswered)."""
    client = QLocalSocket()
    client.connectToServer(name)
    assert client.waitForConnected(1000)
    qtbot.wait(50)  # the instance accepts the connection
    gc.collect()
    client.write(cli_verbs.encode(UserCommand(CommandKind.TOGGLE)))
    qtbot.waitUntil(client.canReadLine, timeout=2000)
    assert bytes(client.readLine().data()).strip() == cli_verbs.OK
    assert instance.commands == [UserCommand(CommandKind.TOGGLE)]
    client.abort()


def test_nothing_answers_when_no_instance_runs(qapp, name):
    assert send(name, UserCommand(CommandKind.START)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows pipes leave no file behind")
def test_a_socket_left_by_a_crash_does_not_stop_the_next_instance(qapp, name):
    path = Path(QDir.tempPath()) / name
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # the file stays, as after a crash
    try:
        assert send(name, None) is None  # nothing listens behind it
        running = Instance(name)
        assert running.server.listen()
        running.server.close()
    finally:
        path.unlink(missing_ok=True)
