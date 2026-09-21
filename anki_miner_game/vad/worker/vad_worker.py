# Ported from SYSTRAN/faster-whisper, commit 65882eee9f5cdbeeb2d877f1131d48cf241b327d (tag v1.2.1):
#   faster_whisper/vad.py    VadOptions, get_speech_timestamps, SileroVADModel
#   faster_whisper/audio.py  decode_audio, _ignore_invalid_frames
# Changed for this app: the audio is streamed through the model and the segmenter instead of being
# decoded into one array, so a region is emitted as soon as it closes and memory stays flat over a
# long session. The max_speech_duration_s split is left out: the app never limits speech length.
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

A standalone script, run by the VAD add-on's own Python, never imported by the app at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

SAMPLING_RATE = 16000
WINDOW_SAMPLES = 512


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
