"""The game profile dialog (spec 5 ``GameProfile``, 11.3 window list, 12, 14; audit UJ-22b, UJ-25..UJ-31, D-03).

The dialog's OBS and owocr calls run on a real asyncio loop in a thread, as on the app's I/O loop; OBS is T14's
``FakeObs`` behind the real ``ObsProvisioner``, with R2's recorded window lists. The picker's start-OBS step is a
``StubStarter`` (an OBS that runs) unless a test gives another.
"""

import asyncio
import re
import threading
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractButton, QComboBox, QDialog, QLabel, QScrollArea, QStyle, QStyleOptionComboBox

from anki_miner_game.gui import strings
from anki_miner_game.gui.game_profile_dialog import (
    LOOKING_FOR_WINDOWS,
    NO_WINDOW_LINUX,
    NO_WINDOW_WINDOWS,
    STARTING_OBS,
    TEXT_FROM_LABELS,
    WAYLAND_WINDOW_NOTE,
    CapturePicker,
    GameProfileDialog,
    ProfileDialogServices,
    TextFrom,
    window_label,
    window_title,
)
from anki_miner_game.gui.widgets.layout import DIALOG_SCREEN_FRACTION, available_size
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import DEFAULT_TEXT_SOURCES, TextSourceConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.messages import AppState
from anki_miner_game.models.obs import REQUIRED_REQUESTS, ObsInfo, ObsStartStage, WindowItem
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
from anki_miner_game.obs.startup import LocalObsStarter
from anki_miner_game.session.naming import slugify
from tests.gui.obs_listing_fake import ListingObs, StartingObs, StubStarter, recorded_window_lists
from tests.obs.fake_obs import LINUX_X11_KINDS, WINDOWS_KINDS
from tests.session.actor_harness import FakeDiscovery

BEFORE, RETITLED, CLOSED = recorded_window_lists("window_retitle.jsonl")
STORED = BEFORE[0]["itemValue"]
RETITLED_VALUE = RETITLED[1]["itemValue"]
X11_NOTE = "OCR on Linux needs an X11 session; Wayland sessions are not supported."
ZERO_WINDOW = "Zero Escape#3A Chapter 1:UnityWndClass:zero.exe"
SG_WINDOW = "STEINS;GATE:SteinsGate:SteinsGate.exe"
WIN_LIST = [
    {"itemEnabled": True, "itemName": "[zero.exe]: Zero Escape: Chapter 1", "itemValue": ZERO_WINDOW},
    {"itemEnabled": True, "itemName": "[SteinsGate.exe]: STEINS;GATE", "itemValue": SG_WINDOW},
]


class FakeSession:
    def __init__(self) -> None:
        self.state = AppState.IDLE


class FakeOcr:
    """``AddonService`` and ``OcrAreaPicker`` in one, as the OCR add-on is."""

    def __init__(
        self,
        *,
        answer: str | None = "100,100,900,260",
        error: str | None = None,
        note: str | None = None,
        status: AddonStatus = AddonStatus.READY,
    ):
        self.answer = answer
        self.error = error
        self._note = note
        self.current = status
        self.picks: list[str | None] = []

    def status(self) -> AddonStatus:
        return self.current

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


class HeldStarter:
    """``ObsStarter`` for an OBS that was closed: it reports the start stages, then waits for ``release``."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    async def start(self, report: Any = None) -> ObsInfo:
        self.calls += 1
        assert report is not None
        report(ObsStartStage.ENABLING_SERVER)
        report(ObsStartStage.LAUNCHING)
        await asyncio.get_running_loop().run_in_executor(None, self.release.wait, 5)
        report(ObsStartStage.CONNECTING)
        return ObsInfo("32.2.2", "5.7.4", frozenset(REQUIRED_REQUESTS))


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
        self,
        loop: asyncio.AbstractEventLoop,
        qtbot,
        *,
        platform: str = "linux",
        obs: ListingObs | None = None,
        starter: Any = None,
    ):
        self.loop = loop
        self.qtbot = qtbot
        self.platform = platform
        if obs is None:
            if platform == "win32":
                obs = ListingObs(input_kinds=WINDOWS_KINDS, window_lists={"game_capture": WIN_LIST})
            else:
                obs = ListingObs(input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED})
        self.obs = obs
        self.session = FakeSession()
        self.provisioner = ObsProvisioner(self.obs, platform=platform)
        self.ocr = FakeOcr()
        self.installs = 0
        self.starter = starter if starter is not None else StubStarter(ObsStartStage.CONNECTING)
        self.picker = CapturePicker(
            self.obs, self.provisioner, self.session, starter=self.starter, switch_timeout_s=2.0
        )

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def install(self) -> None:
        self.installs += 1

    def services(self, *, installs: bool = True) -> ProfileDialogServices:
        return ProfileDialogServices(
            run=self.run,
            capture=self.picker,
            ocr_picker=self.ocr,
            ocr_addon=self.ocr,
            slugify=slugify,
            platform=self.platform,
            install_addons=self.install if installs else None,
        )

    def open(
        self,
        profile: GameProfile | None = None,
        *,
        text_sources: tuple[TextSourceConfig, ...] = DEFAULT_TEXT_SOURCES,
        taken: tuple[str, ...] = (),
        wayland: bool = False,
        installs: bool = True,
    ) -> GameProfileDialog:
        dialog = GameProfileDialog(
            self.services(installs=installs), text_sources, profile, taken_slugs=taken, wayland=wayland
        )
        self.qtbot.addWidget(dialog)
        self.settle(dialog)
        return dialog

    def settle(self, dialog: GameProfileDialog) -> None:
        self.qtbot.waitUntil(lambda: "asking" not in dialog.capture_method_label.text(), timeout=5000)

    def list_windows(self, dialog: GameProfileDialog) -> None:
        """Open the Game window dropdown, wait for its listing, close it again."""
        dialog.window_combo.showPopup()
        self.qtbot.waitUntil(lambda: not dialog.listing_windows, timeout=5000)
        dialog.window_combo.hidePopup()
        self.settle(dialog)

    def choose(self, dialog: GameProfileDialog, value: str | None) -> None:
        combo = dialog.window_combo
        index = 0 if value is None else combo.findData(value)
        assert index >= 0, value
        combo.setCurrentIndex(index)
        combo.activated.emit(index)


@pytest.fixture
def rig(io_loop, qtbot) -> Rig:
    return Rig(io_loop, qtbot)


@pytest.fixture
def win_rig(io_loop, qtbot) -> Rig:
    return Rig(io_loop, qtbot, platform="win32")


def offered(dialog: GameProfileDialog) -> list[tuple[str, object]]:
    combo = dialog.window_combo
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def texts(dialog: GameProfileDialog) -> str:
    parts = [label.text() for label in dialog.findChildren(QLabel)]
    parts += [button.text() for button in dialog.findChildren(QAbstractButton)]
    for combo in dialog.findChildren(QComboBox):
        parts += [combo.itemText(i) for i in range(combo.count())]
    return "\n".join(parts)


def save(rig: Rig, dialog: GameProfileDialog) -> list[object]:
    with rig.qtbot.waitSignal(dialog.profile_saved) as saved:
        dialog.buttons.button(dialog.buttons.StandardButton.Save).click()
    return saved.args


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

HOOKER_LINUX = GameProfile(
    slug="chaos-head",
    title="Chaos;Head",
    capture=CaptureSettings(),
    audio=AudioSettings(mode=AudioMode.DESKTOP),
    ocr=OcrSettings(engine=OcrEngine.MEIKIOCR),
)
"""Every default: hooker text, no window, auto mode off."""

OCR_LINUX = replace(HOOKER_LINUX, text_mode=TextMode.OCR)

WIN_HOOK_DESKTOP = GameProfile(
    slug="persona-5",
    title="Persona 5",
    capture=CaptureSettings(window=SG_WINDOW),
    audio=AudioSettings(mode=AudioMode.DESKTOP),
    ocr=OcrSettings(engine=OcrEngine.ONEOCR),
)


# Review Focus 3: Save without a change keeps every stored value ------------------------------------


@pytest.mark.parametrize(
    ("platform", "profile"),
    [
        ("linux", FULL_HOOK),  # a hooker subset + the clipboard
        ("linux", replace(FULL_HOOK, clipboard=False)),  # a subset alone
        ("linux", replace(FULL_HOOK, source_ids=(), clipboard=True)),  # copied text only
        ("linux", replace(FULL_HOOK, source_ids=None, clipboard=True)),  # every hooker + the clipboard
        ("win32", FULL_OCR_WINDOWS),
        ("win32", replace(FULL_OCR_WINDOWS, ocr=replace(FULL_OCR_WINDOWS.ocr, language="de"))),
        (
            "win32",
            replace(
                FULL_OCR_WINDOWS,
                capture=CaptureSettings(kind=CaptureKind.AUTO, window=ZERO_WINDOW),
                ocr=replace(FULL_OCR_WINDOWS.ocr, window_title="Zero Escape"),  # hand-trimmed
            ),
        ),
        ("win32", WIN_HOOK_DESKTOP),  # a window, but the whole desktop's sound
        (
            "linux",
            replace(
                FULL_HOOK,
                capture=CaptureSettings(kind=CaptureKind.AUTO),
                auto=AutoSettings(enabled=True, start_on_first_line=False, stop_idle_minutes=10),
            ),
        ),  # stop_on_window_close=True with no window
        ("linux", replace(FULL_HOOK, capture=CaptureSettings(kind=CaptureKind.GAME, window=STORED))),  # Windows kind
        (
            "linux",
            replace(
                FULL_HOOK,
                auto=AutoSettings(
                    enabled=False, start_on_first_line=True, stop_idle_minutes=25, stop_on_window_close=False
                ),
            ),
        ),  # auto settings while auto is off
    ],
    ids=[
        "subset+clipboard",
        "subset",
        "clipboard",
        "every+clipboard",
        "ocr-windows",
        "ocr-language-de",
        "ocr-title-trimmed",
        "window-desktop-sound",
        "close-without-window",
        "other-platform-kind",
        "auto-off",
    ],
)
def test_save_without_a_change_emits_the_stored_profile(io_loop, qtbot, platform: str, profile: GameProfile):
    rig = Rig(io_loop, qtbot, platform=platform)
    dialog = rig.open(profile)

    assert save(rig, dialog) == [profile]


# Text from (UJ-25) ----------------------------------------------------------------------------------


def test_text_from_offers_three_ways_and_a_fourth_only_for_a_profile_they_cannot_express(rig: Rig):
    plain = rig.open(HOOKER_LINUX)
    kept = rig.open(FULL_HOOK)

    assert [plain.text_from_combo.itemText(i) for i in range(plain.text_from_combo.count())] == [
        "A text hooker (Agent, Textractor, LunaTranslator)",
        "Copied text (clipboard)",
        "Reading the screen (OCR add-on)",
    ]
    assert TEXT_FROM_LABELS[TextFrom.CLIPBOARD] == strings.COPIED_TEXT
    assert kept.text_from_combo.count() == 4
    assert kept.text_from_combo.currentText() == "As saved: Agent and copied text"


@pytest.mark.parametrize(
    ("choice", "mode", "source_ids", "clipboard"),
    [
        (TextFrom.HOOKER, TextMode.HOOK, None, False),
        (TextFrom.CLIPBOARD, TextMode.HOOK, (), True),
        (TextFrom.OCR, TextMode.OCR, None, False),
    ],
)
def test_each_text_from_choice_sets_the_stored_fields(rig: Rig, choice, mode, source_ids, clipboard):
    dialog = rig.open(FULL_HOOK)
    dialog.text_from_combo.setCurrentIndex(dialog.text_from_combo.findData(choice))

    profile = dialog.profile()

    assert (profile.text_mode, profile.source_ids, profile.clipboard) == (mode, source_ids, clipboard)
    assert dialog.problems(profile) == []


def test_the_reading_the_screen_group_shows_only_for_ocr(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)
    assert dialog.ocr_group.isHidden()

    dialog.text_from_combo.setCurrentIndex(dialog.text_from_combo.findData(TextFrom.OCR))

    assert not dialog.ocr_group.isHidden()
    assert dialog.ocr_group.title() == "Reading the screen"


@pytest.mark.parametrize("wayland", [True, False])
def test_copied_text_warns_about_wayland_only_there(rig: Rig, wayland: bool):
    dialog = rig.open(replace(HOOKER_LINUX, source_ids=(), clipboard=True), wayland=wayland)

    assert dialog.clipboard_note.isHidden() is not wayland
    if wayland:
        assert dialog.clipboard_note.text() == strings.WAYLAND_CLIPBOARD_TEXT


def test_a_text_hooker_needs_one_turned_on_in_settings(rig: Rig):
    sources = tuple(replace(source, enabled=False) for source in DEFAULT_TEXT_SOURCES)
    dialog = rig.open(HOOKER_LINUX, text_sources=sources)

    problems = "; ".join(dialog.problems(dialog.profile()))

    assert "Settings -> Advanced -> Text hookers" in problems
    dialog.text_from_combo.setCurrentIndex(dialog.text_from_combo.findData(TextFrom.CLIPBOARD))
    assert dialog.problems(dialog.profile()) == []


# Game window (UJ-26, D-03, B4-01) -------------------------------------------------------------------


def test_the_first_item_says_what_is_recorded_without_a_window(rig: Rig, win_rig: Rig):
    assert rig.open(HOOKER_LINUX).window_combo.itemText(0) == NO_WINDOW_LINUX
    assert win_rig.open().window_combo.itemText(0) == NO_WINDOW_WINDOWS
    assert NO_WINDOW_WINDOWS == "None: records the whole screen and all sound"


def test_the_stored_window_is_the_current_item_from_the_start(rig: Rig):
    dialog = rig.open(FULL_HOOK)

    assert dialog.window_combo.currentData() == STORED
    assert dialog.window_combo.currentText() == "amg-probe-window"
    assert rig.obs.names() == ["GetInputKindList"]  # nothing listed before the dropdown opens


def test_opening_the_dropdown_lists_the_windows_and_choosing_one_stores_it_verbatim(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)

    assert offered(dialog) == [
        (NO_WINDOW_LINUX, None),
        *[(i["itemName"], i["itemValue"]) for i in RETITLED if i["itemEnabled"]],
    ]
    assert rig.obs.listed_in == [OBS_COLLECTION_NAME]
    assert rig.obs.current_collection == "Untitled"
    rig.choose(dialog, RETITLED_VALUE)
    assert dialog.profile().capture.window == RETITLED_VALUE
    assert dialog.windows_message.isHidden()


def test_a_stored_window_that_is_not_open_now_stays_chosen(rig: Rig):
    dialog = rig.open(FULL_HOOK)

    rig.list_windows(dialog)

    assert dialog.window_combo.currentData() == STORED
    assert dialog.window_combo.currentText() == "amg-probe-window (not open now)"
    assert dialog.profile().capture.window == STORED


def test_choosing_none_clears_the_window(rig: Rig):
    dialog = rig.open(FULL_HOOK)

    rig.choose(dialog, None)

    assert dialog.profile().capture.window is None


def test_every_open_lists_again(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)
    rig.list_windows(dialog)

    assert rig.obs.listed_in == [OBS_COLLECTION_NAME, OBS_COLLECTION_NAME]


def test_opening_the_list_with_obs_closed_says_it_starts_obs_until_the_windows_come(io_loop, qtbot):
    """D-03: a disabled item says what is happening; OBS's windows replace it."""
    starter = HeldStarter()
    rig = Rig(io_loop, qtbot, starter=starter)
    dialog = rig.open(HOOKER_LINUX)
    combo = dialog.window_combo

    combo.showPopup()

    assert combo.itemText(combo.count() - 1) == LOOKING_FOR_WINDOWS == "Looking for windows…"
    assert not combo.model().item(combo.count() - 1).isEnabled()
    qtbot.waitUntil(lambda: combo.itemText(combo.count() - 1) == STARTING_OBS, timeout=5000)
    assert STARTING_OBS == "Starting OBS… (up to 30 s)"
    combo.showPopup()  # opening again while it lists starts nothing more
    starter.release.set()
    qtbot.waitUntil(lambda: not dialog.listing_windows, timeout=5000)
    combo.hidePopup()
    assert starter.calls == 1
    assert [value for _, value in offered(dialog)] == [None, *[i["itemValue"] for i in RETITLED if i["itemEnabled"]]]


def test_the_popup_is_shown_again_as_wide_as_its_widest_item(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)
    combo = dialog.window_combo

    combo.showPopup()
    rig.qtbot.waitUntil(lambda: not dialog.listing_windows, timeout=5000)

    view = combo.view()
    assert view.isVisible()
    widest = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
    assert view.minimumWidth() >= widest
    combo.hidePopup()


def test_a_listing_that_started_obs_asks_for_the_capture_method_again(io_loop, qtbot):
    """B4-01: "In use" says nothing useful while OBS is closed; it is asked again once OBS runs."""
    obs = StartingObs(input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED})
    discovery = FakeDiscovery()
    discovery.running = False
    rig = Rig(io_loop, qtbot, obs=obs, starter=LocalObsStarter(discovery, obs, timeout_s=1.0))
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))
    assert dialog.capture_method_label.text() == "In use: not known until OBS answers"

    rig.list_windows(dialog)

    assert (discovery.launches, obs.connects) == (1, 1)
    assert dialog.capture_method_label.text() == "In use: Window Capture (X11)"


def test_while_ready_the_list_comes_at_once_and_nothing_is_started(rig: Rig):
    rig.run(rig.provisioner.ensure_collection(FULL_HOOK)).result(5)
    rig.session.state = AppState.ARMED
    rig.obs.reset_calls()
    dialog = rig.open(FULL_HOOK)

    rig.list_windows(dialog)

    assert rig.starter.calls == 0
    assert rig.obs.mutating() == []
    assert dialog.window_combo.count() == 1 + len([i for i in RETITLED if i["itemEnabled"]]) + 1  # + not open now


def test_an_active_output_lists_nothing_and_says_which(io_loop, qtbot):
    obs = ListingObs(
        input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED}, active=["GetRecordStatus"]
    )
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)

    assert offered(dialog) == [(NO_WINDOW_LINUX, None)]
    assert "recording" in dialog.windows_message.text()
    assert not dialog.windows_message.isHidden()
    assert obs.listed_in == []


def test_a_listing_failure_is_shown(io_loop, qtbot):
    obs = ListingObs(input_kinds=LINUX_X11_KINDS)
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(FULL_HOOK)
    obs.crashed = True

    rig.list_windows(dialog)

    assert "OBS could not list its windows" in dialog.windows_message.text()


def test_the_switch_back_warning_is_shown_with_the_windows(io_loop, qtbot):
    obs = ListingObs(input_kinds=LINUX_X11_KINDS, window_lists={"xcomposite_input": RETITLED}, changed_event="never")
    rig = Rig(io_loop, qtbot, obs=obs)
    rig.picker = CapturePicker(
        obs, rig.provisioner, rig.session, starter=StubStarter(ObsStartStage.CONNECTING), switch_timeout_s=0.05
    )
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)

    assert dialog.window_combo.count() > 1
    assert "Untitled" in dialog.windows_message.text()


def test_an_empty_window_list_says_why(io_loop, qtbot):
    obs = ListingObs(input_kinds=("pipewire-screen-capture-source", "pulse_output_capture"))
    rig = Rig(io_loop, qtbot, obs=obs)
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)

    assert offered(dialog) == [(NO_WINDOW_LINUX, None)]
    assert "PipeWire" in dialog.windows_message.text()


def test_on_wayland_the_game_window_is_a_note_and_the_stored_window_is_kept(rig: Rig):
    dialog = rig.open(FULL_HOOK, wayland=True)

    assert dialog.window_combo.isHidden()
    assert dialog.window_note.text() == WAYLAND_WINDOW_NOTE
    assert dialog.profile().capture.window == STORED


def test_closing_the_dialog_lets_a_listing_finish_and_obs_switches_back(rig: Rig):
    """Cancelling a request already sent drops the link (T12), so the dialog's OBS calls run to their end."""
    entered = threading.Event()
    release = threading.Event()
    real_list = rig.provisioner.list_windows

    async def slow() -> list[WindowItem]:
        entered.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
        return await real_list()

    rig.provisioner.list_windows = slow  # type: ignore[method-assign]
    dialog = rig.open(HOOKER_LINUX)
    dialog.window_combo.showPopup()
    assert entered.wait(5)
    assert rig.obs.current_collection == OBS_COLLECTION_NAME

    dialog.window_combo.hidePopup()
    dialog.reject()
    release.set()

    rig.qtbot.waitUntil(lambda: rig.obs.current_collection == "Untitled", timeout=5000)
    assert rig.obs.listed_in == [OBS_COLLECTION_NAME]


# Sound follows the game window (UJ-26) -------------------------------------------------------------


def test_a_new_windows_game_saves_with_a_title_only(win_rig: Rig):
    dialog = win_rig.open()
    dialog.title_edit.setText("Persona 5")

    profile = dialog.profile()

    assert profile.slug == "persona-5"
    assert (profile.capture.window, profile.audio.mode) == (None, AudioMode.DESKTOP)
    assert profile.ocr.engine is OcrEngine.ONEOCR
    assert profile.source_ids is None
    assert dialog.problems(profile) == []


def test_a_window_on_windows_records_only_its_sound_unless_unticked(win_rig: Rig):
    dialog = win_rig.open()
    dialog.title_edit.setText("Zero Escape")
    check = dialog.game_sound_check
    assert check is not None and check.isHidden()

    win_rig.list_windows(dialog)
    win_rig.choose(dialog, ZERO_WINDOW)

    assert (dialog.profile().capture.window, dialog.profile().audio.mode) == (ZERO_WINDOW, AudioMode.APP)
    assert not check.isHidden()
    assert check.text() == "Record only the game's sound"
    check.setChecked(False)
    assert dialog.profile().audio.mode is AudioMode.DESKTOP
    win_rig.choose(dialog, None)
    assert (dialog.profile().capture.window, dialog.profile().audio.mode) == (None, AudioMode.DESKTOP)
    assert check.isHidden()


def test_linux_has_no_sound_control_and_keeps_the_stored_sound(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)

    rig.list_windows(dialog)
    rig.choose(dialog, RETITLED_VALUE)

    assert dialog.game_sound_check is None
    assert dialog.profile().audio == HOOKER_LINUX.audio


# Capture method in use (UJ-29) ---------------------------------------------------------------------


def test_the_capture_method_in_use_is_shown_and_follows_the_capture_setting(rig: Rig):
    dialog = rig.open(replace(FULL_HOOK, capture=CaptureSettings(window=STORED)))
    assert dialog.capture_method_label.text() == "In use: Window Capture (X11)"

    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.PIPEWIRE))
    rig.settle(dialog)

    assert dialog.capture_method_label.text() == "In use: Screen Capture (PipeWire)"
    assert rig.obs.mutating() == []


def test_a_late_answer_for_an_older_capture_setting_is_ignored(rig: Rig):
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
    dialog = GameProfileDialog(
        rig.services(), DEFAULT_TEXT_SOURCES, replace(FULL_HOOK, capture=CaptureSettings(window=STORED)), wayland=False
    )
    rig.qtbot.addWidget(dialog)
    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.PIPEWIRE))
    rig.settle(dialog)
    assert dialog.capture_method_label.text() == "In use: Screen Capture (PipeWire)"

    release.set()
    rig.qtbot.waitUntil(lambda: not dialog._pending, timeout=5000)

    assert dialog.capture_method_label.text() == "In use: Screen Capture (PipeWire)"


def test_the_capture_method_says_when_obs_does_not_answer(io_loop, qtbot):
    obs = ListingObs(input_kinds=LINUX_X11_KINDS)
    obs.crashed = True
    dialog = Rig(io_loop, qtbot, obs=obs).open(FULL_HOOK)

    assert dialog.capture_method_label.text() == "In use: not known until OBS answers"


def test_a_windows_obs_without_game_capture_names_its_display_capture(io_loop, qtbot):
    obs = ListingObs(input_kinds=("monitor_capture", "wasapi_output_capture"))
    dialog = Rig(io_loop, qtbot, platform="win32", obs=obs).open(replace(WIN_HOOK_DESKTOP, capture=CaptureSettings()))

    assert dialog.capture_method_label.text() == "In use: Display Capture"


def test_capture_choices_follow_the_platform_and_keep_a_stored_kind(rig: Rig, win_rig: Rig):
    linux = rig.open(replace(FULL_HOOK, capture=CaptureSettings(kind=CaptureKind.GAME)))
    windows = win_rig.open(FULL_OCR_WINDOWS)

    def kinds(dialog: GameProfileDialog) -> list[object]:
        return [dialog.capture_kind_combo.itemData(i) for i in range(dialog.capture_kind_combo.count())]

    assert kinds(linux) == [CaptureKind.AUTO, CaptureKind.PIPEWIRE, CaptureKind.XCOMPOSITE, CaptureKind.GAME]
    assert linux.profile().capture.kind is CaptureKind.GAME
    assert kinds(windows) == [CaptureKind.AUTO, CaptureKind.GAME, CaptureKind.WINDOW]


# Reading the screen (UJ-27, UJ-22b) ----------------------------------------------------------------


def test_the_language_is_chosen_by_name_and_a_stored_other_value_is_kept(rig: Rig):
    dialog = rig.open(replace(OCR_LINUX, ocr=replace(OCR_LINUX.ocr, language="de")))
    combo = dialog.language_combo

    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Japanese",
        "Chinese",
        "Korean",
        "Arabic",
        "Russian",
        "Greek",
        "Hebrew",
        "Thai",
        "Other (Latin letters)",
        "de",
    ]
    assert [combo.itemData(i) for i in range(9)] == ["ja", "zh", "ko", "ar", "ru", "el", "he", "th", "en"]
    assert dialog.profile().ocr.language == "de"
    combo.setCurrentIndex(combo.findData("ko"))
    assert dialog.profile().ocr.language == "ko"


def test_the_add_on_status_shows_only_when_it_is_not_ready(rig: Rig):
    ready = rig.open(OCR_LINUX)
    assert ready.addon_row.isHidden()

    rig.ocr.current = AddonStatus.MISSING
    missing = rig.open(OCR_LINUX)

    assert not missing.addon_row.isHidden()
    assert missing.ocr_status_label.text() == f"OCR add-on: {strings.ADDON_STATUS_TEXT[AddonStatus.MISSING]}"
    assert missing.install_button.text() == "Install…"
    assert not missing.install_button.isHidden()


def test_install_opens_the_add_ons_page_and_a_refresh_hides_the_row(rig: Rig):
    rig.ocr.current = AddonStatus.BROKEN
    dialog = rig.open(OCR_LINUX)
    assert dialog.install_button.text() == "Repair…"

    dialog.install_button.click()
    assert rig.installs == 1
    rig.ocr.current = AddonStatus.READY
    dialog.refresh_addons()

    assert dialog.addon_row.isHidden()


def test_without_an_install_route_the_button_is_hidden(rig: Rig):
    rig.ocr.current = AddonStatus.MISSING
    dialog = rig.open(OCR_LINUX, installs=False)

    assert not dialog.addon_row.isHidden()
    assert dialog.install_button.isHidden()


def test_the_add_ons_platform_note_is_shown(rig: Rig):
    rig.ocr = FakeOcr(note=X11_NOTE)
    dialog = rig.open(OCR_LINUX)

    assert dialog.ocr_note_label.text() == X11_NOTE


def test_engines_name_where_screenshots_go(rig: Rig, win_rig: Rig):
    def engines(dialog: GameProfileDialog) -> list[tuple[str, object]]:
        combo = dialog.engine_combo
        return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]

    assert engines(rig.open(OCR_LINUX)) == [
        ("meikiocr (this computer)", OcrEngine.MEIKIOCR),
        ("Google Lens (sends screenshots to Google)", OcrEngine.GLENS),
        ("Bing (sends screenshots to Microsoft)", OcrEngine.BING),
    ]
    assert engines(win_rig.open(FULL_OCR_WINDOWS))[0] == ("OneOCR (this computer)", OcrEngine.ONEOCR)


def test_the_ocr_rows_in_advanced_show_only_for_ocr(win_rig: Rig):
    dialog = win_rig.open(WIN_HOOK_DESKTOP)
    assert dialog.engine_combo.isHidden()
    assert dialog.window_title_edit is not None and dialog.window_title_edit.isHidden()

    dialog.text_from_combo.setCurrentIndex(dialog.text_from_combo.findData(TextFrom.OCR))

    assert not dialog.engine_combo.isHidden()
    assert not dialog.window_title_edit.isHidden()


def test_the_ocr_window_title_follows_the_game_window_until_it_is_edited(win_rig: Rig):
    dialog = win_rig.open(
        replace(FULL_OCR_WINDOWS, capture=CaptureSettings(), ocr=replace(FULL_OCR_WINDOWS.ocr, window_title=None))
    )
    edit = dialog.window_title_edit
    assert edit is not None and edit.text() == ""

    win_rig.list_windows(dialog)
    win_rig.choose(dialog, ZERO_WINDOW)
    assert edit.text() == "Zero Escape: Chapter 1"
    win_rig.choose(dialog, SG_WINDOW)
    assert edit.text() == "STEINS;GATE"  # still the derived title: it follows
    edit.setText("STEINS")  # hand-trimmed to the stable part
    win_rig.choose(dialog, ZERO_WINDOW)

    assert edit.text() == "STEINS"
    assert dialog.profile().ocr.window_title == "STEINS"


def test_select_area_stores_the_rectangles_verbatim(win_rig: Rig):
    dialog = win_rig.open(replace(FULL_OCR_WINDOWS, ocr=replace(FULL_OCR_WINDOWS.ocr, rects=None)))
    assert (dialog.area_label.text(), dialog.clear_area_button.isHidden()) == ("Not selected", True)
    assert dialog.select_area_button.text() == "Select area…"

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert win_rig.ocr.picks == ["Zero Escape"]
    assert dialog.profile().ocr.rects == "100,100,900,260"
    assert (dialog.area_label.text(), dialog.clear_area_button.isHidden()) == ("Selected", False)


def test_on_linux_the_area_picker_gets_no_window_title(rig: Rig):
    dialog = rig.open(replace(FULL_HOOK, text_mode=TextMode.OCR, source_ids=None, clipboard=False))

    dialog.select_area_button.click()
    rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert rig.ocr.picks == [None]
    assert dialog.window_title_edit is None
    assert dialog.profile().ocr.window_title == "Kept"


def test_a_cancelled_area_selection_keeps_the_previous_area(win_rig: Rig):
    win_rig.ocr.answer = None
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert dialog.profile().ocr.rects == "10,20,300,400"
    assert dialog.area_message.isHidden()


def test_an_area_selection_error_is_shown_in_the_dialog(win_rig: Rig):
    win_rig.ocr.error = "owocr could not start: no such file"
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.select_area_button.click()
    win_rig.qtbot.waitUntil(dialog.select_area_button.isEnabled, timeout=5000)

    assert dialog.area_message.text() == "owocr could not start: no such file"
    assert not dialog.area_message.isHidden()
    assert dialog.profile().ocr.rects == "10,20,300,400"


def test_clear_area(win_rig: Rig):
    dialog = win_rig.open(FULL_OCR_WINDOWS)

    dialog.clear_area_button.click()

    assert dialog.profile().ocr.rects is None
    assert dialog.clear_area_button.isHidden()


# Auto mode (UJ-28) ---------------------------------------------------------------------------------


def test_auto_mode_is_one_check_whose_options_show_only_while_it_is_on(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)
    assert dialog.auto_check.text() == "Auto mode: start and stop the recording for me"
    assert dialog.auto_options.isHidden()

    dialog.auto_check.setChecked(True)

    assert not dialog.auto_options.isHidden()
    assert dialog.auto_start_check.text() == "Start at the first line (that line's voice start is cut)"
    assert dialog.idle_spin.specialValueText() == "Never"


def test_auto_mode_off_keeps_its_settings(rig: Rig):
    dialog = rig.open(FULL_HOOK)

    dialog.auto_check.setChecked(False)

    assert dialog.profile().auto == replace(FULL_HOOK.auto, enabled=False)


def test_stop_when_the_window_closes_needs_a_window_and_no_pipewire(rig: Rig):
    dialog = rig.open(FULL_HOOK)  # a window, X11 capture
    check = dialog.window_close_check
    assert check.text() == "Stop when the game window closes"
    assert not check.isHidden()

    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.PIPEWIRE))
    rig.settle(dialog)
    assert check.isHidden()
    dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData(CaptureKind.AUTO))
    rig.settle(dialog)
    rig.choose(dialog, None)

    assert check.isHidden()
    assert dialog.profile().auto.stop_on_window_close is False  # kept while hidden


# Advanced (UJ-29) ----------------------------------------------------------------------------------


def test_advanced_is_collapsed_for_defaults_and_holds_the_filters(rig: Rig):
    dialog = rig.open(HOOKER_LINUX)

    assert dialog.advanced_box.isHidden()
    assert dialog.speaker_check.text() == "Remove a leading speaker name (【name】, or name: before a quote)"
    assert dialog.typewriter_check.text() == "Merge lines that are typed out gradually (can swallow a short real line)"
    dialog.advanced_button.click()
    assert not dialog.advanced_box.isHidden()


@pytest.mark.parametrize(
    ("platform", "profile"),
    [
        ("linux", FULL_HOOK),  # filters differ
        ("linux", replace(HOOKER_LINUX, capture=CaptureSettings(kind=CaptureKind.PIPEWIRE))),
        ("win32", WIN_HOOK_DESKTOP),  # a window, but not only the game's sound
        ("win32", FULL_OCR_WINDOWS),  # a cloud engine
    ],
)
def test_advanced_opens_expanded_when_anything_inside_differs_from_its_default(
    io_loop, qtbot, platform: str, profile: GameProfile
):
    dialog = Rig(io_loop, qtbot, platform=platform).open(profile)

    assert not dialog.advanced_box.isHidden()


# Layout and errors (UJ-30, UJ-31, UJ-32) -----------------------------------------------------------


def test_a_save_that_fails_shows_why_until_the_next_edit(win_rig: Rig):
    dialog = win_rig.open(taken=("persona-5",))
    dialog.title_edit.setText("Persona 5")

    with win_rig.qtbot.assertNotEmitted(dialog.profile_saved):
        dialog.accept()

    assert dialog.problems_label.text() == "Cannot save: a game with this title exists already."
    assert not dialog.problems_label.isHidden()
    assert dialog.result() != dialog.DialogCode.Accepted.value
    dialog.title_edit.textEdited.emit("Persona 5 Royal")
    assert dialog.problems_label.isHidden()


def test_save_emits_the_edited_profile(rig: Rig):
    dialog = rig.open(FULL_HOOK)
    dialog.title_edit.setText("  Steins;Gate 0 ")
    dialog.typewriter_check.setChecked(False)

    assert save(rig, dialog) == [replace(FULL_HOOK, title="Steins;Gate 0", filters=FilterSettings(False, False))]
    assert dialog.result() == dialog.DialogCode.Accepted.value


def test_the_dialog_never_scrolls_sideways(win_rig: Rig):
    dialog = win_rig.open(FULL_OCR_WINDOWS)
    scroll = dialog.findChild(QScrollArea)

    assert scroll is not None
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_the_dialog_opens_wide_enough_for_its_content(io_loop, qtbot, platform: str):
    """Nothing is squeezed under its minimum: the shared label column counts when the dialog is sized,
    also with screen reading chosen and Advanced open."""
    rig = Rig(io_loop, qtbot, platform=platform)
    dialog = rig.open(FULL_OCR_WINDOWS if platform == "win32" else None)
    dialog.show()
    qtbot.waitExposed(dialog)
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    assert opens_wide_enough(dialog, scroll)
    dialog.text_from_combo.setCurrentIndex(dialog.text_from_combo.findData(TextFrom.OCR))
    if not dialog.advanced_button.isChecked():
        dialog.advanced_button.click()
    qtbot.waitUntil(lambda: opens_wide_enough(dialog, scroll), timeout=1000)


def opens_wide_enough(dialog: QDialog, scroll: QScrollArea) -> bool:
    """The viewport holds the content's minimum width, or the dialog is as wide as ``fit_dialog`` lets it
    be on this screen. Windows's offscreen platform has no fonts: its fallback measures the content wider
    than 90 % of its 800 px screen, which the real app at 100 % and 150 % never meets."""
    content = scroll.widget()
    assert content is not None
    bound = int(available_size(dialog).width() * DIALOG_SCREEN_FRACTION)
    return scroll.viewport().width() >= content.minimumSizeHint().width() or dialog.width() >= bound


def test_the_game_window_dropdown_is_as_wide_as_the_title(win_rig: Rig):
    """The mock's Game window row spans the field, so its first item is not cut."""
    dialog = win_rig.open()
    dialog.show()
    win_rig.qtbot.waitExposed(dialog)

    assert dialog.window_combo.width() == dialog.title_edit.width()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_the_no_window_item_is_not_cut(io_loop, qtbot, platform: str):
    rig = Rig(io_loop, qtbot, platform=platform)
    dialog = rig.open()
    dialog.show()
    qtbot.waitExposed(dialog)
    combo = dialog.window_combo
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    style = combo.style()
    assert style is not None

    field = style.subControlRect(
        QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField, combo
    )

    assert field.width() >= combo.fontMetrics().horizontalAdvance(combo.itemText(0))


def test_a_failed_save_grows_the_dialog_instead_of_squeezing_the_form(win_rig: Rig):
    dialog = win_rig.open()
    dialog.show()
    win_rig.qtbot.waitExposed(dialog)
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None
    before = scroll.height()

    dialog.accept()  # no title: the problems line shows
    win_rig.qtbot.waitUntil(lambda: dialog.problems_label.isVisible(), timeout=5000)
    dialog.layout().activate()

    assert scroll.height() >= before


@pytest.mark.parametrize(
    ("platform", "profile", "status"),
    [
        ("win32", None, AddonStatus.READY),
        ("linux", FULL_HOOK, AddonStatus.READY),
        ("win32", FULL_OCR_WINDOWS, AddonStatus.MISSING),
    ],
)
def test_the_dialog_speaks_the_glossary(io_loop, qtbot, platform, profile, status):
    """D-01, UJ-32: no "arm", no "pinned"/"Pick", "line" not "cue", "WebSocket" spelt so."""
    rig = Rig(io_loop, qtbot, platform=platform)
    rig.ocr.current = status
    dialog = rig.open(profile)
    dialog.advanced_button.setChecked(True)
    dialog.auto_check.setChecked(True)

    assert not re.search(r"\b(dis)?arm(ed|ing)?\b|\bpinned\b|\bPick\b|\bcues?\b|websocket", texts(dialog))


# Pure helpers --------------------------------------------------------------------------------------


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


@pytest.mark.parametrize(
    ("value", "title"),
    [
        (ZERO_WINDOW, "Zero Escape: Chapter 1"),
        ("Zero #22 1#3A The Game:UnityWndClass:zero.exe", "Zero # 1: The Game"),
        (STORED, None),  # X11: OCR window titles are Windows only
        (None, None),
        (":UnityWndClass:zero.exe", None),
    ],
)
def test_window_title(value: str | None, title: str | None) -> None:
    assert window_title(value) == title
