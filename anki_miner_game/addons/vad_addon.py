"""The VAD add-on: a uv-built Python environment plus the Silero model (spec 13.1).

Everything lives under ``<home>/addons/vad/``: ``uv`` keeps its managed CPython, cache and tool
folders there (``bootstrap.uv_environment``), and ``env/`` holds what the VAD pass runs:

    env/venv/            onnxruntime, numpy and PyAV from vad/worker/requirements.txt (hash-pinned)
    env/pins.sha256      what env/venv/ was built from: pins_digest() at install time
    env/silero_vad_v6.onnx

``install`` builds ``env/`` in place and writes the model last, through ``<name>.part``, checked
against its pinned size and sha256. Any failure or cancellation removes ``env/`` again, so a failed
install leaves the add-on missing, never half there: a running ``uv`` is killed
(``bootstrap.run_uv``), and the uv and model downloads stop at their next chunk
(``bootstrap.in_worker_thread``). ``status`` is ``ready`` only while the interpreter exists,
``pins.sha256`` matches the requirements and Python version this release pins, and the model's
bytes match its pin; any other ``env/`` is ``broken`` (after an update that changed a pin, too) and
a reinstall replaces it.

``VadSettings.enabled`` takes effect only while this add-on is ready: ``vad.trimmer`` checks
``status()`` before every pass and records ``unavailable`` instead of running without it.
"""

import asyncio
import hashlib
import os
import platform
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from anki_miner_game.addons import bootstrap
from anki_miner_game.addons.bootstrap import _REDIRECT_STATUSES, MAX_REDIRECTS, BootstrapError, Transport
from anki_miner_game.interfaces.addons import ProgressCallback
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.runtime.child_env import child_environ
from anki_miner_game.vad import model_pin


@dataclass(frozen=True)
class ModelPin:
    url: str
    sha256: str
    size: int
    """Exact size in bytes; also the download cap."""
    filename: str


MODEL: Final = ModelPin(
    url=model_pin.MODEL_URL,
    sha256=model_pin.MODEL_SHA256,
    size=model_pin.MODEL_SIZE_BYTES,
    filename=model_pin.MODEL_FILENAME,
)
MODEL_HOSTS: Final = frozenset({"raw.githubusercontent.com"})
"""The only host the model is fetched from, redirects included."""

PYTHON_VERSION: Final = "3.12"
"""The requirements are resolved for Python 3.12 and later; uv fetches a managed 3.12."""

REQUIREMENTS: Final = Path(__file__).resolve().parents[1] / "vad" / "worker" / "requirements.txt"

PINS_FILE: Final = "pins.sha256"
"""In ``env/``: the ``pins_digest()`` the environment was built from."""

ENVIRONMENT_BYTES: Final[Mapping[str, int]] = {"linux": 111_000_000, "win32": 77_000_000}
"""What ``uv`` downloads for the environment: the managed CPython (python-build-standalone
20260901, ``install_only_stripped``) plus the pinned wheels, from their release and PyPI sizes on
2026-09-21. Shown by the wizard as an approximation."""

UvRunner = Callable[[Sequence[str], Mapping[str, str], Path | None], Awaitable[tuple[int, str]]]
"""``bootstrap.run_uv``'s shape; a seam for tests."""


def pins_digest() -> str:
    """sha256 over the requirements file's bytes and ``PYTHON_VERSION``: what ``env/venv/`` is built from."""
    digest = hashlib.sha256(REQUIREMENTS.read_bytes())
    digest.update(f"\npython=={PYTHON_VERSION}\n".encode())
    return digest.hexdigest()


class VadAddonError(RuntimeError):
    """The add-on could not be installed; the message says why and is fit for a banner."""


class VadAddon:
    """``AddonService`` for the VAD pass; also tells the trimmer where the interpreter and model are."""

    def __init__(
        self,
        home: Path,
        *,
        model: ModelPin = MODEL,
        run_uv: UvRunner = bootstrap.run_uv,
        ensure_uv: Callable[..., Path] = bootstrap.ensure_uv,
        transport: Transport | None = None,
    ) -> None:
        self._home = home
        self._model = model
        self._run_uv = run_uv
        self._ensure_uv = ensure_uv
        self._transport = transport or bootstrap.urllib_transport
        self._installing = False

    @property
    def root(self) -> Path:
        return self._home / "addons" / "vad"

    @property
    def python_path(self) -> Path:
        venv = self.root / "env" / "venv"
        return venv / "Scripts" / "python.exe" if sys.platform == "win32" else venv / "bin" / "python"

    @property
    def model_path(self) -> Path:
        return self.root / "env" / self._model.filename

    @property
    def size_bytes(self) -> int:
        """The uv download, the environment and the model; approximate."""
        return self._uv_bytes() + ENVIRONMENT_BYTES.get(sys.platform, ENVIRONMENT_BYTES["linux"]) + self._model.size

    @property
    def note(self) -> str | None:
        """No platform limitation."""
        return None

    def status(self) -> AddonStatus:
        if self._installing:
            return AddonStatus.INSTALLING
        if not (self.root / "env").exists():
            return AddonStatus.MISSING
        if self.python_path.is_file() and self._pins_ok() and self._model_ok():
            return AddonStatus.READY
        return AddonStatus.BROKEN

    async def install(self, progress: ProgressCallback) -> None:
        """Build the environment and fetch the model (``AddonService.install``); nothing while ready.

        ``progress(done_bytes, total_bytes)`` is called on a worker thread during the uv and model
        downloads and on the loop thread otherwise; ``total_bytes`` is ``size_bytes`` throughout.
        Raises ``VadAddonError``, also when an install is already running.
        """
        if self._installing:
            raise VadAddonError("The VAD add-on is already being installed.")
        if self.status() is AddonStatus.READY:
            return
        self._installing = True
        try:
            await self._build(progress)
        except BaseException as exc:
            await asyncio.to_thread(shutil.rmtree, self.root / "env", ignore_errors=True)
            if isinstance(exc, BootstrapError | OSError | HTTPException):
                raise VadAddonError(f"The VAD add-on could not be installed: {exc}") from exc
            raise
        finally:
            self._installing = False

    async def _build(self, progress: ProgressCallback) -> None:
        uv_share = self._uv_bytes()
        env_share = ENVIRONMENT_BYTES.get(sys.platform, ENVIRONMENT_BYTES["linux"])
        total = uv_share + env_share + self._model.size

        def fetch_uv(check: Callable[[], None]) -> Path:
            def uv_progress(done: int, uv_total: int) -> None:
                check()
                progress(done * uv_share // uv_total if uv_total else 0, total)

            return self._ensure_uv(self._home, progress=uv_progress)

        uv = await bootstrap.in_worker_thread(fetch_uv)
        progress(uv_share, total)

        env = self.root / "env"
        await asyncio.to_thread(shutil.rmtree, env, ignore_errors=True)
        if env.exists():
            raise VadAddonError(f"The VAD add-on could not remove its old files in {env}")
        env.mkdir(parents=True)
        await self._uv(uv, "venv", ["--no-project", "--python", PYTHON_VERSION, str(env / "venv")])
        await self._uv(
            uv,
            "pip",
            ["install", "--python", str(self.python_path), "--require-hashes", "--only-binary", ":all:"]
            + ["-r", str(REQUIREMENTS)],
        )
        (env / PINS_FILE).write_text(pins_digest(), encoding="ascii")
        progress(uv_share + env_share, total)

        base = uv_share + env_share

        def fetch_model(check: Callable[[], None]) -> None:
            def model_progress(done: int) -> None:
                check()
                progress(base + done, total)

            self._download_model(model_progress)

        await bootstrap.in_worker_thread(fetch_model)
        progress(total, total)

    async def _uv(self, uv: Path, command: str, args: Sequence[str]) -> None:
        env = {**child_environ(), **bootstrap.uv_environment(self._home, "vad")}
        code, output = await self._run_uv([str(uv), command, *args], env, self.root)
        if code != 0:
            raise VadAddonError(f"uv {command} failed (exit {code}): {_tail(output)}")

    def _download_model(self, progress: Callable[[int], None]) -> None:
        pin = self._model
        part = self.model_path.with_name(f"{pin.filename}.part")
        url = pin.url
        for _ in range(MAX_REDIRECTS + 1):
            _check_model_url(url)
            with self._transport(url) as reply:
                if reply.status in _REDIRECT_STATUSES:
                    if not reply.location:
                        raise VadAddonError(f"{url} answered {reply.status} without a Location")
                    url = urljoin(url, reply.location)
                    continue
                if reply.status != 200:
                    raise VadAddonError(f"The VAD model download failed: {url} answered HTTP {reply.status}")
                digest = _write_capped(reply.chunks, reply.length, part, pin.size, progress)
                break
        else:
            raise VadAddonError(f"more than {MAX_REDIRECTS} redirects from {pin.url}")
        if digest != pin.sha256:
            raise VadAddonError(f"VAD model checksum mismatch: expected {pin.sha256}, got {digest}")
        os.replace(part, self.model_path)

    def _pins_ok(self) -> bool:
        try:
            return (self.root / "env" / PINS_FILE).read_text(encoding="ascii").strip() == pins_digest()
        except (OSError, UnicodeDecodeError):
            return False

    def _model_ok(self) -> bool:
        try:
            if self.model_path.stat().st_size != self._model.size:
                return False
            return hashlib.sha256(self.model_path.read_bytes()).hexdigest() == self._model.sha256
        except OSError:
            return False

    @staticmethod
    def _uv_bytes() -> int:
        pin = bootstrap.pin_for(sys.platform, platform.machine())
        return pin.size if pin is not None else 0


def _check_model_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in MODEL_HOSTS:
        raise VadAddonError(f"refusing {url}: the VAD model is only fetched over HTTPS from {sorted(MODEL_HOSTS)}")


def _write_capped(chunks: Any, length: int | None, part: Path, cap: int, progress: Callable[[int], None]) -> str:
    """Stream ``chunks`` into ``part``, refusing more than ``cap`` bytes; the sha256 of what was written."""
    if length is not None and length > cap:
        raise VadAddonError(f"The VAD model download is larger than the pinned {cap} bytes ({length})")
    digest = hashlib.sha256()
    done = 0
    with part.open("wb") as fh:
        for chunk in chunks:
            done += len(chunk)
            if done > cap:
                raise VadAddonError(f"The VAD model download is larger than the pinned {cap} bytes")
            digest.update(chunk)
            fh.write(chunk)
            progress(done)
    return digest.hexdigest()


def _tail(text: str, limit: int = 600) -> str:
    """The last lines of a tool's output, enough to say what went wrong."""
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
