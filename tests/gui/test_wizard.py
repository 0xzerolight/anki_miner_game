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
from PyQt6.QtWidgets import QWizard

import anki_miner_game.gui.wizard as wizard_module
from anki_miner_game.gui.wizard import (
    OBS_DOWNLOAD_URL,
    ObsCheck,
    ObsSetup,
    ObsStatus,
    SetupWizard,
    WizardStep,
)
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import DEFAULT_TEXT_SOURCES, AppConfig, TextSourceConfig
from anki_miner_game.models.messages import SourceStatus
from anki_miner_game.obs.provision import ObsProvisioner
from tests.gui.wizard_fakes import FakeDiscovery, WizardObs
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
    assert "What the app changes in OBS" in page.changes.text()
    assert "auto-configuration wizard" in page.changes.text()
    assert not h.next_enabled()
    qtbot.mouseClick(page.button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(h.next_enabled)
    assert page.status.text() == "OBS 32.2.2 is set up."
    assert h.obs.threads[0].name == "io-loop"


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
    assert "disk full" in page.status.text()
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
    qtbot.waitUntil(lambda: "see the log" in page.status.text())
    assert page.button.isEnabled() and not h.next_enabled()


def test_work_that_cannot_reach_the_io_loop_is_reported_and_the_step_stays_usable(qtbot, io_loop, caplog):
    def stopped(coro):
        raise RuntimeError("Event loop is closed")

    obs = StubObsSetup(ObsCheck(ObsStatus.READY, "ok"))
    h = Harness(qtbot, io_loop, obs=obs, run=stopped)
    page = h.wizard.obs_page
    page.button.click()
    assert "see the log" in page.status.text()
    assert page.button.isEnabled() and not h.next_enabled()
    assert obs.configs == []  # the coroutine was closed, never run


def test_obs_errors_are_plain_text_not_markup(qtbot, io_loop):
    h = Harness(qtbot, io_loop, obs=StubObsSetup(ObsCheck(ObsStatus.FAILED, "Cannot connect to OBS: <b>x</b>")))
    page = h.wizard.obs_page
    page.button.click()
    qtbot.waitUntil(lambda: "Cannot connect" in page.status.text())
    assert page.status.textFormat() == Qt.TextFormat.PlainText


def test_the_real_obs_step_runs_on_the_io_loop_and_returns_obs_to_the_users_names(qtbot, io_loop):
    obs = WizardObs()
    setup = ObsSetup(FakeDiscovery(), obs, ObsProvisioner(obs, platform="linux", sleep=Sleeps()))
    h = Harness(qtbot, io_loop, obs=setup)
    h.wizard.obs_page.button.click()
    qtbot.waitUntil(h.next_enabled, timeout=10_000)
    assert "32.2.2" in h.wizard.obs_page.status.text()
    assert (obs.current_profile, obs.current_collection) == ("Untitled", "Untitled")


# The text sources step ----------------------------------------------------------------------------


def sources_config() -> AppConfig:
    textractor, agent, luna = DEFAULT_TEXT_SOURCES
    return AppConfig(text_sources=(textractor, agent, TextSourceConfig(luna.id, luna.name, luna.uri, enabled=False)))


def test_each_enabled_source_starts_on_the_io_loop_and_waits_for_a_line(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    assert h.wizard.currentId() == WizardStep.SOURCES
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.started_on for s in h.sources))
    assert [s.id for s in h.sources] == ["textractor", "agent"]
    assert {s.started_on.name for s in h.sources} == {"io-loop"}
    assert set(page.rows) == {"textractor", "agent"}
    for row in page.rows.values():
        assert row.line.text() == "waiting for a line"
    qtbot.waitUntil(lambda: page.rows["agent"].status.text() == "connecting")
    assert h.next_enabled()  # a hooker that is not running does not block the wizard


def test_a_line_and_a_status_change_show_in_their_row(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=sources_config(), start=WizardStep.SOURCES)
    page = h.wizard.sources_page
    qtbot.waitUntil(lambda: len(h.sources) == 2 and all(s.sink for s in h.sources))
    textractor = h.sources[0]
    h.on_loop(lambda: textractor._set(SourceStatus.RECEIVING))
    h.on_loop(lambda: textractor.sink("<b>お前は誰だ？</b>", 1.0, "textractor"))
    qtbot.waitUntil(lambda: page.rows["textractor"].line.text() == "<b>お前は誰だ？</b>")
    assert page.rows["textractor"].line.textFormat() == Qt.TextFormat.PlainText
    assert page.rows["textractor"].status.text() == "receiving"
    assert page.rows["agent"].line.text() == "waiting for a line"


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
    qtbot.waitUntil(lambda: page.rows["textractor"].line.text() == "fresh")
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []  # no call on a gone row


def test_no_enabled_source_says_where_to_add_one(qtbot, io_loop):
    cfg = AppConfig(text_sources=())
    h = Harness(qtbot, io_loop, config=cfg, start=WizardStep.SOURCES)
    assert h.wizard.sources_page.rows == {}
    assert "Settings" in h.wizard.sources_page.empty.text()
    assert not h.wizard.sources_page.empty.isHidden()


@pytest.mark.parametrize("wayland", [True, False])
def test_the_clipboard_note_warns_about_wayland_only_there(qtbot, io_loop, wayland):
    h = Harness(qtbot, io_loop, start=WizardStep.SOURCES, wayland=wayland)
    text = h.wizard.sources_page.clipboard.text()
    assert "clipboard" in text
    assert ("Wayland" in text) is wayland
    if wayland:
        assert "focus" in text and "websocket" in text


# The output folder step ----------------------------------------------------------------------------


def test_the_folder_step_shows_the_configured_folder(qtbot, io_loop):
    h = Harness(qtbot, io_loop, config=AppConfig(output_root="/games/rec"), start=WizardStep.FOLDER)
    assert h.wizard.folder_page.path.text() == "/games/rec"


def test_a_relative_folder_is_refused(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.FOLDER)
    h.wizard.folder_page.path.setText("recordings")
    h.wizard.next()
    assert h.wizard.currentId() == WizardStep.FOLDER
    assert "full path" in h.wizard.folder_page.error.text()
    assert h.saved == []


def test_an_empty_folder_cannot_go_on(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.FOLDER)
    h.wizard.folder_page.path.setText("")
    assert not h.next_enabled()


def test_the_chosen_folder_is_created_and_saved(qtbot, io_loop, tmp_path):
    h = Harness(qtbot, io_loop, start=WizardStep.FOLDER)
    folder = tmp_path / "Game Sessions" / "rec"
    h.wizard.folder_page.path.setText(str(folder))
    h.wizard.next()
    assert h.wizard.currentId() == WizardStep.ADDONS
    assert folder.is_dir()
    assert h.saved[-1].output_root == str(folder)
    assert h.wizard.config.output_root == str(folder)


def test_a_folder_that_cannot_be_created_is_reported(qtbot, io_loop, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    h = Harness(qtbot, io_loop, start=WizardStep.FOLDER)
    h.wizard.folder_page.path.setText(str(blocker / "rec"))
    h.wizard.next()
    assert h.wizard.currentId() == WizardStep.FOLDER
    assert "cannot be created" in h.wizard.folder_page.error.text()


def test_a_config_that_cannot_be_saved_keeps_the_step(qtbot, io_loop, tmp_path):
    h = Harness(qtbot, io_loop, start=WizardStep.FOLDER, save_error=OSError("read-only"))
    h.wizard.folder_page.path.setText(str(tmp_path / "rec"))
    h.wizard.next()
    assert h.wizard.currentId() == WizardStep.FOLDER
    assert "read-only" in h.wizard.folder_page.error.text()
    assert h.wizard.config.output_root == AppConfig().output_root


# The add-ons step ----------------------------------------------------------------------------------


def test_each_addon_shows_its_size_note_and_status(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS)
    vad, ocr = h.wizard.addons_page.rows
    assert "about 190 MB" in vad.size.text()
    assert "about 210 MB" in ocr.size.text()
    assert vad.note.isHidden()
    assert not ocr.note.isHidden() and "X11" in ocr.note.text()
    assert vad.status.text() == "Not installed"
    assert vad.button.isEnabled() and vad.button.text() == "Install"
    assert h.wizard.button(QWizard.WizardButton.FinishButton).isEnabled()


def test_install_runs_on_the_io_loop_shows_progress_and_ends_installed(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS)
    row = h.wizard.addons_page.rows[0]
    row.button.click()
    qtbot.waitUntil(lambda: row.progress.value() == 500)
    assert not row.progress.isHidden()
    assert not row.button.isEnabled()
    assert row.status.text() == "Installing…"
    h.vad.proceed.set()
    qtbot.waitUntil(lambda: row.status.text() == "Installed")
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
    assert row.status.text() == "Not installed"
    assert row.button.isEnabled()


def test_an_install_already_running_elsewhere_disables_the_button(qtbot, io_loop):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, vad=FakeAddon(1, status=AddonStatus.INSTALLING))
    row = h.wizard.addons_page.rows[0]
    assert row.status.text() == "Installing…"
    assert not row.button.isEnabled()


@pytest.mark.parametrize(
    ("status", "text", "button"),
    [(AddonStatus.READY, "Installed", None), (AddonStatus.BROKEN, "Damaged; install it again to repair it", "Repair")],
)
def test_ready_and_broken_addons(qtbot, io_loop, status, text, button):
    h = Harness(qtbot, io_loop, start=WizardStep.ADDONS, vad=FakeAddon(1, status=status))
    row = h.wizard.addons_page.rows[0]
    assert row.status.text() == text
    if button is None:
        assert row.button.isHidden()
    else:
        assert row.button.text() == button and row.button.isEnabled()


# The wizard as a whole ------------------------------------------------------------------------------


def test_the_four_steps_come_in_the_spec_order(qtbot, io_loop):
    h = Harness(qtbot, io_loop)
    assert h.wizard.pageIds() == [WizardStep.OBS, WizardStep.SOURCES, WizardStep.FOLDER, WizardStep.ADDONS]


@pytest.mark.parametrize("step", list(WizardStep))
def test_any_step_can_be_opened_on_its_own(qtbot, io_loop, step):
    h = Harness(qtbot, io_loop, start=step)
    assert h.wizard.currentId() == step


def test_gui_reaches_the_rest_only_through_interfaces():
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
        name for name in imported if not name.startswith(("anki_miner_game.models.", "anki_miner_game.interfaces."))
    ] == []
