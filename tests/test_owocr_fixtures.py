"""Pins the owocr 1.26.8 log fixtures captured by the R3 spike (docs/m0/owocr.md).

T24's log parser is tested against these files; this module keeps the fixture set honest:
which file carries which coordinate line, which ones are synthetic, and that nothing from the
capturing host leaked into them.
"""

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "owocr"

# owocr run.py:1956/2480 (screen) and 2035/2517 (window): f"Selected {kind}coordinates: {rects}"
# where rects is "x1,y1,x2,y2" joined by "_" for several rectangles.
COORD_LINE = re.compile(r"\| Selected (window )?coordinates: (\d+,\d+,\d+,\d+(?:_\d+,\d+,\d+,\d+)*)$")

# file name -> (kind, rects) of its coordinate line, or None when it must have none.
EXPECTED: dict[str, tuple[str, str] | None] = {
    "linux-x11-explicit-rect.log": ("screen", "100,100,900,260"),
    "linux-x11-multi-rect.log": ("screen", "100,100,500,260_500,100,900,260"),
    "linux-x11-whole-screen.log": None,
    "linux-x11-picker-killed.log": None,
    "linux-x11-off-screen-rect.log": None,
    "linux-x11-window-name.log": None,
    "synthetic-screen-picker.log": ("screen", "412,610,1508,1002"),
    "synthetic-window-picker.log": ("window", "0,540,1280,720"),
    "synthetic-window-picker-multi-rect.log": ("window", "10,500,640,700_640,500,1270,700"),
}


def _coordinate_lines(path: Path) -> list[tuple[str, str]]:
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = COORD_LINE.search(line)
        if m:
            found.append(("window" if m.group(1) else "screen", m.group(2)))
    return found


def test_fixture_set_is_exactly_the_documented_one():
    assert sorted(p.name for p in FIXTURES.glob("*.log")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_carries_its_documented_coordinate_line(name):
    expected = EXPECTED[name]
    assert _coordinate_lines(FIXTURES / name) == ([] if expected is None else [expected])


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_synthetic_fixtures_are_labelled_and_real_ones_are_not(name):
    first = (FIXTURES / name).read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# SYNTHETIC") == name.startswith("synthetic-")


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixtures_are_bom_free_utf8_with_the_capturing_host_redacted(name):
    raw = (FIXTURES / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "/home/" not in text
    assert "\r" not in text


def test_real_fixtures_start_with_the_pinned_version_banner():
    for name in EXPECTED:
        if not name.startswith("synthetic-"):
            first = (FIXTURES / name).read_text(encoding="utf-8").splitlines()[0]
            assert first.endswith("| Starting owocr version 1.26.8"), name
