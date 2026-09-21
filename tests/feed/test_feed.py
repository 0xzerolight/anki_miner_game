"""Text feed (spec 15): websocket re-broadcast plus the one-page HTTP server."""

from __future__ import annotations

import asyncio
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from anki_miner_game.feed import FEED_HOST, FeedPortInUseError, FeedServer, http_server

PAGE = Path(__file__).resolve().parents[2] / "anki_miner_game" / "feed" / "page.html"


def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


async def _wait_for_clients(server: FeedServer, n: int) -> None:
    for _ in range(200):
        if server.client_count >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} clients, have {server.client_count}")


@pytest.fixture
async def feed():
    server = FeedServer(ws_port=0, http_port=0)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _occupied_port() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((FEED_HOST, 0))
    sock.listen()
    return sock


async def test_broadcast_reaches_two_clients(feed: FeedServer) -> None:
    url = f"ws://{FEED_HOST}:{feed.ws_port}"
    async with connect(url) as one, connect(url) as two:
        await _wait_for_clients(feed, 2)
        feed.broadcast("こんにちは、世界")
        got = await asyncio.wait_for(asyncio.gather(one.recv(), two.recv()), timeout=5)
    assert got == ["こんにちは、世界", "こんにちは、世界"]  # text frames, so recv() yields str


async def test_broadcast_before_start_and_after_stop_is_a_no_op() -> None:
    server = FeedServer(ws_port=0, http_port=0)
    server.broadcast("ignored")
    await server.start()
    await server.stop()
    server.broadcast("ignored")
    await server.stop()  # idempotent


async def test_a_second_start_keeps_the_running_servers(feed: FeedServer) -> None:
    bound = feed.bound_addresses()
    await feed.start()
    assert feed.bound_addresses() == bound
    status, _, _ = await asyncio.to_thread(_get, feed.page_url)
    assert status == 200


async def test_a_page_missing_from_the_bundle_raises_an_oserror_and_binds_nothing(monkeypatch) -> None:
    # Not a FeedPortInUseError, but still an OSError: the composition treats any OSError from
    # start() as "feed off + banner".
    monkeypatch.setattr(http_server, "PAGE_PATH", PAGE.with_name("missing.html"))
    server = FeedServer(ws_port=0, http_port=0)
    with pytest.raises(OSError) as exc:
        await server.start()
    assert not isinstance(exc.value, FeedPortInUseError)
    assert server.bound_addresses() == []


async def test_both_servers_bind_loopback_only(feed: FeedServer) -> None:
    assert feed.ws_port > 0 and feed.http_port > 0
    assert feed.bound_addresses() == [(FEED_HOST, feed.ws_port), (FEED_HOST, feed.http_port)]
    assert FEED_HOST == "127.0.0.1"


async def test_page_served_as_utf8_html_with_ws_port(feed: FeedServer) -> None:
    status, headers, body = await asyncio.to_thread(_get, feed.page_url)
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    text = body.decode("utf-8")
    assert "<!DOCTYPE html>" in text
    assert f'Number("{feed.ws_port}")' in text
    assert int(headers["Content-Length"]) == len(body)


async def test_only_the_page_is_served(feed: FeedServer) -> None:
    status, _, _ = await asyncio.to_thread(_get, f"http://{FEED_HOST}:{feed.http_port}/etc/passwd")
    assert status == 404
    status, _, _ = await asyncio.to_thread(_get, f"http://{FEED_HOST}:{feed.http_port}/index.html")
    assert status == 200


async def test_ws_port_in_use_raises_typed_error_with_port() -> None:
    blocker = _occupied_port()
    port = blocker.getsockname()[1]
    try:
        server = FeedServer(ws_port=port, http_port=0)
        with pytest.raises(FeedPortInUseError) as exc:
            await server.start()
        assert exc.value.port == port
        assert str(port) in str(exc.value)
        assert server.client_count == 0
    finally:
        blocker.close()


async def test_http_port_in_use_raises_and_releases_ws_port() -> None:
    blocker = _occupied_port()
    port = blocker.getsockname()[1]
    try:
        server = FeedServer(ws_port=0, http_port=port)
        with pytest.raises(FeedPortInUseError) as exc:
            await server.start()
        assert exc.value.port == port
        assert server.bound_addresses() == []  # the websocket half was closed again
    finally:
        blocker.close()


def test_port_in_use_error_is_an_oserror() -> None:
    err = FeedPortInUseError(6678)
    assert isinstance(err, OSError)
    assert err.port == 6678


def test_page_has_no_external_references() -> None:
    html = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"https?://", html)
    assert not re.search(r"""(src|href)\s*=""", html, re.IGNORECASE)
    assert not re.search(r"<link\b", html, re.IGNORECASE)
    assert not re.search(r"@import|url\(", html, re.IGNORECASE)
    assert "ws://" in html  # the one connection it makes, back to this app


def test_page_features() -> None:
    html = PAGE.read_text(encoding="utf-8")
    assert "prefers-color-scheme" in html  # follows the system light/dark scheme
    assert 'id="count"' in html  # line counter
    assert 'id="light"' in html  # connection light
    assert "new WebSocket(" in html
    assert "textContent" in html and "innerHTML" not in html  # lines never parsed as markup
    assert "__FEED_WS_PORT__" in html  # placeholder the HTTP server fills in
