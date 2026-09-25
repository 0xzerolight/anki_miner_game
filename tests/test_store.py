import dataclasses
import json
import os
import stat
import sys

import pytest

from anki_miner_game import paths, store
from anki_miner_game.models.codec import dump_document
from anki_miner_game.models.config import AppConfig, CueSettings, ObsSettings
from anki_miner_game.models.profile import AudioMode, AudioSettings, GameProfile, TextMode


def _profile(slug: str = "steins-gate") -> GameProfile:
    return GameProfile(slug=slug, title="Steins;Gate", audio=AudioSettings(mode=AudioMode.DESKTOP))


def _write_config(text: str) -> None:
    paths.home().mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(text, encoding="utf-8")


def test_load_config_without_a_file_returns_defaults():
    assert store.load_config() == AppConfig()


def test_save_config_round_trips_with_schema():
    cfg = AppConfig(last_game="steins-gate", cue=CueSettings(max_cue_seconds=20))
    path = store.save_config(cfg)
    assert path == paths.home() / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1
    assert store.load_config() == cfg


def test_store_follows_the_home_env_var_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "elsewhere"))
    path = store.save_config(AppConfig(last_game="x"))
    assert path == tmp_path / "elsewhere" / "config.json"
    assert store.load_config().last_game == "x"


def test_save_leaves_no_temporary_file():
    store.save_config(AppConfig())
    store.save_config(AppConfig(last_game="again"))
    assert [p.name for p in paths.home().iterdir()] == ["config.json"]


def test_failed_replace_keeps_the_old_file_and_removes_the_temporary(monkeypatch):
    store.save_config(AppConfig(last_game="old"))
    before = paths.config_path().read_bytes()

    def fail_replace(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(store.StoreWriteError, match="disk gone") as info:
        store.save_config(AppConfig(last_game="new"))
    assert info.value.path == paths.config_path()
    assert isinstance(info.value.__cause__, OSError)
    assert paths.config_path().read_bytes() == before
    assert [p.name for p in paths.home().iterdir()] == ["config.json"]


def test_saved_text_is_utf8_with_lf_line_ends():
    store.save_config(AppConfig(output_root="~/ビデオ"))
    raw = paths.config_path().read_bytes()
    assert b"\r\n" not in raw
    assert "ビデオ".encode() in raw


def test_password_override_is_stored_only_when_set():
    store.save_config(AppConfig())
    assert json.loads(paths.config_path().read_text(encoding="utf-8"))["obs"]["password_override"] is None
    store.save_config(AppConfig(obs=ObsSettings(password_override="typed")))
    assert store.load_config().obs.password_override == "typed"


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"last_game": null}',
        '{"schema": "1"}',
        '{"schema": 1, "last_game": 5}',
        '{"schema": 1, "cue": {"max_cue_seconds": 99}}',
    ],
)
def test_corrupt_config_raises_corrupt_file_error(text):
    _write_config(text)
    with pytest.raises(store.CorruptFileError) as info:
        store.load_config()
    assert info.value.path == paths.config_path()


def test_non_utf8_config_is_corrupt():
    paths.home().mkdir(parents=True, exist_ok=True)
    paths.config_path().write_bytes(b'{"schema": 1, "last_game": "\xff"}')
    with pytest.raises(store.CorruptFileError, match="not UTF-8"):
        store.load_config()


def test_future_schema_config_raises_future_schema_error():
    _write_config('{"schema": 2, "brand_new": true}')
    with pytest.raises(store.FutureSchemaError) as info:
        store.load_config()
    assert info.value.found == 2
    assert info.value.path == paths.config_path()
    assert isinstance(info.value, store.StoreError)


def test_save_profile_writes_slug_json_with_schema():
    path = store.save_profile(_profile())
    assert path == paths.home() / "games" / "steins-gate.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_profiles_round_trip_and_bad_files_are_reported():
    first = _profile("a")
    second = dataclasses.replace(_profile("b"), text_mode=TextMode.OCR)
    store.save_profile(first)
    store.save_profile(second)
    (paths.games_dir() / "broken.json").write_text("{", encoding="utf-8")
    (paths.games_dir() / "future.json").write_text('{"schema": 2}', encoding="utf-8")
    loaded = store.load_profiles()
    assert loaded.profiles == {"a": first, "b": second}
    assert sorted((type(e).__name__, e.path.name) for e in loaded.errors) == [
        ("CorruptFileError", "broken.json"),
        ("FutureSchemaError", "future.json"),
    ]


def test_load_profiles_without_a_games_folder_is_empty():
    loaded = store.load_profiles()
    assert dict(loaded.profiles) == {}
    assert loaded.errors == ()


def test_save_profile_refuses_an_invalid_profile():
    bad = dataclasses.replace(_profile(), text_mode=TextMode.OCR, clipboard=True)
    with pytest.raises(store.InvalidProfileError) as info:
        store.save_profile(bad)
    assert info.value.problems == ("OCR mode cannot use the clipboard",)
    assert not paths.games_dir().exists()


def test_unreadable_config_is_a_store_error():
    paths.config_path().mkdir(parents=True)
    with pytest.raises(store.StoreError) as info:
        store.load_config()
    assert info.value.path == paths.config_path()


def test_load_profiles_reports_an_unreadable_file():
    store.save_profile(_profile("a"))
    (paths.games_dir() / "folder.json").mkdir()
    loaded = store.load_profiles()
    assert list(loaded.profiles) == ["a"]
    assert [(type(e).__name__, e.path.name) for e in loaded.errors] == [("StoreError", "folder.json")]


def test_load_profiles_rejects_a_slug_that_differs_from_the_file_name_or_cannot_be_one():
    first = _profile("a")
    store.save_profile(first)
    (paths.games_dir() / "zz.json").write_text(dump_document(_profile("a")), encoding="utf-8")
    evil = dataclasses.replace(_profile("x"), slug="../evil")
    (paths.games_dir() / "evil.json").write_text(dump_document(evil), encoding="utf-8")
    # "a." matches its file name "a..json" but save_profile and profile_path refuse it (trailing dot).
    unsafe = dataclasses.replace(_profile("x"), slug="a.")
    (paths.games_dir() / "a..json").write_text(dump_document(unsafe), encoding="utf-8")
    loaded = store.load_profiles()
    assert loaded.profiles == {"a": first}
    assert sorted((type(e).__name__, e.path.name, str(e).split(": ", 1)[1]) for e in loaded.errors) == [
        ("CorruptFileError", "a..json", "slug 'a.' cannot be a file name"),
        ("CorruptFileError", "evil.json", "slug '../evil' does not match the file name"),
        ("CorruptFileError", "zz.json", "slug 'a' does not match the file name"),
    ]


def test_write_text_atomic_creates_the_folder_and_keeps_lf(tmp_path):
    target = tmp_path / "new" / "x.srt"
    store.write_text_atomic(target, "1\n00:00:00,000 --> 00:00:01,000\nはい\n")
    assert target.read_bytes() == "1\n00:00:00,000 --> 00:00:01,000\nはい\n".encode()
    assert [p.name for p in target.parent.iterdir()] == ["x.srt"]


def test_write_text_atomic_keeps_the_temp_name_short_regardless_of_the_target_name(tmp_path, monkeypatch):
    """S3-2: a deep output folder can put the final path within a few characters of Windows's 260-char
    limit; a temp name built from the target's own (long) name pushed a still-valid final path over
    that limit. The temp name must stay short no matter how long the target name is."""
    target = tmp_path / ("Deep Path Test Game XY - 01" * 3 + ".srt")  # a long stem, short suffix
    limit = len(str(target)) + 10  # room for the final path; not for a temp name derived from it
    real_open = os.open

    def guarded_open(path, *args, **kwargs):
        if len(os.fspath(path)) > limit:
            raise OSError(2, "No such file or directory")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    store.write_text_atomic(target, "content")
    assert target.read_text(encoding="utf-8") == "content"


def test_write_text_atomic_fails_at_the_first_refusal_in_a_folder_windows_denies(tmp_path, monkeypatch):
    """S5-4: Windows's ``os.access`` calls any folder writable (it reads only the read-only attribute),
    so ``tempfile`` took the ``PermissionError`` of a folder whose ACL denies writing for a name clash
    and retried it 2**31 times: the app hung. The temp file is one exclusive create per name."""
    calls: list[str] = []

    def denied(path, *args, **kwargs):
        calls.append(os.fspath(path))
        if len(calls) > 3:
            raise AssertionError("a PermissionError was retried")
        raise PermissionError(13, "Access is denied", os.fspath(path))

    monkeypatch.setattr(os, "name", "nt")  # with os.access(tmp_path, W_OK) true, as Windows says of any folder
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PermissionError):
        store.write_text_atomic(tmp_path / "x.srt", "1\n")
    assert len(calls) == 1


def test_write_text_atomic_takes_another_temp_name_when_one_is_taken(tmp_path, monkeypatch):
    real_open = os.open
    names: list[str] = []

    def taken_twice(path, *args, **kwargs):
        names.append(os.path.basename(path))
        if len(names) <= 2:
            raise FileExistsError(17, "File exists", os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", taken_twice)
    target = tmp_path / "x.srt"
    store.write_text_atomic(target, "1\n")
    assert target.read_text(encoding="utf-8") == "1\n"
    assert len(set(names)) == 3
    assert all(len(name) == len(".12345678.tmp") and name.startswith(".") and name.endswith(".tmp") for name in names)
    assert [p.name for p in tmp_path.iterdir()] == ["x.srt"]


def test_write_text_atomic_gives_up_after_a_few_taken_temp_names(tmp_path, monkeypatch):
    calls: list[str] = []

    def always_taken(path, *args, **kwargs):
        calls.append(os.fspath(path))
        if len(calls) > 100:
            raise AssertionError("no bound on the retries")
        raise FileExistsError(17, "File exists", os.fspath(path))

    monkeypatch.setattr(os, "open", always_taken)
    with pytest.raises(FileExistsError):
        store.write_text_atomic(tmp_path / "x.srt", "1\n")
    assert len(calls) <= 10


posix_non_root = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions that bind the user"
)


@posix_non_root
def test_saving_into_an_unwritable_folder_is_a_store_error():
    games = paths.games_dir()
    games.mkdir(parents=True)
    paths.home().chmod(0o500)
    games.chmod(0o500)
    try:
        with pytest.raises(store.StoreWriteError) as config_info:
            store.save_config(AppConfig())
        with pytest.raises(store.StoreWriteError) as profile_info:
            store.save_profile(_profile())
    finally:
        games.chmod(0o700)
        paths.home().chmod(0o700)
    assert config_info.value.path == paths.config_path()
    assert profile_info.value.path == paths.profile_path("steins-gate")
    assert isinstance(profile_info.value, store.StoreError)
    assert "cannot be written" in str(profile_info.value)


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
@pytest.mark.parametrize(
    ("umask", "expected"), [(0o022, 0o644), (0o027, 0o640), (0o002, 0o664)], ids=["022", "027", "002"]
)
def test_write_text_atomic_gives_the_umask_mode_not_mkstemps_0600(tmp_path, monkeypatch, umask, expected):
    monkeypatch.setattr(store, "_UMASK", umask)
    target = tmp_path / "x.srt"
    store.write_text_atomic(target, "1\n")
    assert _mode(target) == expected


@posix_only
def test_write_text_atomic_takes_an_explicit_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_UMASK", 0o022)
    target = tmp_path / "secret.json"
    store.write_text_atomic(target, "{}", mode=0o600)
    assert _mode(target) == 0o600


@posix_only
def test_config_stays_private_and_profiles_follow_the_umask(monkeypatch):
    monkeypatch.setattr(store, "_UMASK", 0o022)
    assert _mode(store.save_config(AppConfig())) == 0o600
    assert _mode(store.save_profile(_profile())) == 0o644


@posix_only
def test_the_umask_is_read_as_the_process_has_it():
    current = os.umask(0o022)
    os.umask(current)
    assert store._read_umask() == current


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/status is Linux")
def test_the_umask_is_read_without_changing_it_on_linux(monkeypatch):
    """Setting the umask to read it would race with other threads creating files."""

    def no_umask_change(_mask):
        raise AssertionError("os.umask called")

    monkeypatch.setattr(store.os, "umask", no_umask_change)
    assert isinstance(store._read_umask(), int)


def test_a_utf8_bom_is_not_corruption():
    """Windows Notepad offers "UTF-8 with BOM"; one hand edit must not turn every setting into a banner."""
    cfg = AppConfig(last_game="steins-gate")
    paths.home().mkdir(parents=True, exist_ok=True)
    paths.config_path().write_bytes(b"\xef\xbb\xbf" + dump_document(cfg).encode())
    assert store.load_config() == cfg
    profile = _profile()
    paths.games_dir().mkdir()
    paths.profile_path(profile.slug).write_bytes(b"\xef\xbb\xbf" + dump_document(profile).encode())
    loaded = store.load_profiles()
    assert loaded.profiles == {profile.slug: profile}
    assert loaded.errors == ()
