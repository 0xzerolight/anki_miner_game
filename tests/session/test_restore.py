"""``obs_restore.json`` (spec 6.2)."""

import json

import pytest

from anki_miner_game.session.restore import (
    RESTORE_FILENAME,
    ObsRestore,
    delete_restore,
    load_restore,
    restore_path,
    save_restore,
)
from anki_miner_game.store import CorruptFileError, FutureSchemaError


def test_restore_path_follows_the_isolated_home(_isolate_game_home):
    assert restore_path() == _isolate_game_home / RESTORE_FILENAME


def test_round_trip_carries_the_schema(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    save_restore(path, ObsRestore(profile="Untitled", collection="Streaming"))
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "profile": "Untitled",
        "collection": "Streaming",
    }
    assert load_restore(path) == ObsRestore(profile="Untitled", collection="Streaming")


def test_missing_file_is_none(tmp_path):
    assert load_restore(tmp_path / RESTORE_FILENAME) is None


def test_corrupt_and_future_files_raise(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptFileError):
        load_restore(path)
    path.write_text('{"schema": 2, "profile": "a", "collection": "b"}', encoding="utf-8")
    with pytest.raises(FutureSchemaError):
        load_restore(path)


def test_save_leaves_no_temporary_file(tmp_path):
    save_restore(tmp_path / RESTORE_FILENAME, ObsRestore(profile="a", collection="b"))
    assert sorted(p.name for p in tmp_path.iterdir()) == [RESTORE_FILENAME]


def test_delete_is_idempotent(tmp_path):
    path = tmp_path / RESTORE_FILENAME
    save_restore(path, ObsRestore(profile="a", collection="b"))
    delete_restore(path)
    delete_restore(path)
    assert not path.exists()
