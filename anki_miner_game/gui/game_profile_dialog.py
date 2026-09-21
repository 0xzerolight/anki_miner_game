"""The game profile dialog (spec 5 ``GameProfile``, 11.3 window picker, 12, 14).

``CapturePicker`` is the dialog's view of OBS: the capture method provisioning would use for the
profile, and the window list the picker offers. ``Provisioner.list_windows`` reads the current
scene collection, so the picker never lists the user's collection:

- In any state but ``idle`` OBS is on the app's collection (arming put it there), so it lists at once.
- While ``idle`` it runs spec 6.2 step 1's output check first (an active output: the picker says
  which and lists nothing), reads the current collection's name, provisions the app's collection
  for the profile (``ensure_collection``), lists, and switches back through the gateway. The switch
  back is done on ``CurrentSceneCollectionChanged``, never on the answer, whose order against the
  event is not fixed (``docs/m0/obs-behaviour.md`` item 5); there is none when the user's collection
  was the app's already (item 4, no event would come).

The picker offers enabled items only (``docs/m0/wave-1-amendments.md`` item 11): OBS keeps listing
the configured window as a disabled item once no live window matches it, and on X11 a retitled
window is that disabled item plus an enabled one under its new name (R2 item 15). The stored
``capture.window`` is kept until the user picks another window or unpins it.

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
from collections.abc import Callable, Collection, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from PyQt6.QtCore import QObject, pyqtSignal
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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from anki_miner_game.interfaces.addons import AddonService, OcrAreaPicker
from anki_miner_game.interfaces.obs import ObsGateway, Provisioner
from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.config import TextSourceConfig
from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.messages import AppState, ObsEvent
from anki_miner_game.models.obs import ObsError, ObsEventName, ObsRequestError, WindowItem
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
    event. Its coroutines run on the I/O loop.
    """

    def __init__(
        self,
        gateway: ObsGateway,
        provisioner: Provisioner,
        session: SessionControl,
        *,
        switch_timeout_s: float = SWITCH_TIMEOUT_S,
    ) -> None:
        self._gateway = gateway
        self._provisioner = provisioner
        self._session = session
        self._switch_timeout_s = switch_timeout_s
        self._waiting: tuple[asyncio.AbstractEventLoop, asyncio.Future[None], str] | None = None
        self._running: set[asyncio.Task[Any]] = set()
        """The calls still running; the reference keeps one whose caller was cancelled alive."""
        gateway.subscribe(self._on_event)

    async def capture_method(self, profile: GameProfile) -> str:
        """The OBS input kind that would capture this profile's game; ``""`` when none is available.

        Runs to its end even when the caller is cancelled (``_to_the_end``).
        """
        return await self._to_the_end(self._provisioner.capture_method(profile))

    async def list_windows(self, profile: GameProfile) -> WindowListing:
        """The windows to pin for ``profile``. Raises ``WindowListError`` or ``ObsError``.

        Runs to its end, switch back included, even when the caller is cancelled (``_to_the_end``).
        """
        return await self._to_the_end(self._list_windows(profile))

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

    async def _list_windows(self, profile: GameProfile) -> WindowListing:
        if self._session.state is not AppState.IDLE:
            return WindowListing(_enabled(await self._provisioner.list_windows()))
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


AUTO_START_NOTE: Final = (
    "The first line starts the recording, so it sits at 0:00 and the start of its voice is missing "
    "from the video. Press Start before the first line if that matters."
)
"""Spec 12, auto-start."""

IDLE_STOP_NOTE: Final = "0 never stops for idling. The last cue is capped, so an idle tail only adds video."

WINDOW_CLOSE_NOTE: Final = (
    "Needs a pinned window. Windows and X11 only: PipeWire capture (Wayland) has no window list, "
    "so there the idle stop applies."
)
"""Spec 12, auto-stop when the window closes."""

TYPEWRITER_NOTE: Final = (
    "A line that extends the previous one within 2 s replaces it. Off by default: it would also "
    "swallow a short real line, such as え followed by えっと…"
)
"""Spec 8.2 step 9."""

CLOUD_OCR_NOTE: Final = (
    "Google Lens and Bing run in the cloud: every screenshot of the OCR area leaves this machine "
    "for Google's or Microsoft's servers."
)
"""Spec 14: the dialog states that screenshots leave the machine."""

NO_WINDOWS: Final = (
    "OBS lists no window to pick. PipeWire capture (Wayland) has no window list: OBS asks which "
    "window to capture the first time it records."
)

CAPTURE_METHOD_NAMES: Final = {
    "game_capture": "Game Capture",
    "window_capture": "Window Capture",
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

_AUDIO_LABELS: Final = {AudioMode.APP: "The game window only", AudioMode.DESKTOP: "The whole desktop"}

_ENGINE_LABELS: Final = {
    OcrEngine.ONEOCR: "OneOCR (on this machine)",
    OcrEngine.MEIKIOCR: "meikiocr (on this machine)",
    OcrEngine.GLENS: "Google Lens (cloud)",
    OcrEngine.BING: "Bing (cloud)",
}
_CLOUD_ENGINES: Final = frozenset({OcrEngine.GLENS, OcrEngine.BING})

_ADDON_STATUS_TEXT: Final = {
    AddonStatus.READY: "OCR add-on installed.",
    AddonStatus.MISSING: "The OCR add-on is not installed: install it from the setup wizard.",
    AddonStatus.INSTALLING: "The OCR add-on is being installed.",
    AddonStatus.BROKEN: "The OCR add-on is damaged: install it again from the setup wizard.",
}


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


class _Relay(QObject):
    """Carries a finished future from the I/O loop's thread to the dialog on the main thread."""

    done = pyqtSignal(object, object)


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _fill(combo: QComboBox, values: Sequence[Any], labels: dict[Any, str], current: Any) -> None:
    """Add ``values`` (plus ``current`` when it is not among them, so a stored value survives) and select ``current``."""
    for value in (*values, *(() if current in values else (current,))):
        combo.addItem(labels.get(value, str(value)), value)
    combo.setCurrentIndex(combo.findData(current))


class GameProfileDialog(QDialog):
    """Create or edit one game profile (spec 5); emits ``profile_saved(GameProfile)`` on Save."""

    profile_saved = pyqtSignal(object)

    def __init__(
        self,
        services: ProfileDialogServices,
        text_sources: Sequence[TextSourceConfig],
        profile: GameProfile | None = None,
        *,
        taken_slugs: Collection[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        """``profile`` is ``None`` for a new game, whose slug comes from its title and must not be in ``taken_slugs``."""
        super().__init__(parent)
        self._services = services
        self._platform = services.platform or sys.platform
        self._windows = self._platform == "win32"
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
        self._rects: str | None = base.ocr.rects
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._capture_request = 0
        self._relay = _Relay(self)
        self._relay.done.connect(self._deliver)

        self.setWindowTitle("Edit game" if profile else "New game")
        content = QWidget()
        column = QVBoxLayout(content)
        column.addWidget(self._build_general(base))
        column.addWidget(self._build_hook(base))
        column.addWidget(self._build_ocr(base))
        column.addWidget(self._build_capture(base))
        column.addWidget(self._build_filters(base))
        column.addWidget(self._build_auto(base))
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        self.problems_label = _note("")
        self.problems_label.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)
        outer.addWidget(self.problems_label)
        outer.addWidget(self.buttons)

        self.finished.connect(self._cancel_pending)
        self._mode_changed()
        self._engine_changed()
        self._show_window()
        self._show_area()
        self._refresh_capture_method()

    # Building -------------------------------------------------------------------------------

    def _build_general(self, base: GameProfile) -> QWidget:
        box = QGroupBox("Game")
        form = QFormLayout(box)
        self.title_edit = QLineEdit(base.title)
        self.title_edit.setPlaceholderText("The game's name, used for its folder and sessions")
        form.addRow("Title", self.title_edit)
        self.mode_combo = QComboBox()
        _fill(
            self.mode_combo,
            (TextMode.HOOK, TextMode.OCR),
            {TextMode.HOOK: "Text hooker or clipboard", TextMode.OCR: "OCR of the screen (add-on)"},
            base.text_mode,
        )
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Text from", self.mode_combo)
        self.audio_combo = QComboBox()
        audio_modes = (AudioMode.APP, AudioMode.DESKTOP) if self._windows else (AudioMode.DESKTOP,)
        _fill(self.audio_combo, audio_modes, _AUDIO_LABELS, base.audio.mode)
        form.addRow("Record audio of", self.audio_combo)
        return box

    def _build_hook(self, base: GameProfile) -> QWidget:
        self.hook_group = QGroupBox("Text sources")
        column = QVBoxLayout(self.hook_group)
        self.all_sources_check = QCheckBox("Every source turned on in Settings")
        self.all_sources_check.setChecked(base.source_ids is None)
        self.all_sources_check.toggled.connect(self._sources_mode_changed)
        column.addWidget(self.all_sources_check)
        chosen = set(base.source_ids or ())
        self.source_checks: dict[str, QCheckBox] = {}
        for source in self._text_sources:
            label = source.name if source.enabled else f"{source.name} (turned off in Settings)"
            check = QCheckBox(label)
            check.setChecked(source.id in chosen)
            self.source_checks[source.id] = check
            column.addWidget(check)
        self.clipboard_check = QCheckBox("Clipboard")
        self.clipboard_check.setChecked(base.clipboard)
        column.addWidget(self.clipboard_check)
        self._sources_mode_changed()
        return self.hook_group

    def _build_ocr(self, base: GameProfile) -> QWidget:
        self.ocr_group = QGroupBox("OCR")
        form = QFormLayout(self.ocr_group)
        addon = self._services.ocr_addon
        self.ocr_status_label = _note(" ".join(filter(None, (_ADDON_STATUS_TEXT[addon.status()], addon.note))))
        form.addRow(self.ocr_status_label)
        self.engine_combo = QComboBox()
        engines = (default_ocr_engine(self._platform), OcrEngine.GLENS, OcrEngine.BING)
        _fill(self.engine_combo, engines, _ENGINE_LABELS, base.ocr.engine)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        form.addRow("Engine", self.engine_combo)
        self.cloud_note = _note(CLOUD_OCR_NOTE)
        form.addRow(self.cloud_note)
        self.language_edit = QLineEdit(base.ocr.language)
        form.addRow("Language", self.language_edit)
        self.window_title_edit: QLineEdit | None = None
        if self._windows:
            self.window_title_edit = QLineEdit(base.ocr.window_title or "")
            self.window_title_edit.setPlaceholderText("Empty: the area is on the screen")
            form.addRow("Game window title", self.window_title_edit)
        self.area_label = _note("")
        self.select_area_button = QPushButton("Select OCR area")
        self.select_area_button.clicked.connect(self._select_area)
        self.clear_area_button = QPushButton("Clear")
        self.clear_area_button.clicked.connect(self._clear_area)
        row = QHBoxLayout()
        row.addWidget(self.area_label, 1)
        row.addWidget(self.select_area_button)
        row.addWidget(self.clear_area_button)
        form.addRow("Area", row)
        self.area_message = _note("")
        form.addRow(self.area_message)
        return self.ocr_group

    def _build_capture(self, base: GameProfile) -> QWidget:
        box = QGroupBox("Capture")
        form = QFormLayout(box)
        self.capture_kind_combo = QComboBox()
        kinds = _WINDOWS_CAPTURE_KINDS if self._windows else _LINUX_CAPTURE_KINDS
        _fill(self.capture_kind_combo, kinds, _CAPTURE_KIND_LABELS, base.capture.kind)
        self.capture_kind_combo.currentIndexChanged.connect(self._refresh_capture_method)
        form.addRow("Capture", self.capture_kind_combo)
        self.capture_method_label = _note("")
        form.addRow(self.capture_method_label)
        self.pinned_label = _note("")
        self.unpin_button = QPushButton("Unpin")
        self.unpin_button.clicked.connect(self._unpin)
        pinned = QHBoxLayout()
        pinned.addWidget(self.pinned_label, 1)
        pinned.addWidget(self.unpin_button)
        form.addRow("Pinned window", pinned)
        self.window_combo = QComboBox()
        self.window_combo.setPlaceholderText("List the windows, then pick the game")
        self.window_combo.activated.connect(self._window_picked)
        self.list_windows_button = QPushButton("List windows")
        self.list_windows_button.clicked.connect(self._list_windows)
        picker = QHBoxLayout()
        picker.addWidget(self.window_combo, 1)
        picker.addWidget(self.list_windows_button)
        form.addRow("Pick", picker)
        self.windows_message = _note("")
        form.addRow(self.windows_message)
        return box

    def _build_filters(self, base: GameProfile) -> QWidget:
        box = QGroupBox("Line filters")
        column = QVBoxLayout(box)
        self.speaker_check = QCheckBox("Remove a leading 【name】 speaker tag")
        self.speaker_check.setChecked(base.filters.speaker_strip)
        column.addWidget(self.speaker_check)
        self.typewriter_check = QCheckBox("Merge lines that are typed out gradually")
        self.typewriter_check.setChecked(base.filters.typewriter_merge)
        column.addWidget(self.typewriter_check)
        column.addWidget(_note(TYPEWRITER_NOTE))
        return box

    def _build_auto(self, base: GameProfile) -> QWidget:
        self.auto_group = QGroupBox("Auto mode")
        self.auto_group.setCheckable(True)
        self.auto_group.setChecked(base.auto.enabled)
        form = QFormLayout(self.auto_group)
        self.auto_start_check = QCheckBox("Start recording at the first line")
        self.auto_start_check.setChecked(base.auto.start_on_first_line)
        form.addRow(self.auto_start_check)
        form.addRow(_note(AUTO_START_NOTE))
        self.idle_spin = QSpinBox()
        self.idle_spin.setRange(0, 24 * 60)
        self.idle_spin.setSuffix(" min")
        self.idle_spin.setSpecialValueText("Never")
        self.idle_spin.setValue(max(0, base.auto.stop_idle_minutes))
        form.addRow("Stop after no line for", self.idle_spin)
        form.addRow(_note(IDLE_STOP_NOTE))
        self.window_close_check = QCheckBox("Stop when the pinned game window closes")
        self.window_close_check.setChecked(base.auto.stop_on_window_close)
        form.addRow(self.window_close_check)
        form.addRow(_note(WINDOW_CLOSE_NOTE))
        return self.auto_group

    # The profile ----------------------------------------------------------------------------

    def profile(self) -> GameProfile:
        """The profile as the form stands; ``problems`` says whether it can be saved."""
        base = self._base
        mode = TextMode(self.mode_combo.currentData())
        hook = mode is TextMode.HOOK
        source_ids: tuple[str, ...] | None = None
        if hook and not self.all_sources_check.isChecked():
            source_ids = tuple(sid for sid, check in self.source_checks.items() if check.isChecked())
        title = self.title_edit.text().strip()
        window_title = base.ocr.window_title
        if self.window_title_edit is not None:
            window_title = self.window_title_edit.text().strip() or None
        return GameProfile(
            slug=self._original.slug if self._original else self._services.slugify(title),
            title=title,
            text_mode=mode,
            source_ids=source_ids,
            clipboard=hook and self.clipboard_check.isChecked(),
            capture=CaptureSettings(kind=CaptureKind(self.capture_kind_combo.currentData()), window=self._window),
            audio=AudioSettings(mode=AudioMode(self.audio_combo.currentData())),
            filters=FilterSettings(
                speaker_strip=self.speaker_check.isChecked(),
                typewriter_merge=self.typewriter_check.isChecked(),
            ),
            ocr=OcrSettings(
                engine=OcrEngine(self.engine_combo.currentData()),
                language=self.language_edit.text().strip(),
                window_title=window_title,
                rects=self._rects,
            ),
            auto=AutoSettings(
                enabled=self.auto_group.isChecked(),
                start_on_first_line=self.auto_start_check.isChecked(),
                stop_idle_minutes=self.idle_spin.value(),
                stop_on_window_close=self.window_close_check.isChecked(),
            ),
        )

    def problems(self, profile: GameProfile) -> list[str]:
        """Why ``profile`` cannot be saved: ``validate`` plus what only the dialog knows."""
        problems = validate(profile)
        if self._original is None and profile.slug in self._taken:
            problems.append("a game with this title exists already")
        if profile.text_mode is TextMode.HOOK:
            if profile.source_ids is None:
                sources = [source.id for source in self._text_sources if source.enabled]
            else:
                sources = list(profile.source_ids)
            if not sources and not profile.clipboard:
                problems.append("choose a text source or the clipboard")
        elif not profile.ocr.language:
            problems.append("the OCR language is empty")
        return problems

    def accept(self) -> None:
        profile = self.profile()
        problems = self.problems(profile)
        if problems:
            text = "; ".join(problems)
            self.problems_label.setText(f"Cannot save: {text}.")
            self.problems_label.show()
            return
        self.problems_label.hide()
        self.profile_saved.emit(profile)
        super().accept()

    # Form behaviour -------------------------------------------------------------------------

    def _mode_changed(self) -> None:
        ocr = TextMode(self.mode_combo.currentData()) is TextMode.OCR
        self.hook_group.setVisible(not ocr)
        self.ocr_group.setVisible(ocr)

    def _sources_mode_changed(self) -> None:
        every = self.all_sources_check.isChecked()
        for check in self.source_checks.values():
            check.setEnabled(not every)

    def _engine_changed(self) -> None:
        self.cloud_note.setVisible(OcrEngine(self.engine_combo.currentData()) in _CLOUD_ENGINES)

    def _show_window(self) -> None:
        if self._window is None:
            self.pinned_label.setText("None")
        else:
            self.pinned_label.setText(self._window_names.get(self._window) or window_label(self._window))
        self.unpin_button.setEnabled(self._window is not None)

    def _show_area(self) -> None:
        self.area_label.setText(self._rects or "None selected")
        self.clear_area_button.setEnabled(self._rects is not None)

    def _window_picked(self, index: int) -> None:
        value = self.window_combo.itemData(index)
        if not isinstance(value, str):
            return
        self._window = value
        self._show_window()
        self._refresh_capture_method()

    def _unpin(self) -> None:
        self._window = None
        self.window_combo.setCurrentIndex(-1)
        self._show_window()
        self._refresh_capture_method()

    def _clear_area(self) -> None:
        self._rects = None
        self._show_area()

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
        self.capture_method_label.setText("Capture method in use: asking OBS…")

        def known(future: concurrent.futures.Future[str]) -> None:
            if request != self._capture_request:
                return
            exc = future.exception()
            if exc is not None:
                text = f"unknown, OBS did not answer ({exc})"
            else:
                kind = future.result()
                text = CAPTURE_METHOD_NAMES.get(kind, kind) if kind else "none: OBS offers no input for it here"
            self.capture_method_label.setText(f"Capture method in use: {text}")

        self._submit(self._services.capture.capture_method(self.profile()), known)

    def _list_windows(self) -> None:
        self.list_windows_button.setEnabled(False)
        self.windows_message.setText("Asking OBS for its windows…")
        self._submit(self._services.capture.list_windows(self.profile()), self._windows_listed)

    def _windows_listed(self, future: concurrent.futures.Future[WindowListing]) -> None:
        self.list_windows_button.setEnabled(True)
        self.window_combo.clear()
        exc = future.exception()
        if exc is not None:
            if isinstance(exc, WindowListError):
                self.windows_message.setText(str(exc))
            elif isinstance(exc, ObsError):
                self.windows_message.setText(f"OBS could not list its windows: {exc}")
            else:
                log.error("Listing windows failed", exc_info=exc)
                self.windows_message.setText(f"Listing windows failed: {exc}")
            return
        listing = future.result()
        for item in listing.items:
            self.window_combo.addItem(item.name, item.value)
            self._window_names[item.value] = item.name
        self.window_combo.setCurrentIndex(self.window_combo.findData(self._window) if self._window else -1)
        self._show_window()
        self.windows_message.setText(listing.warning or ("" if listing.items else NO_WINDOWS))

    def _select_area(self) -> None:
        title = None if self.window_title_edit is None else (self.window_title_edit.text().strip() or None)
        self.select_area_button.setEnabled(False)
        self.area_message.setText("Select the area in owocr's picker…")
        self._submit(self._services.ocr_picker.pick(title), self._area_picked)

    def _area_picked(self, future: concurrent.futures.Future[str | None]) -> None:
        self.select_area_button.setEnabled(True)
        exc = future.exception()
        if exc is not None:
            if not isinstance(exc, RuntimeError):
                log.error("Selecting the OCR area failed", exc_info=exc)
            self.area_message.setText(str(exc))
            return
        rects = future.result()
        if rects is None:
            self.area_message.setText("No area was selected; the previous one stays.")
            return
        self._rects = rects
        self.area_message.setText("")
        self._show_area()
