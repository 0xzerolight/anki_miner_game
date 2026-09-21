"""The Silero model pin (spec 13.1): one file, one immutable URL, one sha256."""

import re
from urllib.parse import urlsplit

from anki_miner_game.vad import model_pin


def test_url_is_https_on_the_raw_github_host():
    parts = urlsplit(model_pin.MODEL_URL)
    assert parts.scheme == "https"
    assert parts.hostname == "raw.githubusercontent.com"
    assert not parts.query and not parts.fragment


def test_url_names_the_pinned_commit_not_a_branch_or_tag():
    assert re.fullmatch(r"[0-9a-f]{40}", model_pin.MODEL_SOURCE_COMMIT)
    assert urlsplit(model_pin.MODEL_URL).path == (
        f"/SYSTRAN/faster-whisper/{model_pin.MODEL_SOURCE_COMMIT}/faster_whisper/assets/{model_pin.MODEL_FILENAME}"
    )


def test_filename_and_digest():
    assert model_pin.MODEL_FILENAME == "silero_vad_v6.onnx"
    assert re.fullmatch(r"[0-9a-f]{64}", model_pin.MODEL_SHA256)
    assert model_pin.MODEL_SIZE_BYTES > 0
