"""The frozen bundle's self-check (spec 19, ``scripts/bundle_smoke.sh``): the add-on modules are absent,
the started text feed serves its page, one real HTTPS GET goes through the add-on bootstrap's
transport, and the result reaches the log before the app quits."""

import asyncio
import contextlib
import os
import ssl
import subprocess
import sys
import textwrap
import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from anki_miner_game import launch, paths, store
from anki_miner_game.addons import bootstrap
from anki_miner_game.feed import FEED_HOST, FeedServer
from anki_miner_game.models.config import AppConfig
from anki_miner_game.runtime.bundle_smoke import (
    ABSENT_MODULES,
    FAIL_MARKER,
    HTTPS_URL,
    PASS_MARKER,
    SMOKE_ENV,
    BundleSmoke,
    SmokeCheckError,
    check_absent_modules,
    check_feed_page,
    check_https,
    requested,
)

REPO = Path(__file__).resolve().parents[2]
RUN_TIMEOUT_S = 60


def transport_answering(status: int, seen: list[str] | None = None) -> bootstrap.Transport:
    @contextlib.contextmanager
    def transport(url: str) -> Iterator[bootstrap.Reply]:
        if seen is not None:
            seen.append(url)
        yield bootstrap.Reply(status=status, location=None, length=0, chunks=iter(()))

    return transport


def transport_raising(exc: Exception) -> bootstrap.Transport:
    @contextlib.contextmanager
    def transport(url: str) -> Iterator[bootstrap.Reply]:
        raise exc
        yield  # pragma: no cover - makes this a generator

    return transport


# --- the switch ---------------------------------------------------------------------------------


def test_the_smoke_runs_only_when_the_variable_is_one():
    assert requested({SMOKE_ENV: "1"})
    assert not requested({SMOKE_ENV: "0"})
    assert not requested({})


# --- absent modules -----------------------------------------------------------------------------


def test_the_absent_modules_are_the_add_ons_own():
    assert set(ABSENT_MODULES) == {"onnxruntime", "numpy", "av", "owocr"}


def test_a_bundled_add_on_module_fails_the_check():
    def find_spec(name: str) -> object | None:
        return object() if name in ("numpy", "owocr") else None

    with pytest.raises(SmokeCheckError, match="numpy, owocr"):
        check_absent_modules(find_spec)


def test_no_add_on_module_passes_the_check():
    check_absent_modules(lambda _name: None)


# --- the feed page ------------------------------------------------------------------------------


async def test_the_started_feed_serves_its_page_with_the_websocket_port():
    feed = FeedServer(ws_port=0, http_port=0)
    await feed.start()
    try:
        await asyncio.to_thread(check_feed_page, feed)
    finally:
        await feed.stop()


def test_no_running_feed_fails_the_check():
    with pytest.raises(SmokeCheckError, match="not running"):
        check_feed_page(None)


class _WrongPage(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html>__FEED_WS_PORT__</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


def test_a_page_without_the_websocket_port_fails_the_check():
    server = ThreadingHTTPServer((FEED_HOST, 0), _WrongPage)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        feed = SimpleNamespace(http_port=server.server_address[1], ws_port=45678)
        with pytest.raises(SmokeCheckError, match="websocket port"):
            check_feed_page(feed)  # type: ignore[arg-type]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# --- HTTPS --------------------------------------------------------------------------------------


def test_the_https_get_asks_the_pinned_uv_host_once():
    seen: list[str] = []
    check_https(transport_answering(302, seen))
    assert seen == [HTTPS_URL]
    parts = urlsplit(HTTPS_URL)
    assert parts.scheme == "https"
    assert parts.hostname in bootstrap.ALLOWED_HOSTS


@pytest.mark.parametrize("status", [200, 302])
def test_a_download_or_its_redirect_passes(status):
    check_https(transport_answering(status))


def test_an_error_status_fails():
    with pytest.raises(SmokeCheckError, match="HTTP 503"):
        check_https(transport_answering(503))


def test_a_failed_certificate_check_fails_with_its_reason():
    refused = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    with pytest.raises(urllib.error.URLError, match="certificate verify failed"):
        check_https(transport_raising(refused))


def test_the_https_check_uses_the_bootstraps_transport_by_default(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(bootstrap, "urllib_transport", transport_answering(302, seen))
    check_https()
    assert seen == [HTTPS_URL]


# --- one run ------------------------------------------------------------------------------------


class Quit:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


async def test_a_clean_run_logs_the_pass_marker_and_quits(caplog):
    feed = FeedServer(ws_port=0, http_port=0)
    await feed.start()
    quit_ = Quit()
    run = BundleSmoke(lambda: feed, quit_, transport=transport_answering(302), find_spec=lambda _name: None)
    try:
        with caplog.at_level("INFO", logger="anki_miner_game.runtime.bundle_smoke"):
            await asyncio.to_thread(run.run)
    finally:
        await feed.stop()
    assert run.exit_code == 0
    assert quit_.calls == 1
    assert PASS_MARKER in caplog.text
    assert FAIL_MARKER not in caplog.text


def test_every_failure_is_logged_and_the_app_still_quits(caplog):
    quit_ = Quit()
    run = BundleSmoke(
        lambda: None,
        quit_,
        transport=transport_raising(OSError("network is unreachable")),
        find_spec=lambda name: object() if name == "av" else None,
    )
    with caplog.at_level("INFO", logger="anki_miner_game.runtime.bundle_smoke"):
        run.run()
    assert run.exit_code == 1
    assert quit_.calls == 1
    assert PASS_MARKER not in caplog.text
    failures = [r.getMessage() for r in caplog.records if FAIL_MARKER in r.getMessage()]
    assert len(failures) == 3
    assert "modules" in failures[0] and "av" in failures[0]
    assert "feed" in failures[1] and "not running" in failures[1]
    assert "https" in failures[2] and "network is unreachable" in failures[2]


def test_the_exit_code_is_unknown_before_the_run():
    assert BundleSmoke(lambda: None, Quit()).exit_code is None


# --- the whole launch ---------------------------------------------------------------------------

LAUNCH = textwrap.dedent("""
    import contextlib
    import os
    import sys

    from PyQt6.QtWidgets import QApplication

    from anki_miner_game import app as app_mod, launch
    from anki_miner_game.addons import bootstrap
    from anki_miner_game.feed import FeedServer
    from anki_miner_game.runtime import ca_bundle
    from tests.session.actor_harness import FakeClock, FakeDiscovery, FakeGateway, FakeObs, FakeProvisioner

    app_mod.FeedServer = lambda _ws, _http: FeedServer(0, 0)  # never a default port in tests
    real_app = launch.App

    def fake_obs_app(**kwargs):
        gateway = FakeGateway(FakeObs(), FakeClock())
        services = app_mod.ObsServices(FakeDiscovery(), gateway, FakeProvisioner(gateway))
        return real_app(obs=lambda _config: services, **kwargs)

    launch.App = fake_obs_app
    order = []
    real_ca = ca_bundle.use_distro_ca_bundle

    def ca_first(*args, **kwargs):
        order.append("ca")
        return real_ca(*args, **kwargs)

    ca_bundle.use_distro_ca_bundle = ca_first

    @contextlib.contextmanager
    def fake_transport(url):
        order.append("https")
        failing = os.environ["SMOKE_TEST_HTTPS"] == "fail" or order != ["ca", "https"]
        if failing:
            raise OSError("no route to github.com")
        yield bootstrap.Reply(status=302, location="https://release-assets.githubusercontent.com/x", length=0,
                              chunks=iter(()))

    bootstrap.urllib_transport = fake_transport
    qapp = QApplication(sys.argv)
    sys.exit(launch.main([]))
    """)


def run_launch(https: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["QT_QPA_PLATFORM"] = "offscreen"
    env[SMOKE_ENV] = "1"
    env["SMOKE_TEST_HTTPS"] = https
    return subprocess.run(
        [sys.executable, "-c", LAUNCH],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
        check=False,
    )


def test_a_smoke_launch_checks_after_the_start_and_quits_on_its_own():
    done = run_launch("pass")
    assert done.returncode == 0, done.stdout + done.stderr
    assert PASS_MARKER in done.stdout
    assert store.load_config() == AppConfig()
    log = (paths.home() / launch.LOG_NAME).read_text(encoding="utf-8")
    assert PASS_MARKER in log and "stopped" in log


def test_a_failed_smoke_launch_exits_nonzero():
    done = run_launch("fail")
    assert done.returncode == 1, done.stdout + done.stderr
    log = (paths.home() / launch.LOG_NAME).read_text(encoding="utf-8")
    assert FAIL_MARKER in log and "no route to github.com" in log
    assert PASS_MARKER not in log
