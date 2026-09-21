"""Load and save ``config.json`` and the game profiles under ``paths.home()`` (spec 5).

Every write goes to a temporary file in the same folder and is moved into place
with ``os.replace``. A file that does not describe its model raises
``CorruptFileError``; one written by a newer app raises ``FutureSchemaError``;
one that cannot be read at all raises ``StoreError``; a save that cannot be
written raises ``StoreWriteError``. All are ``StoreError``s, which callers turn
into banners.
"""

import contextlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from anki_miner_game import paths
from anki_miner_game.models.codec import DecodeError, UnsupportedSchemaError, dump_document, load_document
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.profile import GameProfile, validate


class StoreError(Exception):
    """A stored document cannot be used; ``path`` names the file."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


class CorruptFileError(StoreError):
    """Not UTF-8, not JSON, or not a valid document for its model."""


class FutureSchemaError(StoreError):
    """Written by a newer version of the app."""

    def __init__(self, path: Path, found: int) -> None:
        super().__init__(path, f"schema {found} is newer than this app supports")
        self.found = found


class StoreWriteError(StoreError):
    """The file could not be written (folder not writable, disk full, ...); the old file is kept."""


class InvalidProfileError(ValueError):
    """``save_profile`` refused a profile that ``validate`` rejects."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class LoadedProfiles:
    profiles: Mapping[str, GameProfile]
    """Keyed by slug."""
    errors: tuple[StoreError, ...]
    """One per file that could not be loaded; those files are skipped."""


def load_config() -> AppConfig:
    """The saved config, or the defaults when none has been saved yet."""
    try:
        return _read(AppConfig, paths.config_path())
    except FileNotFoundError:
        return AppConfig()


def save_config(cfg: AppConfig) -> Path:
    """Write ``<home>/config.json``; raises ``StoreWriteError`` when it cannot be written."""
    path = paths.config_path()
    _write(path, dump_document(cfg))
    return path


def load_profiles() -> LoadedProfiles:
    """Every ``<home>/games/*.json``; a file that fails to load is reported in ``errors``, not raised.

    A profile whose ``slug`` differs from its file name is reported as corrupt.
    """
    profiles: dict[str, GameProfile] = {}
    errors: list[StoreError] = []
    folder = paths.games_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            try:
                profile = _read(GameProfile, path)
            except FileNotFoundError:
                continue
            except StoreError as exc:
                errors.append(exc)
                continue
            if profile.slug != path.stem:
                errors.append(CorruptFileError(path, f"slug {profile.slug!r} does not match the file name"))
            else:
                profiles[profile.slug] = profile
    return LoadedProfiles(profiles=profiles, errors=tuple(errors))


def save_profile(profile: GameProfile) -> Path:
    """Write ``<home>/games/<slug>.json``.

    Raises ``InvalidProfileError`` when ``validate`` finds problems and
    ``StoreWriteError`` when the file cannot be written.
    """
    problems = validate(profile)
    if problems:
        raise InvalidProfileError(problems)
    path = paths.profile_path(profile.slug)
    _write(path, dump_document(profile))
    return path


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 (no BOM) with ``\\n`` line ends, all or nothing.

    The text goes to ``.<name>.*.tmp`` in the target folder (created when
    missing), is fsynced, then ``os.replace``-d onto ``path``. On any failure the
    temporary file is removed and an existing ``path`` is left untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _write(path: Path, text: str) -> None:
    try:
        write_text_atomic(path, text)
    except OSError as exc:
        raise StoreWriteError(path, f"cannot be written ({exc.strerror or exc})") from exc


def _read[T](cls: type[T], path: Path) -> T:
    """Load one document; ``FileNotFoundError`` passes through, every other failure is a ``StoreError``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except UnicodeDecodeError as exc:
        raise CorruptFileError(path, f"not UTF-8 ({exc.reason})") from exc
    except OSError as exc:
        raise StoreError(path, f"cannot be read ({exc.strerror or exc})") from exc
    try:
        return load_document(cls, text)
    except UnsupportedSchemaError as exc:
        raise FutureSchemaError(path, exc.found) from exc
    except DecodeError as exc:
        raise CorruptFileError(path, str(exc)) from exc
