"""The user guide, the H5 checklist and the README (master plan T30).

The guide writes this app's own UI labels in bold and nothing else in bold, so every bold span must
still be text the GUI shows: a renamed button fails here instead of leaving the guide stale.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "user-guide.md"
CHECKLIST = ROOT / "docs" / "qa" / "h5-checklist.md"
README = ROOT / "README.md"
GUI = ROOT / "anki_miner_game" / "gui"

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_FORBIDDEN = {"\u2014": "em dash", "\u2013": "en dash", "\u2192": "arrow character"}


def _gui_strings() -> list[str]:
    """Every string constant in the GUI package, f-string pieces included."""
    found: list[str] = []
    for path in GUI.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.append(node.value)
    return found


@pytest.mark.parametrize("path", [GUIDE, CHECKLIST, README], ids=lambda p: p.name)
def test_plain_punctuation(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for char, name in _FORBIDDEN.items():
        assert char not in text, f"{path.name} has an {name}"


def test_bold_spans_are_gui_labels() -> None:
    labels = _gui_strings()
    spans = [" ".join(span.split()) for span in _BOLD.findall(GUIDE.read_text(encoding="utf-8"))]
    assert spans, "the guide names no UI label"
    missing = sorted({span for span in spans if not any(span in label for label in labels)})
    assert not missing, f"bold spans the GUI does not show: {missing}"


@pytest.mark.parametrize(
    "needle",
    [
        "2.78 GB",  # R1 disk rate
        "0.0.0.0",  # owocr's bind address (spec 14)
        "Google Lens",  # cloud OCR note
        "pkill",  # ending an owocr left running by a crash on Linux
        "Video -> Single",  # Anki Miner hand-off (Appendix C)
        "Video -> Batch",
        "Audio Padding",  # Anki Miner settings (Appendix A)
        "Screenshot Offset",
        "Deduplicate by Sentence",
        "Textractor",
        "Agent",
        "LunaTranslator",
    ],
)
def test_guide_covers(needle: str) -> None:
    assert needle in GUIDE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "needle",
    [
        "Video -> Batch",  # spec 18.4
        "Ten cards",
        "RegisterHotKey",  # D2 Windows items
        "rename",
        "wasapi_process_output_capture",
        "PipeWire",
        "Wayland",
        "clipboard",
    ],
)
def test_checklist_covers(needle: str) -> None:
    assert needle in CHECKLIST.read_text(encoding="utf-8")


def test_readme_links_the_guide() -> None:
    assert "docs/user-guide.md" in README.read_text(encoding="utf-8")
