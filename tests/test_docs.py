"""The README, the one user-facing doc (master plan T30; docs/ is local-only since the repo took Anki Miner's layout)."""

from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"

_FORBIDDEN = {"—": "em dash", "–": "en dash", "→": "arrow character"}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_plain_punctuation() -> None:
    text = _readme()
    for char, name in _FORBIDDEN.items():
        assert char not in text, f"README.md has an {name}"


@pytest.mark.parametrize(
    "needle",
    [
        "2.78 GB",  # R1 disk rate
        "0.0.0.0",  # owocr's bind address (spec 14)
        "Google Lens",  # cloud OCR note
        "pkill",  # ending an owocr left running by a crash on Linux
        "Video -> Single",  # Anki Miner hand-off (Appendix C)
        "Video -> Batch",
        "Textractor",
        "Agent",
        "LunaTranslator",
        "websocket extension on port 6677",  # what to turn on in each hooker (spec M5 quick start)
        "websocket server on",
        "network service on",
    ],
)
def test_readme_covers(needle: str) -> None:
    assert needle in _readme()
