"""``addons/ocr_addon.py``: owocr's command line, its log, install, private home and picker (spec 14).

No test launches the real owocr: processes are ``tests/fakes/fake_owocr.py`` run by this venv's
Python, and the install drives a fake ``uv``. The real install is the one ``network`` test.
"""

import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

import pytest

from anki_miner_game.addons import ocr_addon
from anki_miner_game.addons.bootstrap import BootstrapError, uv_environment
from anki_miner_game.addons.ocr_addon import (
    LogEvent,
    LogKind,
    OcrAddon,
    OcrError,
    owocr_args,
    parse_log_line,
    uv_install_args,
)
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.profile import OcrEngine, OcrSettings

FIXTURES = Path(__file__).parent.parent / "fixtures" / "owocr"
FAKES = Path(__file__).parent.parent / "fakes"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake uv runs through a shebang")

WINDOW_MISSING_LINE = (
    '10:00:00 | "screen_capture_area" must be empty, "screen_N" where N is a screen number starting from 1,'
    " one or more sets of rectangle coordinates, or a window name"
)
"""What owocr logs when no window has the title it was given (``run.py:1991,2005``)."""

# --- command line ----------------------------------------------------------------------------------

BASE = ["-r", "screencapture", "-w", "websocket", "-wp", "5000", "-t", "False"]
WHOLE_LINES = ["-sf", "1.0", "-sl", "False"]
"""Whole lines only: 1 s of frame stabilisation, no line recovery (S4-1)."""
ANY_WINDOW = ["-sw", "False"]
"""Window capture reads the game window also while another window is in front (S4-2)."""


def _ocr(**kwargs) -> OcrSettings:
    kwargs.setdefault("engine", OcrEngine.MEIKIOCR)
    return OcrSettings(**kwargs)


@pytest.mark.parametrize(
    ("platform", "settings", "tail"),
    [
        pytest.param(
            "linux",
            _ocr(rects="100,100,900,260"),
            ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", *WHOLE_LINES, "-sa=100,100,900,260"],
            id="linux screen rectangles",
        ),
        pytest.param(
            "linux",
            _ocr(rects="100,100,500,260_500,100,900,260", window_title="Some Game"),
            ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", *WHOLE_LINES, "-sa=100,100,500,260_500,100,900,260"],
            id="linux ignores the window title: X11 has no window capture",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, window_title="Some Game", rects="0,540,1280,720"),
            [
                "-l",
                "ja",
                "-e",
                "oneocr",
                "-el",
                "oneocr",
                *WHOLE_LINES,
                *ANY_WINDOW,
                "-sa=Some Game",
                "-swa=0,540,1280,720",
            ],
            id="windows window-relative rectangles",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, window_title="-Game - Title"),
            [
                "-l",
                "ja",
                "-e",
                "oneocr",
                "-el",
                "oneocr",
                *WHOLE_LINES,
                *ANY_WINDOW,
                "-sa=-Game - Title",
                "-swa=window",
            ],
            id="windows whole window; a leading dash stays a value",
        ),
        pytest.param(
            "win32",
            _ocr(engine=OcrEngine.ONEOCR, rects="-1920,0,-100,200"),
            ["-l", "ja", "-e", "oneocr", "-el", "oneocr", *WHOLE_LINES, "-sa=-1920,0,-100,200"],
            id="windows screen rectangles, negative on a left monitor",
        ),
        pytest.param(
            "linux",
            _ocr(engine=OcrEngine.GLENS, language="zh", rects="1,2,3,4"),
            ["-l", "zh", "-e", "glens", "-el", "glens", *WHOLE_LINES, "-sa=1,2,3,4"],
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
        pytest.param("win32", "Some Game", [*ANY_WINDOW, "-sa=Some Game", "-swa="], id="windows window picker"),
    ],
)
def test_picker_args_leave_the_area_empty(platform, window_title, area):
    settings = _ocr(window_title=window_title, rects="1,2,3,4")
    args = owocr_args(settings, 5000, platform=platform, pick=True)
    assert args == BASE + ["-l", "ja", "-e", "meikiocr", "-el", "meikiocr", *WHOLE_LINES, *area]


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
                LogKind.WINDOW_MISSING, '"screen_capture_area" must be empty, "screen_N" where N is a screen number'
            ),
            id="window title not found is worth a restart",
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


# --- install -----------------------------------------------------------------------------------------


def test_uv_install_args_on_linux_override_pygobject_out(tmp_path):
    overrides = tmp_path / "overrides.txt"
    assert uv_install_args("linux", overrides) == [
        "tool",
        "install",
        "--python",
        "3.12",
        "--overrides",
        str(overrides),
        "owocr[meikiocr]==1.26.8",
    ]


def test_uv_install_args_on_windows_take_the_oneocr_extra():
    assert uv_install_args("win32", None) == ["tool", "install", "--python", "3.12", "owocr[oneocr]==1.26.8"]


def _fake_uv(tmp_path: Path) -> Path:
    uv = tmp_path / "fake-bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(
        f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKES / 'fake_uv.py')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return uv


class Progress:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, done: int, total: int) -> None:
        self.calls.append((done, total))


def _addon(home: Path, tmp_path: Path, *, platform: str = "linux", uv_plan: dict | None = None, **environ) -> OcrAddon:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "real-home"), **environ}
    if uv_plan is not None:
        env["FAKE_UV_PLAN"] = json.dumps(uv_plan)
    uv = _fake_uv(tmp_path) if sys.platform != "win32" else Path("uv-not-used-on-windows")
    return OcrAddon(home, platform=platform, environ=env, ensure_uv=lambda _home, **_: uv)


def _root(home: Path) -> Path:
    return home / "addons" / "ocr"


def test_a_fresh_home_has_no_ocr_addon(tmp_path):
    assert _addon(tmp_path / "home", tmp_path).status() is AddonStatus.MISSING


@posix_only
async def test_install_runs_uv_tool_install_in_the_addon_folder(tmp_path):
    home = tmp_path / "home"
    record = tmp_path / "uv.json"
    addon = _addon(home, tmp_path, uv_plan={"record": str(record)})
    progress = Progress()
    await addon.install(progress)
    assert addon.status() is AddonStatus.READY
    seen = json.loads(record.read_text(encoding="utf-8"))
    overrides = _root(home) / "overrides.txt"
    assert seen["argv"] == uv_install_args("linux", overrides)
    assert overrides.read_text(encoding="utf-8") == 'pygobject; sys_platform == "never"\n'
    for name, value in uv_environment(home, "ocr").items():
        assert seen["env"][name] == value
    assert progress.calls[0] == (0, addon.size_bytes)
    assert progress.calls[-1] == (addon.size_bytes, addon.size_bytes)


@posix_only
async def test_status_is_installing_while_the_install_runs(tmp_path):
    home = tmp_path / "home"
    seen: list[AddonStatus] = []
    uv = _fake_uv(tmp_path)

    def ensure_uv(_home: Path, **_: object) -> Path:
        seen.append(addon.status())
        return uv

    addon = OcrAddon(home, platform="linux", environ={"PATH": os.environ.get("PATH", "")}, ensure_uv=ensure_uv)
    await addon.install(Progress())
    assert seen == [AddonStatus.INSTALLING]
    assert addon.status() is AddonStatus.READY


@posix_only
async def test_a_failed_install_leaves_nothing_behind_and_says_why(tmp_path):
    home = tmp_path / "home"
    addon = _addon(home, tmp_path, uv_plan={"fail": True})
    with pytest.raises(OcrError, match='Dependency "cairo" not found'):
        await addon.install(Progress())
    assert not (_root(home) / "tools").exists()
    assert not (_root(home) / "bin").exists()
    assert addon.status() is AddonStatus.MISSING


@posix_only
async def test_a_cancelled_install_kills_uv_and_leaves_nothing_behind(tmp_path):
    home = tmp_path / "home"
    record = tmp_path / "uv.json"
    addon = _addon(home, tmp_path, uv_plan={"record": str(record), "hang": True})
    install = asyncio.create_task(addon.install(Progress()))
    async with asyncio.timeout(10):
        while not record.exists() or not (_root(home) / "tools" / "owocr").exists():
            await asyncio.sleep(0.01)
    install.cancel()
    with pytest.raises(asyncio.CancelledError):
        await install
    pid = json.loads(record.read_text(encoding="utf-8"))["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not (_root(home) / "tools").exists()
    assert addon.status() is AddonStatus.MISSING


@posix_only
async def test_an_install_of_another_owocr_version_is_broken_and_reinstall_repairs_it(tmp_path):
    home = tmp_path / "home"
    _install_fake(home, "linux", version="1.26.7")
    addon = _addon(home, tmp_path, uv_plan={})
    assert addon.status() is AddonStatus.BROKEN
    await addon.install(Progress())
    assert addon.status() is AddonStatus.READY


@posix_only
async def test_an_install_that_does_not_verify_fails_and_leaves_nothing_behind(tmp_path):
    home = tmp_path / "home"
    addon = _addon(home, tmp_path, uv_plan={"version": "1.26.7"})
    with pytest.raises(OcrError, match="do not check out"):
        await addon.install(Progress())
    assert addon.status() is AddonStatus.MISSING


def test_a_receipt_with_keys_uv_added_still_verifies(tmp_path):
    _install_fake(tmp_path, "linux")
    receipt = _root(tmp_path) / "tools" / "owocr" / "uv-receipt.toml"
    receipt.write_text(receipt.read_text(encoding="utf-8").replace(" }]", ", marker = \"python_version >= '3'\" }]"))
    assert "marker" in receipt.read_text(encoding="utf-8")
    assert OcrAddon(tmp_path, platform="linux", environ={}).status() is AddonStatus.READY


@posix_only
async def test_a_missing_entry_point_or_receipt_is_broken(tmp_path):
    home = tmp_path / "home"
    addon = _addon(home, tmp_path, uv_plan={})
    await addon.install(Progress())
    (_root(home) / "bin" / "owocr").unlink()
    assert addon.status() is AddonStatus.BROKEN
    await addon.install(Progress())
    (_root(home) / "tools" / "owocr" / "uv-receipt.toml").write_text("not [toml", encoding="utf-8")
    assert addon.status() is AddonStatus.BROKEN


async def test_a_failed_uv_bootstrap_is_an_ocr_error_and_keeps_the_previous_install(tmp_path):
    home = tmp_path / "home"
    _install_fake(home, "linux", version="1.26.7")

    def no_uv(_home: Path, **_: object) -> Path:
        raise BootstrapError("uv could not be installed: offline")

    addon = OcrAddon(home, platform="linux", environ={}, ensure_uv=no_uv)
    with pytest.raises(OcrError, match="offline"):
        await addon.install(Progress())
    assert addon.status() is AddonStatus.BROKEN


async def test_a_uv_that_cannot_start_is_an_ocr_error(tmp_path):
    home = tmp_path / "home"
    addon = OcrAddon(home, platform="linux", environ={}, ensure_uv=lambda _home, **_: tmp_path / "no-uv")
    with pytest.raises(OcrError, match="could not be installed"):
        await addon.install(Progress())
    assert addon.status() is AddonStatus.MISSING


async def test_install_while_ready_does_nothing(tmp_path):
    home = tmp_path / "home"
    _install_fake(home, "linux")
    calls: list[Path] = []
    progress = Progress()

    def ensure_uv(home: Path, **_: object) -> Path:
        calls.append(home)
        raise AssertionError("no install while ready")

    addon = OcrAddon(home, platform="linux", environ={}, ensure_uv=ensure_uv)
    await addon.install(progress)
    assert calls == [] and progress.calls == []
    assert addon.status() is AddonStatus.READY


async def test_a_cancelled_uv_download_stops_at_its_next_chunk(tmp_path):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    chunks: list[int] = []

    def ensure_uv(_home: Path, *, progress) -> Path:
        loop.call_soon_threadsafe(started.set)
        for done in range(1000):
            progress(done, 1000)
            chunks.append(done)
            time.sleep(0.01)
        raise AssertionError("the download ran to the end")

    addon = OcrAddon(tmp_path / "home", platform="linux", environ={}, ensure_uv=ensure_uv)
    install = asyncio.create_task(addon.install(Progress()))
    async with asyncio.timeout(5):
        await started.wait()
    install.cancel()
    with pytest.raises(asyncio.CancelledError):
        await install
    stopped_at = len(chunks)
    await asyncio.sleep(0.05)
    assert len(chunks) == stopped_at < 1000
    assert addon.status() is AddonStatus.MISSING


async def test_a_second_install_while_one_runs_is_refused(tmp_path):
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_uv(_home: Path, **_: object) -> Path:
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        raise BootstrapError("stop here")

    addon = OcrAddon(tmp_path / "home", platform="linux", environ={}, ensure_uv=slow_uv)
    first = asyncio.create_task(addon.install(Progress()))
    async with asyncio.timeout(5):
        while addon.status() is not AddonStatus.INSTALLING:
            await asyncio.sleep(0.01)
    with pytest.raises(OcrError, match="already"):
        await addon.install(Progress())
    release.set()
    with pytest.raises(OcrError, match="stop here"):
        await first
    assert addon.status() is AddonStatus.MISSING


def test_size_and_note_per_platform(tmp_path):
    linux = OcrAddon(tmp_path, platform="linux", environ={})
    windows = OcrAddon(tmp_path, platform="win32", environ={})
    assert linux.size_bytes > windows.size_bytes > 0
    assert "X11" in (linux.note or "")
    assert windows.note is None


@pytest.mark.parametrize("protocol_name", ["AddonService", "OcrAreaPicker"])
def test_the_ocr_addon_conforms_to_the_addon_protocols(protocol_name):
    from anki_miner_game.interfaces import addons

    protocol = getattr(addons, protocol_name)
    members = [attr for attr in vars(protocol) if not attr.startswith("_")]
    assert members
    for member in members:
        expected = inspect.getattr_static(protocol, member)
        actual = inspect.getattr_static(OcrAddon, member)
        if isinstance(expected, property):
            assert isinstance(actual, property), member
        else:
            assert inspect.iscoroutinefunction(actual) is inspect.iscoroutinefunction(expected), member
            assert list(inspect.signature(actual).parameters) == list(inspect.signature(expected).parameters), member


def test_pick_and_install_raise_the_runtime_error_their_protocols_name():
    assert issubclass(OcrError, RuntimeError)


@pytest.mark.network
@pytest.mark.skipif(sys.platform != "linux", reason="checked on Linux; Windows is H5")
async def test_real_install_of_the_pinned_owocr(tmp_path):
    """Downloads uv, a managed CPython and owocr with its dependencies (about 200 MB)."""
    home = tmp_path / "home"
    addon = OcrAddon(home)
    await addon.install(Progress())
    assert addon.status() is AddonStatus.READY
    proc = await asyncio.create_subprocess_exec(
        str(addon.executable), "-h", env=addon.owocr_environment(), stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0
    assert b"screen_capture_area" in out


# --- private home ------------------------------------------------------------------------------------


def _install_fake(home: Path, platform: str, version: str = "1.26.8") -> None:
    """What a finished install leaves behind, without running uv."""
    root = _root(home)
    exe = root / "bin" / ("owocr.exe" if platform == "win32" else "owocr")
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    tool_env = root / "tools" / "owocr"
    tool_env.mkdir(parents=True)
    extra = "oneocr" if platform == "win32" else "meikiocr"
    (tool_env / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "owocr", extras = ["{extra}"], specifier = "=={version}" }}]\n',
        encoding="utf-8",
    )


def test_owocr_runs_with_a_private_home_holding_a_minimal_config(tmp_path):
    home, real_home = tmp_path / "home", tmp_path / "real-home"
    (real_home / ".config").mkdir(parents=True)
    users_config = real_home / ".config" / "owocr_config.ini"
    users_config.write_text("[general]\nscreen_capture_frame_stabilization = 0\n", encoding="utf-8")
    before = users_config.stat()
    env = OcrAddon(home, platform="linux", environ={"HOME": str(real_home), "LANG": "C.UTF-8"}).owocr_environment()
    private = _root(home) / "home"
    assert env["HOME"] == str(private)
    assert env["LANG"] == "C.UTF-8"
    assert "USERPROFILE" not in env
    assert (private / ".config" / "owocr_config.ini").read_text(encoding="utf-8") == "[general]\n"
    after = users_config.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_windows_redirects_userprofile_too(tmp_path):
    env = OcrAddon(tmp_path / "home", platform="win32", environ={"USERPROFILE": "C:/Users/me"}).owocr_environment()
    private = str(_root(tmp_path / "home") / "home")
    assert env["USERPROFILE"] == private
    assert env["HOME"] == private
    assert "XAUTHORITY" not in env


def test_a_changed_private_config_is_put_back(tmp_path):
    addon = OcrAddon(tmp_path / "home", platform="linux", environ={})
    addon.owocr_environment()
    config = _root(tmp_path / "home") / "home" / ".config" / "owocr_config.ini"
    config.write_text("[general]\nnotifications = True\n", encoding="utf-8")
    addon.owocr_environment()
    assert config.read_text(encoding="utf-8") == "[general]\n"


def test_x11_keeps_the_users_xauthority_under_the_private_home(tmp_path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (real_home / ".Xauthority").write_bytes(b"cookie")
    env = OcrAddon(tmp_path / "home", platform="linux", environ={"HOME": str(real_home)}).owocr_environment()
    assert env["XAUTHORITY"] == str(real_home / ".Xauthority")
    env = OcrAddon(
        tmp_path / "home", platform="linux", environ={"HOME": str(real_home), "XAUTHORITY": "/run/xauth"}
    ).owocr_environment()
    assert env["XAUTHORITY"] == "/run/xauth"
    env = OcrAddon(tmp_path / "home", platform="linux", environ={"HOME": str(tmp_path)}).owocr_environment()
    assert "XAUTHORITY" not in env


@pytest.mark.parametrize(
    ("platform", "environ", "installed", "reason"),
    [
        pytest.param("linux", {"XDG_SESSION_TYPE": "x11"}, True, None, id="linux x11"),
        pytest.param("linux", {"XDG_SESSION_TYPE": "wayland"}, True, "X11", id="linux wayland"),
        pytest.param("win32", {"XDG_SESSION_TYPE": "wayland"}, True, None, id="windows ignores XDG"),
        pytest.param("linux", {}, False, "not installed", id="not installed"),
    ],
)
def test_unavailable_reason(tmp_path, platform, environ, installed, reason):
    if installed:
        _install_fake(tmp_path, platform)
    got = OcrAddon(tmp_path, platform=platform, environ=environ).unavailable_reason()
    assert (got is None) if reason is None else (reason in (got or ""))


# --- picker ------------------------------------------------------------------------------------------


def _picker(tmp_path: Path, *, platform: str = "linux", **plan) -> tuple[OcrAddon, Path]:
    home = tmp_path / "home"
    _install_fake(home, platform)
    record = tmp_path / "owocr.json"
    environ = {**os.environ, "FAKE_OWOCR_PLAN": json.dumps({"record": str(record), **plan})}
    environ.pop("XDG_SESSION_TYPE", None)
    addon = OcrAddon(home, platform=platform, environ=environ, argv0=[sys.executable, str(FAKES / "fake_owocr.py")])
    return addon, record


async def _gone(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # the job-object test covers Windows
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # a zombie waiting for its new parent to reap it is already dead
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return True
        except OSError:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_pick_returns_the_screen_rectangles_and_kills_owocr(tmp_path):
    addon, record = _picker(tmp_path, log_file=str(FIXTURES / "synthetic-screen-picker.log"), grandchild=True)
    assert await addon.pick(None) == "412,610,1508,1002"
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"][-1] == "-sa="
    assert seen["argv"][seen["argv"].index("-el") + 1] == "meikiocr"
    assert seen["env"]["HOME"] == str(_root(tmp_path / "home") / "home")
    assert await _gone(seen["pid"])
    assert await _gone(seen["grandchild"])


async def test_pick_with_a_window_title_on_windows_returns_window_rectangles(tmp_path):
    addon, record = _picker(tmp_path, platform="win32", log_file=str(FIXTURES / "synthetic-window-picker.log"))
    assert await addon.pick("Some Game") == "0,540,1280,720"
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert argv[-2:] == ["-sa=Some Game", "-swa="]
    assert argv[argv.index("-e") + 1] == "oneocr"


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param({"log": ["10:00:00 | Selection is empty, selecting whole screen"]}, id="empty selection"),
        pytest.param(
            {"log": ["10:00:00 | Picker window was closed or an error occurred", "10:00:00 | Terminated!"], "exit": 1},
            id="picker closed",
        ),
    ],
)
async def test_pick_without_a_selection_returns_none(tmp_path, plan):
    addon, _ = _picker(tmp_path, **plan)
    assert await addon.pick(None) is None


async def test_pick_reports_owocrs_error(tmp_path):
    addon, _ = _picker(tmp_path, log_file=str(FIXTURES / "linux-x11-window-name.log"), exit=1)
    with pytest.raises(OcrError, match="Window capture is only currently supported"):
        await addon.pick(None)


async def test_pick_reports_an_exit_before_any_selection(tmp_path):
    addon, _ = _picker(tmp_path, log=["10:00:00 | Launching screen coordinate picker"], exit=1)
    with pytest.raises(OcrError, match="Launching screen coordinate picker"):
        await addon.pick(None)


async def test_pick_names_a_window_that_is_not_open(tmp_path):
    addon, _ = _picker(tmp_path, platform="win32", log=[WINDOW_MISSING_LINE, "10:00:00 | Terminated!"], exit=1)
    with pytest.raises(OcrError, match='the window "Some Game" is not open'):
        await addon.pick("Some Game")


@posix_only
async def test_pick_turns_an_owocr_that_cannot_start_into_an_ocr_error(tmp_path):
    home = tmp_path / "home"
    _install_fake(home, "linux")
    addon = OcrAddon(home, platform="linux", environ={})
    addon.executable.chmod(0o644)  # a READY install on a noexec mount, say
    assert addon.status() is AddonStatus.READY
    with pytest.raises(OcrError, match="owocr could not start") as raised:
        await addon.pick(None)
    assert isinstance(raised.value.__cause__, OSError)


async def test_a_cancelled_pick_kills_owocr(tmp_path):
    addon, record = _picker(tmp_path, log=["10:00:00 | Launching screen coordinate picker"], grandchild=True)
    pick = asyncio.create_task(addon.pick(None))
    async with asyncio.timeout(10):
        while not record.exists():
            await asyncio.sleep(0.01)
    pick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pick
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert await _gone(seen["pid"])
    assert await _gone(seen["grandchild"])


async def test_pick_needs_the_addon_and_an_x11_session(tmp_path):
    with pytest.raises(OcrError, match="not installed"):
        await OcrAddon(tmp_path, platform="linux", environ={}).pick(None)
    _install_fake(tmp_path, "linux")
    with pytest.raises(OcrError, match="X11"):
        await OcrAddon(tmp_path, platform="linux", environ={"XDG_SESSION_TYPE": "wayland"}).pick(None)
