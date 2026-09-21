"""OBS provisioning: the app's profile, scene collection, capture inputs and window list (spec 11.3).

``parse_obs_window_target`` and ``get_video_source_priority`` are ported from GameSentenceMiner
``GameSentenceMiner/obs/launch.py`` (lines 134 and 59) at commit
479747fe82d64f66980797a50bd6782ea06f58fa (GPL-3.0). Kept: the ``<title>:<class>:<exe>`` split and
the priority table. Changed: the parts are decoded the way OBS escapes them (``#3A`` for ``:``,
``#22`` for ``#``, ``obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:8-12``) instead of
GSM's ``rsplit`` and ``strip``, since OBS never writes a raw colon inside a part; the result is a
frozen dataclass instead of a dict.

Values from M0 that R2 (the OBS behaviour spike) may still change are named constants below,
marked provisional; they come from source reading (``docs/m0/source-findings.md``) and R1.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from anki_miner_game import paths
from anki_miner_game.interfaces.obs import ObsGateway
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import OBS_PROFILE_NAME
from anki_miner_game.models.obs import ObsError, ProvisionResult

log = logging.getLogger(__name__)

_X11_SEP: Final = "\r\n"

VIDEO_SOURCE_PRIORITY: Final = {"game_capture": 0, "window_capture": 1, "monitor_capture": 2}
"""Lower is preferred; any other kind ranks 999 (GSM ``VIDEO_SOURCE_PRIORITY``)."""


@dataclass(frozen=True)
class WindowTarget:
    """A Windows window string (``game_capture``, ``window_capture``, ``wasapi_process_output_capture``), decoded."""

    title: str
    window_class: str
    exe: str


def _decode(part: str) -> str:
    # OBS's own order (libobs/util/windows/window-helpers.c): "#3A" first, then "#22".
    return part.replace("#3A", ":").replace("#22", "#")


def parse_obs_window_target(value: str) -> WindowTarget | None:
    """``<title>:<class>:<exe>`` with each part decoded; ``None`` for an X11 value or anything else."""
    if _X11_SEP in value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    title, window_class, exe = (_decode(part) for part in parts)
    return WindowTarget(title=title, window_class=window_class, exe=exe)


def window_class_exe(value: str) -> tuple[str, str] | None:
    """Class and exe of a Windows window string, casefolded as OBS's ``window_rating`` compares them."""
    target = parse_obs_window_target(value)
    if target is None:
        return None
    return target.window_class.casefold(), target.exe.casefold()


def get_video_source_priority(input_kind: str | None) -> int:
    return VIDEO_SOURCE_PRIORITY.get(input_kind or "", 999)


# Profile ---------------------------------------------------------------------------------------

CONTAINER: Final = "mkv"
"""An ``.mkv`` survives an OBS crash (spec 6.4)."""

CONTAINER_KEYS: Final = (("SimpleOutput", "RecFormat2"), ("AdvOut", "RecFormat2"))
"""Provisional until R2 reads a real ``basic.ini``: source-confirmed (source findings 1)."""

OFF_KEYS: Final = (("AdvOut", "RecSplitFile"), ("Video", "AutoRemux"))
"""Bool keys provisioning turns off: file splitting (Advanced mode only; Simple mode never splits)
and auto-remux, which would race finalise's rename with a second video (source findings 1 and 7,
summary item 15). Provisional until R2 reads a real ``basic.ini``."""

REBUILD_CONTAINERS: Final = frozenset({"hybrid_mp4", "hybrid_mov"})
"""Containers whose muxer OBS fixes when it builds the output handler: leaving one takes effect only
after the profile is re-activated or OBS restarts, so it sets ``needs_restart``. Every other row
applies at the next ``StartRecord`` (source findings section 2). Provisional until R2 checks the
file with ffprobe."""

PROFILE_SWITCH_TIMEOUT_S: Final = 15.0
"""How long to wait for OBS to switch to a profile ``CreateProfile`` made (spec 6.2's switch timeout)."""

PROFILE_POLL_S: Final = 0.2


def scaled_output_size(base_width: int, base_height: int, max_height: int) -> tuple[int, int]:
    """Output size for a base (canvas) size: height capped at ``max_height``, aspect kept.

    Aligned the way libobs aligns the output at video reset (width to 4, height to 2,
    ``obs-studio@ba2f32bd libobs/obs.c:1541-1543``), so ``GetVideoSettings`` reports back exactly
    this size; never below ``SetVideoSettings``' minimum of 8.
    """
    height = min(base_height, max_height)
    width = base_width if height == base_height else (base_width * height + base_height // 2) // base_height
    return max(width & ~3, 8), max(height & ~1, 8)


def _config_bool(value: str | None) -> bool:
    """``config_get_bool``: true for ``true`` or a non-zero integer (``libobs/util/config-file.c:682-689``)."""
    if value is None:
        return False
    text = value.strip()
    if text.lower() == "true":
        return True
    try:
        return int(text) != 0
    except ValueError:
        return False


class ObsProvisioner:
    """``Provisioner`` over an ``ObsGateway``: reads first and sends a change only where OBS differs."""

    def __init__(
        self,
        gateway: ObsGateway,
        *,
        platform: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``platform`` defaults to ``sys.platform``, read at call time."""
        self._gateway = gateway
        self._platform = platform
        self._sleep = sleep

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult:
        """Make the app's profile current (creating it when missing) and apply spec 11.3's profile rows.

        A switch here is not undone; arming has already switched (spec 6.2), and any other caller
        restores the user's profile itself.
        """
        changed = await self._use_profile()
        changed |= await self._ensure_record_directory(cfg)
        changed |= await self._ensure_video(cfg)
        needs_restart = False
        for section, key in CONTAINER_KEYS:
            current = await self._profile_parameter(section, key)
            if current != CONTAINER:
                await self._set_profile_parameter(section, key, CONTAINER)
                changed = True
                needs_restart |= current is None or current in REBUILD_CONTAINERS
        for section, key in OFF_KEYS:
            if _config_bool(await self._profile_parameter(section, key)):
                await self._set_profile_parameter(section, key, "false")
                changed = True
        return ProvisionResult(changed=changed, needs_restart=needs_restart)

    async def _request(self, name: str, **fields: Any) -> dict[str, Any]:
        return await self._gateway.request(name, **fields)

    async def _use_profile(self) -> bool:
        listing = await self._request("GetProfileList")
        if listing.get("currentProfileName") == OBS_PROFILE_NAME:
            return False
        if OBS_PROFILE_NAME in (listing.get("profiles") or []):
            await self._request("SetCurrentProfile", profileName=OBS_PROFILE_NAME)
            return True
        # CreateProfile answers before the profile exists; OBS then switches to it (source findings 8).
        await self._request("CreateProfile", profileName=OBS_PROFILE_NAME)
        polls = round(PROFILE_SWITCH_TIMEOUT_S / PROFILE_POLL_S)
        for attempt in range(polls + 1):
            if attempt:
                await self._sleep(PROFILE_POLL_S)
            listing = await self._request("GetProfileList")
            if listing.get("currentProfileName") == OBS_PROFILE_NAME:
                return True
        raise ObsError(f"OBS did not switch to the profile {OBS_PROFILE_NAME!r} within {PROFILE_SWITCH_TIMEOUT_S:g} s")

    async def _ensure_record_directory(self, cfg: AppConfig) -> bool:
        wanted = str(paths.incoming_dir(cfg))
        if (await self._request("GetRecordDirectory")).get("recordDirectory") == wanted:
            return False
        await self._request("SetRecordDirectory", recordDirectory=wanted)
        return True

    async def _ensure_video(self, cfg: AppConfig) -> bool:
        video = await self._request("GetVideoSettings")
        width, height = scaled_output_size(video["baseWidth"], video["baseHeight"], cfg.recording.max_height)
        pairs: dict[str, int] = {}
        if (video.get("outputWidth"), video.get("outputHeight")) != (width, height):
            pairs.update(outputWidth=width, outputHeight=height)
        if (video.get("fpsNumerator"), video.get("fpsDenominator")) != (cfg.recording.fps, 1):
            pairs.update(fpsNumerator=cfg.recording.fps, fpsDenominator=1)
        if not pairs:
            return False
        await self._request("SetVideoSettings", **pairs)
        return True

    async def _profile_parameter(self, section: str, key: str) -> str | None:
        data = await self._request("GetProfileParameter", parameterCategory=section, parameterName=key)
        value = data.get("parameterValue")
        if value is None:
            value = data.get("defaultParameterValue")
        return value if isinstance(value, str) else None

    async def _set_profile_parameter(self, section: str, key: str, value: str) -> None:
        await self._request("SetProfileParameter", parameterCategory=section, parameterName=key, parameterValue=value)
