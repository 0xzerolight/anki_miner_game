"""Stands in for ``vad/worker/vad_worker.py`` in tests (spec 13.2 protocol); stdlib only.

Run as the real worker is, ``<python> fake_vad_worker.py --video <path> --model <path>``. It reads
its script from ``<video>.fake.json``::

    {"lines": ["{\\"t\\": \\"done\\"}"],   raw stdout lines, written and flushed in order
     "wait_for": "<path>",                optional: after the lines, wait until this file exists
     "stderr": "text",                    optional: written to stderr before exiting
     "exit": 0}                           exit code

and records how it was started in ``<video>.argv.json``: its arguments and whether the interpreter
ran isolated (``-I``). Once that file is closed it creates an empty ``<video>.started``: a test polls
that marker, never the argv file, because Windows refuses to open a file another process is still
writing or renaming.
"""

import json
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    video = Path(args[args.index("--video") + 1])
    Path(f"{video}.argv.json").write_text(json.dumps({"argv": args, "isolated": sys.flags.isolated}))
    Path(f"{video}.started").touch()  # only now: a test polling for it never reads half a file
    script = json.loads(Path(f"{video}.fake.json").read_text(encoding="utf-8"))
    for line in script.get("lines", []):
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    wait_for = script.get("wait_for")
    if wait_for:
        deadline = time.monotonic() + 30
        while not Path(wait_for).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    sys.stderr.buffer.write(script.get("stderr", "").encode("utf-8"))  # the scripted bytes, on Windows too
    sys.stderr.flush()
    return int(script.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
