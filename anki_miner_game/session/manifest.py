"""Session files on disk (spec 5, 10): names in ``_incoming/``, the game folder, manifest I/O, and NN.

While recording, a session's files sit in ``_incoming/`` named after OBS's file stem (spec 10.2);
finalise moves them to ``<output root>/<sanitised title>/<title> - NN.*`` (spec 10.3).
"""

from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Final

from anki_miner_game.models.manifest import SessionManifest, to_json
from anki_miner_game.session.naming import parse_index, sanitise_title
from anki_miner_game.store import StoreError, _read, _write

MANIFEST_SUFFIX: Final = ".session.json"
JOURNAL_SUFFIX: Final = ".lines.jsonl"
SUBTITLE_SUFFIX: Final = ".srt"
VIDEO_SUFFIX: Final = ".mkv"
"""The container the app's OBS profile records to (spec 10.3: ``.mkv`` survives an OBS crash)."""


@dataclass(frozen=True)
class IncomingFiles:
    """One session's files in ``_incoming/``, all named after OBS's file stem (spec 10.2)."""

    video: Path
    subtitle: Path
    journal: Path
    manifest: Path


def incoming_files(incoming: Path, output_path: str) -> IncomingFiles:
    """The files of the recording OBS reported as ``output_path`` (``RecordStateChanged.outputPath``).

    Only the file name of ``output_path`` is used, split on ``/`` and ``\\`` alike, so a Windows path
    or a Flatpak sandbox path names the same files inside ``incoming``.
    """
    video = PureWindowsPath(output_path).name  # PureWindowsPath treats both separators as separators
    stem = PurePath(video).stem
    return IncomingFiles(
        video=incoming / video,
        subtitle=incoming / f"{stem}{SUBTITLE_SUFFIX}",
        journal=incoming / f"{stem}{JOURNAL_SUFFIX}",
        manifest=incoming / f"{stem}{MANIFEST_SUFFIX}",
    )


def game_folder(incoming: Path, title: str) -> Path:
    """``<output root>/<sanitised title>``: the output root is the folder that holds ``incoming``."""
    return incoming.parent / sanitise_title(title)


def load_manifest(path: Path) -> SessionManifest:
    """Read a manifest.

    Raises ``FileNotFoundError`` when there is none, ``store.CorruptFileError`` or
    ``store.FutureSchemaError`` when it cannot be used, and ``store.StoreError`` when it cannot be read.
    """
    return _read(SessionManifest, path)


def write_manifest_atomic(path: Path, manifest: SessionManifest) -> None:
    """Write through a temporary file and ``os.replace``; ``store.StoreWriteError`` leaves the old file."""
    _write(path, to_json(manifest))


def reserve_index(game_dir: Path, incoming: Path, slug: str) -> int:
    """NN for a session that starts now (spec 10.2): 1 + the highest index already taken, else 1.

    Taken: every ``.mkv`` and every manifest in ``game_dir`` (NN read from the ``<title> - NN`` name),
    and every manifest in ``incoming`` whose game is ``slug`` (NN read from the manifest). A manifest
    in ``incoming`` that cannot be read is skipped. Either folder may be missing.
    """
    taken = [0]
    for path in _entries(game_dir):
        if path.name.endswith(MANIFEST_SUFFIX):
            index = parse_index(path.name.removesuffix(MANIFEST_SUFFIX))
        elif path.suffix.lower() == VIDEO_SUFFIX:
            index = parse_index(path.stem)
        else:
            continue
        if index is not None:
            taken.append(index)
    for path in _entries(incoming):
        if not path.name.endswith(MANIFEST_SUFFIX):
            continue
        try:
            manifest = load_manifest(path)
        except (FileNotFoundError, StoreError):  # gone meanwhile (finalised), or unreadable
            continue
        if manifest.game.slug == slug:
            taken.append(manifest.index)
    return max(taken) + 1


def _entries(folder: Path) -> list[Path]:
    try:
        return list(folder.iterdir())
    except FileNotFoundError:
        return []
