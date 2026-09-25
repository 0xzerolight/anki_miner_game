import pytest

from anki_miner_game.models import constants


def test_spec_values():
    assert constants.SCHEMA == 1
    assert constants.SKIP_MS == 300
    assert constants.MIN_CUE_MS == 500
    assert constants.MAX_LINE_CHARS == 300
    assert constants.TYPEWRITER_WINDOW_S == 2.0
    assert (constants.MAX_CUE_SECONDS_MIN, constants.MAX_CUE_SECONDS_MAX) == (5, 60)
    assert constants.OBS_PROFILE_NAME == "Anki Miner Game"
    assert constants.OBS_COLLECTION_NAME == "Anki Miner Game"
    assert constants.OBS_SCENE_NAME == "Game"
    assert constants.INCOMING_DIRNAME == "_incoming"


def test_start_shift_values():
    assert dict(constants.START_SHIFT_MS) == {"hook": -400, "ocr": -1250}


def test_start_shift_is_read_only():
    with pytest.raises(TypeError):
        constants.START_SHIFT_MS["hook"] = 5  # type: ignore[index]
