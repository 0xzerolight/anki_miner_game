"""The Silero VAD model the VAD add-on downloads (spec 13.1).

The file is faster-whisper's own ``silero_vad_v6.onnx`` asset, fetched from the commit tagged
v1.2.1, the same revision ``vad/worker/vad_worker.py`` is ported from. A commit URL cannot move;
the sha256 is the file's digest at that commit, and the add-on refuses any other bytes.
"""

from typing import Final

MODEL_FILENAME: Final = "silero_vad_v6.onnx"
MODEL_SOURCE_COMMIT: Final = "65882eee9f5cdbeeb2d877f1131d48cf241b327d"  # SYSTRAN/faster-whisper tag v1.2.1
MODEL_URL: Final = (
    f"https://raw.githubusercontent.com/SYSTRAN/faster-whisper/{MODEL_SOURCE_COMMIT}"
    f"/faster_whisper/assets/{MODEL_FILENAME}"
)
MODEL_SHA256: Final = "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"
MODEL_SIZE_BYTES: Final = 1_245_151
