"""OBS facts and records shared by discovery, the gateway, provisioning and the actor (spec 3.3, 11)."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

REQUIRED_REQUESTS: Final[tuple[str, ...]] = (
    "GetVersion",
    "GetRecordStatus",
    "StartRecord",
    "StopRecord",
    "GetStreamStatus",
    "GetReplayBufferStatus",
    "GetVirtualCamStatus",
    "GetProfileList",
    "CreateProfile",
    "SetCurrentProfile",
    "GetSceneCollectionList",
    "CreateSceneCollection",
    "SetCurrentSceneCollection",
    "GetProfileParameter",
    "SetProfileParameter",
    "GetVideoSettings",
    "SetVideoSettings",
    "GetRecordDirectory",
    "SetRecordDirectory",
    "CreateScene",
    "GetInputKindList",
    "CreateInput",
    "SetInputSettings",
    "GetSpecialInputs",
    "SetInputMute",
    "GetInputPropertiesListPropertyItems",
    "GetSceneList",
    "SetCurrentProgramScene",
    "GetInputSettings",
    "GetInputMute",
    "RemoveInput",
    "GetOutputSettings",
)
"""Every request the app sends (spec 3.3); ``GetVersion.availableRequests`` must contain them all."""


class OutputState(StrEnum):
    STARTING = "OBS_WEBSOCKET_OUTPUT_STARTING"
    STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
    STOPPING = "OBS_WEBSOCKET_OUTPUT_STOPPING"
    STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
    PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
    RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"


class ObsEventName(StrEnum):
    RECORD_STATE_CHANGED = "RecordStateChanged"
    RECORD_FILE_CHANGED = "RecordFileChanged"
    CURRENT_SCENE_COLLECTION_CHANGING = "CurrentSceneCollectionChanging"
    CURRENT_SCENE_COLLECTION_CHANGED = "CurrentSceneCollectionChanged"
    CURRENT_PROFILE_CHANGING = "CurrentProfileChanging"
    CURRENT_PROFILE_CHANGED = "CurrentProfileChanged"
    EXIT_STARTED = "ExitStarted"
    CONNECTED = "_Connected"
    """Sent by the gateway itself after every successful connect or reconnect; the actor reconciles on it."""
    CONNECTION_LOST = "_ConnectionLost"
    """Sent by the gateway itself when the connection drops."""


@dataclass(frozen=True)
class ObsInfo:
    obs_version: str
    websocket_version: str
    available_requests: frozenset[str]

    def missing_requests(self) -> tuple[str, ...]:
        """Required requests this OBS lacks, in ``REQUIRED_REQUESTS`` order."""
        return tuple(name for name in REQUIRED_REQUESTS if name not in self.available_requests)


@dataclass(frozen=True)
class ObsCredentials:
    host: str
    port: int
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WsConfig:
    """``plugin_config/obs-websocket/config.json`` under the OBS config root (spec 3.3)."""

    server_enabled: bool
    port: int
    password: str | None = field(repr=False)
    auth_required: bool


@dataclass(frozen=True)
class ObsInstall:
    """How to start the OBS that discovery found (spec 11.1)."""

    argv: tuple[str, ...]
    """The command without ``--minimize-to-tray``."""
    cwd: Path | None
    """The folder OBS must start in (Windows: ``bin\\64bit``); ``None`` when any folder works."""
    flatpak: bool


@dataclass(frozen=True)
class WindowItem:
    name: str
    """Shown to the user."""
    value: str
    """Stored verbatim in ``capture.window``."""
    enabled: bool
    """``itemEnabled`` from ``GetInputPropertiesListPropertyItems``.

    ``False`` on the configured value OBS keeps listing when no live window matches it.
    """


@dataclass(frozen=True)
class ProvisionResult:
    changed: bool
    needs_restart: bool
    """A changed setting takes effect only once OBS re-activates the app's profile (a switch to
    another profile and back, which the next disarm and arm do) or restarts. Text for the user only:
    the app never restarts OBS, and provisioning re-activates the profile itself whenever it
    switched from another one (spec 11.3)."""


class ObsError(Exception):
    """Base of the failures the OBS gateway reports."""


class ObsConnectError(ObsError):
    """OBS cannot be reached, or the websocket handshake failed."""


class ObsAuthError(ObsConnectError):
    """Authentication failed after re-reading OBS's config once (spec 17)."""


class ObsConfigError(ObsConnectError):
    """OBS's websocket ``config.json`` is missing or unreadable, so the port to connect to is unknown."""


class ObsRequestError(ObsError):
    """OBS answered a request with a failure status."""

    def __init__(self, request: str, code: int, comment: str) -> None:
        super().__init__(f"{request} failed ({code}): {comment}")
        self.request = request
        self.code = code
        self.comment = comment


class ObsUnsupportedError(ObsError):
    """This OBS lacks a required request (spec 11.1)."""

    def __init__(self, obs_version: str, missing: tuple[str, ...]) -> None:
        super().__init__(f"OBS {obs_version} lacks {', '.join(missing)}")
        self.obs_version = obs_version
        self.missing = missing
