"""The game profile dialog (spec 5 ``GameProfile``, 11.3 window picker, 12 texts, 14 OCR area).

The dialog's OBS and owocr calls run on a real asyncio loop in a thread, as on the app's I/O loop;
OBS is T14's ``FakeObs`` behind the real ``ObsProvisioner``, with R2's recorded window lists.
"""

import asyncio
import threading
from collections.abc import Iterator
from dataclasses import replace

import pytest

from anki_miner_game.gui.game_profile_dialog import (
    AUTO_START_NOTE,
    CLOUD_OCR_NOTE,
    WINDOW_CLOSE_NOTE,
    CapturePicker,
    GameProfileDialog,
    ProfileDialogServices,
    window_label,
)
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import DEFAULT_TEXT_SOURCES, TextSourceConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.messages import AppState
from anki_miner_game.models.obs import WindowItem
from anki_miner_game.models.profile import (
    AudioMode,
    AudioSettings,
    AutoSettings,
    CaptureKind,
    CaptureSettings,
    FilterSettings,
    GameProfile,
    OcrEngine,
    OcrSettings,
    TextMode,
)
from anki_miner_game.obs.provision import ObsProvisioner
from anki_miner_game.session.naming import slugify
from tests.gui.obs_listing_fake import ListingObs, recorded_window_lists
from tests.obs.fake_obs import LINUX_X11_KINDS, WINDOWS_KINDS

BEFORE, RETITLED, CLOSED = recorded_window_lists("window_retitle.jsonl")
STORED = BEFORE[0]["itemValue"]
RETITLED_VALUE = RETITLED[1]["itemValue"]
X11_NOTE = "OCR on Linux needs an X11 session; Wayland sessions are not supported."


class FakeSession:
    def __init__(self) -> None:
        self.state = AppState.IDLE


class FakeOcr:
    """``AddonService`` and ``OcrAreaPicker`` in one, as the OCR add-on is."""

    def __init__(self, *, answer: str | None = "100,100,900,260", error: str | None = None, note: str | None = None):
        self.answer = answer
        self.error = error
        self._note = note
        self.picks: list[str | None] = []

    def status(self) -> AddonStatus:
        return AddonStatus.READY

    @property
    def size_bytes(self) -> int:
        return 1

    @property
    def note(self) -> str | None:
        return self._note

    async def install(self, progress: object) -> None:
        raise AssertionError("the dialog never installs")

    async def pick(self, window_title: str | None) -> str | None:
        self.picks.append(window_title)
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.answer


@pytest.fixture
def io_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="io-loop", daemon=True)
    thread.start()
    yield loop

    async def cancel_all() -> None:
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(cancel_all(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


class Rig:
    def __init__(
        self, loop: asyncio.AbstractEventLoop, qtbot, *, platform: str = "linux", obs: ListingObs | None = None
    ):
        self.loop = loop
        self.qtbot = qtbot
        self.platform = platform
        kinds = WINDOWS_KINDS if platform == "win32" else LINUX_X11_KINDS
        self.obs = obs or ListingObs(input_kinds=kinds, window_lists={"xcomposite_input": RETITLED})
        self.session = FakeSession()
        self.provisioner = ObsProvisioner(self.obs, platform=platform)
        self.ocr = FakeOcr()
        self.picker = CapturePicker(self.obs, self.provisioner, self.session, switch_timeout_s=2.0)

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def open(
        self,
        profile: GameProfile | None = None,
        *,
        text_sources: tuple[TextSourceConfig, ...] = DEFAULT_TEXT_SOURCES,
        taken: tuple[str, ...] = (),
    ) -> GameProfileDialog:
        services = ProfileDialogServices(
            run=self.run,
            capture=self.picker,
            ocr_picker=self.ocr,
            ocr_addon=self.ocr,
            slugify=slugify,
            platform=self.platform,
        )
        dialog = GameProfileDialog(services, text_sources, profile, taken_slugs=taken)
        self.qtbot.addWidget(dialog)
        self.settle(dialog)
        return dialog

    def settle(self, dialog: GameProfileDialog) -> None:
        self.qtbot.waitUntil(lambda: "asking" not in dialog.capture_method_label.text(), timeout=5000)

    def list_windows(self, dialog: GameProfileDialog) -> None:
        dialog.list_windows_button.click()
        self.qtbot.waitUntil(dialog.list_windows_button.isEnabled, timeout=5000)


@pytest.fixture
def rig(io_loop, qtbot) -> Rig:
    return Rig(io_loop, qtbot)


@pytest.fixture
def win_rig(io_loop, qtbot) -> Rig:
    return Rig(io_loop, qtbot, platform="win32")


def texts(dialog: GameProfileDialog) -> str:
    from PyQt6.QtWidgets import QAbstractButton, QGroupBox, QLabel

    parts = [label.text() for label in dialog.findChildren(QLabel)]
    parts += [button.text() for button in dialog.findChildren(QAbstractButton)]
    parts += [box.title() for box in dialog.findChildren(QGroupBox)]
    return "\n".join(parts)


FULL_HOOK = GameProfile(
    slug="steins-gate",
    title="Steins;Gate",
    text_mode=TextMode.HOOK,
    source_ids=("agent",),
    clipboard=True,
    capture=CaptureSettings(kind=CaptureKind.XCOMPOSITE, window=STORED),
    audio=AudioSettings(mode=AudioMode.DESKTOP),
    filters=FilterSettings(speaker_strip=False, typewriter_merge=True),
    ocr=OcrSettings(engine=OcrEngine.MEIKIOCR, language="ja", window_title="Kept", rects="1,2,3,4"),
    auto=AutoSettings(enabled=True, start_on_first_line=True, stop_idle_minutes=25, stop_on_window_close=False),
)

FULL_OCR_WINDOWS = GameProfile(
    slug="zero-escape",
    title="Zero Escape",
    text_mode=TextMode.OCR,
    capture=CaptureSettings(kind=CaptureKind.WINDOW, window="Zero:UnityWndClass:zero.exe"),
    audio=AudioSettings(mode=AudioMode.APP),
    filters=FilterSettings(speaker_strip=True, typewriter_merge=False),
    ocr=OcrSettings(engine=OcrEngine.GLENS, language="en", window_title="Zero Escape", rects="10,20,300,400"),
    auto=AutoSettings(enabled=False, start_on_first_line=False, stop_idle_minutes=0, stop_on_window_close=True),
)


def test_editing_keeps_every_field_of_a_hook_profile(rig: Rig) -> None:
    dialog = rig.open(FULL_HOOK)

    assert dialog.profile() == FULL_HOOK


def test_editing_keeps_every_field_of_an_ocr_profile_on_windows(win_rig: Rig) -> None:
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    assert dialog.profile() == FULL_OCR_WINDOWS


def test_save_emits_the_edited_profile(rig: Rig) -> None:
    dialog = rig.open(FULL_HOOK)
    dialog.title_edit.setText("  Steins;Gate 0 ")
    dialog.typewriter_check.setChecked(False)

    with rig.qtbot.waitSignal(dialog.profile_saved) as saved:
        dialog.buttons.button(dialog.buttons.StandardButton.Save).click()

    assert saved.args == [replace(FULL_HOOK, title="Steins;Gate 0", filters=FilterSettings(False, False))]
    assert dialog.result() == dialog.DialogCode.Accepted.value


def test_a_new_game_takes_its_slug_from_the_title_and_the_platform_defaults(win_rig: Rig) -> None:
    dialog = win_rig.open()
    dialog.title_edit.setText("Persona 5")

    profile = dialog.profile()

    assert profile.slug == "persona-5"
    assert profile.audio.mode is AudioMode.APP
    assert profile.ocr.engine is OcrEngine.ONEOCR
    assert profile.source_ids is None


def test_a_new_game_cannot_take_an_existing_slug(rig: Rig) -> None:
    dialog = rig.open(taken=("persona-5",))
    dialog.title_edit.setText("Persona 5")

    with rig.qtbot.assertNotEmitted(dialog.profile_saved):
        dialog.accept()

    assert "exists already" in dialog.problems_label.text()
    assert not dialog.problems_label.isHidden()


def test_an_invalid_profile_is_not_saved_and_the_problem_is_shown(win_rig: Rig) -> None:
    dialog = win_rig.open()
    dialog.title_edit.setText("Persona 5")
    assert dialog.profile().audio.mode is AudioMode.APP  # needs a pinned window

    with win_rig.qtbot.assertNotEmitted(dialog.profile_saved):
        dialog.accept()

    assert "pinned window" in dialog.problems_label.text()
    assert dialog.result() != dialog.DialogCode.Accepted.value


def test_hook_mode_needs_a_source_or_the_clipboard(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, source_ids=(), clipboard=False))

    assert "text source" in "; ".join(dialog.problems(dialog.profile()))
    dialog.clipboard_check.setChecked(True)
    assert dialog.problems(dialog.profile()) == []


def test_every_enabled_source_is_the_default_and_disabled_ones_are_marked(rig: Rig) -> None:
    sources = (*DEFAULT_TEXT_SOURCES[:2], replace(DEFAULT_TEXT_SOURCES[2], enabled=False))
    dialog = rig.open(replace(FULL_HOOK, source_ids=None), text_sources=sources)

    assert dialog.all_sources_check.isChecked()
    assert not dialog.source_checks["agent"].isEnabled()
    assert "turned off" in dialog.source_checks["luna"].text()
    dialog.all_sources_check.setChecked(False)
    dialog.source_checks["luna"].setChecked(True)
    assert dialog.profile().source_ids == ("luna",)


def test_ocr_mode_shows_the_ocr_fields_and_drops_hook_sources(rig: Rig) -> None:
    dialog = rig.open(FULL_HOOK)
    assert dialog.ocr_group.isHidden() and not dialog.hook_group.isHidden()

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(TextMode.OCR))

    assert dialog.hook_group.isHidden() and not dialog.ocr_group.isHidden()
    profile = dialog.profile()
    assert profile.text_mode is TextMode.OCR
    assert profile.source_ids is None and profile.clipboard is False
    assert dialog.problems(profile) == []


def test_the_capture_method_in_use_is_shown_and_follows_the_capture_setting(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))
    assert dialog.capture_method_label.text() == "Capture method in use: Window Capture (X11)"

    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.PIPEWIRE))
    rig.settle(dialog)

    assert dialog.capture_method_label.text() == "Capture method in use: Screen Capture (PipeWire)"
    assert rig.obs.mutating() == []


def test_a_late_answer_for_an_older_capture_setting_is_ignored(rig: Rig) -> None:
    release = threading.Event()
    real_method = rig.provisioner.capture_method
    calls = 0

    async def first_one_slow(profile: GameProfile) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
        return await real_method(profile)

    rig.provisioner.capture_method = first_one_slow  # type: ignore[method-assign]
    services = ProfileDialogServices(
        run=rig.run, capture=rig.picker, ocr_picker=rig.ocr, ocr_addon=rig.ocr, slugify=slugify, platform="linux"
    )
    dialog = GameProfileDialog(
        services, DEFAULT_TEXT_SOURCES, replace(FULL_HOOK, capture=CaptureSettings(window=STORED))
    )
    rig.qtbot.addWidget(dialog)
    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.PIPEWIRE))
    rig.settle(dialog)
    assert dialog.capture_method_label.text() == "Capture method in use: Screen Capture (PipeWire)"

    release.set()
    rig.qtbot.waitUntil(lambda: not dialog._pending, timeout=5000)

    assert dialog.capture_method_label.text() == "Capture method in use: Screen Capture (PipeWire)"


def test_the_capture_method_says_when_obs_does_not_answer(io_loop, qtbot) -> None:
    obs = ListingObs(input_kinds=LINUX_X11_KINDS)
    obs.crashed = True
    dialog = Rig(io_loop, qtbot, obs=obs).open(FULL_HOOK)

    assert "OBS did not answer" in dialog.capture_method_label.text()


def test_capture_choices_follow_the_platform_and_keep_a_stored_kind(rig: Rig, win_rig: Rig) -> None:
    linux = rig.open(replace(FULL_HOOK, capture=CaptureSettings(kind=CaptureKind.GAME)))
    windows = win_rig.open(FULL_OCR_WINDOWS)

    def kinds(dialog: GameProfileDialog) -> list[object]:
        return [dialog.capture_kind_combo.itemData(i) for i in range(dialog.capture_kind_combo.count())]

    assert kinds(linux) == [CaptureKind.AUTO, CaptureKind.PIPEWIRE, CaptureKind.XCOMPOSITE, CaptureKind.GAME]
    assert linux.profile().capture.kind is CaptureKind.GAME
    assert kinds(windows) == [CaptureKind.AUTO, CaptureKind.GAME, CaptureKind.WINDOW]


def test_the_picker_lists_enabled_windows_and_pins_a_value_verbatim(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))

    rig.list_windows(dialog)

    offered = [
        (dialog.window_combo.itemText(i), dialog.window_combo.itemData(i)) for i in range(dialog.window_combo.count())
    ]
    assert offered == [(i["itemName"], i["itemValue"]) for i in RETITLED if i["itemEnabled"]]
    assert rig.obs.current_collection == "Untitled"
    assert rig.obs.listed_in == [OBS_COLLECTION_NAME]

    dialog.window_combo.setCurrentIndex(0)
    dialog.window_combo.activated.emit(0)

    assert dialog.profile().capture.window == RETITLED_VALUE
    assert dialog.pinned_label.text() == "amg-probe-window - level 2 - 59 fps"


def test_a_pinned_window_that_is_not_listed_stays_pinned(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))

    rig.list_windows(dialog)

    values = [dialog.window_combo.itemData(i) for i in range(dialog.window_combo.count())]
    assert STORED not in values
    assert dialog.window_combo.currentIndex() == -1
    assert dialog.pinned_label.text() == "amg-probe-window"
    assert dialog.profile().capture.window == STORED


def test_unpin_clears_the_window(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))

    dialog.unpin_button.click()

    assert dialog.profile().capture.window is None
    assert not dialog.unpin_button.isEnabled()


def test_while_armed_the_picker_lists_at_once(rig: Rig) -> None:
    rig.run(rig.provisioner.ensure_collection(FULL_HOOK)).result(5)
    rig.session.state = AppState.ARMED
    rig.obs.reset_calls()
    dialog = rig.open(FULL_HOOK)

    rig.list_windows(dialog)

    assert dialog.window_combo.count() == len([i for i in RETITLED if i["itemEnabled"]])
    assert rig.obs.mutating() == []


def test_an_active_output_leaves_the_picker_empty_and_says_which(io_loop, qtbot) -> None:
    obs = ListingObs(
        input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED}, active=["GetRecordStatus"]
    )
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(FULL_HOOK)

    rig.list_windows(dialog)

    assert dialog.window_combo.count() == 0
    assert "recording" in dialog.windows_message.text()
    assert obs.listed_in == []


def test_a_listing_failure_is_shown(io_loop, qtbot) -> None:
    obs = ListingObs(input_kinds=LINUX_X11_KINDS)
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(FULL_HOOK)
    obs.crashed = True

    rig.list_windows(dialog)

    assert "OBS could not list its windows" in dialog.windows_message.text()


def test_the_switch_back_warning_is_shown_with_the_windows(io_loop, qtbot) -> None:
    obs = ListingObs(input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED}, changed_event="never")
    rig = Rig(io_loop, qtbot, obs=obs)
    rig.picker = CapturePicker(obs, rig.provisioner, rig.session, switch_timeout_s=0.05)
    dialog = rig.open(FULL_HOOK)

    rig.list_windows(dialog)

    assert dialog.window_combo.count() > 0
    assert "Untitled" in dialog.windows_message.text()


def test_an_empty_window_list_says_why(io_loop, qtbot) -> None:
    obs = ListingObs(input_kinds=("pipewire-screen-capture-source", "pulse_output_capture"))
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings()))

    rig.list_windows(dialog)

    assert dialog.window_combo.count() == 0
    assert "PipeWire" in dialog.windows_message.text()


def test_closing_the_dialog_lets_a_listing_finish_and_obs_switches_back(rig: Rig) -> None:
    """Cancelling a request already sent drops the link (T12), so the dialog's OBS calls run to their end."""
    entered = threading.Event()
    release = threading.Event()
    real_list = rig.provisioner.list_windows

    async def slow() -> list[WindowItem]:
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
        return await real_list()

    rig.provisioner.list_windows = slow  # type: ignore[method-assign]
    dialog = rig.open(FULL_HOOK)
    dialog.list_windows_button.click()
    assert entered.wait(5)
    assert rig.obs.current_collection == OBS_COLLECTION_NAME

    dialog.reject()
    release.set()

    rig.qtbot.waitUntil(lambda: rig.obs.current_collection == "Untitled", timeout=5000)
    assert rig.obs.listed_in == [OBS_COLLECTION_NAME]


def test_select_ocr_area_stores_the_rectangles_verbatim(win_rig: Rig) -> None:
    dialog = win_rig.open(replace(FULL_OCR_WINDOWS, ocr=replace(FULL_OCR_WINDOWS.ocr, rects=None)))

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert win_rig.ocr.picks == ["Zero Escape"]
    assert dialog.profile().ocr.rects == "100,100,900,260"
    assert dialog.area_label.text() == "100,100,900,260"


def test_on_linux_the_area_picker_gets_no_window_title(rig: Rig) -> None:
    dialog = rig.open(replace(FULL_HOOK, text_mode=TextMode.OCR, source_ids=None, clipboard=False))

    dialog.select_area_button.click()
    rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert rig.ocr.picks == [None]
    assert dialog.window_title_edit is None
    assert dialog.profile().ocr.window_title == "Kept"


def test_a_cancelled_area_selection_keeps_the_previous_area(win_rig: Rig) -> None:
    win_rig.ocr.answer = None
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert dialog.profile().ocr.rects == "10,20,300,400"
    assert "previous" in dialog.area_message.text()


def test_an_area_selection_error_is_shown_in_the_dialog(win_rig: Rig) -> None:
    win_rig.ocr.error = "owocr could not start: no such file"
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert dialog.area_message.text() == "owocr could not start: no such file"
    assert dialog.profile().ocr.rects == "10,20,300,400"


def test_clear_area(win_rig: Rig) -> None:
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.clear_area_button.click()

    assert dialog.profile().ocr.rects is None


@pytest.mark.parametrize(
    ("engine", "cloud"), [(OcrEngine.GLENS, True), (OcrEngine.BING, True), (OcrEngine.MEIKIOCR, False)]
)
def test_cloud_engines_state_that_screenshots_leave_the_machine(rig: Rig, engine: OcrEngine, cloud: bool) -> None:
    dialog = rig.open(replace(FULL_HOOK, text_mode=TextMode.OCR, source_ids=None, clipboard=False))

    dialog.engine_combo.setCurrentIndex(dialog.engine_combo.findData(engine))

    assert dialog.cloud_note.isHidden() is not cloud
    assert dialog.cloud_note.text() == CLOUD_OCR_NOTE
    assert "leaves this machine" in CLOUD_OCR_NOTE


def test_engines_offered_are_the_platforms_local_one_and_the_cloud_ones(rig: Rig, win_rig: Rig) -> None:
    def engines(dialog: GameProfileDialog) -> list[object]:
        return [dialog.engine_combo.itemData(i) for i in range(dialog.engine_combo.count())]

    assert engines(rig.open(FULL_HOOK)) == [OcrEngine.MEIKIOCR, OcrEngine.GLENS, OcrEngine.BING]
    assert engines(win_rig.open(FULL_OCR_WINDOWS)) == [OcrEngine.ONEOCR, OcrEngine.GLENS, OcrEngine.BING]


def test_the_ocr_addon_status_and_its_platform_note_are_shown(rig: Rig) -> None:
    rig.ocr = FakeOcr(note=X11_NOTE)
    dialog = rig.open(FULL_HOOK)

    assert "installed" in dialog.ocr_status_label.text()
    assert X11_NOTE in dialog.ocr_status_label.text()


def test_the_auto_mode_texts_of_spec_12_are_shown(rig: Rig) -> None:
    dialog = rig.open(FULL_HOOK)

    shown = texts(dialog)

    assert AUTO_START_NOTE in shown and WINDOW_CLOSE_NOTE in shown
    assert "0:00" in AUTO_START_NOTE and "missing" in AUTO_START_NOTE
    assert "PipeWire" in WINDOW_CLOSE_NOTE and "idle" in WINDOW_CLOSE_NOTE


def test_auto_mode_off_keeps_its_settings(rig: Rig) -> None:
    dialog = rig.open(FULL_HOOK)

    dialog.auto_group.setChecked(False)

    assert dialog.profile().auto == replace(FULL_HOOK.auto, enabled=False)


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (STORED, "amg-probe-window"),
        ("Zero #22 1#3A The Game:UnityWndClass:zero.exe", "[zero.exe]: Zero # 1: The Game"),
        ("odd", "odd"),
    ],
)
def test_window_label(value: str, label: str) -> None:
    assert window_label(value) == label
