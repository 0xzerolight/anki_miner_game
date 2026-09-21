"""VAD jobs: fit a finalised session's subtitle to the voice in its recording (spec 13, 17 VAD row).

``VadTrimmer`` implements ``VadJobs``. Jobs run one at a time, in the order they were asked for,
on one job thread; a pass is one worker subprocess (``vad/worker/vad_worker.py`` under the add-on's
Python) whose JSON lines the thread reads as they arrive (spec 4.2, 13.2).

A pass takes the manifest's ``live_cues``, never the subtitle on disk, so a re-run or a restore
is a rewrite from the manifest and no second subtitle file exists (spec 13.3). The manifest moves:

- ``queue`` / ``rerun``: ``vad`` becomes ``queued`` before the call returns, so a pass still
  pending at exit stays visible as one.
- The pass starts: ``state`` ``vad_running``. It ends with ``state`` ``ready`` and ``vad``:
  ``done`` (the trimmed subtitle is written), ``failed`` (the worker failed; the message says how)
  or ``unavailable`` (the add-on is not ready, so the pass never ran). Anything but ``done``
  rewrites the subtitle from ``live_cues``: the live subtitle stands (spec 13, 17).
- ``restore``: the subtitle from ``live_cues``, ``vad`` ``restored``; needs no add-on.

``VadSettings.enabled`` is the switch for ``queue`` (the pass after finalise); ``rerun`` is the
user's explicit request and runs either way. Either runs only while the add-on is ready.

``trimmed`` and ``no_speech`` split the cues by whether their final span overlaps a voiced
region: ``no_speech`` counts the cues the pass found no voice in.

Only sessions in their game folder with a subtitle are touched: ``ready`` (or ``vad_running``,
left so by a crash) with ``live_cues``. Anything else, or a manifest that cannot be read, is
skipped with a log line and no ``Presenter`` call.
"""

import concurrent.futures
import itertools
import json
import logging
import subprocess
import tempfile
import threading
from bisect import bisect_left
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import IO, Any, Final, Protocol, TypeGuard

from anki_miner_game.interfaces.presenter import Presenter
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.cue import Cue, Region
from anki_miner_game.models.manifest import ManifestState, SessionManifest, VadRecord, VadState
from anki_miner_game.session.manifest import load_manifest, write_manifest_atomic
from anki_miner_game.session.srt_writer import write_srt_atomic
from anki_miner_game.store import StoreError
from anki_miner_game.vad import model_pin
from anki_miner_game.vad.assign import assign

log = logging.getLogger(__name__)

WORKER_SCRIPT: Final = Path(__file__).resolve().parent / "worker" / "vad_worker.py"
MODEL_NAME: Final = PurePath(model_pin.MODEL_FILENAME).stem
"""Recorded in ``VadRecord.model``."""

_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
"""Keeps a console window from flashing up on Windows; 0 elsewhere."""

_UNAVAILABLE: Final = {
    AddonStatus.MISSING: "The VAD add-on is not installed.",
    AddonStatus.BROKEN: "The VAD add-on is damaged; reinstall it.",
    AddonStatus.INSTALLING: "The VAD add-on was still installing; re-run VAD once it is ready.",
}


class VadRuntime(Protocol):
    """What a pass needs from the add-on; ``addons.vad_addon.VadAddon`` provides it."""

    def status(self) -> AddonStatus: ...

    @property
    def python_path(self) -> Path: ...

    @property
    def model_path(self) -> Path: ...


@dataclass(frozen=True)
class _Session:
    """A session a job can work on: its manifest as read, and its files in the game folder."""

    manifest: SessionManifest
    video: Path
    subtitle: Path


class _CancelledError(Exception):
    """``close`` stopped the pass."""


class _PassFailedError(Exception):
    """The pass cannot produce regions; the message goes into ``VadRecord.message``."""


class VadTrimmer:
    """``VadJobs`` over manifest paths; see the module docstring. Call ``close`` at exit."""

    def __init__(
        self,
        addon: VadRuntime,
        presenter: Presenter,
        config: Callable[[], AppConfig],
        *,
        worker_script: Path = WORKER_SCRIPT,
    ) -> None:
        self._addon = addon
        self._presenter = presenter
        self._config = config
        self._worker_script = worker_script
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vad-job")
        self._manifest_lock = threading.Lock()
        """Held for every read-modify-write of a manifest, from the job thread and from callers."""
        self._lock = threading.Lock()
        """Guards the fields below."""
        self._closed = False
        self._process: subprocess.Popen[bytes] | None = None
        self._futures: set[Future[None]] = set()

    def queue(self, manifest_path: Path) -> None:
        """The pass after finalise; nothing while ``VadSettings.enabled`` is off."""
        if self._config().vad.enabled:
            self._submit_pass(manifest_path)

    def rerun(self, manifest_path: Path) -> None:
        self._submit_pass(manifest_path)

    def restore(self, manifest_path: Path) -> None:
        self._submit(self._restore, manifest_path)

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the jobs asked for so far; false when ``timeout`` ran out first."""
        with self._lock:
            pending = list(self._futures)
        return not concurrent.futures.wait(pending, timeout).not_done

    def close(self) -> None:
        """Stop: kill a running worker and drop pending jobs; their manifests keep ``queued``.

        Returns once the job thread is idle. Later calls to ``queue``, ``rerun`` and ``restore``
        are ignored.
        """
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None:
            process.kill()
        self._executor.shutdown(wait=True, cancel_futures=True)

    # --- submission, on the caller's thread ---

    def _submit_pass(self, manifest_path: Path) -> None:
        with self._lock:
            if self._closed:
                log.info("VAD pass for %s not queued: shutting down", manifest_path)
                return
        try:
            with self._manifest_lock:
                session = self._eligible(manifest_path)
                if session is None:
                    return
                write_manifest_atomic(manifest_path, replace(session.manifest, vad=VadRecord(state=VadState.QUEUED)))
        except StoreError as exc:
            log.warning("VAD pass for %s not queued: %s", manifest_path, exc)
            return
        self._submit(self._pass, manifest_path)

    def _submit(self, job: Callable[[Path], None], manifest_path: Path) -> None:
        with self._lock:
            if self._closed:
                log.info("VAD job for %s ignored: shutting down", manifest_path)
                return
            future = self._executor.submit(self._guarded, job, manifest_path)
            self._futures.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    # --- jobs, on the job thread ---

    def _guarded(self, job: Callable[[Path], None], manifest_path: Path) -> None:
        try:
            job(manifest_path)
        except Exception:
            log.exception("VAD job for %s ended unexpectedly", manifest_path)

    def _pass(self, manifest_path: Path) -> None:
        session = self._eligible_or_log(manifest_path)
        if session is None:
            return
        status = self._addon.status()
        if status is not AddonStatus.READY:
            record = VadRecord(state=VadState.UNAVAILABLE, message=_UNAVAILABLE[status])
            self._settle(manifest_path, session, record, None)
            return
        if not self._update(manifest_path, lambda m: replace(m, state=ManifestState.VAD_RUNNING)):
            return
        try:
            regions = self._run_worker(manifest_path, session.video)
        except _CancelledError:
            log.info("VAD pass for %s stopped; it stays queued", manifest_path)
            self._update(manifest_path, lambda m: replace(m, state=ManifestState.READY))
            return
        except _PassFailedError as exc:
            log.warning("VAD pass for %s failed: %s", manifest_path, exc)
            self._settle(manifest_path, session, VadRecord(VadState.FAILED, model=MODEL_NAME, message=str(exc)), None)
            return
        live = [cue.to_cue() for cue in session.manifest.live_cues]
        cues = assign(live, regions, session.manifest.text_mode, self._config().cue)
        trimmed, no_speech = _speech_counts(cues, regions)
        record = VadRecord(VadState.DONE, model=MODEL_NAME, trimmed=trimmed, no_speech=no_speech)
        self._settle(manifest_path, session, record, cues)

    def _restore(self, manifest_path: Path) -> None:
        session = self._eligible_or_log(manifest_path)
        if session is not None:
            self._settle(manifest_path, session, VadRecord(state=VadState.RESTORED), None)

    def _settle(self, manifest_path: Path, session: _Session, record: VadRecord, cues: Sequence[Cue] | None) -> None:
        """Write the subtitle (``cues``, or the live cues), then ``vad`` and ``ready``; tell the presenter."""
        try:
            write_srt_atomic(
                session.subtitle, cues if cues is not None else [c.to_cue() for c in session.manifest.live_cues]
            )
        except OSError as exc:
            log.warning("VAD job for %s could not write %s: %s", manifest_path, session.subtitle, exc)
            record = VadRecord(VadState.FAILED, model=record.model, message=f"The subtitle could not be written: {exc}")
        if self._update(manifest_path, lambda m: replace(m, state=ManifestState.READY, vad=record)):
            log.info("VAD job for %s: %s", manifest_path, record.state)
            self._presenter.vad_finished(manifest_path, record.state)

    def _update(self, manifest_path: Path, change: Callable[[SessionManifest], SessionManifest]) -> bool:
        """Apply ``change`` to the manifest on disk; false (logged) when it cannot be read or written."""
        try:
            with self._manifest_lock:
                write_manifest_atomic(manifest_path, change(load_manifest(manifest_path)))
        except (FileNotFoundError, StoreError) as exc:
            log.warning("VAD job could not update %s: %s", manifest_path, exc)
            return False
        return True

    def _eligible_or_log(self, manifest_path: Path) -> _Session | None:
        try:
            return self._eligible(manifest_path)
        except StoreError as exc:
            log.warning("VAD job for %s skipped: %s", manifest_path, exc)
            return None

    @staticmethod
    def _eligible(manifest_path: Path) -> _Session | None:
        """The session when it is placed in its game folder with cues and a subtitle; ``None`` otherwise.

        A missing manifest is ``None``; one that cannot be read raises ``StoreError``.
        """
        try:
            manifest = load_manifest(manifest_path)
        except FileNotFoundError:
            log.warning("VAD job for %s skipped: no such manifest", manifest_path)
            return None
        files = manifest.files
        placed = manifest.state in (ManifestState.READY, ManifestState.VAD_RUNNING)
        if placed and files is not None and files.subtitle and manifest.live_cues:
            return _Session(manifest, manifest_path.parent / files.video, manifest_path.parent / files.subtitle)
        log.info("VAD job for %s skipped: state %s, no subtitle to trim", manifest_path, manifest.state)
        return None

    # --- the worker ---

    def _run_worker(self, manifest_path: Path, video: Path) -> list[Region]:
        """Run the worker over ``video``; its regions once it reports ``done`` and exits 0."""
        cmd = [
            str(self._addon.python_path),
            "-I",  # the user's PYTHONPATH, PYTHONHOME or user site-packages never reach the add-on
            str(self._worker_script),
            "--video",
            str(video),
            "--model",
            str(self._addon.model_path),
        ]
        with tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, creationflags=_NO_WINDOW
                )
            except OSError as exc:
                raise _PassFailedError(f"The VAD worker could not start: {exc}") from exc
            with self._lock:
                self._process = process
                if self._closed:
                    process.kill()
            try:
                assert process.stdout is not None
                regions, done, error = self._read(process.stdout, manifest_path)
            except BaseException:
                process.kill()
                raise
            finally:
                code = process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                with self._lock:
                    self._process = None
                    closed = self._closed
            if closed:
                raise _CancelledError
            if error is not None:
                raise _PassFailedError(f"The VAD worker failed: {error}")
            if code != 0:
                stderr.seek(0)
                detail = stderr.read().decode("utf-8", "replace").strip()[-600:]
                raise _PassFailedError(f"The VAD worker exited with code {code}" + (f": {detail}" if detail else ""))
            if not done:
                raise _PassFailedError("The VAD worker stopped before finishing")
            return regions

    def _read(self, stdout: IO[bytes], manifest_path: Path) -> tuple[list[Region], bool, str | None]:
        """The worker's regions, whether it said ``done``, and its ``error`` message (spec 13.2)."""
        regions: list[Region] = []
        done = False
        error: str | None = None
        for raw in stdout:
            message = _parse(raw)
            kind = message.get("t")
            if kind == "region":
                regions.append(_region(message, raw))
            elif kind == "progress":
                done_ms, total_ms = message.get("done_ms"), message.get("total_ms")
                if _is_int(done_ms) and (total_ms is None or _is_int(total_ms)):
                    self._presenter.vad_progress(manifest_path, done_ms, total_ms)
            elif kind == "done":
                done = True
            elif kind == "error":
                error = str(message.get("message", "no message"))
        return regions, done, error


def _parse(raw: bytes) -> dict[str, Any]:
    """One protocol line as a dict; anything else (a library printing to stdout) as ``{}``, ignored."""
    try:
        message = json.loads(raw)
    except ValueError:
        log.debug("VAD worker line ignored: %r", raw)
        return {}
    return message if isinstance(message, dict) else {}


def _region(message: dict[str, Any], raw: bytes) -> Region:
    start, end = message.get("start_ms"), message.get("end_ms")
    if not (_is_int(start) and _is_int(end) and 0 <= start < end):
        raise _PassFailedError(f"The VAD worker sent an invalid region: {raw.decode('ascii', 'replace').strip()}")
    return Region(start_ms=start, end_ms=end)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _speech_counts(cues: Sequence[Cue], regions: Sequence[Region]) -> tuple[int, int]:
    """``(trimmed, no_speech)``: how many cues' spans overlap a region, and how many overlap none."""
    ordered = sorted(regions, key=lambda r: r.start_ms)
    starts = [r.start_ms for r in ordered]
    latest_end = list(itertools.accumulate((r.end_ms for r in ordered), max))
    voiced = 0
    for cue in cues:
        before_end = bisect_left(starts, cue.end_ms)  # regions that start before the cue ends
        if before_end and latest_end[before_end - 1] > cue.start_ms:
            voiced += 1
    return voiced, len(cues) - voiced
