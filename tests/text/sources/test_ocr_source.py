"""OcrSource: owocr supervised as a websocket text source (spec 8.1, 14, 17 owocr row).

The supervisor tests drive fake processes through a fake launcher, with an injected ``sleep`` and
``now``. The last tests run the real ``OcrAddon`` launch path against ``tests/fakes/fake_owocr.py``
(never the real owocr), which serves lines on its websocket the way owocr does.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from anki_miner_game.addons.ocr_addon import OcrAddon, OcrError
from anki_miner_game.models.messages import Banner, BannerCleared, BannerLevel, BannerRaised, SourceStatus
from anki_miner_game.models.profile import OcrSettings
from anki_miner_game.text.sources import ocr_source
from anki_miner_game.text.sources.ocr_source import OcrSource

FAKES = Path(__file__).parent.parent.parent / "fakes"
SETTINGS = OcrSettings(rects="100,100,900,260")


class FakeProc:
    """An owocr run: exits at once with ``code`` unless ``runs``; ``kill_tree`` ends it."""

    def __init__(
        self, code: int = 1, *, fatal: str | None = None, window_missing: bool = False, runs: bool = False
    ) -> None:
        self.code = code
        self.fatal = fatal
        self.window_missing = window_missing
        self.last_message = "Something went wrong"
        self.kills = 0
        self._exited = asyncio.Event()
        if not runs:
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.code

    async def kill_tree(self) -> None:
        self.kills += 1
        self.code = -15 if not self._exited.is_set() else self.code
        self._exited.set()


class FakeLauncher:
    def __init__(self, make=lambda n: FakeProc(), *, reason: str | None = None, on_launch=None) -> None:
        self.make = make
        self.reason = reason
        self.on_launch = on_launch
        self.launches: list[tuple[OcrSettings, int]] = []
        self.procs: list[FakeProc] = []

    def unavailable_reason(self) -> str | None:
        return self.reason

    async def launch(self, ocr: OcrSettings, port: int) -> FakeProc:
        self.launches.append((ocr, port))
        if self.on_launch is not None:
            self.on_launch()
        proc = self.make(len(self.launches))
        if isinstance(proc, Exception):
            raise proc
        self.procs.append(proc)
        return proc


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


class Banners:
    def __init__(self) -> None:
        self.events: list[BannerRaised | BannerCleared] = []

    def __call__(self, event: BannerRaised | BannerCleared) -> None:
        self.events.append(event)

    @property
    def raised(self) -> list[Banner]:
        return [event.banner for event in self.events if isinstance(event, BannerRaised)]

    async def wait_raised(self) -> Banner:
        async with asyncio.timeout(5):
            while not self.raised:
                await asyncio.sleep(0.005)
        return self.raised[-1]


class Sink:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[tuple[str, float, str]] = asyncio.Queue()

    def __call__(self, raw: str, t_mono: float, source_id: str) -> None:
        self.lines.put_nowait((raw, t_mono, source_id))

    async def next(self) -> tuple[str, float, str]:
        async with asyncio.timeout(10):
            return await self.lines.get()


@pytest.fixture
async def make_source():
    made: list[OcrSource] = []

    def make(launcher, settings: OcrSettings = SETTINGS, **kwargs) -> OcrSource:
        kwargs.setdefault("sleep", FakeSleep())
        source = OcrSource(launcher, settings, **kwargs)
        made.append(source)
        return source

    yield make
    for source in made:
        source.stop()
        await source.wait_closed()


async def test_three_restarts_with_backoff_then_a_banner(make_source):
    launcher, sleep, banners = FakeLauncher(), FakeSleep(), Banners()
    source = make_source(launcher, sleep=sleep, on_banner=banners)
    source.start(Sink())
    banner = await banners.wait_raised()
    assert len(launcher.launches) == 4
    assert sleep.delays == [1.0, 2.0, 5.0]
    assert banner.key == "ocr" and banner.level is BannerLevel.WARNING
    assert "4 times" in banner.text and "Something went wrong" in banner.text
    assert all(proc.kills >= 1 for proc in launcher.procs), "leftover children are killed after every exit"
    assert source.status is SourceStatus.DISCONNECTED


async def test_a_config_error_gets_a_banner_and_no_restart(make_source):
    message = "Invalid coordinate set(s) in screen_capture_area"
    launcher, sleep, banners = FakeLauncher(lambda n: FakeProc(fatal=message)), FakeSleep(), Banners()
    make_source(launcher, sleep=sleep, on_banner=banners).start(Sink())
    banner = await banners.wait_raised()
    assert message in banner.text
    assert len(launcher.launches) == 1
    assert sleep.delays == []


async def test_a_missing_game_window_gets_three_restarts_then_a_banner_naming_it(make_source):
    settings = OcrSettings(rects="0,540,1280,720", window_title="Some Game")
    launcher, sleep, banners = FakeLauncher(lambda n: FakeProc(window_missing=True)), FakeSleep(), Banners()
    make_source(launcher, settings, sleep=sleep, on_banner=banners).start(Sink())
    banner = await banners.wait_raised()
    assert len(launcher.launches) == 4
    assert sleep.delays == [1.0, 2.0, 5.0]
    assert 'the window "Some Game" is not open' in banner.text
    assert "Something went wrong" not in banner.text


async def test_a_run_that_lasted_resets_the_restart_count(make_source):
    clock = [0.0]

    def later() -> None:
        clock[0] += ocr_source.STABLE_RUN_S

    launcher, sleep, banners = FakeLauncher(on_launch=later), FakeSleep(), Banners()
    source = make_source(launcher, sleep=sleep, on_banner=banners, now=lambda: clock[0])
    source.start(Sink())
    async with asyncio.timeout(5):
        while len(launcher.launches) < 8:
            await asyncio.sleep(0.005)
    assert banners.raised == []
    assert set(sleep.delays) == {1.0}


@pytest.mark.parametrize("reason", ["The OCR add-on is not installed.", "OCR on Linux needs an X11 session"])
async def test_an_unavailable_addon_gets_a_banner_and_starts_nothing(make_source, reason):
    launcher, banners = FakeLauncher(reason=reason), Banners()
    make_source(launcher, on_banner=banners).start(Sink())
    assert (await banners.wait_raised()).text == reason
    assert launcher.launches == []


@pytest.mark.parametrize(
    "error",
    [OcrError("No OCR area is selected"), FileNotFoundError(2, "No such file", "owocr")],
    ids=["no area", "no executable"],
)
async def test_a_launch_that_fails_gets_a_banner(make_source, error):
    launcher, sleep, banners = FakeLauncher(lambda n: error), FakeSleep(), Banners()
    make_source(launcher, sleep=sleep, on_banner=banners).start(Sink())
    assert str(error) in (await banners.wait_raised()).text
    assert len(launcher.launches) == 1


async def test_start_clears_an_earlier_ocr_banner(make_source):
    banners = Banners()
    make_source(FakeLauncher(reason="nope"), on_banner=banners).start(Sink())
    await banners.wait_raised()
    assert banners.events[0] == BannerCleared("ocr")


async def test_stop_kills_owocr_and_does_not_restart_it(make_source):
    launcher = FakeLauncher(lambda n: FakeProc(runs=True))
    statuses: list[SourceStatus] = []
    source = make_source(launcher)
    source.set_status_listener(lambda source_id, status: statuses.append(status))
    source.start(Sink())
    async with asyncio.timeout(5):
        while not launcher.procs:
            await asyncio.sleep(0.005)
    source.stop()
    await source.wait_closed()
    assert launcher.procs[0].kills >= 1
    assert len(launcher.launches) == 1
    assert source.status is SourceStatus.DISCONNECTED
    assert statuses[-1] is SourceStatus.DISCONNECTED


async def test_each_launch_gets_its_own_port(make_source):
    ports = iter(range(40001, 40100))
    launcher, banners = FakeLauncher(), Banners()
    make_source(launcher, on_banner=banners, port_finder=lambda: next(ports)).start(Sink())
    await banners.wait_raised()
    assert [port for _, port in launcher.launches] == [40001, 40002, 40003, 40004]
    assert all(settings is SETTINGS for settings, _ in launcher.launches)


async def test_start_twice_is_an_error(make_source):
    source = make_source(FakeLauncher(lambda n: FakeProc(runs=True)))
    source.start(Sink())
    with pytest.raises(RuntimeError):
        source.start(Sink())


def test_the_source_id_is_ocr():
    assert OcrSource(FakeLauncher(), SETTINGS).id == "ocr"


# --- against the fake owocr ------------------------------------------------------------------------------


def _installed_addon(tmp_path: Path, plan: dict) -> OcrAddon:
    home = tmp_path / "home"
    root = home / "addons" / "ocr"
    platform = "win32" if sys.platform == "win32" else "linux"
    exe = root / "bin" / ("owocr.exe" if platform == "win32" else "owocr")
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    (root / "tools" / "owocr").mkdir(parents=True)
    extra = "oneocr" if platform == "win32" else "meikiocr"
    (root / "tools" / "owocr" / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "owocr", extras = ["{extra}"], specifier = "==1.26.8" }}]\n',
        encoding="utf-8",
    )
    environ = {**os.environ, "FAKE_OWOCR_PLAN": json.dumps(plan)}
    environ.pop("XDG_SESSION_TYPE", None)
    return OcrAddon(home, platform=platform, environ=environ, argv0=[sys.executable, str(FAKES / "fake_owocr.py")])


async def _gone(pid: int) -> bool:
    if sys.platform == "win32":
        return True  # tests/addons/test_owocr_process.py covers the job object
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


async def test_owocr_lines_reach_the_sink_as_the_ocr_source(make_source, tmp_path):
    record = tmp_path / "owocr.json"
    frames = ["今日はいい天気ですね", "明日も学校に行きます"]
    addon = _installed_addon(tmp_path, {"record": str(record), "frames": frames, "grandchild": True})
    statuses: list[SourceStatus] = []
    source = make_source(addon, now=lambda: 7.5, sleep=asyncio.sleep)
    source.set_status_listener(lambda source_id, status: statuses.append(status))
    sink = Sink()
    source.start(sink)
    assert await sink.next() == ("今日はいい天気ですね", 7.5, "ocr")
    assert await sink.next() == ("明日も学校に行きます", 7.5, "ocr")
    assert source.status is SourceStatus.RECEIVING
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"][-1] == "-sa=100,100,900,260"
    port = int(seen["argv"][seen["argv"].index("-wp") + 1])
    assert port > 0
    source.stop()
    await source.wait_closed()
    assert statuses[-1] is SourceStatus.DISCONNECTED
    assert await _gone(seen["pid"])
    assert await _gone(seen["grandchild"])


async def test_owocr_config_error_reaches_a_banner(make_source, tmp_path):
    addon = _installed_addon(
        tmp_path, {"log": ["10:00:00 | No engines available!", "10:00:00 | Terminated!"], "exit": 1}
    )
    banners = Banners()
    make_source(addon, on_banner=banners).start(Sink())
    assert "No engines available!" in (await banners.wait_raised()).text


async def test_owocr_that_cannot_find_the_game_window_is_restarted_then_named(make_source, tmp_path):
    missing = (
        '10:00:00 | "screen_capture_area" must be empty, "screen_N" where N is a screen number starting from 1,'
        " one or more sets of rectangle coordinates, or a window name"
    )
    addon = _installed_addon(tmp_path, {"log": [missing, "10:00:00 | Terminated!"], "exit": 1})
    settings = OcrSettings(rects="0,540,1280,720", window_title="Some Game")
    sleep, banners = FakeSleep(), Banners()
    make_source(addon, settings, sleep=sleep, on_banner=banners).start(Sink())
    banner = await banners.wait_raised()
    assert sleep.delays == [1.0, 2.0, 5.0], "three restarts before the banner"
    assert 'the window "Some Game" is not open' in banner.text
