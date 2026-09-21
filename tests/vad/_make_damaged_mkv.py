"""Writes an AAC Matroska recording and a copy with a damaged span; run by the add-on's Python.

    python _make_damaged_mkv.py <speech clip> <clean.mkv> <damaged.mkv>

One audio track: the speech clip, 5 s of digital silence, the clip again. The copy has 400 bytes
zeroed from the block that starts at 2.5 s, inside the first line: the demuxer loses everything up
to its next cluster, as it would over a bad sector, and the second line's packets keep their times.
"""

import sys
from fractions import Fraction

import av
import numpy as np
from _make_mkv import RATE, _speech

AAC_FRAME = 1024
DAMAGE_AT_S = 2.5
DAMAGE_BYTES = 400


def main(clip: str, clean: str, damaged: str) -> None:
    speech = _speech(clip)
    samples = np.concatenate([speech, np.zeros(5 * RATE, dtype=np.int16), speech])
    with av.open(clean, mode="w", format="matroska") as container:
        stream = container.add_stream("aac", rate=RATE, layout="mono")
        for pts in range(0, len(samples), AAC_FRAME):
            frame = av.AudioFrame.from_ndarray(samples[None, pts : pts + AAC_FRAME], format="s16", layout="mono")
            frame.sample_rate = RATE
            frame.time_base = Fraction(1, RATE)
            frame.pts = pts
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    with av.open(clean) as container:
        at = next(
            packet.pos
            for packet in container.demux(audio=0)
            if packet.pts is not None and packet.pts * packet.time_base >= DAMAGE_AT_S
        )
    with open(clean, "rb") as source:
        data = bytearray(source.read())
    data[at : at + DAMAGE_BYTES] = bytes(DAMAGE_BYTES)
    with open(damaged, "wb") as out:
        out.write(data)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
