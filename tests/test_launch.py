"""The entry point (spec 16 "Global control", 19 smoke): the log, a whole offscreen launch in a process of
its own, CLI verbs sent by a second process, and the umask read before any thread exists."""

import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QLockFile

from anki_miner_game import launch, paths, store
from anki_miner_game.gui.cli_verbs import server_name
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import AppState
from tests.app_rig import Rig

REPO = Path(__file__).resolve().parents[1]
RUN_TIMEOUT_S = 60


def child_env() -> dict[str, str]:
    """This test's isolated homes and offscreen Qt, with the repo importable (the package is never installed)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO,
        env=child_env(),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        check=False,
    )


def send_verb(qtbot, *args: str) -> int:
    """Run the CLI in a second process while this thread's event loop serves the running instance."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "anki_miner_game.launch", *args],
        cwd=REPO,
        env=child_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        qtbot.waitUntil(lambda: proc.poll() is not None, timeout=RUN_TIMEOUT_S * 1000)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode


def test_the_log_goes_to_the_home_folder(tmp_path):
    handler = launch.setup_logging(tmp_path / "home")
    try:
        logging.getLogger("anki_miner_game.test").info("hello from the test")
    finally:
        launch.teardown_logging(handler)
    text = (tmp_path / "home" / launch.LOG_NAME).read_text(encoding="utf-8")
    assert "hello from the test" in text
    assert handler not in logging.getLogger().handlers


def test_an_unhandled_error_is_logged_instead_of_ending_the_app(tmp_path):
    handler = launch.setup_logging(tmp_path)
    restore = launch.install_exception_hooks()
    try:
        try:
            raise RuntimeError("a slot failed")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        worker = threading.Thread(target=lambda: 1 / 0, name="doomed")
        worker.start()
        worker.join()
    finally:
        restore()
        launch.teardown_logging(handler)
    text = (tmp_path / launch.LOG_NAME).read_text(encoding="utf-8")
    assert "RuntimeError: a slot failed" in text
    assert "ZeroDivisionError" in text and "doomed" in text


SMOKE = textwrap.dedent("""
    import sys

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from anki_miner_game import app as app_mod, launch
    from anki_miner_game.feed import FeedServer
    from tests.session.actor_harness import FakeClock, FakeDiscovery, FakeGateway, FakeObs, FakeProvisioner

    app_mod.FeedServer = lambda _ws, _http: FeedServer(0, 0)  # never a default port in tests
    real_app = launch.App

    def fake_obs_app(**kwargs):
        gateway = FakeGateway(FakeObs(), FakeClock())
        services = app_mod.ObsServices(FakeDiscovery(), gateway, FakeProvisioner(gateway))
        return real_app(obs=lambda _config: services, **kwargs)

    launch.App = fake_obs_app
    qapp = QApplication(sys.argv)

    def close_the_window():
        shown = sorted(w.windowTitle() for w in QApplication.topLevelWidgets() if w.isVisible())
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible() and widget.windowTitle() == "Anki Miner Game":
                print("SHOWN", shown, flush=True)
                widget.close()
                return
        QTimer.singleShot(50, close_the_window)

    QTimer.singleShot(50, close_the_window)
    sys.exit(launch.main([]))
    """)


def test_an_offscreen_launch_writes_the_settings_and_the_log_and_quits_when_closed():
    """The whole wiring in a process of its own: a first launch also opens the setup wizard."""
    done = run_python("-c", SMOKE)
    assert done.returncode == 0, done.stderr
    assert "SHOWN ['Anki Miner Game', 'Anki Miner Game setup']" in done.stdout
    assert store.load_config() == AppConfig()
    log = (paths.home() / launch.LOG_NAME).read_text(encoding="utf-8")
    assert "starting" in log and "stopped" in log


def test_a_verb_from_a_second_process_reaches_the_running_instance(qtbot, tmp_path):
    rig = Rig(qtbot, tmp_path)
    rig.start(name=server_name(paths.home()))
    try:
        assert send_verb(qtbot, "--arm", "steins-gate") == 0
        rig.wait(lambda: rig.state() is AppState.ARMED)
    finally:
        rig.close()
    assert not (paths.home() / launch.LOG_NAME).exists()  # the sender logs nothing into the running app's log


def test_a_verb_the_running_instance_refuses_fails_the_command(qtbot, tmp_path):
    rig = Rig(qtbot, tmp_path)
    rig.start(name=server_name(paths.home()))
    try:
        assert send_verb(qtbot, "--arm", "") == 1
        assert send_verb(qtbot, "--start") == 0  # the instance still answers
    finally:
        rig.close()


HOLD = textwrap.dedent("""
    import os
    import sys
    from pathlib import Path

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from anki_miner_game import app as app_mod, launch
    from anki_miner_game.feed import FeedServer
    from tests.session.actor_harness import FakeClock, FakeDiscovery, FakeGateway, FakeObs, FakeProvisioner

    app_mod.FeedServer = lambda _ws, _http: FeedServer(0, 0)  # never a default port in tests
    real_app = launch.App

    def fake_obs_app(**kwargs):
        gateway = FakeGateway(FakeObs(), FakeClock())
        services = app_mod.ObsServices(FakeDiscovery(), gateway, FakeProvisioner(gateway))
        return real_app(obs=lambda _config: services, **kwargs)

    launch.App = fake_obs_app
    qapp = QApplication(sys.argv)

    def say_shown():
        if any(w.isVisible() and w.windowTitle() == "Anki Miner Game" for w in QApplication.topLevelWidgets()):
            Path(os.environ["SHOWN_MARKER"]).touch()
            return
        QTimer.singleShot(50, say_shown)

    QTimer.singleShot(50, say_shown)
    sys.exit(launch.main([]))
    """)
"""An instance that stays up until it is killed; it touches ``$SHOWN_MARKER`` once its window shows."""


def test_a_launch_while_another_instance_starts_waits_for_its_answer(qtbot, tmp_path):
    """B2-01: the lock is held (an instance is starting) and nothing listens yet. The launch neither
    becomes a second instance nor gives up: once the instance listens, it gets its answer."""
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    held = QLockFile(str(home / launch.INSTANCE_LOCK_NAME))
    assert held.tryLock(0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "anki_miner_game.launch"],
        cwd=REPO,
        env=child_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        qtbot.wait(3000)
        assert proc.poll() is None  # still waiting: not started, not given up
        rig = Rig(qtbot, tmp_path)
        rig.start(name=server_name(home))
        try:
            qtbot.waitUntil(lambda: proc.poll() is not None, timeout=RUN_TIMEOUT_S * 1000)
        finally:
            rig.close()
        assert proc.returncode == 0
        assert not (home / launch.LOG_NAME).exists()  # it never became an instance (an instance logs)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        held.unlock()


def test_a_lock_left_by_a_killed_instance_does_not_stop_the_next_launch(tmp_path):
    """Review Focus 5: after a crash or power loss the lock file is there but its holder is gone."""
    marker = tmp_path / "shown"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD],
        cwd=REPO,
        env={**child_env(), "SHOWN_MARKER": str(marker)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not marker.exists():
            assert holder.poll() is None, "the first instance ended before its window showed"
            assert time.monotonic() < deadline, "the first instance never showed its window"
            time.sleep(0.05)
    finally:
        holder.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: nothing is cleaned up
        holder.wait()
    assert (paths.home() / launch.INSTANCE_LOCK_NAME).exists()

    done = run_python("-c", SMOKE)  # times out (60 s) if the stale lock made it wait (130 s)

    assert done.returncode == 0, done.stderr
    assert "SHOWN ['Anki Miner Game'" in done.stdout


BOOM = textwrap.dedent("""
    import sys

    from PyQt6.QtWidgets import QApplication

    from anki_miner_game import launch

    def boom(**_kwargs):
        raise RuntimeError("start-up boom")

    launch.App = boom
    qapp = QApplication(sys.argv)
    sys.exit(launch.main([]))
    """)


def test_a_start_up_failure_reaches_the_log():
    """B2-05: the exception used to unwind past the handler's removal: the log ended "starting ... exited"."""
    done = run_python("-c", BOOM)

    assert done.returncode != 0
    log = (paths.home() / launch.LOG_NAME).read_text(encoding="utf-8")
    assert "start-up failed" in log
    assert "RuntimeError: start-up boom" in log
    assert not (paths.home() / launch.INSTANCE_LOCK_NAME).exists()  # released on the way out


def test_two_verbs_at_once_are_a_usage_error():
    done = run_python("-m", "anki_miner_game.launch", "--start", "--stop")
    assert done.returncode == 2
    assert "not allowed with argument" in done.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the umask is a POSIX notion")
def test_the_store_reads_the_umask_before_any_thread_starts():
    """``store`` reads the umask at import, briefly changing it off Linux: nothing may run beside it."""
    done = run_python(
        "-c",
        "import sys, threading; import anki_miner_game.launch; "
        "assert 'anki_miner_game.store' in sys.modules; assert threading.active_count() == 1, threading.enumerate()",
    )
    assert done.returncode == 0, done.stderr
