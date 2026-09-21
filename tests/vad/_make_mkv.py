"""Writes a small Matroska recording for the VAD worker tests; run by the add-on's Python.

    python _make_mkv.py <speech clip> <out.mkv> <audio start seconds>

Streams in file order: a video track, audio track 0 (digital silence), audio track 1 (the speech
clip). Both audio tracks start ``audio start`` seconds after the video, as a track with a late
first packet does in a real recording.
"""

import sys
from fractions import Fraction

import av
import numpy as np

RATE = 16000
FPS = 10


def _speech(path: str) -> np.ndarray:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    parts = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            parts.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
    parts.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    return np.concatenate(parts)


def _write_audio(container: av.container.OutputContainer, stream, samples: np.ndarray, first_pts: int) -> None:
    for pts in range(0, len(samples), 1024):
        frame = av.AudioFrame.from_ndarray(samples[None, pts : pts + 1024], format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.time_base = Fraction(1, RATE)
        frame.pts = first_pts + pts
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)


def main(clip: str, out: str, audio_start_s: float) -> None:
    speech = _speech(clip)
    silence = np.zeros_like(speech)
    first_pts = round(audio_start_s * RATE)
    seconds = audio_start_s + len(speech) / RATE
    with av.open(out, mode="w", format="matroska") as container:
        video = container.add_stream("ffv1", rate=FPS)
        video.width = video.height = 32
        video.pix_fmt = "yuv420p"
        tracks = [container.add_stream("pcm_s16le", rate=RATE, layout="mono") for _ in range(2)]
        for i in range(int(seconds * FPS) + 1):
            image = np.full((32, 32, 3), (i * 8) % 256, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts = i
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
        _write_audio(container, tracks[0], silence, first_pts)
        _write_audio(container, tracks[1], speech, first_pts)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]))
