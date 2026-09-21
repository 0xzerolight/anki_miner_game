"""A stand-in for the owocr executable, for tests. The real owocr is never launched by the suite.

Run as ``<python> fake_owocr.py <owocr args>``. The JSON object in ``FAKE_OWOCR_PLAN`` says what it
does, in this order:

- ``"record"``: a path; the fake first writes ``{"argv", "env", "pid", "grandchild"}`` there as JSON,
  ``env`` holding ``HOME``, ``USERPROFILE`` and ``XAUTHORITY`` as it saw them.
- ``"grandchild": true``: start a child that ignores SIGTERM and sleeps, the way ``multiprocessing``'s
  resource tracker behaves in owocr's picker; the fake goes on once it does. It inherits the fake's
  stderr, so it keeps the log pipe open after the fake itself has gone.
- ``"frames"``: texts sent to every client of a websocket server on ``127.0.0.1:<-wp>``, each client
  getting all of them on connect; the connection then stays open.
- ``"log"``: lines written to stderr; ``"log_file"``: a file whose lines follow them.
- ``"exit"``: exit with this code (absent: run until killed).
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

GRANDCHILD = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(600)\n"
)


def _port(argv: list[str]) -> int:
    return int(argv[argv.index("-wp") + 1])


def _serve(port: int, frames: list[str]) -> None:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import ServerConnection, serve

    def handler(ws: ServerConnection) -> None:
        for frame in frames:
            ws.send(frame)
        try:
            for _ in ws:
                pass
        except ConnectionClosed:
            pass

    server = serve(handler, "127.0.0.1", port)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    plan = json.loads(os.environ.get("FAKE_OWOCR_PLAN", "{}"))
    argv = sys.argv[1:]
    grandchild = None
    if plan.get("grandchild"):
        grandchild = subprocess.Popen([sys.executable, "-c", GRANDCHILD], stdout=subprocess.PIPE)
        assert grandchild.stdout is not None
        grandchild.stdout.readline()  # SIGTERM is ignored from here on
    if "record" in plan:
        record = Path(plan["record"])
        env = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE", "XAUTHORITY")}
        data = {"argv": argv, "env": env, "pid": os.getpid(), "grandchild": grandchild and grandchild.pid}
        tmp = record.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, record)
    if "frames" in plan:
        _serve(_port(argv), plan["frames"])
    lines = list(plan.get("log", []))
    if "log_file" in plan:
        lines += Path(plan["log_file"]).read_text(encoding="utf-8").splitlines()
    for line in lines:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    if "exit" in plan:
        sys.exit(plan["exit"])
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
