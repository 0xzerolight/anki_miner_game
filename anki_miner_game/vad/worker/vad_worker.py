# Ported from SYSTRAN/faster-whisper, commit 65882eee9f5cdbeeb2d877f1131d48cf241b327d (tag v1.2.1):
#   faster_whisper/vad.py    VadOptions, get_speech_timestamps, SileroVADModel
#   faster_whisper/audio.py  decode_audio, _ignore_invalid_frames
# Changed for this app: the audio is streamed through the model and the segmenter instead of being
# decoded into one array, so a region is emitted as soon as it closes and memory stays flat over a
# long session. A packet the decoder rejects is skipped instead of ending the decode, and audio the
# demuxer or decoder lost is replaced by silence up to the next frame's time, so later regions keep
# their place on the file's timeline. The max_speech_duration_s split is left out: the app never
# limits speech length.
#
# MIT License. Copyright (c) 2023 SYSTRAN.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
# associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
# NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
# OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""Silero VAD over a recording's audio track (spec 13.2).

A standalone script, run by the VAD add-on's own Python, never imported by the app at runtime::

    <addon python> vad_worker.py --video <path> --model <onnx> [--track 0]

``--track`` counts audio tracks only. stdout carries one ASCII JSON object per line, in file
milliseconds::

    {"t": "region", "start_ms": 5310, "end_ms": 8920}       as each region closes, in order
    {"t": "progress", "done_ms": 600000, "total_ms": 5248120}  after each model batch
    {"t": "done"}                                            last line, exit code 0
    {"t": "error", "message": "..."}                         last line instead, exit code 1

``total_ms`` is null when the file does not state its duration (a recording cut off by a crash).
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

try:
    import av
    import numpy as np
    import onnxruntime
except ImportError as exc:  # an incomplete add-on environment; main() reports it on the protocol
    _MISSING_DEPENDENCY: ImportError | None = exc
else:
    _MISSING_DEPENDENCY = None

SAMPLING_RATE = 16000
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
BATCH_WINDOWS = 1024  # 32.8 s of audio per model call; the model's cost per window does not depend on it


@dataclass(frozen=True)
class VadOptions:
    """faster-whisper's VadOptions with the spec 13.2 parameters as defaults.

    ``neg_threshold`` None means ``max(threshold - 0.15, 0.01)``: below it a window is always
    silence; between it and ``threshold`` a window continues speech but never starts it.
    """

    threshold: float = 0.5
    neg_threshold: float | None = None
    min_speech_duration_ms: int = 250
    min_silence_duration_ms: int = 300
    speech_pad_ms: int = 100


class SpeechSegmenter:
    """get_speech_timestamps, one window probability at a time.

    ``push`` takes the probability of the next 512-sample window and returns the regions it
    finalised, as ``(start, end)`` sample offsets with padding applied; ``finish`` takes the true
    audio length and returns the rest. The regions equal the reference's for the same
    probabilities. A region's end padding depends on where the next kept region starts, so a
    closed region is held back only until that start is known to be at least twice the pad away,
    which with the spec parameters is the window that closes it.
    """

    def __init__(self, options: VadOptions | None = None, sampling_rate: int = SAMPLING_RATE) -> None:
        options = options or VadOptions()
        self._threshold = options.threshold
        if options.neg_threshold is None:
            self._neg_threshold = max(options.threshold - 0.15, 0.01)
        else:
            self._neg_threshold = options.neg_threshold
        self._min_speech_samples = sampling_rate * options.min_speech_duration_ms / 1000
        self._min_silence_samples = sampling_rate * options.min_silence_duration_ms / 1000
        self._pad_samples = sampling_rate * options.speech_pad_ms / 1000
        self._window_index = 0
        self._triggered = False
        self._start = 0
        self._temp_end = 0  # 0 means unset, as in the reference
        self._pending: tuple[int, int] | None = None  # closed region: (padded start, raw end)

    def push(self, prob: float) -> list[tuple[int, int]]:
        position = WINDOW_SAMPLES * self._window_index
        self._window_index += 1
        out: list[tuple[int, int]] = []
        if prob >= self._threshold and self._temp_end:
            self._temp_end = 0
        if prob >= self._threshold and not self._triggered:
            self._triggered = True
            self._start = position
        elif prob < self._neg_threshold and self._triggered:
            if not self._temp_end:
                self._temp_end = position
            if position - self._temp_end >= self._min_silence_samples:
                if self._temp_end - self._start > self._min_speech_samples:
                    out.extend(self._close(self._start, self._temp_end))
                self._triggered = False
                self._temp_end = 0
        # The next kept region starts no earlier than the current speech, or than this window.
        # Both are <= the audio length, so the reference's end clamp cannot bite here.
        next_start_bound = self._start if self._triggered else position
        if self._pending is not None and next_start_bound - self._pending[1] >= 2 * self._pad_samples:
            start, end = self._pending
            out.append((start, int(end + self._pad_samples)))
            self._pending = None
        return out

    def finish(self, audio_length_samples: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        if self._triggered and audio_length_samples - self._start > self._min_speech_samples:
            out.extend(self._close(self._start, audio_length_samples))
        self._triggered = False
        if self._pending is not None:
            start, end = self._pending
            out.append((start, int(min(audio_length_samples, end + self._pad_samples))))
            self._pending = None
        return out

    def _close(self, start: int, end: int) -> list[tuple[int, int]]:
        """Keep a region; settle the previous one's end against this one's start."""
        out: list[tuple[int, int]] = []
        shift = self._pad_samples
        if self._pending is not None:
            prev_start, prev_end = self._pending
            silence = start - prev_end
            if silence < 2 * self._pad_samples:
                out.append((prev_start, prev_end + int(silence // 2)))
                shift = silence // 2
            else:
                out.append((prev_start, int(prev_end + self._pad_samples)))
        self._pending = (int(max(0, start - shift)), end)
        return out


def region_ms(start_samples: int, end_samples: int, offset_ms: int) -> tuple[int, int]:
    """Sample offsets to file milliseconds: the start rounds down and the end up, never shrinking."""
    start_ms = start_samples * 1000 // SAMPLING_RATE
    end_ms = -(-end_samples * 1000 // SAMPLING_RATE)
    return start_ms + offset_ms, end_ms + offset_ms


class SileroVADModel:
    """The Silero model, with its LSTM state and 64-sample context carried from call to call.

    faster-whisper runs the whole track as consecutive batches that share this state; carrying it
    across calls gives the same probabilities one batch at a time.
    """

    def __init__(self, path: str) -> None:
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def __call__(self, windows: Any) -> Any:
        """Speech probability of each row of a ``(n, 512)`` float32 array."""
        context = np.empty((len(windows), CONTEXT_SAMPLES), dtype=np.float32)
        context[0] = self._context
        context[1:] = windows[:-1, -CONTEXT_SAMPLES:]
        batch = np.concatenate([context, windows], axis=1)
        probs, self._h, self._c = self._session.run(None, {"input": batch, "h": self._h, "c": self._c})
        self._context = windows[-1, -CONTEXT_SAMPLES:].copy()
        return probs


def _decoded_frames(container: Any, stream: Any) -> Iterator[Any]:
    """Every frame of the track; a packet the decoder rejects as invalid data is skipped."""
    for packet in container.demux(stream):
        try:
            frames = packet.decode()
        except av.InvalidDataError:
            continue
        yield from frames


def _duration_ms(container: Any, stream: Any) -> int | None:
    if container.duration is not None:
        return int(container.duration * 1000 // av.time_base)
    if stream.duration is not None and stream.time_base is not None:
        return int(stream.duration * stream.time_base * 1000)
    return None


def run(video: str, model_path: str, track: int, emit: Callable[[dict[str, Any]], None]) -> None:
    """Emit every speech region of one audio track, then return; raises on any failure."""
    model = SileroVADModel(model_path)
    segmenter = SpeechSegmenter()
    with av.open(video, mode="r", metadata_errors="ignore") as container:
        audio_streams = container.streams.audio
        if not 0 <= track < len(audio_streams):
            raise ValueError(f"no audio track {track}: the file has {len(audio_streams)} audio track(s)")
        stream = audio_streams[track]
        total_ms = _duration_ms(container, stream)

        frames = _decoded_frames(container, stream)
        first = next(frames, None)
        # Regions are on the file's timeline: a track that starts after the file does shifts them.
        offset_ms = 0
        if first is not None and first.time is not None:
            file_start_s = container.start_time / av.time_base if container.start_time is not None else 0.0
            offset_ms = max(0, round((first.time - file_start_s) * 1000))

        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLING_RATE)
        chunks: list[Any] = []
        buffered = 0
        audio_length = 0
        windows_done = 0

        def process(samples: Any) -> None:
            nonlocal windows_done
            for prob in model(samples.reshape(-1, WINDOW_SAMPLES)).tolist():
                for start, end in segmenter.push(prob):
                    start_ms, end_ms = region_ms(start, end, offset_ms)
                    emit({"t": "region", "start_ms": start_ms, "end_ms": end_ms})
            windows_done += len(samples) // WINDOW_SAMPLES
            done = min(windows_done * WINDOW_SAMPLES, audio_length)
            emit({"t": "progress", "done_ms": offset_ms + done * 1000 // SAMPLING_RATE, "total_ms": total_ms})

        def append(samples: Any) -> None:
            nonlocal chunks, buffered, audio_length
            chunks.append(samples)
            buffered += len(samples)
            audio_length += len(samples)
            if buffered >= BATCH_WINDOWS * WINDOW_SAMPLES:
                data = np.concatenate(chunks)
                whole = len(data) - len(data) % WINDOW_SAMPLES
                process(data[:whole])
                chunks, buffered = [data[whole:]], len(data) - whole

        def resample(frame: Any) -> None:
            for resampled in resampler.resample(frame):
                append(resampled.to_ndarray().reshape(-1).astype(np.float32) / 32768.0)

        # A span the demuxer or decoder lost (a bad packet, a resync to the next cluster) would pull
        # every later sample early, so it is filled with silence up to the next frame's own time.
        # Smaller gaps add up until the drift passes one window.
        track_start_s = first.time if first is not None else None
        expected_s = track_start_s
        for frame in itertools.chain([first] if first is not None else [], frames):
            if expected_s is not None:
                if frame.time is not None and frame.time - expected_s > WINDOW_SAMPLES / SAMPLING_RATE:
                    resample(None)  # flush, so the samples before the gap stay before it
                    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLING_RATE)
                    silence = round((frame.time - track_start_s) * SAMPLING_RATE) - audio_length
                    while silence > 0:  # a batch at a time, so a long gap cannot grow memory
                        size = min(silence, BATCH_WINDOWS * WINDOW_SAMPLES)
                        append(np.zeros(size, dtype=np.float32))
                        silence -= size
                    expected_s = frame.time
                expected_s += frame.samples / frame.sample_rate
            frame.pts = None  # as faster-whisper: timestamps play no part once decoded
            resample(frame)
        resample(None)

        # faster-whisper pads the track with 512 - len % 512 zeros, a whole window when it divides.
        data = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        process(np.pad(data, (0, WINDOW_SAMPLES - len(data) % WINDOW_SAMPLES)))
        for start, end in segmenter.finish(audio_length):
            start_ms, end_ms = region_ms(start, end, offset_ms)
            emit({"t": "region", "start_ms": start_ms, "end_ms": end_ms})


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(message).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Silero VAD over a recording's audio track, as JSON lines.")
    parser.add_argument("--video", required=True, help="the recording")
    parser.add_argument("--model", required=True, help="path to silero_vad_v6.onnx")
    parser.add_argument("--track", type=int, default=0, help="index among the file's audio tracks (default 0)")
    args = parser.parse_args(argv)
    try:
        if _MISSING_DEPENDENCY is not None:
            raise RuntimeError(f"the VAD add-on environment is incomplete: {_MISSING_DEPENDENCY}")
        run(args.video, args.model, args.track, _emit)
    except Exception as exc:  # the protocol's single failure report; the session row shows it (spec 17)
        _emit({"t": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    _emit({"t": "done"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
