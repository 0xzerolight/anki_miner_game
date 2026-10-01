"""Auto mode: start on the first line, stop when idle or when the game window closes (spec 12).

``AutoMode`` subscribes to the session actor's events and sends it ordinary
``UserCommand``s; no other module knows it exists. Everything is gated on
the armed game's ``auto.enabled``. The profile is looked up by
``StateChanged.slug`` whenever the state or the armed game changes, so an
edit made while armed applies from the next state, and arming another game
starts a new armed period with that game's settings.

- Auto-start: the first ``LineAccepted`` while ``armed`` posts ``start``
  carrying that line (``UserCommand.line``), once per armed period; the
  actor holds the line and journals it at offset 0 on ``STARTED``. A
  ``StartRecord`` failure leaves the state at ``armed``; its banner
  (``START_FAILED_BANNER_KEY``) lets the next line try again. A recording
  that ends without auto mode's own stop (the user's Stop in the window,
  the tray, the hotkey or OBS) pauses auto-start until a recording starts,
  the app goes idle or another game is readied (D-06); OBS closing during
  the recording (``obs_exited``) is no such stop. ``auto_start_pending``
  tells the window whether the next line starts a recording.
- Auto-stop, idle: while ``recording``, no accepted line for
  ``auto.stop_idle_minutes`` (counted from the recording start when no line
  came yet) posts ``stop``.
- Auto-stop, window closed: while ``recording``, every ``POLL_S`` the pinned
  window's list is read; two consecutive misses post ``stop``. The rule
  counts enabled items only (``window_open``, spec 12 as amended by
  ``docs/m0/wave-1-amendments.md`` item 10). PipeWire capture has no window
  list, so it relies on the idle stop.
- A ``StopRecord`` failure leaves the state at ``recording``; its banner
  (``STOP_FAILED_BANNER_KEY``) lets the next check stop again.

``on_event`` runs on the actor's thread and ``check``/``run`` on the I/O
loop, which is the same thread (``SessionControl``), so no lock is needed.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Protocol

from anki_miner_game.interfaces.session import SessionControl
from anki_miner_game.models.messages import (
    START_FAILED_BANNER_KEY,
    STOP_FAILED_BANNER_KEY,
    AppState,
    BannerRaised,
    CommandKind,
    LineAccepted,
    SessionEvent,
    StateChanged,
    UserCommand,
)
from anki_miner_game.models.obs import ObsError
from anki_miner_game.models.profile import AutoSettings, CaptureKind, GameProfile
from anki_miner_game.obs.provision import window_class_exe
from anki_miner_game.session.session import BannerKey

log = logging.getLogger(__name__)

POLL_S: Final = 5.0
"""Seconds between checks: the window-list poll interval of spec 12, and the idle check's resolution."""

MISSES_TO_STOP: Final = 2

_X11_SEP: Final = "\r\n"


class ListedWindow(Protocol):
    """One item of the pinned input's window list (``Provisioner.list_windows``)."""

    @property
    def value(self) -> str: ...

    @property
    def enabled(self) -> bool:
        """``itemEnabled``: ``False`` on the configured value OBS keeps listing when no live window matches it."""
        ...


ListWindows = Callable[[], Awaitable[Sequence[ListedWindow]]]


def window_open(items: Sequence[ListedWindow], stored: str) -> bool | None:
    """Whether the window pinned as ``stored`` is still open; ``None`` when the list cannot tell.

    Only enabled items count: OBS keeps listing the configured value, disabled,
    once no live window has exactly that string, which also happens to a
    window that is open but retitled.

    - X11 (``<xid>\\r\\n<name>\\r\\n<class>``): open while item 0 is enabled or
      an enabled item has the stored xid.
    - Windows (``<title>:<class>:<exe>``, ``#3A``/``#22`` escaped): open while
      an enabled item has the stored class and exe, compared case-insensitively.

    An empty list (OBS always lists the configured value, so the input has no
    window list) or a stored value in neither format gives ``None``.
    """
    if not items:
        return None
    if _X11_SEP in stored:
        xid = stored.split(_X11_SEP, 1)[0]
        return items[0].enabled or any(i.enabled and i.value.split(_X11_SEP, 1)[0] == xid for i in items)
    wanted = window_class_exe(stored)
    if wanted is None:
        return None
    return any(i.enabled and window_class_exe(i.value) == wanted for i in items)


class AutoMode:
    def __init__(
        self,
        control: SessionControl,
        profile_for: Callable[[str], GameProfile | None],
        list_windows: ListWindows,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """``profile_for(slug)`` returns that game's profile, or ``None``.

        It runs on the actor's thread, so it must not read the disk: pass a
        lookup over the profiles already loaded. ``SessionControl`` exposes no
        armed game, so build this before the actor publishes its first
        ``StateChanged`` (at launch, while it is idle).
        """
        self._control = control
        self._profile_for = profile_for
        self._list_windows = list_windows
        self._now = now
        self._state = control.state
        self._slug: str | None = None
        self._profile: GameProfile | None = None
        self._start_sent = False
        self._stop_sent = False
        self._last_activity = now()
        self._misses = 0
        self._manual_end = False
        """The last recording ended without this auto mode's stop (D-06): auto-start waits."""
        self._obs_exited = False
        """``obs_exited`` was raised since the last recording started: OBS closing is no manual stop."""
        control.subscribe(self.on_event)

    def on_event(self, event: SessionEvent) -> None:
        if isinstance(event, StateChanged):
            if event.state is self._state and event.slug == self._slug:
                return
            self._note_pause(event)
            self._state, self._slug = event.state, event.slug
            self._profile = None if event.slug is None else self._profile_for(event.slug)
            self._start_sent = False
            self._stop_sent = False
            self._misses = 0
            self._last_activity = self._now()
        elif isinstance(event, BannerRaised):
            key = event.banner.key
            if key == START_FAILED_BANNER_KEY and self._state is AppState.ARMED:
                self._start_sent = False
            elif key == STOP_FAILED_BANNER_KEY and self._state is AppState.RECORDING:
                self._stop_sent = False  # B1-08: the next check posts STOP again
            elif key == BannerKey.OBS_EXITED:
                self._obs_exited = True
        elif isinstance(event, LineAccepted):
            self._last_activity = self._now()
            if not self._start_sent and self.auto_start_pending():
                self._start_sent = True
                self._control.post(UserCommand(CommandKind.START, line=event.line))

    def auto_start_pending(self) -> bool:
        """True while the app is Ready (``armed``) with a game whose auto mode starts at the first line,
        and auto-start is not paused by a manual stop (D-06): the next accepted line starts a recording.
        Called from the Qt main thread; it reads attributes the loop writes at each ``StateChanged``,
        before the presenter forwards that state (``AutoMode`` subscribes to the actor before the
        presenter does, ``App._build``)."""
        return self._state is AppState.ARMED and self._start_on_first_line() and not self._paused()

    def _note_pause(self, event: StateChanged) -> None:
        """D-06, on each state change, before the per-state resets read ``_stop_sent`` away."""
        if event.state is AppState.RECORDING or event.state is AppState.IDLE or event.slug != self._slug:
            self._manual_end = False  # a recording starts, Done playing, or another game: auto-start is live
            self._obs_exited = False
        elif self._state is AppState.RECORDING:
            self._manual_end = not self._stop_sent

    def _paused(self) -> bool:
        return self._manual_end and not self._obs_exited

    async def check(self) -> None:
        """One idle check and one window poll; ``run`` calls it every ``POLL_S``."""
        auto = self._auto()
        if auto is None or self._state is not AppState.RECORDING or self._stop_sent:
            return
        idle_s = auto.stop_idle_minutes * 60
        if idle_s > 0 and self._now() - self._last_activity >= idle_s:
            self._stop("idle")
            return
        window = self._watched_window()
        if window is None:
            return
        try:
            items = await self._list_windows()
        except ObsError as exc:
            log.debug("auto mode: window list unavailable: %s", exc)
            return
        if self._state is not AppState.RECORDING or self._stop_sent:
            return
        is_open = window_open(items, window)
        if is_open is None:
            return
        self._misses = 0 if is_open else self._misses + 1
        if self._misses >= MISSES_TO_STOP:
            self._stop("window closed")

    async def run(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Call ``check`` every ``POLL_S`` until cancelled."""
        while True:
            await sleep(POLL_S)
            await self.check()

    def _auto(self) -> AutoSettings | None:
        if self._profile is None or not self._profile.auto.enabled:
            return None
        return self._profile.auto

    def _start_on_first_line(self) -> bool:
        auto = self._auto()
        return auto is not None and auto.start_on_first_line

    def _watched_window(self) -> str | None:
        profile = self._profile
        if profile is None or not profile.auto.stop_on_window_close or profile.capture.kind is CaptureKind.PIPEWIRE:
            return None
        return profile.capture.window

    def _stop(self, reason: str) -> None:
        log.info("auto mode: stopping the session (%s)", reason)
        self._stop_sent = True
        self._control.post(UserCommand(CommandKind.STOP))
