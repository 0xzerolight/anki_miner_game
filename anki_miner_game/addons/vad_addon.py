"""The VAD add-on: a uv-built Python environment plus the Silero model (spec 13.1).

Everything lives under ``<home>/addons/vad/``: ``uv`` keeps its managed CPython, cache and tool
folders there (``bootstrap.uv_environment``), and ``env/`` holds what the VAD pass runs:

    env/venv/            onnxruntime, numpy and PyAV from vad/worker/requirements.txt (hash-pinned)
    env/silero_vad_v6.onnx

``install`` builds ``env/`` in place and writes the model last, through ``<name>.part``, checked
against its pinned size and sha256. Any failure removes ``env/`` again, so a failed install leaves
the add-on missing, never half there. ``status`` is ``ready`` only while the interpreter exists and
the model's bytes match the pin; an ``env/`` without both is ``broken`` and a reinstall replaces it.

``VadSettings.enabled`` takes effect only while this add-on is ready: ``vad.trimmer`` checks
``status()`` before every pass and records ``unavailable`` instead of running without it.
"""

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from anki_miner_game.addons import bootstrap
from anki_miner_game.addons.bootstrap import _REDIRECT_STATUSES, MAX_REDIRECTS, BootstrapError, Transport
from anki_miner_game.interfaces.addons import ProgressCallback
from anki_miner_game.models.addons import AddonStatus
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

ENVIRONMENT_BYTES: Final[Mapping[str, int]] = {"linux": 111_000_000, "win32": 77_000_000}
"""What ``uv`` downloads for the environment: the managed CPython (python-build-standalone
20260901, ``install_only_stripped``) plus the pinned wheels, from their release and PyPI sizes on
2026-09-21. Shown by the wizard as an approximation."""

_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
"""Keeps a console window from flashing up on Windows; 0 elsewhere."""

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
"""``subprocess.run``'s shape; a seam for tests."""


class VadAddonError(Exception):
    """The add-on could not be installed; the message says why and is fit for a banner."""


class VadAddon:
    """``AddonService`` for the VAD pass; also tells the trimmer where the interpreter and model are."""

    def __init__(
        self,
        home: Path,
        *,
        model: ModelPin = MODEL,
        run: Runner = subprocess.run,
        ensure_uv: Callable[..., Path] = bootstrap.ensure_uv,
        transport: Transport | None = None,
    ) -> None:
        self._home = home
        self._model = model
        self._run = run
        self._ensure_uv = ensure_uv
        self._transport = transport or bootstrap.urllib_transport
        self._lock = threading.Lock()
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
        if self.python_path.is_file() and self._model_ok():
            return AddonStatus.READY
        return AddonStatus.BROKEN

    async def install(self, progress: ProgressCallback) -> None:
        """Build the environment and fetch the model; no-op when ready. Raises ``VadAddonError``.

        The work runs on a worker thread, so ``progress(done_bytes, total_bytes)`` is called there;
        ``total_bytes`` is ``size_bytes`` throughout. A second call while one runs waits for it.
        """
        await asyncio.to_thread(self._install, progress)

    def _install(self, progress: ProgressCallback) -> None:
        with self._lock:
            if self.status() is AddonStatus.READY:
                return
            self._installing = True
            try:
                self._build(progress)
            except BaseException as exc:
                shutil.rmtree(self.root / "env", ignore_errors=True)
                if isinstance(exc, BootstrapError | OSError | HTTPException):
                    raise VadAddonError(f"The VAD add-on could not be installed: {exc}") from exc
                raise
            finally:
                self._installing = False

    def _build(self, progress: ProgressCallback) -> None:
        uv_share = self._uv_bytes()
        env_share = ENVIRONMENT_BYTES.get(sys.platform, ENVIRONMENT_BYTES["linux"])
        total = uv_share + env_share + self._model.size

        def uv_progress(done: int, uv_total: int) -> None:
            progress(done * uv_share // uv_total if uv_total else 0, total)

        uv = self._ensure_uv(self._home, progress=uv_progress)
        progress(uv_share, total)

        env = self.root / "env"
        shutil.rmtree(env, ignore_errors=True)
        if env.exists():
            raise VadAddonError(f"The VAD add-on could not remove its old files in {env}")
        env.mkdir(parents=True)
        self._uv(uv, "venv", ["--no-project", "--python", PYTHON_VERSION, str(env / "venv")])
        self._uv(
            uv,
            "pip",
            ["install", "--python", str(self.python_path), "--require-hashes", "--only-binary", ":all:"]
            + ["-r", str(REQUIREMENTS)],
        )
        progress(uv_share + env_share, total)

        base = uv_share + env_share
        self._download_model(lambda done: progress(base + done, total))
        progress(total, total)

    def _uv(self, uv: Path, command: str, args: Sequence[str]) -> None:
        env = {**os.environ, **bootstrap.uv_environment(self._home, "vad")}
        result = self._run(
            [str(uv), command, *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            detail = _tail(result.stderr or result.stdout)
            raise VadAddonError(f"uv {command} failed (exit {result.returncode}): {detail}")

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
