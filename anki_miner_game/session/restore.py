"""``obs_restore.json``: the OBS profile and scene collection to switch back to (spec 6.2).

Arming writes the user's current names here before it switches OBS to the app's own profile and
collection; disarm switches back and deletes the file. A file still present at launch means the app
exited while armed, and the session actor restores it at the first connection that allows it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from anki_miner_game import paths
from anki_miner_game.models.codec import dump_document
from anki_miner_game.store import _read, _write

RESTORE_FILENAME: Final = "obs_restore.json"


@dataclass(frozen=True, kw_only=True)
class ObsRestore:
    profile: str
    """``GetProfileList.currentProfileName`` before arming."""
    collection: str
    """``GetSceneCollectionList.currentSceneCollectionName`` before arming."""


def restore_path() -> Path:
    """``<home>/obs_restore.json``, read from ``paths.home()`` at call time."""
    return paths.home() / RESTORE_FILENAME


def load_restore(path: Path) -> ObsRestore | None:
    """The saved names, or ``None`` when there is no file.

    Raises ``store.CorruptFileError`` or ``store.FutureSchemaError`` when the file cannot be used and
    ``store.StoreError`` when it cannot be read.
    """
    try:
        return _read(ObsRestore, path)
    except FileNotFoundError:
        return None


def save_restore(path: Path, saved: ObsRestore) -> None:
    """Write through a temporary file and ``os.replace``; ``store.StoreWriteError`` leaves no file behind."""
    _write(path, dump_document(saved))


def delete_restore(path: Path) -> None:
    path.unlink(missing_ok=True)
