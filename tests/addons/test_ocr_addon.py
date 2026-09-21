"""``addons/ocr_addon.py``: owocr's command line, its log, install, private home and picker (spec 14).

No test launches the real owocr: processes are ``tests/fakes/fake_owocr.py`` run by this venv's
Python, and the install drives a fake ``uv``. The real install is the one ``network`` test.
"""

from pathlib import Path

import pytest

from anki_miner_game.addons import ocr_addon
from anki_miner_game.addons.ocr_addon import LogEvent, LogKind, OcrError, owocr_args, parse_log_line
from anki_miner_game.models.profile import OcrEngine, OcrSettings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "owocr"

# --- command line ----------------------------------------------------------------------------------

BASE = ["-r", "screencapture", "-w", "websocket", "-wp", "5000", "-t", "False"]


def _ocr(**kwargs) -> OcrSettings:
    kwargs.setdefault("engine", OcrEngine.MEIKIOCR)
    return OcrSettings(**kwargs)


@pytest.mark.parametrize(
    ("platform", "settings", "tail"),
    [
        pytest.param(
            "linux",
            _ocr(rects="100,100,900,260"),
            ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", "-sa=100,100,900,260"],
            id="linux screen rectangles",
        ),
        pytest.param(
            "linux",
            _ocr(rects="100,100,500,260_500,100,900,260", window_title="Some Game"),
            ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", "-sa=100,100,500,260_500,100,900,260"],
            id="linux ignores the window title: X11 has no window capture",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, window_title="Some Game", rects="0,540,1280,720"),
            ["-l", "ja", "-e", "oneocr", "-el", "oneocr", "-sa=Some Game", "-swa=0,540,1280,720"],
            id="windows window-relative rectangles",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, window_title="-Game - Title"),
            ["-l", "ja", "-e", "oneocr", "-el", "oneocr", "-sa=-Game - Title", "-swa=window"],
            id="windows whole window; a leading dash stays a value",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, rects="-1920,0,-100,200"),
            ["-l", "ja", "-e", "oneocr", "-el", "oneocr", "-sa=-1920,0,-100,200"],
            id="windows screen rectangles, negative on a left monitor",
        ),
        pytest.param(
            "linux",
            _ocr(engine=OcrEngine.GLENS, language="zh", rects="1,2,3,4"),
            ["-l", "zh", "-e", "glens", "-el", "glens", "-sa=1,2,3,4"],
            id="cloud engine and language",
        ),
    ],
)
def test_owocr_args(platform, settings, tail):
    assert owocr_args(settings, 5000, platform=platform) == BASE + tail


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_owocr_args_refuses_a_run_with_no_area(platform):
    with pytest.raises(OcrError, match="OCR area"):
        owocr_args(_ocr(), 5000, platform=platform)


@pytest.mark.parametrize(
    ("platform", "window_title", "area"),
    [
        pytest.param("linux", None, ["-sa="], id="linux screen picker"),
        pytest.param("linux", "Some Game", ["-sa="], id="linux screen picker whatever the title"),
        pytest.param("win32", None, ["-sa="], id="windows screen picker"),
        pytest.param("win32", "Some Game", ["-sa=Some Game", "-swa="], id="windows window picker"),
    ],
)
def test_picker_args_leave_the_area_empty(platform, window_title, area):
    settings = _ocr(window_title=window_title, rects="1,2,3,4")
    args = owocr_args(settings, 5000, platform=platform, pick=True)
    assert args == BASE + ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", *area]


# --- log ---------------------------------------------------------------------------------------------

SCREEN, WINDOW = LogKind.COORDINATES, LogKind.WINDOW_COORDINATES

FIXTURE_EVENTS: dict[str, list[LogEvent]] = {
    "linux-x11-explicit-rect.log": [LogEvent(SCREEN, "100,100,900,260")],
    "linux-x11-multi-rect.log": [LogEvent(SCREEN, "100,100,500,260_500,100,900,260")],
    "linux-x11-whole-screen.log": [],
    "linux-x11-picker-killed.log": [],
    "linux-x11-off-screen-rect.log": [
        LogEvent(LogKind.CONFIG_ERROR, "Invalid coordinate set(s) in screen_capture_area")
    ],
    "linux-x11-window-name.log": [
        LogEvent(
            LogKind.CONFIG_ERROR, "Window capture is only currently supported on Windows, macOS and Linux + Wayland"
        )
    ],
    "synthetic-screen-picker.log": [LogEvent(SCREEN, "412,610,1508,1002")],
    "synthetic-window-picker.log": [LogEvent(WINDOW, "0,540,1280,720")],
    "synthetic-window-picker-multi-rect.log": [LogEvent(WINDOW, "10,500,640,700_640,500,1270,700")],
}


def test_every_owocr_fixture_is_covered():
    assert sorted(p.name for p in FIXTURES.glob("*.log")) == sorted(FIXTURE_EVENTS)


@pytest.mark.parametrize("name", sorted(FIXTURE_EVENTS))
def test_parse_log_line_against_the_r3_fixtures(name):
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    events = [event for event in map(parse_log_line, lines) if event is not None]
    assert events == FIXTURE_EVENTS[name]


@pytest.mark.parametrize(
    ("line", "event"),
    [
        pytest.param(
            "10:00:00 | Selected coordinates: -1920,0,-100,200",
            LogEvent(SCREEN, "-1920,0,-100,200"),
            id="negative coordinates on a monitor left of the primary",
        ),
        pytest.param(
            "\x1b[32m10:00:00\x1b[0m | \x1b[1mSelected window coordinates: 1,2,3,4\x1b[0m\r\n",
            LogEvent(WINDOW, "1,2,3,4"),
            id="colour codes and CRLF",
        ),
        pytest.param(
            "10:00:00 | Selection is empty, selecting whole screen",
            LogEvent(LogKind.EMPTY_SELECTION, "Selection is empty, selecting whole screen"),
            id="empty screen selection",
        ),
        pytest.param(
            "10:00:00 | Window is minimized, selecting whole window",
            LogEvent(LogKind.EMPTY_SELECTION, "Window is minimized, selecting whole window"),
            id="minimised window",
        ),
        pytest.param(
            "10:00:00 | Picker window was closed or an error occurred",
            LogEvent(LogKind.PICKER_CLOSED, "Picker window was closed or an error occurred"),
            id="screen picker closed",
        ),
        pytest.param(
            "10:00:00 | Picker window was closed or an error occurred, selecting whole window",
            LogEvent(LogKind.PICKER_CLOSED, "Picker window was closed or an error occurred, selecting whole window"),
            id="window picker closed",
        ),
        pytest.param(
            "10:00:00 | No engines available!",
            LogEvent(LogKind.CONFIG_ERROR, "No engines available!"),
            id="no engine",
        ),
        pytest.param(
            "10:00:00 | Error initializing screenshots: XGetImage() failed",
            LogEvent(LogKind.CONFIG_ERROR, "Error initializing screenshots: XGetImage() failed"),
            id="no screen to capture",
        ),
        pytest.param(
            '10:00:00 | "screen_capture_area" must be empty, "screen_N" where N is a screen number',
            LogEvent(
                LogKind.CONFIG_ERROR, '"screen_capture_area" must be empty, "screen_N" where N is a screen number'
            ),
            id="window title not found",
        ),
        pytest.param(
            "10:00:00 | Couldn't start websocket server. Make sure port 5000 is not already in use",
            None,
            id="port taken is worth a restart",
        ),
        pytest.param(
            "10:00:00 | Text recognized in 0.05s using meikiocr: Selected coordinates: 1,2,3,4",
            None,
            id="recognised game text never counts",
        ),
        pytest.param("Selected coordinates: 1,2,3,4", None, id="no timestamp, not owocr's logger"),
        pytest.param("10:00:00 | Selected coordinates: 1,2,3", None, id="not a rectangle"),
        pytest.param("termios.error: (25, 'Inappropriate ioctl for device')", None, id="traceback"),
    ],
)
def test_parse_log_line(line, event):
    assert parse_log_line(line) == event


def test_log_message_is_the_text_after_the_timestamp():
    assert ocr_addon.log_message("15:00:27 | Terminated!\n") == "Terminated!"
    assert ocr_addon.log_message("    self.run()") is None
