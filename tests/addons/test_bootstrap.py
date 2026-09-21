"""``addons/bootstrap.py``: the sha256-pinned uv download (spec 19, last bullet).

Every test but the ``network`` one drives ``ensure_uv`` through an injected
transport serving archives built here, with a pin made from those archives.
"""

import contextlib
import hashlib
import io
import os
import platform
import re
import subprocess
import sys
import tarfile
import threading
import zipfile
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from anki_miner_game.addons import bootstrap
from anki_miner_game.addons.bootstrap import BootstrapError, Reply, UvPin, ensure_uv

RELEASE = "https://github.com/astral-sh/uv/releases/download/0.0.0"
ASSET_HOST_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc?sig=x"
LINUX_MEMBER = "uv-x86_64-unknown-linux-gnu/uv"
EXE = b"#!/bin/sh\necho 'uv 0.0.0 (fake)'\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def tar_pin(archive: bytes, **overrides: object) -> UvPin:
    fields: dict[str, object] = {
        "url": f"{RELEASE}/uv-x86_64-unknown-linux-gnu.tar.gz",
        "sha256": _sha(archive),
        "size": len(archive),
        "member": LINUX_MEMBER,
        "member_sha256": _sha(EXE),
    }
    fields.update(overrides)
    return UvPin(**fields)  # type: ignore[arg-type]


@pytest.fixture
def linux_archive() -> bytes:
    return make_tar_gz(
        {"uv-x86_64-unknown-linux-gnu/": b"", LINUX_MEMBER: EXE, "uv-x86_64-unknown-linux-gnu/uvx": b"x"}
    )


def ok(data: bytes, *, length: int | None = -1, chunk: int = 7) -> Callable[[], Reply]:
    """A 200 reply factory; ``length=-1`` means the true length."""

    def make() -> Reply:
        chunks = (data[i : i + chunk] for i in range(0, len(data), chunk))
        return Reply(status=200, location=None, length=len(data) if length == -1 else length, chunks=chunks)

    return make


def redirect(location: str | None, status: int = 302) -> Callable[[], Reply]:
    return lambda: Reply(status=status, location=location, length=0, chunks=iter(()))


class FakeTransport:
    """Serves one reply factory per URL and records every URL asked for."""

    def __init__(self, routes: dict[str, Callable[[], Reply]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    @contextlib.contextmanager
    def __call__(self, url: str) -> Iterator[Reply]:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected request for {url}")
        yield self.routes[url]()


def refuse_all() -> FakeTransport:
    return FakeTransport({})


def bin_dir(home: Path) -> Path:
    return home / "bin"


def leftovers(home: Path) -> list[str]:
    """Every file under ``<home>/bin`` except the final executable."""
    if not bin_dir(home).exists():
        return []
    return sorted(p.name for p in bin_dir(home).iterdir() if p.name not in ("uv", "uv.exe"))


# --- the pins ---------------------------------------------------------------


@pytest.mark.parametrize("key", [("linux", "x86_64"), ("win32", "x86_64")])
def test_pins_are_https_allowlisted_and_versioned(key):
    pin = bootstrap.PINS[key]
    assert pin.url.startswith(f"https://github.com/astral-sh/uv/releases/download/{bootstrap.UV_VERSION}/")
    assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", pin.member_sha256)
    assert 0 < pin.size < 64 * 1024 * 1024


def test_pins_cover_exactly_linux_and_windows_x86_64():
    assert set(bootstrap.PINS) == {("linux", "x86_64"), ("win32", "x86_64")}
    assert bootstrap.PINS[("linux", "x86_64")].member == LINUX_MEMBER
    assert bootstrap.PINS[("win32", "x86_64")].member == "uv.exe"


def test_allowlist_is_the_release_redirect_chain():
    assert frozenset({"github.com", "release-assets.githubusercontent.com"}) == bootstrap.ALLOWED_HOSTS


@pytest.mark.parametrize(
    ("plat", "machine", "key"),
    [
        ("linux", "x86_64", ("linux", "x86_64")),
        ("linux", "AMD64", ("linux", "x86_64")),
        ("win32", "AMD64", ("win32", "x86_64")),
        ("win32", "x86_64", ("win32", "x86_64")),
    ],
)
def test_pin_for_normalises_the_machine(plat, machine, key):
    assert bootstrap.pin_for(plat, machine) is bootstrap.PINS[key]


@pytest.mark.parametrize(("plat", "machine"), [("darwin", "arm64"), ("linux", "aarch64"), ("win32", "ARM64")])
def test_pin_for_unpinned_platform_is_none(plat, machine):
    assert bootstrap.pin_for(plat, machine) is None


def test_unpinned_platform_raises_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "riscv64")
    transport = refuse_all()
    with pytest.raises(BootstrapError, match="no pinned uv"):
        ensure_uv(tmp_path, transport=transport)
    assert transport.calls == []


# --- success ----------------------------------------------------------------


def test_success_installs_the_member_into_home_bin(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    transport = FakeTransport({pin.url: ok(linux_archive)})

    result = ensure_uv(tmp_path, pin=pin, transport=transport)

    assert result == tmp_path / "bin" / "uv"
    assert result.read_bytes() == EXE
    assert leftovers(tmp_path) == []
    assert transport.calls == [pin.url]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX exec bit")
def test_success_sets_the_exec_bit(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    result = ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))
    assert os.access(result, os.X_OK)


def test_success_from_a_zip(tmp_path):
    archive = make_zip({"uv.exe": EXE, "uvx.exe": b"x", "uvw.exe": b"w"})
    pin = UvPin(
        url=f"{RELEASE}/uv-x86_64-pc-windows-msvc.zip",
        sha256=_sha(archive),
        size=len(archive),
        member="uv.exe",
        member_sha256=_sha(EXE),
    )
    result = ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(archive)}))
    assert result == tmp_path / "bin" / "uv.exe"
    assert result.read_bytes() == EXE
    assert leftovers(tmp_path) == []


def test_success_follows_the_release_redirect(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    transport = FakeTransport({pin.url: redirect(ASSET_HOST_URL), ASSET_HOST_URL: ok(linux_archive)})

    result = ensure_uv(tmp_path, pin=pin, transport=transport)

    assert result.read_bytes() == EXE
    assert transport.calls == [pin.url, ASSET_HOST_URL]


def test_relative_redirect_resolves_against_the_current_url(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    moved = "https://github.com/astral-sh/uv/moved.tar.gz"
    transport = FakeTransport({pin.url: redirect("/astral-sh/uv/moved.tar.gz", 301), moved: ok(linux_archive)})

    ensure_uv(tmp_path, pin=pin, transport=transport)

    assert transport.calls == [pin.url, moved]


def test_progress_reports_bytes_up_to_the_total(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    seen: list[tuple[int, int]] = []

    ensure_uv(
        tmp_path,
        pin=pin,
        transport=FakeTransport({pin.url: ok(linux_archive)}),
        progress=lambda d, t: seen.append((d, t)),
    )

    assert seen[-1] == (len(linux_archive), len(linux_archive))
    dones = [d for d, _ in seen]
    assert dones == sorted(dones)


def test_progress_total_falls_back_to_the_pinned_size(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    seen: list[tuple[int, int]] = []

    ensure_uv(
        tmp_path,
        pin=pin,
        transport=FakeTransport({pin.url: ok(linux_archive, length=None)}),
        progress=lambda d, t: seen.append((d, t)),
    )

    assert {t for _, t in seen} == {pin.size}


def test_installed_uv_with_the_pinned_hash_is_reused(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))
    transport = refuse_all()

    result = ensure_uv(tmp_path, pin=pin, transport=transport)

    assert result == tmp_path / "bin" / "uv"
    assert transport.calls == []


def test_installed_uv_with_another_hash_is_replaced(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    bin_dir(tmp_path).mkdir()
    (bin_dir(tmp_path) / "uv").write_bytes(b"an older uv")

    result = ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))

    assert result.read_bytes() == EXE


# --- refusals: nothing installed, nothing left behind -----------------------


def test_hash_mismatch_installs_nothing(tmp_path, linux_archive):
    pin = tar_pin(linux_archive, sha256="0" * 64)

    with pytest.raises(BootstrapError, match="checksum"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))

    assert not (tmp_path / "bin" / "uv").exists()
    assert leftovers(tmp_path) == []


def test_failed_replacement_keeps_the_previous_uv(tmp_path, linux_archive):
    pin = tar_pin(linux_archive, sha256="0" * 64)
    bin_dir(tmp_path).mkdir()
    (bin_dir(tmp_path) / "uv").write_bytes(b"an older uv")

    with pytest.raises(BootstrapError):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))

    assert (bin_dir(tmp_path) / "uv").read_bytes() == b"an older uv"
    assert leftovers(tmp_path) == []


def test_oversize_content_length_is_refused_before_reading(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    read: list[bytes] = []

    def big() -> Reply:
        def chunks() -> Iterator[bytes]:
            read.append(b"x")
            yield linux_archive

        return Reply(status=200, location=None, length=pin.size + 1, chunks=chunks())

    with pytest.raises(BootstrapError, match="larger than"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: big}))

    assert read == []
    assert leftovers(tmp_path) == []


def test_endless_body_stops_at_the_size_cap(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    served = 0

    def endless() -> Reply:
        def chunks() -> Iterator[bytes]:
            nonlocal served
            while True:
                served += 1024
                yield b"\0" * 1024

        return Reply(status=200, location=None, length=None, chunks=chunks())

    with pytest.raises(BootstrapError, match="larger than"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: endless}))

    assert served <= pin.size + 1024
    assert not (tmp_path / "bin" / "uv").exists()
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/astral-sh/uv/releases/download/0.0.0/uv.tar.gz",
        "ftp://github.com/uv.tar.gz",
        "file:///etc/passwd",
    ],
)
def test_non_https_pin_is_refused_without_a_request(tmp_path, linux_archive, url):
    transport = refuse_all()
    with pytest.raises(BootstrapError, match="HTTPS"):
        ensure_uv(tmp_path, pin=tar_pin(linux_archive, url=url), transport=transport)
    assert transport.calls == []


def test_redirect_to_http_is_not_followed(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    plain = "http://release-assets.githubusercontent.com/asset"
    transport = FakeTransport({pin.url: redirect(plain), plain: ok(linux_archive)})

    with pytest.raises(BootstrapError, match="HTTPS"):
        ensure_uv(tmp_path, pin=pin, transport=transport)

    assert transport.calls == [pin.url]
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/uv.tar.gz",
        "https://github.com.evil.example/uv.tar.gz",
        "https://github.com@evil.example/uv.tar.gz",
        "https://objects.githubusercontent.com/uv.tar.gz",
        "https://evil.example/github.com/uv.tar.gz",
    ],
)
def test_foreign_host_pin_is_refused_without_a_request(tmp_path, linux_archive, url):
    transport = refuse_all()
    with pytest.raises(BootstrapError, match="not allowed"):
        ensure_uv(tmp_path, pin=tar_pin(linux_archive, url=url), transport=transport)
    assert transport.calls == []


def test_redirect_to_a_foreign_host_is_not_followed(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    foreign = "https://evil.example/uv.tar.gz"
    transport = FakeTransport({pin.url: redirect(foreign), foreign: ok(linux_archive)})

    with pytest.raises(BootstrapError, match="not allowed"):
        ensure_uv(tmp_path, pin=pin, transport=transport)

    assert transport.calls == [pin.url]
    assert not (tmp_path / "bin" / "uv").exists()


def test_interrupted_download_leaves_no_target(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)

    def broken() -> Reply:
        def chunks() -> Iterator[bytes]:
            yield linux_archive[:10]
            raise ConnectionResetError("connection reset by peer")

        return Reply(status=200, location=None, length=len(linux_archive), chunks=chunks())

    with pytest.raises(BootstrapError, match="connection reset"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: broken}))

    assert not (tmp_path / "bin" / "uv").exists()
    assert leftovers(tmp_path) == []


def test_transport_failure_before_the_body_is_a_bootstrap_error(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)

    @contextlib.contextmanager
    def offline(url: str) -> Iterator[Reply]:
        raise OSError("Name or service not known")
        yield  # pragma: no cover

    with pytest.raises(BootstrapError, match="Name or service not known"):
        ensure_uv(tmp_path, pin=pin, transport=offline)


def test_truncated_body_fails_the_checksum(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    with pytest.raises(BootstrapError, match="checksum"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive[:-5])}))
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_http_error_status_is_refused(tmp_path, linux_archive, status):
    pin = tar_pin(linux_archive)
    transport = FakeTransport({pin.url: lambda: Reply(status=status, location=None, length=0, chunks=iter(()))})
    with pytest.raises(BootstrapError, match=str(status)):
        ensure_uv(tmp_path, pin=pin, transport=transport)
    assert leftovers(tmp_path) == []


def test_redirect_without_location_is_refused(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    with pytest.raises(BootstrapError, match="Location"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: redirect(None)}))


def test_redirect_loop_is_cut_off(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    transport = FakeTransport({pin.url: redirect(pin.url)})
    with pytest.raises(BootstrapError, match="redirects"):
        ensure_uv(tmp_path, pin=pin, transport=transport)
    assert len(transport.calls) == bootstrap.MAX_REDIRECTS + 1


def test_archive_without_the_member_installs_nothing(tmp_path):
    archive = make_tar_gz({"uv-x86_64-unknown-linux-gnu/uvx": b"x"})
    pin = tar_pin(archive)
    with pytest.raises(BootstrapError, match="missing"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(archive)}))
    assert not (tmp_path / "bin" / "uv").exists()
    assert leftovers(tmp_path) == []


def test_member_hash_mismatch_installs_nothing(tmp_path, linux_archive):
    pin = tar_pin(linux_archive, member_sha256="0" * 64)
    with pytest.raises(BootstrapError, match="checksum"):
        ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))
    assert not (tmp_path / "bin" / "uv").exists()
    assert leftovers(tmp_path) == []


def test_stale_part_files_from_a_killed_run_are_overwritten(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    bin_dir(tmp_path).mkdir()
    (bin_dir(tmp_path) / "uv-x86_64-unknown-linux-gnu.tar.gz.part").write_bytes(b"half a download")

    result = ensure_uv(tmp_path, pin=pin, transport=FakeTransport({pin.url: ok(linux_archive)}))

    assert result.read_bytes() == EXE
    assert leftovers(tmp_path) == []


def test_concurrent_calls_download_once(tmp_path, linux_archive):
    pin = tar_pin(linux_archive)
    calls: list[str] = []
    second_request = threading.Event()

    @contextlib.contextmanager
    def slow(url: str) -> Iterator[Reply]:
        calls.append(url)
        if len(calls) > 1:
            second_request.set()
        second_request.wait(0.5)  # an unserialised second call would overlap this one
        yield ok(linux_archive)()

    results: list[Path] = []
    threads = [
        threading.Thread(target=lambda: results.append(ensure_uv(tmp_path, pin=pin, transport=slow))) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert results == [tmp_path / "bin" / "uv"] * 2
    assert calls == [pin.url]


# --- the real transport, against a loopback server --------------------------

BODY = b"0123456789" * 500


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://github.com/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/file":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def local_server(monkeypatch) -> Iterator[str]:
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_urllib_transport_does_not_follow_redirects(local_server):
    with bootstrap.urllib_transport(f"{local_server}/redirect") as reply:
        assert reply.status == 302
        assert reply.location == "https://github.com/elsewhere"


def test_urllib_transport_streams_the_body(local_server):
    with bootstrap.urllib_transport(f"{local_server}/file") as reply:
        assert reply.status == 200
        assert reply.length == len(BODY)
        assert b"".join(reply.chunks) == BODY


def test_urllib_transport_returns_error_statuses(local_server):
    with bootstrap.urllib_transport(f"{local_server}/missing") as reply:
        assert reply.status == 404


# --- the real release -------------------------------------------------------


@pytest.mark.network
def test_real_download_matches_the_pin(tmp_path):
    pin = bootstrap.pin_for(sys.platform, platform.machine())
    if pin is None:
        pytest.skip("no pinned uv for this platform")

    uv = ensure_uv(tmp_path)

    assert hashlib.sha256(uv.read_bytes()).hexdigest() == pin.member_sha256
    out = subprocess.run([str(uv), "--version"], capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.startswith(f"uv {bootstrap.UV_VERSION} ")
