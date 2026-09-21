"""Synthetic recordings for the sync-probe tests, made with ffmpeg's lavfi ``color`` source."""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not on PATH"
)


def make_flash_video(
    path: Path,
    flash_starts_s: Sequence[float],
    *,
    fps: int = 30,
    frames: int = 3,
    duration_s: float = 6,
    offset_s: float | None = None,
    size: str = "160x90",
    box: str = "x=0:y=0:w=iw:h=ih",
) -> Path:
    """Black H.264 (with B-frames) whose ``box`` turns white for ``frames`` frames at each start.

    The enable window runs from half a frame before the start to half a frame before the frame
    after the last white one, so float rounding of ``t`` cannot shift a flash by a frame.
    """
    half = 0.5 / fps
    enable = "+".join(f"between(t,{s - half:.6f},{s + (frames - 0.5) / fps:.6f})" for s in flash_starts_s)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi"]
    cmd += ["-i", f"color=c=black:s={size}:r={fps}:d={duration_s}"]
    cmd += ["-vf", f"drawbox={box}:t=fill:c=white:enable='{enable}'"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-bf", "2"]
    if offset_s is not None:
        cmd += ["-output_ts_offset", str(offset_s)]
    subprocess.run([*cmd, str(path)], check=True)
    return path
