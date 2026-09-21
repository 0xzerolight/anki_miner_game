"""Where the app keeps its files. Every function reads the environment at call time; nothing is created here."""

import os
from pathlib import Path

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import INCOMING_DIRNAME
from anki_miner_game.models.profile import is_safe_slug

HOME_ENV = "ANKI_MINER_GAME_HOME"


def home() -> Path:
    """``$ANKI_MINER_GAME_HOME`` when set and non-empty, else ``~/.anki_miner_game``."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".anki_miner_game"


def config_path() -> Path:
    return home() / "config.json"


def games_dir() -> Path:
    return home() / "games"


def profile_path(slug: str) -> Path:
    """``<home>/games/<slug>.json``; ``ValueError`` for a slug that cannot be a file name."""
    if not is_safe_slug(slug):
        raise ValueError(f"slug {slug!r} cannot be a file name")
    return games_dir() / f"{slug}.json"


def output_root(cfg: AppConfig) -> Path:
    """``cfg.output_root`` with ``~`` expanded."""
    return Path(cfg.output_root).expanduser()


def incoming_dir(cfg: AppConfig) -> Path:
    """``<output root>/_incoming``, OBS's record directory in the app's profile (spec 10.2)."""
    return output_root(cfg) / INCOMING_DIRNAME
