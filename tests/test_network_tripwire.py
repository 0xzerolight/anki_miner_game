"""The ``_network_guard`` fixture suppresses the tripwire for ``network``-marked tests only.

``e2e`` is not in the gate's deselect string, so an ``e2e``-marked test runs in the
gate and in CI and must stay under the no-network-except-loopback rule. The real
connect is stubbed out, so neither test reaches the network whatever the guard does.
"""

import socket

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
