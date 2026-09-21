"""Finalise (spec 10.3): a stopped session in ``_incoming/`` becomes ``<Game>/<Game> - NN.*``.

One routine for a normal stop, for reconcile and at launch. It moves the manifest's ``state``
forward in two phases, and each step first checks whether it has already happened, so a crash
between any two file operations is repaired by running it again:

- ``recording``: read the journal, build the cues, write ``<obs stem>.srt`` and the manifest
  (``live_cues``, counts, ``no_cues``, ``stopped_at``) as ``finalise_pending``; delete the journal.
  From here on the manifest alone holds what the journal held.
- ``finalise_pending``: move the video into the game folder, write the subtitle and the manifest
  there (``ready``, ``files``) from the manifest, delete them from ``_incoming/``.

A manifest in ``_incoming/`` is never ``ready``, so a ``ready`` one has already been placed.
"""

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import START_SHIFT_MS
from anki_miner_game.models.manifest import FilesRecord, Flag, LiveCue, ManifestState, SessionManifest
from anki_miner_game.session.cues import build_cues
from anki_miner_game.session.journal import JournalRecord, ReplaceRecord, StopRecord, read_journal, timed_lines
from anki_miner_game.session.manifest import (
    MANIFEST_SUFFIX,
    SUBTITLE_SUFFIX,
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    write_manifest_atomic,
)
from anki_miner_game.session.naming import session_stem
from anki_miner_game.session.srt_writer import write_srt_atomic
from anki_miner_game.store import StoreError


class FinaliseError(Exception):
    """Finalise cannot go on: the manifest is missing or unusable, a file cannot be written, or the
    video is gone. The files stay as the last completed step left them; running it again resumes."""

    def __init__(self, manifest_path: Path, message: str) -> None:
        super().__init__(message)
        self.manifest_path = manifest_path


@dataclass(frozen=True)
class FinaliseResult:
    manifest_path: Path
    """Where the manifest is now: the game folder when ``ready``, ``_incoming/`` when ``finalise_pending``."""
    manifest: SessionManifest
    queue_vad: bool
    """Queue the VAD pass (``VadJobs.queue(manifest_path)``): true only from the run that placed a
    session with a subtitle, and only when ``cfg.vad.enabled``."""


def finalise(manifest_path: Path, cfg: AppConfig, *, sleep: Callable[[float], None] = time.sleep) -> FinaliseResult:
    """Finalise the session whose manifest is ``manifest_path``; see the module docstring.

    Blocking file I/O: call it off the I/O loop. Anything that stops it raises ``FinaliseError``.

    Counts: ``accepted`` becomes the number of cues and ``skip`` the journalled lines the skip rule
    dropped; the other counters are kept as the manifest holds them (the actor's pipeline counts).
    """
    try:
        manifest = load_manifest(manifest_path)
        if manifest.state in (ManifestState.READY, ManifestState.VAD_RUNNING):
            return FinaliseResult(manifest_path, manifest, queue_vad=False)
        files = incoming_files(manifest_path.parent, manifest.obs.output_path)
        if manifest.state is ManifestState.RECORDING:
            manifest = _build(manifest_path, manifest, files, cfg)
        files.journal.unlink(missing_ok=True)
        return _place(manifest_path, manifest, files, cfg)
    except (StoreError, OSError, ValueError) as exc:  # ValueError: a hand-edited index or offset
        raise FinaliseError(manifest_path, str(exc)) from exc


def _build(manifest_path: Path, manifest: SessionManifest, files: IncomingFiles, cfg: AppConfig) -> SessionManifest:
    """Steps 1-3: journal -> cues -> ``<obs stem>.srt`` beside the video, manifest ``finalise_pending``."""
    try:  # before anything is written, whatever ``stopped_at`` holds
        video_mtime = files.video.stat().st_mtime
    except FileNotFoundError:
        raise FinaliseError(manifest_path, f"the video {files.video} is gone") from None
    stopped_at = manifest.stopped_at or _utc_stamp(video_mtime)  # the video's last write
    try:
        records = read_journal(files.journal)
    except FileNotFoundError:  # stopped before the actor created the journal
        records = []
    lines = timed_lines(records)
    cues = build_cues(lines, _stop_ms(records, cfg.cue.max_cue_seconds), START_SHIFT_MS[manifest.text_mode], cfg.cue)
    if cues:
        write_srt_atomic(files.subtitle, cues)
    built = replace(
        manifest,
        state=ManifestState.FINALISE_PENDING,
        stopped_at=stopped_at,
        counts=replace(manifest.counts, accepted=len(cues), skip=len(lines) - len(cues)),
        flags=manifest.flags if cues else (*manifest.flags, Flag.NO_CUES),
        live_cues=tuple(LiveCue.from_cue(cue) for cue in cues),
    )
    write_manifest_atomic(manifest_path, built)
    return built


def _stop_ms(records: Sequence[JournalRecord], max_cue_seconds: int) -> int:
    """The first stop record's offset; without one (a crash), the last offset in the journal + the cap.

    Several stop records mean OBS split the recording: the session is finalised against the first
    file (spec 7), and lines after that stop fall outside it.
    """
    stops = [record.offset_ms for record in records if isinstance(record, StopRecord)]
    if stops:
        return stops[0]
    offsets = [record.offset_ms for record in records if not isinstance(record, ReplaceRecord)]
    return (offsets[-1] if offsets else 0) + max_cue_seconds * 1000


def _utc_stamp(timestamp: float) -> str:
    """``started_at``'s format: UTC, whole seconds, ``Z``."""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _place(manifest_path: Path, manifest: SessionManifest, files: IncomingFiles, cfg: AppConfig) -> FinaliseResult:
    """Steps 4-5: video, subtitle and manifest to ``<Game>/<Game> - NN.*``; ``_incoming/`` emptied."""
    folder = game_folder(manifest_path.parent, manifest.game.title)
    video, subtitle, placed = _targets(folder, manifest.game.title, manifest.index, files.video.suffix)
    if files.video.exists():
        folder.mkdir(parents=True, exist_ok=True)
        os.replace(files.video, video)
    elif not video.exists():
        raise FinaliseError(manifest_path, f"the video {files.video} is gone")
    if manifest.live_cues:
        write_srt_atomic(subtitle, [cue.to_cue() for cue in manifest.live_cues])
    files.subtitle.unlink(missing_ok=True)
    ready = replace(
        manifest,
        state=ManifestState.READY,
        files=FilesRecord(video=video.name, subtitle=subtitle.name if manifest.live_cues else None),
    )
    write_manifest_atomic(placed, ready)
    manifest_path.unlink()
    return FinaliseResult(placed, ready, queue_vad=cfg.vad.enabled and bool(ready.live_cues))


def _targets(folder: Path, title: str, index: int, video_suffix: str) -> tuple[Path, Path, Path]:
    """The final video, subtitle and manifest paths for session ``index``."""
    stem = session_stem(title, index)
    return folder / f"{stem}{video_suffix}", folder / f"{stem}{SUBTITLE_SUFFIX}", folder / f"{stem}{MANIFEST_SUFFIX}"
