"""ClipboardSource: lines copied to the system clipboard (spec 8.1)."""

import itertools
import threading

import pytest
from PyQt6.QtCore import QMimeData

from anki_miner_game.models.messages import SourceStatus
from anki_miner_game.text.sources.clipboard_source import CLIPBOARD_SOURCE_ID, ClipboardSource
from tests.test_contracts import _assert_conforms


class Recorder:
    """Sink and status listener in one: records what arrives and on which thread."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, float, str]] = []
        self.statuses: list[tuple[str, SourceStatus]] = []
        self.threads: set[threading.Thread] = set()

    def sink(self, raw: str, t_mono: float, source_id: str) -> None:
        self.threads.add(threading.current_thread())
        self.lines.append((raw, t_mono, source_id))

    def listener(self, source_id: str, status: SourceStatus) -> None:
        self.threads.add(threading.current_thread())
        self.statuses.append((source_id, status))


@pytest.fixture
def clipboard(qapp):
    board = qapp.clipboard()
    board.clear()
    yield board
    board.clear()


@pytest.fixture
def ticks():
    """An injected ``now`` returning 10.0, 11.0, ... and counting its calls."""
    counter = itertools.count(10)

    class Now:
        calls = 0

        def __call__(self) -> float:
            Now.calls += 1
            return float(next(counter))

    return Now()


@pytest.fixture
def source(ticks):
    src = ClipboardSource(now=ticks)
    yield src
    src.stop()


def test_conforms_to_the_text_source_protocol():
    from anki_miner_game.interfaces.text_source import TextSource

    _assert_conforms(TextSource, ClipboardSource)
    assert ClipboardSource().id == CLIPBOARD_SOURCE_ID == "clipboard"
    assert ClipboardSource(source_id="copy").id == "copy"


def test_copied_text_reaches_the_sink_stamped_at_the_signal(clipboard, source, ticks):
    rec = Recorder()
    source.start(rec.sink)
    clipboard.setText("お前は誰だ？")
    clipboard.setText("行くぞ\n二行目")
    assert rec.lines == [("お前は誰だ？", 10.0, "clipboard"), ("行くぞ\n二行目", 11.0, "clipboard")]
    assert ticks.calls == 2


def test_non_text_contents_are_ignored(clipboard, source):
    rec = Recorder()
    source.start(rec.sink)
    image = QMimeData()
    image.setData("image/png", b"\x89PNG")
    clipboard.setMimeData(image)
    clipboard.clear()
    assert rec.lines == []


def test_changes_made_by_this_app_are_ignored(clipboard, source, monkeypatch):
    rec = Recorder()
    source.start(rec.sink)
    monkeypatch.setattr(clipboard, "ownsClipboard", lambda: True)
    clipboard.setText("自分のコピー")
    monkeypatch.undo()
    clipboard.setText("ゲームの台詞")
    assert [line for line, _, _ in rec.lines] == ["ゲームの台詞"]


def test_status_transitions_reach_the_listener_on_the_main_thread(clipboard, source):
    rec = Recorder()
    source.set_status_listener(rec.listener)
    assert source.status is SourceStatus.DISCONNECTED
    source.start(rec.sink)
    assert source.status is SourceStatus.CONNECTED
    clipboard.setText("一")
    assert source.status is SourceStatus.RECEIVING
    clipboard.setText("二")
    source.stop()
    assert source.status is SourceStatus.DISCONNECTED
    assert rec.statuses == [
        ("clipboard", SourceStatus.CONNECTED),
        ("clipboard", SourceStatus.RECEIVING),
        ("clipboard", SourceStatus.DISCONNECTED),
    ]
    assert rec.threads == {threading.main_thread()}


def test_the_listener_sees_the_new_status_already_reported(clipboard, source):
    seen: list[tuple[SourceStatus, SourceStatus]] = []
    source.set_status_listener(lambda _id, status: seen.append((status, source.status)))
    source.start(lambda raw, t, sid: None)
    clipboard.setText("一")
    source.stop()
    assert seen and all(status is current for status, current in seen)


def test_a_later_listener_replaces_the_earlier_one(source):
    first, second = Recorder(), Recorder()
    source.set_status_listener(first.listener)
    source.set_status_listener(second.listener)
    source.start(lambda raw, t, sid: None)
    assert first.statuses == []
    assert second.statuses == [("clipboard", SourceStatus.CONNECTED)]


def test_stop_disconnects_and_a_restart_listens_again(clipboard, source):
    rec = Recorder()
    source.start(rec.sink)
    source.stop()
    clipboard.setText("聞こえない")
    assert rec.lines == []
    source.start(rec.sink)
    clipboard.setText("聞こえる")
    assert [line for line, _, _ in rec.lines] == ["聞こえる"]


def test_stop_without_start_reports_nothing(source):
    rec = Recorder()
    source.set_status_listener(rec.listener)
    source.stop()
    assert rec.statuses == []


def test_starting_twice_is_an_error(source):
    source.start(lambda raw, t, sid: None)
    with pytest.raises(RuntimeError):
        source.start(lambda raw, t, sid: None)


def test_a_failing_sink_or_listener_is_logged_not_raised(clipboard, source, caplog):
    def boom(*_args: object) -> None:
        raise ValueError("boom")

    source.set_status_listener(boom)
    source.start(boom)
    clipboard.setText("一")
    assert source.status is SourceStatus.RECEIVING
    assert sum("failed" in r.getMessage() for r in caplog.records) == 3
