"""The frozen bundle's self-check (spec 19), run by ``scripts/bundle_smoke.sh``.

With ``ANKI_MINER_GAME_SMOKE=1`` the app starts as usual (offscreen, in the isolated home the script
sets up) and, once started, checks from inside the bundle what only the frozen build can show:
onnxruntime, numpy, PyAV and owocr cannot be imported (the spec ``excludes`` them); the text feed
it started serves ``page.html`` with the websocket port written in; and one real HTTPS GET through
the add-on bootstrap's transport verifies a certificate with the bundle's OpenSSL (the frozen Linux
build may need ``runtime.ca_bundle``). Each result goes to the log, and to stdout when there is
one, as a ``BUNDLED_SMOKE_PASS`` or ``BUNDLED_SMOKE_FAIL`` line; then the app quits, and the launch
exits 1 when anything failed. The script checks ``config.json``, the log and the markers.
"""

import http.client
import importlib.util
import logging
import os
import ssl
from collections.abc import Callable, Mapping
from typing import Final

from anki_miner_game.addons import bootstrap
from anki_miner_game.feed import FEED_HOST, FeedServer
from anki_miner_game.feed.http_server import WS_PORT_PLACEHOLDER
from anki_miner_game.runtime.ca_bundle import CERT_FILE_ENV

log = logging.getLogger(__name__)

SMOKE_ENV: Final = "ANKI_MINER_GAME_SMOKE"
PASS_MARKER: Final = "BUNDLED_SMOKE_PASS"
FAIL_MARKER: Final = "BUNDLED_SMOKE_FAIL"

ABSENT_MODULES: Final = ("onnxruntime", "numpy", "av", "owocr")
"""The add-ons' own packages; ``anki_miner_game.spec`` excludes the same names."""

HTTPS_URL: Final = bootstrap.PINS[("linux", "x86_64")].url
"""The first request ``ensure_uv`` makes; ``github.com`` answers it with a redirect and no body."""
HTTPS_OK: Final = frozenset({200, 301, 302, 303, 307, 308})

PAGE_CONTENT_TYPE: Final = "text/html; charset=utf-8"
PAGE_TIMEOUT_S: Final = 10.0

FindSpec = Callable[[str], object | None]


class SmokeCheckError(Exception):
    """A check failed; the message says what the bundle got wrong."""


def requested(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this launch is the bundle smoke: ``ANKI_MINER_GAME_SMOKE=1``."""
    return (os.environ if environ is None else environ).get(SMOKE_ENV) == "1"


def check_absent_modules(find_spec: FindSpec = importlib.util.find_spec) -> None:
    present = [name for name in ABSENT_MODULES if find_spec(name) is not None]
    if present:
        raise SmokeCheckError(f"the bundle can import {', '.join(present)}")


def check_feed_page(feed: FeedServer | None, timeout_s: float = PAGE_TIMEOUT_S) -> None:
    """GET ``/`` from the running feed straight over loopback (no proxy) and check the page."""
    if feed is None:
        raise SmokeCheckError("the text feed is not running")
    conn = http.client.HTTPConnection(FEED_HOST, feed.http_port, timeout=timeout_s)
    try:
        conn.request("GET", "/")
        response = conn.getresponse()
        content_type = response.getheader("Content-Type")
        page = response.read().decode("utf-8")
    finally:
        conn.close()
    if response.status != 200:
        raise SmokeCheckError(f"the text feed page answered HTTP {response.status}")
    if content_type != PAGE_CONTENT_TYPE:
        raise SmokeCheckError(f"the text feed page came as {content_type!r}")
    if WS_PORT_PLACEHOLDER in page or str(feed.ws_port) not in page:
        raise SmokeCheckError(f"the text feed page does not carry the websocket port {feed.ws_port}")


def check_https(transport: bootstrap.Transport | None = None, url: str = HTTPS_URL) -> None:
    """One GET of ``url`` through ``transport`` (default: the bootstrap's own), redirect not followed."""
    get = bootstrap.urllib_transport if transport is None else transport
    log.info("CA certificates: %s; %s=%s", ssl.get_default_verify_paths(), CERT_FILE_ENV, os.environ.get(CERT_FILE_ENV))
    with get(url) as reply:
        status = reply.status
    if status not in HTTPS_OK:
        raise SmokeCheckError(f"GET {url} answered HTTP {status}")
    log.info("GET %s answered HTTP %s", url, status)


class BundleSmoke:
    """The checks, one after the other, then the quit. ``run`` on the Qt main thread once the app started.

    ``feed`` returns the app's running feed; ``quit_app`` starts the app's quit. ``exit_code`` is
    ``None`` until ``run`` ends, then 0 when every check passed and 1 otherwise.
    """

    def __init__(
        self,
        feed: Callable[[], FeedServer | None],
        quit_app: Callable[[], None],
        *,
        transport: bootstrap.Transport | None = None,
        find_spec: FindSpec = importlib.util.find_spec,
    ) -> None:
        self._feed = feed
        self._quit_app = quit_app
        self._transport = transport
        self._find_spec = find_spec
        self.exit_code: int | None = None

    def run(self) -> None:
        checks: tuple[tuple[str, Callable[[], None]], ...] = (
            ("modules", lambda: check_absent_modules(self._find_spec)),
            ("feed", lambda: check_feed_page(self._feed())),
            ("https", lambda: check_https(self._transport)),
        )
        failures: list[str] = []
        for name, check in checks:
            try:
                check()
            except Exception as exc:  # every failure is reported, and the app still quits
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        for failure in failures:
            _report(logging.ERROR, f"{FAIL_MARKER}: {failure}")
        if not failures:
            _report(logging.INFO, f"{PASS_MARKER}: {', '.join(name for name, _check in checks)}")
        self.exit_code = 1 if failures else 0
        self._quit_app()


def _report(level: int, line: str) -> None:
    log.log(level, "%s", line)
    print(line, flush=True)  # a no-op without a stdout (a windowed Windows build started from Explorer)
