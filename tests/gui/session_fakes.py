"""Session manifests where finalise leaves them, and a ``VadJobs`` that records its calls."""

import os
from pathlib import Path

from anki_miner_game.models.manifest import (
    FilesRecord,
    Flag,
    GameRef,
    LiveCue,
    ManifestState,
    ObsRecord,
    SessionManifest,
    VadRecord,
    to_json,
)
from anki_miner_game.models.profile import TextMode

TITLE = "Steins;Gate"
CUES = (LiveCue(1, 5230, 9410, "はい", "textractor"), LiveCue(2, 9600, 11050, "いいえ", "textractor"))


def manifest(
    index: int = 3,
    *,
    title: str = TITLE,
    state: ManifestState = ManifestState.READY,
    vad: VadRecord | None = None,
    cues: tuple[LiveCue, ...] = CUES,
    started_at: str = "2026-10-02T18:04:11Z",
    stopped_at: str | None = "2026-10-02T19:31:40Z",
) -> SessionManifest:
    stem = f"{title} - {index:02d}"
    placed = state in (ManifestState.READY, ManifestState.VAD_RUNNING)
    return SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug="steins-gate", title=title),
        index=index,
        state=state,
        started_at=started_at,
        stopped_at=stopped_at,
        obs=ObsRecord("32.2.2", "5.7.4", "Anki Miner Game", "Anki Miner Game", f"/out/_incoming/{index}.mkv"),
        text_mode=TextMode.HOOK,
        flags=() if cues else (Flag.NO_CUES,),
        live_cues=cues,
        vad=vad,
        files=FilesRecord(f"{stem}.mkv", f"{stem}.srt" if cues else None) if placed else None,
    )


def place(root: Path, m: SessionManifest, *, age_s: float = 0.0) -> Path:
    """Write ``m`` where finalise leaves it: the game folder, or ``_incoming/`` before it is placed."""
    if m.files is not None:
        path = root / m.game.title / f"{Path(m.files.video).stem}.session.json"
    else:
        path = root / "_incoming" / f"2026-10-02 18-04-{m.index:02d}.session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(m), encoding="utf-8")
    stamp = path.stat().st_mtime - age_s
    os.utime(path, (stamp, stamp))
    return path


class Jobs:
    """``VadJobs`` that records the calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def queue(self, manifest_path: Path) -> None:
        self.calls.append(("queue", manifest_path))

    def rerun(self, manifest_path: Path) -> None:
        self.calls.append(("rerun", manifest_path))

    def restore(self, manifest_path: Path) -> None:
        self.calls.append(("restore", manifest_path))
