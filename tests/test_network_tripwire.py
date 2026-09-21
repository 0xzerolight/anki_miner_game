"""The ``_network_guard`` fixture suppresses the tripwire for ``network``-marked tests only.

``e2e`` is not in the gate's deselect string, so an ``e2e``-marked test runs in the
gate and in CI and must stay under the no-network-except-loopback rule. The real
connect is stubbed out, so neither test reaches the network whatever the guard does.
"""

import shutil
import socket
from pathlib import Path

import pytest

from tests import _network_tripwire as _net

_EXTERNAL = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737): never routable


def _stub_real_connect(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    passed_through: list[object] = []
    monkeypatch.setitem(_net._ORIGINALS, "connect", lambda _sock, address: passed_through.append(address))
    return passed_through


@pytest.mark.e2e
def test_e2e_marked_test_still_blocks_non_loopback(monkeypatch):
    passed_through = _stub_real_connect(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(_net.NetworkTripwireError):
        sock.connect(_EXTERNAL)
    assert passed_through == []
    assert _net.RECORDED and _net.RECORDED[-1][1] == "192.0.2.1:9"
    _net.RECORDED.clear()  # the block above is the expected outcome, not a leak


@pytest.mark.network
def test_network_marked_test_passes_through(monkeypatch):
    passed_through = _stub_real_connect(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(_EXTERNAL)
    assert passed_through == [_EXTERNAL]
    assert _net.RECORDED == []


_INNER_TESTS = """
import pytest


def test_first():
    pass


@pytest.mark.network
def test_second():
    pass


def test_third():
    pass
"""

# Simulates a leaked thread of test_first connecting after its teardown.
_INNER_HOOK = """


def pytest_runtest_logfinish(nodeid, location):
    if nodeid.endswith("::test_first"):
        _net.RECORDED.append(("leaked thread of test_first", "192.0.2.1:9"))
"""


def test_a_stray_connect_fails_the_next_test_even_a_network_one(pytester):
    """The stray check runs before the ``network`` branch, so a leak is not blamed on a later test."""
    inner = pytester.mkpydir("tests")
    here = Path(__file__).parent
    shutil.copy2(here / "_network_tripwire.py", inner / "_network_tripwire.py")
    (inner / "conftest.py").write_text(
        (here / "conftest.py").read_text(encoding="utf-8") + _INNER_HOOK, encoding="utf-8"
    )
    (inner / "test_inner.py").write_text(_INNER_TESTS, encoding="utf-8")
    result = pytester.runpytest_subprocess("-v", "-p", "no:cacheprovider")
    result.stdout.fnmatch_lines(["*test_first PASSED*", "*test_second ERROR*", "*test_third PASSED*"])
    result.stdout.fnmatch_lines(
        ["*ERROR at setup of test_second*", "stray network connect(s) landed between tests*", "*192.0.2.1:9*"]
    )
