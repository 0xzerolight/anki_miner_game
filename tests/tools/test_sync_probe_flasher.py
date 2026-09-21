"""tools/sync_probe/flasher.py: schedule, line texts, the hooker-style server and the Qt window."""

import json
import time

import pytest
from websockets.sync.client import connect

from tools.sync_probe import flasher as fl

# --- schedule and texts ---------------------------------------------------------------------


def test_the_schedule_is_deterministic_per_seed_and_jittered_within_bounds():
    schedule = fl.flash_schedule(count=5, interval_s=2.0, initial_delay_s=1.0, jitter_s=0.1, seed=7)
    assert len(schedule) == 5
    assert schedule == fl.flash_schedule(count=5, interval_s=2.0, initial_delay_s=1.0, jitter_s=0.1, seed=7)
    assert schedule != fl.flash_schedule(count=5, interval_s=2.0, initial_delay_s=1.0, jitter_s=0.1, seed=8)
    for i, t in enumerate(schedule):
        assert 1.0 + 2.0 * i <= t < 1.0 + 2.0 * i + 0.1


def test_the_schedule_without_jitter_is_exact():
    assert fl.flash_schedule(count=3, interval_s=2.5, initial_delay_s=1.0, jitter_s=0.0, seed=1) == [1.0, 3.5, 6.0]


def test_the_schedule_refuses_flashes_too_close_to_tell_apart():
    with pytest.raises(ValueError, match="apart"):
        fl.flash_schedule(count=3, interval_s=1.0, initial_delay_s=1.0, jitter_s=0.5, seed=1)


def test_line_texts_are_distinct_have_letters_and_never_extend_one_another():
    # The app drops letterless lines and duplicates, and may merge a line that extends the last.
    texts = [fl.line_text(i) for i in range(1000)]
    assert len(set(texts)) == len(texts)
    assert all(any(ch.isalpha() for ch in text) for text in texts)
    assert not any(a != b and b.startswith(a) for a in texts for b in texts[:100])
    assert fl.line_text(3) == "sync probe flash 003"


# --- the hooker-style websocket server -----------------------------------------------------


def _wait(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_server_binds_loopback_and_broadcasts_to_every_client():
    server = fl.HookerServer(port=0)
    server.start()
    try:
        assert server.host == "127.0.0.1" and server.port > 0
        assert server.broadcast("nobody listens") == 0
        url = f"ws://127.0.0.1:{server.port}"
        with connect(url, proxy=None, open_timeout=5) as a, connect(url, proxy=None, open_timeout=5) as b:
            _wait(lambda: server.client_count() == 2)
            assert server.broadcast("sync probe flash 000") == 2
            assert a.recv(timeout=5) == "sync probe flash 000"
            assert b.recv(timeout=5) == "sync probe flash 000"
    finally:
        server.stop()


def test_a_port_in_use_is_reported():
    first = fl.HookerServer(port=0)
    first.start()
    try:
        with pytest.raises(OSError):
            fl.HookerServer(port=first.port).start()
    finally:
        first.stop()


# --- the window ------------------------------------------------------------------------------


def test_the_window_turns_white_at_each_flash_and_black_after_its_frames(qtbot):
    seen = []
    window = fl.FlasherWindow(
        [0.05, 0.25], fps=30, frames=3, on_flash=lambda i, t: seen.append((i, t, window.is_white()))
    )
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.finished, timeout=5000):
        t0 = window.start()
    assert [(i, white) for i, _, white in seen] == [(0, True), (1, True)]
    assert seen[0][1] >= t0 + 0.04
    assert seen[1][1] >= t0 + 0.24
    assert not window.is_white()


def test_a_flash_lasts_the_given_number_of_frames(qtbot):
    changes = []
    window = fl.FlasherWindow([0.0], fps=10, frames=3, on_flash=lambda i, t: None, tail_s=0.1)
    window.colour_changed.connect(lambda white: changes.append((white, time.monotonic())))
    qtbot.addWidget(window)
    window.show()
    with qtbot.waitSignal(window.finished, timeout=5000):
        window.start()
    assert [white for white, _ in changes] == [True, False]
    assert 0.295 <= changes[1][1] - changes[0][1] < 1.0  # three frames at 10 fps; never shorter


def test_the_probe_logs_each_flash_and_serves_its_line(qtbot, tmp_path):
    server = fl.HookerServer(port=0)
    server.start()
    log_path = tmp_path / "flasher.jsonl"
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            probe = fl.Probe(server, log)
            window = fl.FlasherWindow([0.2, 0.4], fps=30, frames=3, on_flash=probe.on_flash)
            qtbot.addWidget(window)
            with connect(f"ws://127.0.0.1:{server.port}", proxy=None, open_timeout=5) as client:
                _wait(lambda: server.client_count() == 1)
                probe.log_start(window, schedule=[0.2, 0.4], fps=30, frames=3)
                window.show()
                with qtbot.waitSignal(window.finished, timeout=5000):
                    window.start()
                received = [client.recv(timeout=5), client.recv(timeout=5)]
    finally:
        server.stop()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["kind"] == "start" and records[0]["port"] == server.port
    flashes = [r for r in records if r["kind"] == "flash"]
    assert [(r["index"], r["text"], r["clients"]) for r in flashes] == [
        (0, "sync probe flash 000", 1),
        (1, "sync probe flash 001", 1),
    ]
    assert received == ["sync probe flash 000", "sync probe flash 001"]
    assert flashes[0]["t_mono"] < flashes[1]["t_mono"]


def test_cli_rejects_a_malformed_size():
    with pytest.raises(SystemExit) as exc:
        fl.main(["--log", "x.jsonl", "--size", "640by360"])
    assert exc.value.code == 2
