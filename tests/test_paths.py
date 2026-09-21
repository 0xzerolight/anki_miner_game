from pathlib import Path

import pytest

from anki_miner_game import paths
from anki_miner_game.models.config import AppConfig


def test_home_follows_the_env_var_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "one"))
    assert paths.home() == tmp_path / "one"
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "two"))
    assert paths.home() == tmp_path / "two"


@pytest.mark.parametrize("value", [None, ""])
def test_home_defaults_to_dot_anki_miner_game(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANKI_MINER_GAME_HOME", raising=False)
    else:
        monkeypatch.setenv("ANKI_MINER_GAME_HOME", value)
    assert paths.home() == Path.home() / ".anki_miner_game"


def test_home_is_not_created(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "absent"))
    paths.home()
    paths.config_path()
    paths.profile_path("g")
    assert not (tmp_path / "absent").exists()


def test_files_live_under_home():
    assert paths.config_path() == paths.home() / "config.json"
    assert paths.games_dir() == paths.home() / "games"
    assert paths.profile_path("steins-gate") == paths.home() / "games" / "steins-gate.json"


@pytest.mark.parametrize("slug", ["", "..", ".hidden", "a/b", "a\\b", "c:d", "CON", "a?b"])
def test_profile_path_refuses_unsafe_slugs(slug):
    with pytest.raises(ValueError, match="cannot be a file name"):
        paths.profile_path(slug)


def test_output_root_expands_home_and_holds_incoming():
    cfg = AppConfig()
    assert paths.output_root(cfg) == Path.home() / "Videos" / "Anki Miner Game"
    assert paths.incoming_dir(cfg) == Path.home() / "Videos" / "Anki Miner Game" / "_incoming"


def test_output_root_keeps_an_absolute_path(tmp_path):
    cfg = AppConfig(output_root=str(tmp_path / "rec"))
    assert paths.output_root(cfg) == tmp_path / "rec"
