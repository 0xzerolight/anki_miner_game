"""Session manifest files (spec 5, 10.2; 18.1 row ``session/journal.py`` + finalise: NN reservation)."""

from dataclasses import replace
from pathlib import Path

import pytest

from anki_miner_game.models.manifest import GameRef, ManifestState, ObsRecord, SessionManifest
from anki_miner_game.models.profile import TextMode
from anki_miner_game.session.manifest import (
    IncomingFiles,
    game_folder,
    incoming_files,
    load_manifest,
    reserve_index,
    write_manifest_atomic,
)
from anki_miner_game.store import CorruptFileError, FutureSchemaError, StoreWriteError

TITLE = "Steins;Gate"
MANIFEST = SessionManifest(
    app_version="0.1.0",
    game=GameRef(slug="steins-gate", title=TITLE),
    index=3,
    state=ManifestState.RECORDING,
    started_at="2026-10-02T18:04:11Z",
    obs=ObsRecord(
        version="31.0.2",
        websocket="5.5.4",
        profile="Anki Miner Game",
        collection="Anki Miner Game",
        output_path="/v/_incoming/2026-10-02 18-04-11.mkv",
    ),
    text_mode=TextMode.HOOK,
)


def test_a_manifest_round_trips_through_its_file(tmp_path):
    path = tmp_path / "2026-10-02 18-04-11.session.json"
    write_manifest_atomic(path, MANIFEST)
    assert load_manifest(path) == MANIFEST
    assert path.read_text(encoding="utf-8").startswith('{\n  "schema": 1,\n')
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_a_missing_manifest_is_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "x.session.json")


@pytest.mark.parametrize(
    ("text", "error"),
    [("{not json", CorruptFileError), ('{"schema": 1}', CorruptFileError), ('{"schema": 2}', FutureSchemaError)],
)
def test_an_unusable_manifest_is_a_store_error(tmp_path, text, error):
    path = tmp_path / "x.session.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(error):
        load_manifest(path)


def test_a_manifest_that_cannot_be_written_is_a_store_write_error(tmp_path):
    (tmp_path / "blocker").write_text("a file where the folder should be", encoding="utf-8")
    with pytest.raises(StoreWriteError):
        write_manifest_atomic(tmp_path / "blocker" / "x.session.json", MANIFEST)


@pytest.mark.parametrize(
    "output_path",
    [
        "/home/u/Videos/Anki Miner Game/_incoming/2026-10-02 18-04-11.mkv",
        "C:\\Users\\u\\Videos\\Anki Miner Game\\_incoming\\2026-10-02 18-04-11.mkv",
        "2026-10-02 18-04-11.mkv",
    ],
)
def test_incoming_files_are_named_after_the_obs_file(tmp_path, output_path):
    assert incoming_files(tmp_path, output_path) == IncomingFiles(
        video=tmp_path / "2026-10-02 18-04-11.mkv",
        subtitle=tmp_path / "2026-10-02 18-04-11.srt",
        journal=tmp_path / "2026-10-02 18-04-11.lines.jsonl",
        manifest=tmp_path / "2026-10-02 18-04-11.session.json",
    )


def test_the_game_folder_is_the_sanitised_title_beside_incoming(tmp_path):
    assert game_folder(tmp_path / "_incoming", "Fate/stay night") == tmp_path / "Fate stay night"


def _touch(folder: Path, names: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"")


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], 1),
        (["Steins;Gate - 01.mkv", "Steins;Gate - 03.mkv"], 4),  # files only
        (["Steins;Gate - 02.session.json"], 3),  # manifests only (the user deleted the video)
        (["Steins;Gate - 02.mkv", "Steins;Gate - 02.srt", "Steins;Gate - 05.session.json"], 6),  # both
        (["Steins;Gate - 08.MKV"], 9),
        (["Steins;Gate - 07.srt", "Steins;Gate - 12.mp4", "notes - 40.txt", "Steins;Gate.mkv", "cover 2.mkv"], 1),
    ],
)
def test_nn_is_one_more_than_the_highest_in_the_game_folder(tmp_path, names, expected):
    _touch(tmp_path / TITLE, names)
    assert reserve_index(tmp_path / TITLE, tmp_path / "_incoming", "steins-gate") == expected


def test_nn_counts_manifests_of_the_same_game_in_incoming(tmp_path):
    _touch(tmp_path / TITLE, ["Steins;Gate - 03.mkv"])
    incoming = tmp_path / "_incoming"
    write_manifest_atomic(incoming / "a.session.json", replace(MANIFEST, index=7))
    write_manifest_atomic(incoming / "b.session.json", replace(MANIFEST, index=5))
    other = replace(MANIFEST, game=GameRef(slug="chaos-head", title="Chaos;Head"), index=12)
    write_manifest_atomic(incoming / "c.session.json", other)
    (incoming / "d.session.json").write_text("{torn", encoding="utf-8")
    _touch(incoming, ["2026-10-02 18-04-11.mkv", "2026-10-02 18-04-11.lines.jsonl"])
    assert reserve_index(tmp_path / TITLE, incoming, "steins-gate") == 8
