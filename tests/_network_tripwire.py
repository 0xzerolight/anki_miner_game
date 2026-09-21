"""Socket-level network tripwire for the test suite (wired up in ``conftest.py``).

WHY: the global constraints (master plan, spec 18.1) require tests to reach no
network beyond loopback — every fake server (``FakeObsServer``,
``FakeHookerServer``, the real ``feed`` servers under test) binds
``127.0.0.1``/``::1``, so loopback connects are allowed through unconditionally;
anything else is a real external service a unit test must never reach. Modelled
on Anki Miner's ``tests/_network_tripwire.py``, whose docstring has the
record-and-block rationale in full; the one difference is the loopback
allowance, since this app's tests deliberately talk to real fakes over
loopback sockets.

MECHANISM: record-and-block, not raise-and-propagate. A connect this wrapper
blocks raises ``NetworkTripwireError``; if production code swallows that (a worker
thread's broad ``except``), the test still fails because the failure signal is
the ``RECORDED`` list, asserted by the autouse ``_network_guard`` fixture in
conftest at test setup AND teardown.
"""

from __future__ import annotations

import ipaddress
import os
import socket

# (test id, "host:port") pairs for every blocked connect attempt.
RECORDED: list[tuple[str, str]] = []

# While True (toggled by ``_network_guard`` around ``network``-marked tests),
# the wrapper passes every connect through untouched.
SUPPRESSED = False

_ORIGINALS: dict[str, object] = {}
_GUARDED_FAMILIES = {socket.AF_INET, socket.AF_INET6}


class NetworkTripwireError(Exception):
    """Raised in place of a real non-loopback TCP connect during tests."""


def _host_of(address: object) -> str | None:
    if isinstance(address, tuple) and len(address) >= 1:
        return str(address[0])
    return None


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _describe(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return repr(address)


def _record_and_raise(address: object) -> None:
    target = _describe(address)
    origin = os.environ.get("PYTEST_CURRENT_TEST", "<outside any test>")
    RECORDED.append((origin, target))
    raise NetworkTripwireError(
        f"blocked real non-loopback network connect to {target} during {origin!r}. "
        "Unit tests may only reach loopback (127.0.0.1/::1) fakes. Mark a "
        "genuinely networked test with @pytest.mark.network."
    )


def install() -> None:
    """Wrap ``socket.socket.connect``/``connect_ex`` for the whole session."""
    if _ORIGINALS:
        return
    _ORIGINALS["connect"] = socket.socket.connect
    _ORIGINALS["connect_ex"] = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address):  # type: ignore[no-untyped-def]
        if self.family in _GUARDED_FAMILIES and not SUPPRESSED and not _is_loopback(_host_of(address)):
            _record_and_raise(address)
        return _ORIGINALS["connect"](self, address)  # type: ignore[operator]

    def guarded_connect_ex(self: socket.socket, address):  # type: ignore[no-untyped-def]
        if self.family in _GUARDED_FAMILIES and not SUPPRESSED and not _is_loopback(_host_of(address)):
            _record_and_raise(address)
        return _ORIGINALS["connect_ex"](self, address)  # type: ignore[operator]

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]


def uninstall() -> None:
    """Restore the original socket methods (session teardown only)."""
    if not _ORIGINALS:
        return
    socket.socket.connect = _ORIGINALS.pop("connect")  # type: ignore[method-assign]
    socket.socket.connect_ex = _ORIGINALS.pop("connect_ex")  # type: ignore[method-assign]


def summarize_recorded(records: list[tuple[str, str]]) -> str | None:
    """Human-readable failure message for recorded connects, or ``None`` if clean."""
    if not records:
        return None
    lines = "\n".join(f"  - {target} (during {origin})" for origin, target in records)
    return (
        f"test attempted {len(records)} real non-loopback network connect(s):\n{lines}\n"
        "Mark the test with @pytest.mark.network if it genuinely needs the network."
    )
