"""Text source for a hooker's websocket server (spec 3.2, 8.1).

Ported from GameSentenceMiner ``GameSentenceMiner/gametext.py::listen_on_websocket`` (line 629) at
commit 479747fe82d64f66980797a50bd6782ea06f58fa (GPL-3.0). Kept: connect to ``ws://<uri>``, the
LunaTranslator path fallback, ``ping_interval=None``, plain/JSON frame parsing and reconnecting.
Changed: the non-dict JSON guard (GSM calls ``.get`` on any JSON value), the spec's 1, 2, 5, 10 s
backoff, and the JSON ``source`` and ``time`` fields are not used: a line belongs to the configured
source and its time is read here at receipt. Dropped: rate limiting, overlay, database and pause
plumbing.
"""

import json


def parse_frame(frame: str | bytes) -> str:
    """The line a hooker frame carries.

    A JSON object's ``sentence`` string is the line; any other frame (plain text, invalid JSON, a
    JSON value that is not an object, an object without a string ``sentence``) is the line itself.
    Binary frames are decoded as UTF-8.
    """
    text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text
    if isinstance(data, dict) and isinstance(data.get("sentence"), str):
        return str(data["sentence"])
    return text
