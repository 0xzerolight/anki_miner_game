"""OBS Protocols: gateway, discovery, provisioning and recorder (spec 11)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsCredentials, ObsInfo, ObsInstall, ProvisionResult, WindowItem, WsConfig
from anki_miner_game.models.profile import GameProfile


class ObsGateway(Protocol):
    """One obs-websocket v5 connection with reconnect (spec 11.2).

    The implementation takes ``now: Callable[[], float] = time.monotonic``.
    Event callbacks run on obsws-python's thread; they only stamp ``now()``
    and hand an ``ObsEvent`` to the subscribed handlers, which only enqueue.
    After every successful connect or reconnect the handlers receive
    ``ObsEvent(ObsEventName.CONNECTED, {}, t)``, and ``CONNECTION_LOST`` when
    the connection drops. Credentials come from ``ObsDiscovery.credentials``
    at every connect; the password is never logged.
    """

    async def connect(self) -> ObsInfo:
        """``GetVersion`` plus the required-request check.

        Raises ``ObsConnectError``, ``ObsAuthError`` or ``ObsUnsupportedError``.
        Retries 207 ``NotReady`` until a timeout, then raises ``ObsRequestError``.
        """
        ...

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return its ``responseData`` (``{}`` when there is none).

        Waits while ``collection_changing`` is true, and retries 207 ``NotReady``
        (OBS loading, or a collection change whose ``...Changing`` event has not
        arrived) until a timeout, then raises ``ObsRequestError``. Raises
        ``ObsRequestError`` when OBS reports failure and ``ObsConnectError``
        when not connected.
        """
        ...

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None: ...

    @property
    def collection_changing(self) -> bool: ...

    async def close(self) -> None:
        """Disconnect and stop reconnecting."""
        ...


class ObsDiscovery(Protocol):
    """Finds, configures and starts the user's OBS (spec 11.1). Never logs a password."""

    def find_install(self) -> ObsInstall | None: ...

    def config_root(self) -> Path | None:
        """The config root of the found install (the Flatpak one for a Flatpak install); ``None`` without one."""
        ...

    def read_ws_config(self) -> WsConfig | None:
        """``plugin_config/obs-websocket/config.json``; ``None`` when the file does not exist.

        Raises ``ObsConfigError`` when the file exists but cannot be read or parsed.
        """
        ...

    def ensure_server_enabled(self) -> bool:
        """Turn the websocket server on while OBS is closed.

        Generates a password only when auth is required and none exists.
        Returns whether the server is enabled afterwards: ``False`` means it
        is off and OBS is running (``is_running``), so the file must be left
        alone, or that no install was found (``find_install`` tells the two
        apart). Raises ``ObsConfigError`` when the file exists but cannot be
        read or parsed, or cannot be written; an unusable file is never
        overwritten.
        """
        ...

    def is_running(self) -> bool:
        """Whether an OBS of this user runs.

        ``True`` when the process list cannot be read: "cannot tell" never
        reads as "not running", which would end a recording OBS is still
        writing or start a second OBS.
        """
        ...

    def launch(self) -> None:
        """Start OBS minimised to the tray, in the folder it needs; nothing while ``is_running``.

        Raises ``ObsConnectError`` when no install is found or the program
        cannot be started.
        """
        ...

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        """``True`` once ``GetVersion`` succeeds; ``False`` after ``timeout_s``.

        OBS answers every request with 207 ``NotReady`` until it has loaded.
        Raises ``ObsAuthError`` when OBS rejects the password on two tries in
        a row; the credentials are read again once in between (spec 17).
        """
        ...

    def credentials(self, cfg: AppConfig) -> ObsCredentials:
        """Host, port and password for the next connect, read from OBS's config each call; ``cfg.obs`` overrides.

        Raises ``ObsConfigError`` (an ``ObsConnectError``, so the gateway's
        ``connect`` passes it on unchanged) when no port is known: OBS's
        websocket ``config.json`` is missing or unreadable and
        ``cfg.obs.port`` is ``None``. With a port override and no readable
        file, the password is ``cfg.obs.password_override`` (possibly ``None``).
        """
        ...


class Provisioner(Protocol):
    """Creates and updates the app's OBS profile and scene collection, touching only what differs (spec 11.3).

    ``ensure_profile`` and ``ensure_collection`` need every OBS output
    inactive (spec 6.2 step 1): OBS refuses ``SetRecordDirectory`` and
    ``SetVideoSettings`` while one runs, by then after the switch, and keeps
    a running output's settings through a profile switch. Both switch OBS to
    the app's profile or collection and never switch back: the caller reads
    the user's names first and restores them afterwards.
    """

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        """Make the app's profile current (creating it when missing) and apply spec 11.3's profile rows.

        Best called while the profile to come back to is current (the wizard;
        arming while the app's profile does not exist yet). It then reads that
        profile's ``[Audio] SampleRate`` and ``ChannelSetup`` before leaving
        it and gives the app's profile the same values, so switching between
        the two never makes OBS ask to restart. After changing a setting OBS
        applies only when it rebuilds its outputs (container, output mode,
        recording quality or encoder) it switches to that profile and back,
        which rebuilds them. When the app's profile is already current it has
        nowhere to switch to: that is the only case of ``needs_restart``.
        """
        ...

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult:
        """Make the app's scene collection current (creating it when missing), with scene ``Game``
        as its program scene and ``profile``'s inputs and mutes; ``needs_restart`` is always ``False``."""
        ...

    async def list_windows(self) -> list[WindowItem]:
        """The window list of the app's scene, for the window picker and auto mode's window-closed check.

        Reads the current scene collection, so call it only while the app's
        collection is current (armed, or after ``ensure_collection``):
        otherwise the answer is ``[]``, or on Windows the list of a user input
        that happens to carry the app's input name.
        ``GetInputPropertiesListPropertyItems`` on one input: Windows reads the
        ``game_capture`` input's ``window`` property, never the ``window_capture``
        fallback, whose list drops minimized windows; X11 reads the
        ``xcomposite_input`` input's ``capture_window``. ``[]`` when the scene has
        no such input (PipeWire capture). Raises only ``ObsError``.
        """
        ...

    async def capture_method(self, profile: GameProfile) -> str:
        """The OBS input kind provisioning would create for this profile's capture settings on the
        connected OBS (feature-detected with ``GetInputKindList``); shown by the game-profile dialog
        as the capture method in use (spec 11.3). Raises ``ObsError`` when OBS is unreachable."""
        ...


class Recorder(Protocol):
    """``StartRecord`` / ``StopRecord``; changes no state itself (spec 11.4)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
