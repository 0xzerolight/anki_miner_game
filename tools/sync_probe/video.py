"""Per-frame mean luma of a recording, and the rising edges that mark each flash.

Times are integer milliseconds relative to the container start (``format.start_time``), the
origin players and Anki Miner's ffmpeg ``-ss`` both use. ``-copyts`` keeps ffmpeg from shifting
the timestamps itself, so the subtraction here is the only one.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

LUMA_FILTER = "scale=64:36:flags=area,signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=luma.txt"
# Below this spread between the darkest and brightest frame there is no flash to find.
MIN_CONTRAST = 8.0

_FRAME_LINE = re.compile(r"^frame:\s*\d+\s+pts:\s*\S+\s+pts_time:\s*(\S+)\s*$")
_YAVG_LINE = re.compile(r"^lavfi\.signalstats\.YAVG=(\S+)\s*$")


class VideoError(Exception):
    """The recording could not be probed or decoded."""


@dataclass(frozen=True)
class Flash:
    t_ms: int
    frame_index: int
    luma: float


@dataclass(frozen=True)
class Detection:
    flashes: list[Flash]
    starts_bright: bool
    threshold: float


def parse_luma_log(text: str) -> list[tuple[float, float]]:
    """Parse the ``metadata=mode=print`` output into (pts_time seconds, mean luma) pairs."""
    pairs: list[tuple[float, float]] = []
    pts_time: float | None = None
    for line in text.splitlines():
        if m := _FRAME_LINE.match(line):
            pts_time = float(m.group(1))
        elif m := _YAVG_LINE.match(line):
            if pts_time is None:
                raise VideoError(f"luma value without a frame line: {line!r}")
            pairs.append((pts_time, float(m.group(1))))
            pts_time = None
    return pairs


def container_start(path: Path) -> float | None:
    """``format.start_time`` in seconds, or None when the container does not report one."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "json", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VideoError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    start = json.loads(proc.stdout).get("format", {}).get("start_time")
    return None if start is None else float(start)


def frame_lumas(path: Path) -> list[tuple[int, float]]:
    """Decode the first video stream: (ms since container start, mean luma) per frame."""
    path = Path(path).resolve()
    start = container_start(path)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-copyts", "-i", str(path)]
        cmd += ["-map", "0:v:0", "-vf", LUMA_FILTER, "-f", "null", "-"]
        proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if proc.returncode != 0:
            raise VideoError(f"ffmpeg failed on {path}: {proc.stderr.strip()}")
        log = Path(tmp, "luma.txt")
        pairs = parse_luma_log(log.read_text(encoding="utf-8")) if log.exists() else []
    if not pairs:
        raise VideoError(f"no video frames decoded from {path}")
    origin = pairs[0][0] if start is None else start
    return [(round((t - origin) * 1000), luma) for t, luma in pairs]


def auto_threshold(frames: list[tuple[int, float]]) -> float:
    """Midway between the darkest and the brightest frame.

    The flasher window usually covers only part of the OBS canvas, so its white frames lift the
    mean luma by an amount that depends on the scene layout; a fixed threshold would miss them.
    """
    lumas = [luma for _, luma in frames]
    low, high = min(lumas), max(lumas)
    if high - low < MIN_CONTRAST:
        raise VideoError(f"no flash contrast in the recording (luma {low:g}..{high:g})")
    return (low + high) / 2


def detect_flashes(frames: list[tuple[int, float]], threshold: float | None = None) -> Detection:
    """A flash starts at a frame with luma >= threshold whose previous frame was below it.

    ``threshold`` defaults to ``auto_threshold(frames)``.
    """
    if threshold is None:
        threshold = auto_threshold(frames)
    flashes: list[Flash] = []
    starts_bright = bool(frames) and frames[0][1] >= threshold
    for i in range(1, len(frames)):
        t_ms, luma = frames[i]
        if luma >= threshold and frames[i - 1][1] < threshold:
            flashes.append(Flash(t_ms, i, luma))
    return Detection(flashes, starts_bright, threshold)
