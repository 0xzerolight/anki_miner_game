"""The game profile dialog (spec 5 ``GameProfile``, 11.3 window picker, 12, 14).

``CapturePicker`` is the dialog's view of OBS: the capture method provisioning would use for the
profile, and the window list the picker offers. ``Provisioner.list_windows`` reads the current
scene collection, so the picker never lists the user's collection:

- In any state but ``idle`` OBS is on the app's collection (arming put it there), so it lists at once.
- While ``idle`` it first gets OBS running and connected (``ObsStarter``, D-03: OBS is usually
  closed when the app starts), then runs spec 6.2 step 1's output check (an active output: the
  picker says which and lists nothing), reads the current collection's name, provisions the app's collection
  for the profile (``ensure_collection``), lists, and switches back through the gateway. The switch
  back is done on ``CurrentSceneCollectionChanged``, never on the answer, whose order against the
  event is not fixed (``docs/m0/obs-behaviour.md`` item 5); there is none when the user's collection
  was the app's already (item 4, no event would come). It holds ``obs_lock``, which the session
  actor takes to arm and to restore OBS, from the output check to the switch back, and checks the
  state again once it has it: an arm meanwhile then finds the user's collection current, and a game
  armed while the listing waited is listed at once.

The picker offers enabled items only (``docs/m0/wave-1-amendments.md`` item 11): OBS keeps listing
the configured window as a disabled item once no live window matches it, and on X11 a retitled
window is that disabled item plus an enabled one under its new name (R2 item 15). The stored
``capture.window`` is kept until the user chooses another window, or none, in the dialog's Game window
dropdown.

``CapturePicker`` runs on the I/O loop, the session actor's thread (``SessionControl``). The dialog
lives on the Qt main thread and hands its coroutines to the loop through ``AsyncRunner`` (in the app
``asyncio.run_coroutine_threadsafe`` on the I/O loop); results come back through a queued signal,
and every call still running when the dialog closes is cancelled. ``CapturePicker``'s OBS calls
still run to their end on the loop, where their results are dropped: cancelling a request already
sent drops the OBS connection (T12). The dialog saves nothing: it emits ``profile_saved`` with a
profile ``validate`` accepts, and the caller stores it.
"""

import asyncio
import concurrent.futures
import contextlib
import logging
import sys
from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.gui import strings
from anki_miner_game.gui.widgets import layout
from anki_miner_game.gui.wizard import is_wayland_session
from anki_miner_game.interfaces.addons import AddonService, OcrAreaPicker
from anki_miner_game.interfaces.obs import ObsGateway, ObsStarter, Provisioner
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import TextSourceConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.messages import AppState, ObsEvent
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConnectError,
    ObsError,
    ObsEventName,
    ObsNotReadyError,
    ObsRequestError,
    ObsServerOffError,
    ObsStartStage,
    WindowItem,
)
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
    default_audio_mode,
    default_ocr_engine,
    validate,
)

log = logging.getLogger(__name__)

SWITCH_TIMEOUT_S: Final = 15.0
"""How long the switch back to the user's collection may take (spec 6.2 step 3's timeout)."""

NOT_AVAILABLE: Final = 604
"""obs-websocket ``RequestStatus::InvalidResourceState``: the replay buffer or virtual camera is not
configured or not installed, which means it is not active (``docs/m0/obs-behaviour.md`` item 6)."""

OUTPUT_STATUS: Final = (
    ("GetStreamStatus", "a stream"),
    ("GetRecordStatus", "a recording"),
    ("GetReplayBufferStatus", "the replay buffer"),
    ("GetVirtualCamStatus", "the virtual camera"),
)
"""Spec 6.2 step 1: each output's status request and how the picker names it."""

_MAYBE_UNAVAILABLE: Final = frozenset({"GetReplayBufferStatus", "GetVirtualCamStatus"})

LIST_SERVER_OFF_TEXT: Final = "OBS's WebSocket server is off. Close OBS and open this list again: the app turns it on."
LIST_NOT_READY_TEXT: Final = (
    "OBS did not answer within 30 s. If OBS shows a dialog, answer it, then open this list again."
)
LIST_AUTH_TEXT: Final = "OBS rejected the app's password. Set it in Settings -> Advanced -> Set up OBS…"
"""Master 4.9: why the window list could not start OBS, and what to do."""


class WindowListError(RuntimeError):
    """The picker cannot list windows; the message says why and is fit for the dialog."""


@dataclass(frozen=True)
class WindowListing:
    items: tuple[WindowItem, ...]
    """The enabled items, in OBS's order."""
    warning: str | None = None
    """Set when OBS stayed on the app's collection because the switch back failed."""


class CapturePicker:
    """The profile dialog's OBS calls: the capture method in use and the window list (see the module docstring).

    Built once by the composition, since it subscribes to the gateway for the collection-changed
    event. Its coroutines run on the I/O loop. ``obs_lock`` is the one the session actor holds while
    it arms or restores OBS; its own when ``None``. ``starter`` is the app's one start-OBS sequence
    (D-03), which the window list uses to start OBS when it is not running.
    """

    def __init__(
        self,
        gateway: ObsGateway,
        provisioner: Provisioner,
        session: SessionControl,
        *,
        starter: ObsStarter,
        switch_timeout_s: float = SWITCH_TIMEOUT_S,
        obs_lock: asyncio.Lock | None = None,
    ) -> None:
        self._gateway = gateway
        self._provisioner = provisioner
        self._session = session
        self._starter = starter
        self._switch_timeout_s = switch_timeout_s
        self._obs_lock = obs_lock if obs_lock is not None else asyncio.Lock()
        self._waiting: tuple[asyncio.AbstractEventLoop, asyncio.Future[None], str] | None = None
        self._running: set[asyncio.Task[Any]] = set()
        """The calls still running; the reference keeps one whose caller was cancelled alive."""
        gateway.subscribe(self._on_event)

    async def capture_method(self, profile: GameProfile) -> str:
        """The OBS input kind that would capture this profile's game; ``""`` when none is available.

        Runs to its end even when the caller is cancelled (``_to_the_end``).
        """
        return await self._to_the_end(self._provisioner.capture_method(profile))

    async def list_windows(
        self, profile: GameProfile, report: Callable[[ObsStartStage], None] | None = None
    ) -> WindowListing:
        """The windows to offer for ``profile``. Raises ``WindowListError`` or ``ObsError``.

        While idle, OBS is started and connected first (``ObsStarter``, D-03); ``report`` hears its stages
        on the I/O loop (only on that path). Runs to its end, switch back included, even when the caller
        is cancelled (``_to_the_end``).
        """
        return await self._to_the_end(self._list_windows(profile, report))

    async def _to_the_end[T](self, call: Coroutine[Any, Any, T]) -> T:
        """Await ``call`` in a task that a cancelled caller leaves running.

        The dialog cancels its calls when it closes, and T12 drops the connection when a request
        already sent is cancelled, since OBS's answer would be read as the next request's. A
        cancelled listing would then leave OBS on the app's collection with no link to switch it
        back, and a cancelled call while armed or recording would drop the actor's link. The caller
        gets ``CancelledError`` at once; the call finishes on the loop and its result is dropped.
        """
        task = asyncio.ensure_future(call)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return await asyncio.shield(task)

    async def _list_windows(
        self, profile: GameProfile, report: Callable[[ObsStartStage], None] | None
    ) -> WindowListing:
        if self._session.state is not AppState.IDLE:
            return WindowListing(_enabled(await self._provisioner.list_windows()))
        async with self._obs_lock:
            if self._session.state is not AppState.IDLE:  # armed while this waited for the lock
                return WindowListing(_enabled(await self._provisioner.list_windows()))
            await self._start_obs(report)
            return await self._list_while_idle(profile)

    async def _start_obs(self, report: Callable[[ObsStartStage], None] | None) -> None:
        """B4-01: OBS running and the gateway connected, as Get ready does; a failure says what to do.

        The gateway's ``CONNECTED`` reaches the actor as after the wizard's connect. A connect failure once
        OBS runs (``CONNECTING`` heard) passes through unchanged; the dialog shows it as OBS's answer.
        """
        heard: list[ObsStartStage] = []

        def tell(stage: ObsStartStage) -> None:
            heard.append(stage)
            if report is not None:
                report(stage)

        try:
            await self._starter.start(tell)
        except ObsServerOffError as exc:
            raise WindowListError(LIST_SERVER_OFF_TEXT) from exc
        except ObsNotReadyError as exc:
            raise WindowListError(LIST_NOT_READY_TEXT) from exc
        except ObsAuthError as exc:
            raise WindowListError(LIST_AUTH_TEXT) from exc
        except ObsConnectError as exc:
            if ObsStartStage.CONNECTING not in heard:  # no install, or OBS could not be started
                raise WindowListError(f"Cannot start OBS: {exc}") from exc
            raise

    async def _list_while_idle(self, profile: GameProfile) -> WindowListing:
        await self._refuse_active_outputs()
        home = (await self._gateway.request("GetSceneCollectionList")).get("currentSceneCollectionName")
        try:
            await self._provisioner.ensure_collection(profile)
            found = await self._provisioner.list_windows()
        except ObsError as exc:
            warning = await self._switch_back(home)
            if warning is None:
                raise
            raise WindowListError(f"{exc}. {warning}") from exc
        return WindowListing(_enabled(found), await self._switch_back(home))

    async def _refuse_active_outputs(self) -> None:
        active: list[str] = []
        for request, label in OUTPUT_STATUS:
            try:
                data = await self._gateway.request(request)
            except ObsRequestError as exc:
                if request in _MAYBE_UNAVAILABLE and exc.code == NOT_AVAILABLE:
                    continue
                raise
            if data.get("outputActive") is True:
                active.append(label)
        if active:
            running = " and ".join(active)
            raise WindowListError(
                f"OBS is running {running}. Stop it in OBS to list windows: they come from the app's "
                "scene collection, and the app switches OBS to it only while no output runs."
            )

    async def _switch_back(self, home: object) -> str | None:
        """Make ``home`` the current collection again; a warning when that fails, else ``None``."""
        if not isinstance(home, str) or not home or home == OBS_COLLECTION_NAME:
            return None
        if self._session.state is not AppState.IDLE:
            return None  # Armed meanwhile (tray, hotkey, CLI): OBS belongs on the app's collection now.
        loop = asyncio.get_running_loop()
        changed: asyncio.Future[None] = loop.create_future()
        try:
            listing = await self._gateway.request("GetSceneCollectionList")
            if listing.get("currentSceneCollectionName") == home:
                return None
            self._waiting = (loop, changed, home)
            async with asyncio.timeout(self._switch_timeout_s):
                await self._gateway.request("SetCurrentSceneCollection", sceneCollectionName=home)
                await changed
        except (ObsError, TimeoutError) as exc:
            reason = f"no switch within {self._switch_timeout_s:g} s" if isinstance(exc, TimeoutError) else str(exc)
            log.warning("OBS did not switch back to the scene collection %r: %s", home, reason)
            return (
                f"OBS stayed on the app's scene collection ({reason}): switch back to "
                f'"{home}" in OBS\'s Scene Collection menu.'
            )
        finally:
            self._waiting = None
        return None

    def _on_event(self, event: ObsEvent) -> None:
        """On the library's event thread: only hand the event to the waiting loop."""
        waiting = self._waiting
        if waiting is None or event.name != ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED:
            return
        loop, changed, name = waiting
        if event.data.get("sceneCollectionName") == name:
            loop.call_soon_threadsafe(_settle, changed)


def _settle(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _enabled(items: Sequence[WindowItem]) -> tuple[WindowItem, ...]:
    return tuple(item for item in items if item.enabled)


# Dialog ------------------------------------------------------------------------------------------


class AsyncRunner(Protocol):
    """Runs a coroutine on the I/O loop from the Qt main thread (``asyncio.run_coroutine_threadsafe``)."""

    def __call__[T](self, coro: Coroutine[Any, Any, T], /) -> concurrent.futures.Future[T]: ...


@dataclass(frozen=True)
class ProfileDialogServices:
    """What the dialog needs besides the profile; built once by the composition."""

    run: AsyncRunner
    capture: CapturePicker
    ocr_picker: OcrAreaPicker
    ocr_addon: AddonService
    slugify: Callable[[str], str]
    """The slug of a new game's title (``session.naming.slugify``)."""
    platform: str | None = None
    """``sys.platform`` when ``None``."""
    install_addons: Callable[[], None] | None = None
    """Opens the wizard's add-ons page alone over the dialog (UJ-22b); ``None`` hides Install…/Repair…."""


class TextFrom(StrEnum):
    """The dialog's "Text from" choice (UJ-25); the model keeps ``text_mode``, ``source_ids`` and ``clipboard``."""

    HOOKER = "hooker"
    """Every text hooker turned on in Settings: ``source_ids=None``, no clipboard."""
    CLIPBOARD = "clipboard"
    """Copied text only: ``source_ids=()``, ``clipboard=True``."""
    OCR = "ocr"
    KEPT = "kept"
    """A stored hook profile the three others cannot express (some hookers, or hookers and the clipboard):
    offered for that profile only and saved as it was."""


TEXT_FROM_LABELS: Final = {
    TextFrom.HOOKER: "A text hooker (Agent, Textractor, LunaTranslator)",
    TextFrom.CLIPBOARD: strings.COPIED_TEXT,
    TextFrom.OCR: "Reading the screen (OCR add-on)",
}

NO_WINDOW_WINDOWS: Final = "None: records the whole screen and all sound"
"""``provision._plan_windows`` without a window: game capture over a whole-screen capture, and desktop sound."""
NO_WINDOW_LINUX: Final = "None: OBS asks which screen or window to record, with all sound"
"""``provision._plan_linux`` without a window: PipeWire screen capture, whose portal asks, and desktop sound."""
WAYLAND_WINDOW_NOTE: Final = "On Wayland, OBS asks which window to record the first time it records."
LOOKING_FOR_WINDOWS: Final = "Looking for windows…"
STARTING_OBS: Final = "Starting OBS… (up to 30 s)"
NOT_OPEN_NOW: Final = "(not open now)"
NO_WINDOWS: Final = (
    "OBS lists no window. PipeWire capture (Wayland) has no window list: OBS asks which window to record "
    "the first time it records."
)

GAME_SOUND_ONLY: Final = "Record only the game's sound"
AUTO_MODE: Final = "Auto mode: start and stop the recording for me"
AUTO_START: Final = "Start at the first line (that line's voice start is cut)"
"""Spec 12: the line that starts the recording sits at 0:00, so the start of its voice is not in the video."""
WINDOW_CLOSE: Final = "Stop when the game window closes"
SPEAKER_STRIP: Final = "Remove a leading speaker name (【name】, or name: before a quote)"
TYPEWRITER_MERGE: Final = "Merge lines that are typed out gradually (can swallow a short real line)"
"""Spec 8.2 step 9: a line that extends the previous one within 2 s replaces it."""
NO_TEXT_SOURCE: Final = (
    "no text hooker is turned on: turn one on in Settings -> Advanced -> Text hookers, or choose Copied text"
)

OCR_LANGUAGES: Final = {
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
    "ru": "Russian",
    "el": "Greek",
    "he": "Hebrew",
    "th": "Thai",
    "en": "Other (Latin letters)",
}
"""owocr's ``-l`` values its ``_get_regex`` knows; it reads any other value as Latin script (UJ-27)."""

CAPTURE_METHOD_NAMES: Final = {
    "game_capture": "Game Capture",
    "window_capture": "Window Capture",
    "monitor_capture": "Display Capture",
    "xcomposite_input": "Window Capture (X11)",
    "pipewire-screen-capture-source": "Screen Capture (PipeWire)",
}

_CAPTURE_KIND_LABELS: Final = {
    CaptureKind.AUTO: "Automatic",
    CaptureKind.GAME: "Game Capture",
    CaptureKind.WINDOW: "Window Capture",
    CaptureKind.PIPEWIRE: "Screen Capture (PipeWire)",
    CaptureKind.XCOMPOSITE: "Window Capture (X11)",
}
_WINDOWS_CAPTURE_KINDS: Final = (CaptureKind.AUTO, CaptureKind.GAME, CaptureKind.WINDOW)
_LINUX_CAPTURE_KINDS: Final = (CaptureKind.AUTO, CaptureKind.PIPEWIRE, CaptureKind.XCOMPOSITE)

_ENGINE_LABELS: Final = {
    OcrEngine.ONEOCR: "OneOCR (this computer)",
    OcrEngine.MEIKIOCR: "meikiocr (this computer)",
    OcrEngine.GLENS: "Google Lens (sends screenshots to Google)",
    OcrEngine.BING: "Bing (sends screenshots to Microsoft)",
}
"""Spec 14: the cloud engines say that screenshots leave the machine."""

_PROBLEM_TEXT: Final = {
    "application audio needs a pinned window": "recording only the game's sound needs a game window",
}
"""``validate``'s texts in the dialog's words (UJ-32: no "pinned")."""


def window_label(value: str) -> str:
    """A readable name for a stored window string, as OBS names its list item.

    X11 ``<xid>\\r\\n<name>\\r\\n<class>`` gives the name; Windows ``<title>:<class>:<exe>`` (``:`` and
    ``#`` escaped as ``#3A`` and ``#22``) gives ``[<exe>]: <title>``. Display only.
    """
    parts = value.split("\r\n")
    if len(parts) == 3:
        return parts[1]
    parts = value.split(":")
    if len(parts) == 3:
        title, _, exe = (part.replace("#3A", ":").replace("#22", "#") for part in parts)
        return f"[{exe}]: {title}"
    return value


def window_title(value: str | None) -> str | None:
    """The title part of a Windows window string ``<title>:<class>:<exe>``, unescaped; ``None`` for anything else."""
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    return parts[0].replace("#3A", ":").replace("#22", "#") or None


class _Relay(QObject):
    """Carries results from the I/O loop's thread to the dialog on the main thread."""

    done = pyqtSignal(object, object)
    stage = pyqtSignal(int, object)
    """``(listing number, ObsStartStage)``: what starting OBS for a window listing is doing (D-03)."""


class _WindowCombo(QComboBox):
    """The Game window dropdown: opening its popup asks OBS for the window list again (UJ-26)."""

    opening = pyqtSignal()

    def showPopup(self) -> None:
        self.opening.emit()
        super().showPopup()


def _plain(text: str = "") -> QLabel:
    """A wrapping plain-text label: OBS's and owocr's words are data, never markup."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _fill(combo: QComboBox, values: Sequence[Any], labels: Mapping[Any, str], current: Any) -> None:
    """Add ``values`` (plus ``current`` when it is not among them, so a stored value survives) and select ``current``."""
    for value in (*values, *(() if current in values else (current,))):
        combo.addItem(labels.get(value, str(value)), value)
    combo.setCurrentIndex(combo.findData(current))


class GameProfileDialog(QDialog):
    """Create or edit one game profile (spec 5); emits ``profile_saved(GameProfile)`` on Save.

    Five rows for a new game (Title, Text from, Game window, Auto mode, Advanced), the OCR group in OCR mode.
    Whatever a stored profile holds that the controls cannot express is kept as it was (Review Focus 3).
    """

    profile_saved = pyqtSignal(object)

    def __init__(
        self,
        services: ProfileDialogServices,
        text_sources: Sequence[TextSourceConfig],
        profile: GameProfile | None = None,
        *,
        taken_slugs: Collection[str] = (),
        wayland: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """``profile`` is ``None`` for a new game, whose slug comes from its title and must not be in
        ``taken_slugs``. ``wayland`` is ``is_wayland_session()`` when ``None``."""
        super().__init__(parent)
        self._built = False
        self._services = services
        self._platform = services.platform or sys.platform
        self._windows = self._platform == "win32"
        self._wayland = is_wayland_session() if wayland is None else wayland
        self._text_sources = tuple(text_sources)
        self._taken = frozenset(taken_slugs)
        self._original = profile
        base = profile or GameProfile(
            slug="",
            title="",
            audio=AudioSettings(mode=default_audio_mode(self._platform)),
            ocr=OcrSettings(engine=default_ocr_engine(self._platform)),
        )
        self._base = base
        self._window: str | None = base.capture.window
        self._window_names: dict[str, str] = {}
        self._last_listing: tuple[WindowItem, ...] | None = None
        """The windows of the last listing that answered; ``None`` before the first."""
        self._listings = 0
        self._listing = 0
        """The number of the listing in flight; 0 while none is."""
        self._busy_text = LOOKING_FOR_WINDOWS
        self._reshowing = False
        self._derived_title = window_title(base.capture.window)
        """The OCR window title last filled in from the game window (UJ-27)."""
        self._rects: str | None = base.ocr.rects
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._capture_request = 0
        self._relay = _Relay(self)
        self._relay.done.connect(self._deliver)
        self._relay.stage.connect(self._stage_heard)

        self.setWindowTitle("Edit game" if profile else "New game")
        content = QWidget()
        column = QVBoxLayout(content)
        column.addLayout(self._build_general(base))
        column.addWidget(self._build_ocr(base))
        column.addWidget(self._build_auto(base))
        column.addWidget(self._build_advanced(base))
        column.addStretch(1)
        self._scroll = QScrollArea()
        self._scroll.setWidget(content)

        self.problems_label = layout.error_label()
        layout.clear_on_edit(self.problems_label, content)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(self._scroll)
        outer.addWidget(self.problems_label)
        outer.addWidget(self.buttons)

        self.text_from_combo.currentIndexChanged.connect(self._text_from_changed)
        self.capture_kind_combo.currentIndexChanged.connect(self._capture_kind_changed)
        self.finished.connect(self._cancel_pending)
        self._text_from_changed()
        self._show_window_choice()
        self._show_window_rows()
        self._show_area()
        self._show_addon()
        self._refresh_capture_method()
        self._built = True
        self._fit()

    # Building -------------------------------------------------------------------------------

    def _build_general(self, base: GameProfile) -> QFormLayout:
        form = QFormLayout()
        self.title_edit = QLineEdit(base.title)
        self.title_edit.setPlaceholderText("The game's name, used for its folder and sessions")
        form.addRow("Title", self.title_edit)

        self.text_from_combo = QComboBox()
        choice = self._stored_choice(base)
        for value in (TextFrom.HOOKER, TextFrom.CLIPBOARD, TextFrom.OCR):
            self.text_from_combo.addItem(TEXT_FROM_LABELS[value], value)
        if choice is TextFrom.KEPT:
            self.text_from_combo.addItem(self._kept_text(base), TextFrom.KEPT)
        self.text_from_combo.setCurrentIndex(self.text_from_combo.findData(choice))
        form.addRow("Text from", self.text_from_combo)
        self.clipboard_note = layout.message_label()
        form.addRow(self.clipboard_note)

        self.window_combo = _WindowCombo()
        self.window_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        metrics = self.window_combo.fontMetrics()  # the first item is never cut; Qt counts in "x" widths
        no_window = NO_WINDOW_WINDOWS if self._windows else NO_WINDOW_LINUX
        self.window_combo.setMinimumContentsLength(
            -(-metrics.horizontalAdvance(no_window) // max(1, metrics.horizontalAdvance("x")))
        )
        self.window_combo.opening.connect(self._list_windows)
        self.window_combo.activated.connect(self._window_chosen)
        self.window_note = layout.message_label()
        window_field = QWidget()
        window_field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)  # the mock's full row
        window_rows = QVBoxLayout(window_field)
        window_rows.setContentsMargins(0, 0, 0, 0)
        window_rows.addWidget(self.window_combo)
        window_rows.addWidget(self.window_note)
        self.window_combo.setVisible(not self._wayland)
        layout.show_message(self.window_note, WAYLAND_WINDOW_NOTE if self._wayland else "")
        form.addRow("Game window", window_field)
        self.windows_message = layout.error_label()
        form.addRow(self.windows_message)
        self._general_form = form
        return form

    def _build_ocr(self, base: GameProfile) -> QWidget:
        self.ocr_group = QGroupBox("Reading the screen")
        form = QFormLayout(self.ocr_group)
        self.addon_row = QWidget()
        status_row = QHBoxLayout(self.addon_row)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.ocr_status_label = _plain()
        self.install_button = QPushButton()
        self.install_button.clicked.connect(self._install_addons)
        status_row.addWidget(self.ocr_status_label, 1)
        status_row.addWidget(self.install_button)
        form.addRow(self.addon_row)
        self.ocr_note_label = layout.message_label()
        form.addRow(self.ocr_note_label)

        self.area_label = _plain()
        self.select_area_button = QPushButton("Select area…")
        self.select_area_button.clicked.connect(self._select_area)
        self.clear_area_button = QPushButton("Clear")
        self.clear_area_button.clicked.connect(self._clear_area)
        area = QHBoxLayout()
        area.addWidget(self.area_label, 1)
        area.addWidget(self.select_area_button)
        area.addWidget(self.clear_area_button)
        form.addRow("Text area", area)
        self.area_message = layout.error_label()
        form.addRow(self.area_message)

        self.language_combo = QComboBox()
        _fill(self.language_combo, tuple(OCR_LANGUAGES), OCR_LANGUAGES, base.ocr.language)
        form.addRow("Language", self.language_combo)
        self._ocr_form = form
        return self.ocr_group

    def _build_auto(self, base: GameProfile) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        self.auto_check = QCheckBox(AUTO_MODE)
        self.auto_check.setChecked(base.auto.enabled)
        column.addWidget(self.auto_check)

        self.auto_options = QWidget()
        options = QVBoxLayout(self.auto_options)
        options.setContentsMargins(24, 0, 0, 0)
        self.auto_start_check = QCheckBox(AUTO_START)
        self.auto_start_check.setChecked(base.auto.start_on_first_line)
        options.addWidget(self.auto_start_check)
        idle = QHBoxLayout()
        idle.addWidget(QLabel("Stop after no line for"))
        self.idle_spin = QSpinBox()
        self.idle_spin.setRange(0, max(24 * 60, base.auto.stop_idle_minutes))
        self.idle_spin.setSuffix(" min")
        self.idle_spin.setSpecialValueText("Never")
        self.idle_spin.setValue(max(0, base.auto.stop_idle_minutes))
        idle.addWidget(self.idle_spin)
        idle.addStretch(1)
        options.addLayout(idle)
        self.window_close_check = QCheckBox(WINDOW_CLOSE)
        self.window_close_check.setChecked(base.auto.stop_on_window_close)
        options.addWidget(self.window_close_check)
        column.addWidget(self.auto_options)

        self.auto_options.setVisible(base.auto.enabled)
        self.auto_check.toggled.connect(self._auto_toggled)
        return box

    def _build_advanced(self, base: GameProfile) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        self.advanced_button = QToolButton()
        self.advanced_button.setText("Advanced")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setAutoRaise(True)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        column.addWidget(self.advanced_button)

        self.advanced_box = QWidget()
        form = QFormLayout(self.advanced_box)
        self.capture_kind_combo = QComboBox()
        kinds = _WINDOWS_CAPTURE_KINDS if self._windows else _LINUX_CAPTURE_KINDS
        _fill(self.capture_kind_combo, kinds, _CAPTURE_KIND_LABELS, base.capture.kind)
        form.addRow("Capture method", self.capture_kind_combo)
        self.capture_method_label = _plain()
        form.addRow("", self.capture_method_label)
        self.game_sound_check: QCheckBox | None = None
        if self._windows:
            self.game_sound_check = QCheckBox(GAME_SOUND_ONLY)
            self.game_sound_check.setChecked(base.audio.mode is AudioMode.APP if base.capture.window else True)
            form.addRow(self.game_sound_check)
        self.speaker_check = QCheckBox(SPEAKER_STRIP)
        self.speaker_check.setChecked(base.filters.speaker_strip)
        form.addRow(self.speaker_check)
        self.typewriter_check = QCheckBox(TYPEWRITER_MERGE)
        self.typewriter_check.setChecked(base.filters.typewriter_merge)
        form.addRow(self.typewriter_check)
        self.engine_combo = QComboBox()
        engines = (default_ocr_engine(self._platform), OcrEngine.GLENS, OcrEngine.BING)
        _fill(self.engine_combo, engines, _ENGINE_LABELS, base.ocr.engine)
        form.addRow("OCR engine", self.engine_combo)
        self.window_title_edit: QLineEdit | None = None
        if self._windows:
            self.window_title_edit = QLineEdit(base.ocr.window_title or "")
            self.window_title_edit.setPlaceholderText("Empty: the text area is on the screen")
            form.addRow("OCR window title", self.window_title_edit)
        self._advanced_form = form
        column.addWidget(self.advanced_box)

        expanded = self._advanced_differs(base)
        self.advanced_button.setChecked(expanded)
        self._advanced_toggled(expanded)
        self.advanced_button.toggled.connect(self._advanced_toggled)
        return box

    # The profile ----------------------------------------------------------------------------

    def profile(self) -> GameProfile:
        """The profile as the form stands; ``problems`` says whether it can be saved."""
        title = self.title_edit.text().strip()
        mode, source_ids, clipboard = self._text_settings(self._choice())
        return GameProfile(
            slug=self._original.slug if self._original else self._services.slugify(title),
            title=title,
            text_mode=mode,
            source_ids=source_ids,
            clipboard=clipboard,
            capture=CaptureSettings(kind=CaptureKind(self.capture_kind_combo.currentData()), window=self._window),
            audio=self._audio(),
            filters=FilterSettings(
                speaker_strip=self.speaker_check.isChecked(),
                typewriter_merge=self.typewriter_check.isChecked(),
            ),
            ocr=OcrSettings(
                engine=OcrEngine(self.engine_combo.currentData()),
                language=str(self.language_combo.currentData()),
                window_title=self._ocr_window_title(),
                rects=self._rects,
            ),
            auto=AutoSettings(
                enabled=self.auto_check.isChecked(),
                start_on_first_line=self.auto_start_check.isChecked(),
                stop_idle_minutes=self.idle_spin.value(),
                stop_on_window_close=self.window_close_check.isChecked(),
            ),
        )

    def problems(self, profile: GameProfile) -> list[str]:
        """Why ``profile`` cannot be saved: ``validate`` plus what only the dialog knows."""
        problems = [_PROBLEM_TEXT.get(problem, problem) for problem in validate(profile)]
        if self._original is None and profile.slug in self._taken:
            problems.append("a game with this title exists already")
        if profile.text_mode is TextMode.HOOK:
            if profile.source_ids is None:
                sources = [source.id for source in self._text_sources if source.enabled]
            else:
                sources = list(profile.source_ids)
            if not sources and not profile.clipboard:
                problems.append(NO_TEXT_SOURCE)
        elif not profile.ocr.language:
            problems.append("choose the OCR language")
        return problems

    def accept(self) -> None:
        profile = self.profile()
        problems = self.problems(profile)
        if problems:
            self.problems_label.set_error(f"Cannot save: {'; '.join(problems)}.")
            self._fit()  # the line grows the dialog rather than squeezing the form
            return
        self.problems_label.clear()
        self.profile_saved.emit(profile)
        super().accept()

    def refresh_addons(self) -> None:
        """Re-read the OCR add-on's status (after Install…/Repair… ran the wizard's add-ons page, UJ-22b)."""
        self._show_addon()
        self._fit()

    @property
    def listing_windows(self) -> bool:
        """A window listing (and the OBS start before it) is in flight."""
        return self._listing != 0

    @staticmethod
    def _stored_choice(base: GameProfile) -> TextFrom:
        if base.text_mode is TextMode.OCR:
            return TextFrom.OCR
        if base.source_ids is None and not base.clipboard:
            return TextFrom.HOOKER
        if base.source_ids == () and base.clipboard:
            return TextFrom.CLIPBOARD
        return TextFrom.KEPT

    def _kept_text(self, base: GameProfile) -> str:
        names = {source.id: source.name for source in self._text_sources}
        if base.source_ids is None:
            hookers = "every text hooker"
        else:
            hookers = ", ".join(names.get(sid, sid) for sid in base.source_ids) or "no text hooker"
        return f"As saved: {hookers}" + (" and copied text" if base.clipboard else "")

    def _choice(self) -> TextFrom:
        return TextFrom(self.text_from_combo.currentData())

    def _text_settings(self, choice: TextFrom) -> tuple[TextMode, tuple[str, ...] | None, bool]:
        """``(text_mode, source_ids, clipboard)`` for ``choice``; a stored mix (``KEPT``) and a stored OCR
        profile keep their own values."""
        base = self._base
        if choice is TextFrom.HOOKER:
            return TextMode.HOOK, None, False
        if choice is TextFrom.CLIPBOARD:
            return TextMode.HOOK, (), True
        if choice is TextFrom.OCR:
            return TextMode.OCR, (base.source_ids if base.text_mode is TextMode.OCR else None), False
        return TextMode.HOOK, base.source_ids, base.clipboard

    def _audio(self) -> AudioSettings:
        """UJ-26: on Windows the sound follows the game window; elsewhere the stored value stays."""
        if self.game_sound_check is None:
            return self._base.audio
        if self._window is None:
            return AudioSettings(mode=AudioMode.DESKTOP)
        return AudioSettings(mode=AudioMode.APP if self.game_sound_check.isChecked() else AudioMode.DESKTOP)

    def _ocr_window_title(self) -> str | None:
        stored = self._base.ocr.window_title
        if self.window_title_edit is None:
            return stored
        text = self.window_title_edit.text().strip()
        if text == (stored or "").strip():
            return stored  # untouched: kept byte for byte
        return text or None

    def _advanced_differs(self, base: GameProfile) -> bool:
        """UJ-29: the disclosure opens expanded when anything inside differs from its default."""
        if base.capture.kind is not CaptureKind.AUTO or base.filters != FilterSettings():
            return True
        if self._windows and base.capture.window and base.audio.mode is not AudioMode.APP:
            return True
        if base.text_mode is TextMode.OCR:
            if base.ocr.engine is not default_ocr_engine(self._platform):
                return True
            if self._windows and base.ocr.window_title not in (None, "", window_title(base.capture.window)):
                return True
        return False

    # Form behaviour -------------------------------------------------------------------------

    def _fit(self) -> None:
        if self._built:
            layout.fit_dialog(
                self, scroll=self._scroll, forms=(self._general_form, self._ocr_form, self._advanced_form)
            )

    def _text_from_changed(self) -> None:
        choice = self._choice()
        self.ocr_group.setVisible(choice is TextFrom.OCR)
        clipboard = choice is TextFrom.CLIPBOARD or (choice is TextFrom.KEPT and self._base.clipboard)
        layout.show_message(self.clipboard_note, strings.WAYLAND_CLIPBOARD_TEXT if clipboard and self._wayland else "")
        ocr = choice is TextFrom.OCR
        self._advanced_form.setRowVisible(self.engine_combo, ocr)
        if self.window_title_edit is not None:
            self._advanced_form.setRowVisible(self.window_title_edit, ocr)
        self._fit()

    def _capture_kind_changed(self) -> None:
        self._show_window_rows()
        self._refresh_capture_method()

    def _auto_toggled(self, on: bool) -> None:
        self.auto_options.setVisible(on)
        self._fit()

    def _advanced_toggled(self, on: bool) -> None:
        self.advanced_button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.advanced_box.setVisible(on)
        self._fit()

    def _show_window_rows(self) -> None:
        """What depends on the game window and the capture method (UJ-26, UJ-28)."""
        chosen = self._window is not None
        if self.game_sound_check is not None:
            self._advanced_form.setRowVisible(self.game_sound_check, chosen)
        pipewire = CaptureKind(self.capture_kind_combo.currentData()) is CaptureKind.PIPEWIRE
        self.window_close_check.setVisible(chosen and not pipewire)

    def _show_addon(self) -> None:
        """UJ-27, UJ-22b: the add-on's status only while it is not ready, with Install…/Repair… when routed."""
        addon = self._services.ocr_addon
        status = addon.status()
        ready = status is AddonStatus.READY
        self.addon_row.setVisible(not ready)
        self.ocr_status_label.setText(f"OCR add-on: {strings.ADDON_STATUS_TEXT[status]}")
        self.install_button.setText(strings.install_elsewhere_text(status))
        routed = self._services.install_addons is not None
        self.install_button.setVisible(routed and status in (AddonStatus.MISSING, AddonStatus.BROKEN))
        layout.show_message(self.ocr_note_label, addon.note or "")

    def _install_addons(self) -> None:
        if self._services.install_addons is not None:
            self._services.install_addons()

    def _show_area(self) -> None:
        self.area_label.setText("Selected" if self._rects else "Not selected")
        self.clear_area_button.setVisible(self._rects is not None)

    def _clear_area(self) -> None:
        self._rects = None
        self._show_area()

    # The Game window dropdown (UJ-26, D-03) ---------------------------------------------------

    def _show_window_choice(self, *, busy: bool = False) -> None:
        """No window, the stored window (marked when the last listing lacked it), the listed windows, and
        while a listing runs a disabled item that says what it is doing."""
        combo = self.window_combo
        combo.clear()
        combo.addItem(NO_WINDOW_WINDOWS if self._windows else NO_WINDOW_LINUX, None)
        listed = self._last_listing
        values = {item.value for item in listed or ()}
        if self._window is not None and self._window not in values:
            name = self._window_names.get(self._window) or window_label(self._window)
            combo.addItem(name if listed is None else f"{name} {NOT_OPEN_NOW}", self._window)
        for item in listed or ():
            combo.addItem(item.name, item.value)
        if busy:
            combo.addItem(self._busy_text, None)
            model = combo.model()
            assert isinstance(model, QStandardItemModel)
            last = model.item(combo.count() - 1)
            assert last is not None
            last.setEnabled(False)
        combo.setCurrentIndex(0 if self._window is None else combo.findData(self._window))
        self._size_popup()

    def _size_popup(self) -> None:
        """U3-09: the popup is as wide as its widest item."""
        combo = self.window_combo
        view, style = combo.view(), combo.style()
        assert view is not None and style is not None
        metrics = combo.fontMetrics()
        widest = max((metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
        scroll_bar = style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        view.setMinimumWidth(widest + scroll_bar + 2 * view.frameWidth() + 16)

    def _list_windows(self) -> None:
        """The dropdown opens: list OBS's windows again, starting OBS first when it is closed (D-03)."""
        if self._listing or self._reshowing:
            return
        self._listings += 1
        listing = self._listing = self._listings
        self._busy_text = LOOKING_FOR_WINDOWS
        self.windows_message.clear()
        self._show_window_choice(busy=True)
        relay = self._relay

        def report(stage: ObsStartStage) -> None:  # on the I/O loop
            with contextlib.suppress(RuntimeError):  # the dialog is gone
                relay.stage.emit(listing, stage)

        self._submit(self._services.capture.list_windows(self.profile(), report), self._windows_listed)

    def _stage_heard(self, listing: int, stage: object) -> None:
        if listing != self._listing:
            return
        starting = stage in (ObsStartStage.ENABLING_SERVER, ObsStartStage.LAUNCHING)
        self._busy_text = STARTING_OBS if starting else LOOKING_FOR_WINDOWS
        combo = self.window_combo
        combo.setItemText(combo.count() - 1, self._busy_text)
        self._size_popup()

    def _windows_listed(self, future: concurrent.futures.Future[WindowListing]) -> None:
        self._listing = 0
        exc = future.exception()
        if exc is not None:
            self._show_window_choice()
            if isinstance(exc, WindowListError):
                self.windows_message.set_error(str(exc))
            elif isinstance(exc, ObsError):
                self.windows_message.set_error(f"OBS could not list its windows: {exc}")
            else:
                log.error("Listing windows failed", exc_info=exc)
                self.windows_message.set_error(f"Listing windows failed: {exc}")
            self._reshow_popup()
            return
        listing = future.result()
        self._last_listing = listing.items
        for item in listing.items:
            self._window_names[item.value] = item.name
        self._show_window_choice()
        self.windows_message.set_error(listing.warning or ("" if listing.items else NO_WINDOWS))
        self._reshow_popup()
        self._refresh_capture_method()  # B4-01: OBS may have just started

    def _reshow_popup(self) -> None:
        """Show an open popup again, so it takes its new size (UJ-26)."""
        view = self.window_combo.view()
        if view is None or not view.isVisible():
            return
        self._reshowing = True
        try:
            self.window_combo.hidePopup()
            self.window_combo.showPopup()
        finally:
            self._reshowing = False

    def _window_chosen(self, index: int) -> None:
        value = self.window_combo.itemData(index)
        window = value if isinstance(value, str) else None
        if window == self._window:
            return
        self._window = window
        self._follow_window_title(window)
        self._show_window_rows()
        self._refresh_capture_method()

    def _follow_window_title(self, window: str | None) -> None:
        """UJ-27: the OCR window title follows the game window while empty or still the derived title."""
        edit = self.window_title_edit
        derived = window_title(window)
        if edit is not None and edit.text().strip() in ("", self._derived_title or ""):
            edit.setText(derived or "")
        self._derived_title = derived

    # OBS and owocr, on the I/O loop -----------------------------------------------------------

    def _submit[T](self, coro: Coroutine[Any, Any, T], on_done: Callable[[concurrent.futures.Future[T]], None]) -> None:
        future = self._services.run(coro)
        self._pending.add(future)
        relay = self._relay

        def finished(done: concurrent.futures.Future[T]) -> None:
            with contextlib.suppress(RuntimeError):  # The dialog is gone.
                relay.done.emit(on_done, done)

        future.add_done_callback(finished)

    def _deliver(self, on_done: Callable[[concurrent.futures.Future[Any]], None], future: Any) -> None:
        self._pending.discard(future)
        if not future.cancelled():
            on_done(future)

    def _cancel_pending(self) -> None:
        for future in list(self._pending):
            future.cancel()

    def _refresh_capture_method(self) -> None:
        self._capture_request += 1
        request = self._capture_request
        self.capture_method_label.setText("In use: asking OBS…")

        def known(future: concurrent.futures.Future[str]) -> None:
            if request != self._capture_request:
                return
            exc = future.exception()
            if isinstance(exc, ObsConnectError):
                text = "not known until OBS answers"
            elif exc is not None:
                text = f"unknown, OBS did not answer ({exc})"
            else:
                kind = future.result()
                text = CAPTURE_METHOD_NAMES.get(kind, kind) if kind else "none: OBS offers no input for it here"
            self.capture_method_label.setText(f"In use: {text}")

        self._submit(self._services.capture.capture_method(self.profile()), known)

    def _select_area(self) -> None:
        title = None if self.window_title_edit is None else (self.window_title_edit.text().strip() or None)
        self.select_area_button.setEnabled(False)
        self.area_message.clear()
        self._submit(self._services.ocr_picker.pick(title), self._area_picked)

    def _area_picked(self, future: concurrent.futures.Future[str | None]) -> None:
        self.select_area_button.setEnabled(True)
        exc = future.exception()
        if exc is not None:
            if not isinstance(exc, RuntimeError):
                log.error("Selecting the OCR area failed", exc_info=exc)
            self.area_message.set_error(str(exc))
            return
        rects = future.result()
        if rects is None:
            return  # cancelled: the previous area stays
        self._rects = rects
        self._show_area()
