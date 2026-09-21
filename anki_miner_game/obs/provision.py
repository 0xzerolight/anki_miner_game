"""OBS provisioning: the app's profile, scene collection, capture inputs and window list (spec 11.3).

``parse_obs_window_target`` and ``get_video_source_priority`` are ported from GameSentenceMiner
``GameSentenceMiner/obs/launch.py`` (lines 134 and 59) at commit
479747fe82d64f66980797a50bd6782ea06f58fa (GPL-3.0). Kept: the ``<title>:<class>:<exe>`` split and
the priority table. Changed: the parts are decoded the way OBS escapes them (``#3A`` for ``:``,
``#22`` for ``#``, ``obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:8-12``) instead of
GSM's ``rsplit`` and ``strip``, since OBS never writes a raw colon inside a part; the result is a
frozen dataclass instead of a dict.
"""

from dataclasses import dataclass
from typing import Final

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
