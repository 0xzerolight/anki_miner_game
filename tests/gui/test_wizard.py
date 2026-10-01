"""The first-run wizard's pages (spec 16): OBS, text sources, output folder, optional add-ons.

Coroutines run on a real asyncio loop in another thread, as on the app's I/O thread, so every result
crosses back to the Qt main thread the way it does in the app.
"""

import ast
import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QFrame, QLabel, QWizard

import anki_miner_game.gui.wizard as wizard_module
from anki_miner_game.gui.strings import WAYLAND_CLIPBOARD_TEXT
from anki_miner_game.gui.widgets.layout import available_size
from anki_miner_game.gui.wizard import (
    CLIPBOARD_TEXT,
    CONNECTED_TEXT,
    FIREWALL_TEXT,
    NO_SOURCE_TEXT,
    OBS_DOWNLOAD_URL,
    OBS_SUMMARY,
    WIZARD_SIZE,
    ObsCheck,
    ObsSetup,
    ObsStatus,
    SetupWizard,
    WizardStep,
    native_path,
    saved_in_text,
    source_hint,
)
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import DEFAULT_TEXT_SOURCES, AppConfig, TextSourceConfig
from anki_miner_game.models.messages import SourceStatus
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.obs.startup import LocalObsStarter
from tests.gui.wizard_fakes import FakeDiscovery, WizardObs, glossary_misses
from tests.obs.fake_obs import Sleeps

NEXT = QWizard.WizardButton.NextButton


class IoLoop:
    """An asyncio loop running in its own thread, like the app's I/O thread.

    ``closing`` runs first at teardown (the tests' wizards close there), while the loop still runs.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="io-loop", daemon=True)
        self.closing: list[Callable[[], None]] = []

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn: Callable[[], None]) -> None:
        self.loop.call_soon_threadsafe(fn)


@pytest.fixture
def io_loop(qapp) -> Iterator[IoLoop]:
    io = IoLoop()
    io.thread.start()
    yield io
    for close in io.closing:
        close()

    async def finish_rest() -> None:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    io.run(finish_rest()).result(5)
    io.loop.call_soon_threadsafe(io.loop.stop)
    io.thread.join(5)
    io.loop.close()


class StubObsSetup:
    """``ObsSetup.run`` answering from a script; each run may report stages and wait for ``proceed``."""

    def __init__(self, *checks: ObsCheck | Exception) -> None:
        self.checks = list(checks)
        self.configs: list[AppConfig] = []
        self.stages: list[str] = []
        self.proceed = threading.Event()
        self.proceed.set()
        self.threads: list[threading.Thread] = []

    async def run(self, cfg: AppConfig, report: Callable[[str], None] = lambda _stage: None) -> ObsCheck:
        self.configs.append(cfg)
        self.threads.append(threading.current_thread())
        for stage in self.stages:
            report(stage)
        await asyncio.to_thread(self.proceed.wait, 5)
        check = self.checks.pop(0)
        if isinstance(check, Exception):
            raise check
        return check


class FakeSource:
    """``TextSource``: records on which thread it was started and stopped."""

    def __init__(self, cfg: TextSourceConfig) -> None:
        self.id = cfg.id
        self.status = SourceStatus.DISCONNECTED
        self.sink: Callable[[str, float, str], None] | None = None
        self.listener: Callable[[str, SourceStatus], None] | None = None
        self.started_on: threading.Thread | None = None
        self.stopped_on: threading.Thread | None = None
        self.closed = False

    def set_status_listener(self, cb: Callable[[str, SourceStatus], None]) -> None:
        self.listener = cb

    def start(self, sink: Callable[[str, float, str], None]) -> None:
        self.started_on = threading.current_thread()
        self.sink = sink
        self._set(SourceStatus.CONNECTING)

    def stop(self) -> None:
        self.stopped_on = threading.current_thread()
        self._set(SourceStatus.DISCONNECTED)

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)
        self.closed = True

    def _set(self, status: SourceStatus) -> None:
        self.status = status
        if self.listener is not None:
            self.listener(self.id, status)


class FakeAddon:
    """``AddonService`` whose install waits for ``proceed`` and then succeeds or raises ``error``."""

    def __init__(self, size: int, *, note: str | None = None, status: AddonStatus = AddonStatus.MISSING) -> None:
        self._size = size
        self._note = note
        self._status = status
        self.error: Exception | None = None
        self.proceed = threading.Event()
        self.installs = 0
        self.thread: threading.Thread | None = None

    @property
    def size_bytes(self) -> int:
        return self._size

    @property
    def note(self) -> str | None:
        return self._note

    def status(self) -> AddonStatus:
        return self._status

    async def install(self, progress: Callable[[int, int], None]) -> None:
        if self._status is AddonStatus.INSTALLING:
            raise RuntimeError("The add-on is already being installed.")
        self.installs += 1
        self.thread = threading.current_thread()
        self._status = AddonStatus.INSTALLING
        progress(0, self._size)
        await asyncio.to_thread(progress, self._size // 2, self._size)  # any thread (AddonService docstring)
        await asyncio.to_thread(self.proceed.wait, 5)
        if self.error is not None:
            self._status = AddonStatus.MISSING
            raise self.error
        progress(self._size, self._size)
        self._status = AddonStatus.READY


class Harness:
    def __init__(self, qtbot, io: IoLoop, *, obs=None, config=None, save_error=None, **kw):
        self.io = io
        self.obs = obs or StubObsSetup(ObsCheck(ObsStatus.READY, "OBS 32.2.2 is set up."))
        self.saved: list[AppConfig] = []
        self.save_error = save_error
        self.sources: list[FakeSource] = []
        self.vad = kw.pop("vad", FakeAddon(190_400_000))
        self.ocr = kw.pop("ocr", FakeAddon(210_000_000, note="OCR on Linux needs an X11 session."))
        self.wizard = SetupWizard(
            obs=self.obs,
            config=config or AppConfig(),
            save_config=self.save,
            run=kw.pop("run", io.run),
            source_factory=self.make_source,
            vad_addon=self.vad,
            ocr_addon=self.ocr,
            **kw,
        )
        qtbot.addWidget(self.wizard)
        io.closing.append(self.wizard.close)
        self.wizard.show()

    def save(self, cfg: AppConfig) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(cfg)

    def make_source(self, cfg: TextSourceConfig) -> FakeSource:
        source = FakeSource(cfg)
        self.sources.append(source)
        return source

    def on_loop(self, fn: Callable[[], None]) -> None:
        self.io.call(fn)

    def next_enabled(self) -> bool:
        return self.wizard.button(NEXT).isEnabled()


# The OBS step ------------------------------------------------------------------------------------


def test_opens_on_obs_and_next_waits_until_obs_is_set_up(qtbot, io_loop):
    h = Harness(qtbot, io_loop)
    page = h.wizard.obs_page
    assert h.wizard.currentId() == WizardStep.OBS
    assert not h.next_enabled()
    qtbot.mouseClick(page.button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(h.next_enabled)
    assert page.status.text() == "OBS 32.2.2 is set up."
    assert page.button.isHidden()  # UJ-15: nothing left to press once OBS is ready
    assert page.error.text() == ""
    assert h.obs.threads[0].name == "io-loop"


def test_the_obs_page_says_what_obs_is_and_keeps_the_details_one_click_away(qtbot, io_loop):
    h = Harness(qtbot, io_loop)
    page = h.wizard.obs_page
    assert page.subTitle() == "OBS is the free recorder this app uses to record your game."
    assert page.summary.text() == OBS_SUMMARY
    assert OBS_SUMMARY == (
        'The app adds its own profile and scene collection, both named "Anki Miner Game", and uses them '
        "only while a game is ready or recording. Your own OBS profiles, scenes and stream settings are left alone."
    )
    assert page.details_button.text() == "What exactly changes in OBS"
    assert page.changes.isHidden()
    page.details_button.click()
    assert not page.changes.isHidden()
    assert native_path(AppConfig().output_root) in page.changes.text()
    assert "auto-configuration wizard" in page.changes.text()
    page.details_button.click()
    assert page.changes.isHidden()


@pytest.mark.parametrize("windows", [True, False])
def test_starting_obs_mentions_the_windows_network_question_only_on_windows(qtbot, io_loop, monkeypatch, windows):
    monkeypatch.setattr(wizard_module, "_on_windows", lambda: windows)
    obs = StubObsSetup(ObsCheck(ObsStatus.READY, "done"))
    obs.stages = ["Starting OBS…"]
    obs.proceed.clear()
    h = Harness(qtbot, io_loop, obs=obs)
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: page.status.text().startswith("Starting OBS…"))
    assert (FIREWALL_TEXT in page.status.text()) is windows
    assert FIREWALL_TEXT == "If Windows asks whether OBS may use networks, you can press Cancel."
    obs.proceed.set()
    qtbot.waitUntil(h.next_enabled)


def test_a_refused_password_is_typed_above_the_button_and_retried_with_enter(qtbot, io_loop):
    obs = StubObsSetup(ObsCheck(ObsStatus.AUTH_FAILED, "rejected"), ObsCheck(ObsStatus.READY, "ok"))
    h = Harness(qtbot, io_loop, obs=obs)
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: not page.password.isHidden())
    assert h.wizard.focusWidget() is page.password
    column = page.body.layout()
    assert column.indexOf(page.password) < column.indexOf(page.button_row)
    assert page.password.placeholderText() == "OBS WebSocket password"
    qtbot.keyClicks(page.password, "typed-secret")
    qtbot.keyClick(page.password, Qt.Key.Key_Return)
    qtbot.waitUntil(h.next_enabled)
    assert obs.configs[1].obs.password_override == "typed-secret"


def test_stages_show_while_the_step_runs_and_the_button_waits(qtbot, io_loop):
    obs = StubObsSetup(ObsCheck(ObsStatus.READY, "done"))
    obs.stages = ["Looking for OBS…", "Connecting to OBS…"]
    obs.proceed.clear()
    h = Harness(qtbot, io_loop, obs=obs)
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: page.status.text() == "Connecting to OBS…")
    assert not page.button.isEnabled() and not h.next_enabled()
    obs.proceed.set()
    qtbot.waitUntil(h.next_enabled)
    assert page.button.isEnabled()


def test_obs_not_installed_shows_the_download_link(qtbot, io_loop):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ObsCheck(ObsStatus.NOT_INSTALLED, "OBS Studio is not installed.")))
    page = h.wizard.obs_page
    assert page.link.isHidden()
    page.button.click()
    qtbot.waitUntil(lambda: not page.link.isHidden())
    assert f'href="{OBS_DOWNLOAD_URL}"' in page.link.text()
    assert not page.link.openExternalLinks()  # routed through the injected open_url instead (spec: app.open_url)
    assert page.button.text() == "Check again"
    assert not h.next_enabled()


def test_activating_the_download_link_opens_it_through_the_injected_opener(qtbot, io_loop):
    opened: list[QUrl] = []
    h = Harness(
        qtbot,
        io_loop,
        obs=StubObsSetup(ObsCheck(ObsStatus.NOT_INSTALLED, "OBS Studio is not installed.")),
        open_url=opened.append,
    )
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: not page.link.isHidden())
    page.link.linkActivated.emit(OBS_DOWNLOAD_URL)
    assert opened == [QUrl(OBS_DOWNLOAD_URL)]


def test_websocket_server_off_offers_fix(qtbot, io_loop):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ObsCheck(ObsStatus.SERVER_OFF, "OBS's websocket server is off.")))
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: page.button.text() == "Fix")
    assert not page.error.isHidden()
    assert not h.next_enabled()


def test_a_refused_password_asks_for_it_and_saves_it_before_the_retry(qtbot, io_loop):
    obs = StubObsSetup(
        ObsCheck(ObsStatus.AUTH_FAILED, "OBS rejected the websocket password."), ObsCheck(ObsStatus.READY, "ok")
    )
    h = Harness(qtbot, io_loop, obs=obs)
    page = h.wizard.obs_page
    assert page.password.isHidden()
    page.button.click()
    qtbot.waitUntil(lambda: not page.password.isHidden())
    page.password.setText("typed-secret")
    page.button.click()
    qtbot.waitUntil(h.next_enabled)
    assert h.saved[-1].obs.password_override == "typed-secret"
    assert obs.configs[1].obs.password_override == "typed-secret"
    assert h.wizard.config.obs.password_override == "typed-secret"
    assert page.password.isHidden()


def test_a_password_that_cannot_be_saved_is_reported_and_not_retried(qtbot, io_loop):
    obs = StubObsSetup(ObsCheck(ObsStatus.AUTH_FAILED, "rejected"))
    h = Harness(qtbot, io_loop, obs=obs, save_error=OSError("disk full"))
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: not page.password.isHidden())
    page.password.setText("typed-secret")
    page.button.click()
    assert "disk full" in page.error.text()
    assert len(obs.configs) == 1


def test_notes_show_under_the_text(qtbot, io_loop):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ObsCheck(ObsStatus.READY, "ok", ("first note", "second note"))))
    page = h.wizard.obs_page
    assert page.notes.isHidden()
    page.button.click()
    qtbot.waitUntil(h.next_enabled)
    assert not page.notes.isHidden()
    assert "first note" in page.notes.text() and "second note" in page.notes.text()


def test_an_unexpected_error_is_shown_on_the_page(qtbot, io_loop, caplog):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ValueError("boom")))
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: "see the log" in page.error.text())
    assert page.button.isEnabled() and not h.next_enabled()


def test_work_that_cannot_reach_the_io_loop_is_reported_and_the_step_stays_usable(qtbot, io_loop, caplog):
    def stopped(coro):
        raise RuntimeError("Event loop is closed")

    obs = StubObsSetup(ObsCheck(ObsStatus.READY, "ok"))
    h = Harness(qtbot, io_loop, obs=obs, run=stopped)
    page = h.wizard.obs_page
    page.button.click()
    assert "see the log" in page.error.text()
    assert page.button.isEnabled() and not h.next_enabled()
    assert obs.configs == []  # the coroutine was closed, never run


def test_obs_errors_are_plain_text_not_markup(qtbot, io_loop):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ObsCheck(ObsStatus.FAILED, "Cannot connect to OBS: <b>x</b>")))
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: "Cannot connect" in page.error.text())
    shown = [label for label in page.error.findChildren(QLabel) if label.text() == "Cannot connect to OBS: <b>x</b>"]
    assert shown and shown[0].textFormat() == Qt.TextFormat.PlainText


def test_the_real_obs_step_runs_on_the_io_loop_and_returns_obs_to_the_users_names(qtbot, io_loop):
    obs = WizardObs()
    discovery = FakeDiscovery()
    provisioner = ObsProvisioner(obs, platform="linux", sleep=Sleeps())
    setup = ObsSetup(discovery, obs, provisioner, starter=LocalObsStarter(discovery, obs))
    h = Harness(qtbot, io_loop, obs=setup)
    h.wizard.obs_page.button.click()
    qtbot.waitUntil(h.next_enabled, timeout=10_000)
    assert "32.2.2" in h.wizard.obs_page.status.text()
    assert (obs.current_profile, obs.current_collection) == ("Untitled", "Untitled")


# The text sources step ----------------------------------------------------------------------------


def sources_config() -> AppConfig:
    textractor, agent, luna = DEFAULT_TEXT_SOURCES
    return AppConfig(text_sources=(textractor, agent, TextSourceConfig(luna.id, luna.name, luna.uri, enabled=False)))


MUTED = QPalette.ColorRole.PlaceholderText


def test_each_enabled_source_starts_on_the_io_loop_and_says_what_to_do(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    assert h.wizard.currentId() == WizardStep.SOURCES
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.started_on for s in h.sources))
    assert [s.id for s in h.sources] == ["textractor", "agent"]
    assert {s.started_on.name for s in h.sources} == {"io-loop"}
    assert set(page.rows) == {"textractor", "agent"}
    assert page.rows["textractor"].state.text() == "Not found. In Textractor, add a WebSocket extension (port 6677)."
    assert page.rows["agent"].state.text() == "Not found. In Agent, turn on its WebSocket server (port 9001)."
    assert page.rows["agent"].state.foregroundRole() == MUTED
    assert page.rows["agent"].name.text() == "Agent"
    assert page.rows["agent"].name.toolTip() == "localhost:9001"
    assert h.next_enabled()  # a hooker that is not running does not block the wizard


def test_every_default_hooker_and_a_user_added_source_has_its_hint():
    textractor, agent, luna = DEFAULT_TEXT_SOURCES
    assert source_hint(luna) == "Not found. In LunaTranslator, turn on its network service (port 2333)."
    assert source_hint(TextSourceConfig("agent", "Agent", "localhost:9002")) == (
        "Not found. In Agent, turn on its WebSocket server (port 9002)."
    )
    assert (
        source_hint(TextSourceConfig("my-hooker", "Mine", "127.0.0.1:7000/text"))
        == "Not found yet (127.0.0.1:7000/text)"
    )


def test_a_connected_source_waits_for_a_line_and_a_line_replaces_its_state(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.sink for s in h.sources))
    textractor = h.sources[0]
    h.on_loop(lambda: textractor._set(SourceStatus.CONNECTED))
    qtbot.waitUntil(lambda: page.rows["textractor"].state.text() == CONNECTED_TEXT)
    assert CONNECTED_TEXT == "Connected, waiting for a line"
    h.on_loop(lambda: textractor._set(SourceStatus.RECEIVING))
    h.on_loop(lambda: textractor.sink("<b>お前は誰だ？</b>", 1.0, "textractor"))
    qtbot.waitUntil(lambda: page.rows["textractor"].state.text() == "<b>お前は誰だ？</b>")
    assert page.rows["textractor"].state.textFormat() == Qt.TextFormat.PlainText
    assert page.rows["textractor"].state.foregroundRole() == QPalette.ColorRole.WindowText
    assert page.rows["agent"].state.text() == source_hint(DEFAULT_TEXT_SOURCES[1])


def test_the_rows_are_two_top_aligned_columns(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    grid = h.wizard.sources_page._grid
    qtbot.waitUntil(lambda: grid.count() == 4)
    assert grid.columnCount() == 2
    assert grid.horizontalSpacing() == 16
    for index in range(grid.count()):
        assert grid.itemAt(index).alignment() == Qt.AlignmentFlag.AlignTop


@pytest.mark.parametrize("leave", ["next", "back", "close"])
def test_leaving_the_step_stops_every_source_on_the_io_loop_and_waits_for_it(qtbot, io_loop, leave):
    start = WizardStep.OBS if leave == "back" else WizardStep.SOURCES
    h = Harness(qtbot, io_loop, config=sources_config(), start=start)
    if leave == "back":
        h.wizard.obs_page.button.click()
        qtbot.waitUntil(h.next_enabled)
        h.wizard.next()
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.started_on for s in h.sources))
    {"next": h.wizard.next, "back": h.wizard.back, "close": h.wizard.reject}[leave]()
    qtbot.waitUntil(lambda: all(s.closed for s in h.sources))
    assert {s.stopped_on.name for s in h.sources} == {"io-loop"}


def test_coming_back_starts_fresh_sources_and_old_ones_no_longer_update_the_rows(qtbot, io_loop, caplog):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.sink for s in h.sources))
    old = h.sources[0]
    h.wizard.next()
    qtbot.waitUntil(lambda: old.closed)
    h.wizard.back()
    qtbot.waitUntil(lambda: len(h.sources) == 4 and all(s.sink for s in h.sources))
    h.on_loop(lambda: old.sink("stale", 1.0, "textractor"))
    h.on_loop(lambda: h.sources[2].sink("fresh", 2.0, "textractor"))
    qtbot.waitUntil(lambda: page.rows["textractor"].state.text() == "fresh")
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []  # no call on a gone row


def test_no_enabled_source_says_where_to_turn_one_on(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=AppConfig(text_sources=()), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    assert page.rows == {}
    assert page.empty.text() == "No text source is enabled. Turn one on in Settings -> Advanced -> Text hookers."
    assert page.empty.text() == NO_SOURCE_TEXT
    assert not page.empty.isHidden()


@pytest.mark.parametrize("wayland", [True, False])
def test_the_clipboard_line_names_the_profile_option_and_warns_about_wayland_only_there(qtbot, io_loop, wayland):
    h = Harness(qtbot, io_loop, start=WizardStep.SOURCES, wayland=wayland)
    text = h.wizard.sources_page.clipboard.text()
    assert CLIPBOARD_TEXT == (
        'No text hooker? A game\'s profile can also take copied text: choose "Copied text (clipboard)" under Text from.'
    )
    assert text == (f"{CLIPBOARD_TEXT} {WAYLAND_CLIPBOARD_TEXT}" if wayland else CLIPBOARD_TEXT)


def test_the_page_is_called_game_text(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.SOURCES, single_step=True)
    page = h.wizard.sources_page
    assert page.title() == "Game text"
    assert page.subTitle() == (
        "Start your game and your text hooker (Textractor, Agent or LunaTranslator). When a line from the "
        "game shows here, the app can read it. You can skip this and test later."
    )


# The add-ons step ----------------------------------------------------------------------------------


def test_each_addon_has_a_plain_title_a_description_and_one_install_button(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, single_step=True)
    page = h.wizard.addons_page
    vad, ocr = page.rows
    assert page.title() == "Optional extras"
    assert page.subTitle() == (
        "Each downloads only if you install it. Voice trimming can be installed later in Settings, screen "
        "reading in a game's profile."
    )
    assert vad.title.text() == "Voice trimming (recommended)"
    assert ocr.title.text() == "Screen reading (OCR)"
    assert (
        vad.description.text()
        == "After each session, ends every subtitle where the voice stops, so cards carry less music."
    )
    assert ocr.description.text() == "Reads the game's text from the screen, for games no text hooker can read."
    assert vad.button.text() == "Install (190 MB)"
    assert ocr.button.text() == "Install (210 MB)"
    assert vad.note.isHidden()
    assert not ocr.note.isHidden() and "X11" in ocr.note.text()
    assert vad.status.isHidden()
    assert vad.column.itemAt(vad.column.indexOf(vad.button)).alignment() == Qt.AlignmentFlag.AlignLeft
    assert h.wizard.button(QWizard.WizardButton.FinishButton).isEnabled()


def test_the_button_becomes_the_progress_bar_and_then_installed(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS)
    row = h.wizard.addons_page.rows[0]
    row.button.click()
    qtbot.waitUntil(lambda: row.progress.value() == 500)
    assert not row.progress.isHidden()
    assert row.button.isHidden()
    h.vad.proceed.set()
    qtbot.waitUntil(lambda: row.status.text() == "Installed")
    assert not row.status.isHidden()
    assert h.vad.thread is not None and h.vad.thread.name == "io-loop"
    assert row.progress.isHidden()
    assert row.button.isHidden()
    assert h.vad.installs == 1


def test_an_install_failure_is_shown_in_its_row(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS)
    row = h.wizard.addons_page.rows[1]
    h.ocr.error = RuntimeError("uv failed: no network")
    h.ocr.proceed.set()
    row.button.click()
    qtbot.waitUntil(lambda: "no network" in row.error.text())
    assert not row.error.isHidden()
    assert row.status.isHidden()
    assert not row.button.isHidden() and row.button.isEnabled()


def test_an_install_already_running_elsewhere_shows_installing(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, vad=FakeAddon(1, status=AddonStatus.INSTALLING))
    row = h.wizard.addons_page.rows[0]
    assert row.status.text() == "Installing…"
    assert row.button.isHidden()


@pytest.mark.parametrize(
    ("status", "text", "button"),
    [(AddonStatus.READY, "Installed", None), (AddonStatus.BROKEN, None, "Repair (96 MB)")],
)
def test_ready_and_broken_addons(qtbot, io_loop, status, text, button):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, vad=FakeAddon(96_000_000, status=status))
    row = h.wizard.addons_page.rows[0]
    if text is None:
        assert row.status.isHidden()
    else:
        assert row.status.text() == text and not row.status.isHidden()
    if button is None:
        assert row.button.isHidden()
    else:
        assert row.button.text() == button and row.button.isEnabled() and not row.button.isHidden()


# The wizard as a whole ------------------------------------------------------------------------------


def test_a_first_run_has_three_numbered_steps(qtbot, io_loop):
    h = Harness(qtbot, io_loop)
    assert h.wizard.pageIds() == [WizardStep.OBS, WizardStep.SOURCES, WizardStep.ADDONS]
    for number, step in enumerate(WizardStep, start=1):
        assert h.wizard.page(step).title().startswith(f"Step {number} of 3: ")
    assert [step.name for step in WizardStep] == ["OBS", "SOURCES", "ADDONS"]


@pytest.mark.parametrize("step", list(WizardStep))
def test_a_single_step_run_shows_only_that_page_with_finish(qtbot, io_loop, step):
    h = Harness(qtbot, io_loop, start=step, single_step=True)
    page = h.wizard.currentPage()
    assert h.wizard.currentId() == step
    assert page.nextId() == -1
    assert not page.title().startswith("Step")
    assert h.wizard.button(QWizard.WizardButton.FinishButton).isVisible()
    assert not h.wizard.button(NEXT).isVisible()


def test_the_last_page_says_where_sessions_are_saved(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS)
    saved_in = h.wizard.addons_page.saved_in
    assert saved_in.text() == f"Sessions are saved in {native_path(AppConfig().output_root)} (change it in Settings)."
    assert saved_in.text() == saved_in_text(AppConfig())
    assert not saved_in.isHidden()


def test_a_single_step_run_does_not_repeat_where_sessions_are_saved(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, single_step=True)
    assert h.wizard.addons_page.saved_in.isHidden()


@pytest.mark.parametrize("step", list(WizardStep))
def test_any_step_can_be_opened_on_its_own(qtbot, io_loop, step):
    h = Harness(qtbot, io_loop, start=step)
    assert h.wizard.currentId() == step


def test_gui_reaches_the_rest_only_through_interfaces_models_and_gui():
    tree = ast.parse(Path(wizard_module.__file__).read_text(encoding="utf-8"))
    imported = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("anki_miner_game")
    ]
    imported += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("anki_miner_game")
    ]
    assert imported
    assert [
        name
        for name in imported
        if not name.startswith(("anki_miner_game.models.", "anki_miner_game.interfaces.", "anki_miner_game.gui."))
    ] == []


def test_every_page_opens_at_one_size_bounded_by_the_screen(qtbot, io_loop):
    sizes = set()
    for step in WizardStep:
        wizard = Harness(qtbot, io_loop, start=step).wizard
        screen = available_size(wizard)
        assert wizard.width() <= max(WIZARD_SIZE.width(), wizard.minimumSizeHint().width())
        assert wizard.height() <= screen.height()
        sizes.add((wizard.width(), wizard.height()))
    assert len(sizes) == 1


def test_long_pages_scroll_down_never_sideways(qtbot, io_loop):
    h = Harness(qtbot, io_loop)
    for page in (h.wizard.obs_page, h.wizard.addons_page):
        assert page.scroll_area.frameShape() == QFrame.Shape.NoFrame
        assert page.scroll_area.widgetResizable()
        assert page.scroll_area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_the_add_on_rows_are_filled_before_their_page_first_shows(qtbot, io_loop):
    h = Harness(qtbot, io_loop)  # opens on OBS: the add-ons page has never been initialised
    assert h.wizard.addons_page.rows[0].button.text() == "Install (190 MB)"


def test_no_user_facing_wizard_text_uses_words_outside_the_glossary():
    assert glossary_misses(wizard_module) == []
