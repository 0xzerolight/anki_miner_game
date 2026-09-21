"""Download a sha256-pinned ``uv`` into ``<home>/bin/`` (spec 19, last bullet).

The VAD and OCR add-ons build their environments with this ``uv``, always under
``uv_environment(home, addon)``, and share the two helpers that keep an install cancellable:
``run_uv`` (an asyncio subprocess, killed when the call is cancelled) and ``in_worker_thread``
(blocking downloads that stop at their next chunk once cancelled). The checks are a minimal copy
of the idea in Anki Miner's ``anki_miner/services/_install_common.py`` (``verify_sha256``, ``.part``
cleanup) at commit ``ea4a30ce``: copied, not imported. Deliberately no resume, no receipt files and
no resolver tiers; an installed ``uv`` is recognised by the sha256 of the executable itself.

Every request goes over HTTPS to a host in ``ALLOWED_HOSTS``, redirects included: the transport
never follows a redirect itself, ``ensure_uv`` checks each hop before asking for it. The download is
capped at the pinned archive size and written to ``<archive>.part``; after the archive hash check the
executable is extracted to ``<name>.staged`` with the exec bit, hash-checked again and moved into
place with ``os.replace``. Any failure raises ``BootstrapError`` and leaves the previous ``uv``, if
any, untouched and no scratch file behind.
"""

import asyncio
import contextlib
import hashlib
import os
import platform
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import IO, Final, Literal
from urllib.parse import urljoin, urlsplit

from anki_miner_game.interfaces.addons import ProgressCallback

UV_VERSION: Final = "0.12.17"

ALLOWED_HOSTS: Final = frozenset({"github.com", "release-assets.githubusercontent.com"})
"""A release download is answered by ``github.com`` with a 302 to
``release-assets.githubusercontent.com``, which serves the file (checked 2026-09-21)."""

MAX_REDIRECTS: Final = 5
TIMEOUT_S: Final = 30.0
"""Per socket operation, not for the whole download."""
CHUNK_SIZE: Final = 64 * 1024

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
"""Keeps a console window from flashing up on Windows; 0 elsewhere."""


class BootstrapError(Exception):
    """``uv`` could not be installed; the message says why."""


class InstallCancelledError(Exception):
    """Raised by ``check()`` inside ``in_worker_thread`` work once the awaiting call was cancelled."""


@dataclass(frozen=True)
class UvPin:
    url: str
    sha256: str
    """Of the archive, from the release's ``<asset>.sha256`` file."""
    size: int
    """Exact archive size in bytes; also the download cap."""
    member: str
    """Archive path of the ``uv`` executable; its base name is the installed file's name."""
    member_sha256: str
    """Of the extracted executable; how an installed ``uv`` is recognised."""


_RELEASE: Final = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}"

PINS: Final[Mapping[tuple[str, str], UvPin]] = {
    ("linux", "x86_64"): UvPin(
        url=f"{_RELEASE}/uv-x86_64-unknown-linux-gnu.tar.gz",
        sha256="fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63",
        size=19_755_224,
        member="uv-x86_64-unknown-linux-gnu/uv",
        member_sha256="553a67a24d306a803d5c45678b7c54ed0c8b698d9fe3835d54905811348ccf2a",
    ),
    ("win32", "x86_64"): UvPin(
        url=f"{_RELEASE}/uv-x86_64-pc-windows-msvc.zip",
        sha256="a252121d5b59398fcb137c6ea448176459a44010f33f67e0072305a637119ca7",
        size=17_906_210,
        member="uv.exe",
        member_sha256="2019cdf564cb8f749262f5f021cedc75a99abb1c6081227ca340bbcda972611d",
    ),
}
"""``(sys.platform, machine)`` -> pin. Archive hashes are the release's ``.sha256`` files, matched
against one download on 2026-09-21; the member hashes were computed from that download."""

_MACHINE_ALIASES: Final = {"amd64": "x86_64"}


def pin_for(plat: str, machine: str) -> UvPin | None:
    """The pin for ``sys.platform`` *plat* on ``platform.machine()`` *machine*, or ``None``."""
    arch = machine.lower()
    return PINS.get((plat, _MACHINE_ALIASES.get(arch, arch)))


@dataclass(frozen=True)
class Reply:
    """One HTTP response; a redirect is returned, never followed."""

    status: int
    location: str | None
    length: int | None
    """``Content-Length``, when the server sent one."""
    chunks: Iterable[bytes]


Transport = Callable[[str], contextlib.AbstractContextManager[Reply]]
"""GETs one URL; the reply is valid inside the context."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: Message, newurl: str
    ) -> urllib.request.Request | None:
        return None


@contextlib.contextmanager
def urllib_transport(url: str) -> Iterator[Reply]:
    """The real transport: stdlib ``urllib``, TLS verified, system proxies honoured."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(url, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as err:  # every non-2xx status, unfollowed redirects included
        response = err
    with response:
        length = response.headers.get("Content-Length")
        yield Reply(
            status=response.status,
            location=response.headers.get("Location"),
            length=int(length) if length is not None and length.isdigit() else None,
            chunks=iter(lambda: response.read(CHUNK_SIZE), b""),
        )


_LOCK = threading.Lock()
"""Serialises installs, so two add-ons installing at once share one download."""


def uv_environment(home: Path, addon: Literal["vad", "ocr"]) -> dict[str, str]:
    """Environment overrides for every ``uv`` call an add-on makes: merge them into ``os.environ``.

    They keep what ``uv`` stores for the add-on under ``<home>/addons/<addon>/`` (spec 13.1, 14).
    Without them ``uv`` links the environment to a Python in ``~/.local/share/uv/python`` or a
    system Python, caches wheels in ``~/.cache/uv``, puts tool commands in ``~/.local/bin`` and
    reads the user's own ``uv.toml``, so the user's ``uv cache clean``, ``uv python uninstall`` or
    index setting could break or change the add-on. ``UV_MANAGED_PYTHON`` is ``--managed-python``.
    Creates nothing.
    """
    root = home / "addons" / addon
    return {
        "UV_NO_CONFIG": "1",
        "UV_MANAGED_PYTHON": "1",
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "cache"),
        "UV_TOOL_DIR": str(root / "tools"),
        "UV_TOOL_BIN_DIR": str(root / "bin"),
    }


def ensure_uv(
    home: Path,
    *,
    progress: ProgressCallback | None = None,
    transport: Transport | None = None,
    pin: UvPin | None = None,
) -> Path:
    """``<home>/bin/uv`` (``uv.exe`` on Windows), downloaded first unless the pinned one is there.

    Blocking: run it off the GUI thread. ``progress(done_bytes, total_bytes)`` is called on the
    calling thread while the archive downloads. ``transport`` and ``pin`` exist for tests.
    Raises ``BootstrapError``.
    """
    if pin is None:
        pin = pin_for(sys.platform, platform.machine())
        if pin is None:
            raise BootstrapError(f"no pinned uv for {sys.platform} on {platform.machine()}")
    target = home / "bin" / PurePosixPath(pin.member).name
    with _LOCK:
        try:
            if target.is_file() and _sha256_of(target) == pin.member_sha256:
                return target
            _install(pin, target, progress, transport or urllib_transport)
        except (OSError, HTTPException, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise BootstrapError(f"uv could not be installed: {exc}") from exc
    return target


def _install(pin: UvPin, target: Path, progress: ProgressCallback | None, transport: Transport) -> None:
    _check_url(pin.url)
    archive_name = PurePosixPath(urlsplit(pin.url).path).name
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.parent / f"{archive_name}.part"
    staged = target.parent / f"{target.name}.staged"
    try:
        _download(pin, part, progress, transport)
        actual = _sha256_of(part)
        if actual != pin.sha256:
            raise BootstrapError(f"uv download checksum mismatch: expected {pin.sha256}, got {actual}")
        _extract(part, archive_name, pin, staged)
        os.replace(staged, target)
    finally:
        for scratch in (part, staged):
            with contextlib.suppress(OSError):
                scratch.unlink(missing_ok=True)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise BootstrapError(f"refusing {url}: uv is only downloaded over HTTPS")
    if parts.hostname not in ALLOWED_HOSTS:
        raise BootstrapError(f"refusing {url}: host {parts.hostname} is not allowed")


def _download(pin: UvPin, part: Path, progress: ProgressCallback | None, transport: Transport) -> None:
    url = pin.url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(url)
        with transport(url) as reply:
            if reply.status in _REDIRECT_STATUSES:
                if not reply.location:
                    raise BootstrapError(f"{url} answered {reply.status} without a Location")
                url = urljoin(url, reply.location)
                continue
            if reply.status != 200:
                raise BootstrapError(f"{url} answered HTTP {reply.status}")
            _write_capped(reply, part, pin.size, progress)
            return
    raise BootstrapError(f"more than {MAX_REDIRECTS} redirects from {pin.url}")


def _write_capped(reply: Reply, part: Path, cap: int, progress: ProgressCallback | None) -> None:
    if reply.length is not None and reply.length > cap:
        raise BootstrapError(f"uv download is larger than the pinned {cap} bytes ({reply.length})")
    total = cap if reply.length is None else reply.length
    done = 0
    with part.open("wb") as fh:
        for chunk in reply.chunks:
            done += len(chunk)
            if done > cap:
                raise BootstrapError(f"uv download is larger than the pinned {cap} bytes")
            fh.write(chunk)
            if progress is not None:
                progress(done, total)


def _extract(archive: Path, archive_name: str, pin: UvPin, staged: Path) -> None:
    missing = BootstrapError(f"uv archive is missing {pin.member}")
    with contextlib.ExitStack() as stack:
        src: IO[bytes] | None
        if archive_name.endswith(".zip"):
            zf = stack.enter_context(zipfile.ZipFile(archive))
            try:
                src = stack.enter_context(zf.open(pin.member))
            except KeyError:
                raise missing from None
        elif archive_name.endswith(".tar.gz"):
            tar = stack.enter_context(tarfile.open(archive, "r:gz"))
            try:
                src = tar.extractfile(pin.member)
            except KeyError:
                src = None
            if src is None:
                raise missing
            stack.enter_context(src)
        else:
            raise BootstrapError(f"unsupported uv archive {archive_name}")
        digest = _copy_executable(src, staged)
    if digest != pin.member_sha256:
        raise BootstrapError(f"uv executable checksum mismatch: expected {pin.member_sha256}, got {digest}")


def _copy_executable(src: IO[bytes], staged: Path) -> str:
    """Copy *src* to a fresh *staged* file with mode 0o755 less the umask; its sha256."""
    staged.unlink(missing_ok=True)
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o755)
    digest = hashlib.sha256()
    with os.fdopen(fd, "wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            dst.write(chunk)
    return digest.hexdigest()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def run_uv(argv: Sequence[str], env: Mapping[str, str], cwd: Path | None = None) -> tuple[int, str]:
    """Run one ``uv`` command line to its end: its exit code and its output, stdout and stderr merged.

    Cancelling the call kills uv and waits for it to exit. Raises ``OSError`` when uv cannot start.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=dict(env),
        cwd=cwd,
        creationflags=NO_WINDOW,
    )
    try:
        out, _ = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    code = proc.returncode
    assert code is not None
    return code, out.decode("utf-8", errors="replace")


async def in_worker_thread[T](work: Callable[[Callable[[], None]], T]) -> T:
    """Run ``work(check)`` on a worker thread and return what it returns.

    ``work`` calls ``check()`` between chunks, for example from a download's ``progress``. Once the
    awaiting call is cancelled, ``check()`` raises ``InstallCancelledError``; the call then waits for
    ``work`` to end before it re-raises ``CancelledError``, so nothing it started still writes files.
    """
    stop = threading.Event()

    def check() -> None:
        if stop.is_set():
            raise InstallCancelledError("the install was cancelled")

    task = asyncio.ensure_future(asyncio.to_thread(work, check))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: ``InstallCancelledError``, or what the work raised on its way out
        raise
