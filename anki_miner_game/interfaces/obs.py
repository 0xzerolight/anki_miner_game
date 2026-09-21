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
        is off and OBS is running, so the file must be left alone.
        """
        ...

    def is_running(self) -> bool: ...

    def launch(self) -> None:
        """Start OBS minimised to the tray, in the folder it needs."""
        ...

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        """``True`` once ``GetVersion`` succeeds; ``False`` after ``timeout_s``.

        OBS answers every request with 207 ``NotReady`` until it has loaded.
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
    """Creates and updates the app's OBS profile and scene collection, touching only what differs (spec 11.3)."""

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult: ...

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult: ...

    async def list_windows(self) -> list[WindowItem]: ...


class Recorder(Protocol):
    """``StartRecord`` / ``StopRecord``; changes no state itself (spec 11.4)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
