"""A stand-in for ``uv tool install``, for tests; nothing is downloaded.

Run as ``<python> fake_uv.py tool install [--python X] [--overrides FILE] owocr[<extra>]==<version>``
(tests put a shebang wrapper in front). It writes what a real install leaves behind: the tool
environment ``$UV_TOOL_DIR/owocr/`` with a ``uv-receipt.toml`` shaped like the one uv 0.11.22 wrote
in the M0 R3 spike, and the entry point in ``$UV_TOOL_BIN_DIR``. The JSON object in
``FAKE_UV_PLAN`` changes that:

- ``"record"``: a path; first write ``{"argv", "env", "pid"}`` there, ``env`` holding every ``UV_*``
  variable and ``HOME``.
- ``"fail": true``: after creating part of the tool environment, print uv-like errors and exit 2.
- ``"hang": true``: after creating part of the tool environment, sleep until killed.
- ``"version"``: write this owocr version into the receipt instead of the requested one.
"""

import json
import os
import sys
import time
from pathlib import Path

RECEIPT = """[tool]
requirements = [{{ name = "owocr", extras = ["{extra}"], specifier = "=={version}" }}]
{overrides}python = "3.12"
entrypoints = [
    {{ name = "owocr", install-path = "{exe}", from = "owocr" }},
]
"""
OVERRIDES = """overrides = [{ name = "pygobject", marker = "sys_platform == 'never'" }]\n"""


def main() -> None:
    plan = json.loads(os.environ.get("FAKE_UV_PLAN", "{}"))
    args = sys.argv[1:]
    if "record" in plan:
        env = {name: value for name, value in os.environ.items() if name.startswith("UV_") or name == "HOME"}
        record = Path(plan["record"])
        tmp = record.with_suffix(".tmp")
        tmp.write_text(json.dumps({"argv": args, "env": env, "pid": os.getpid()}), encoding="utf-8")
        os.replace(tmp, record)
    assert args[:2] == ["tool", "install"], args
    tool_env = Path(os.environ["UV_TOOL_DIR"]) / "owocr"
    tool_env.mkdir(parents=True, exist_ok=True)
    (tool_env / "pyvenv.cfg").write_text("version_info = 3.12.13\n", encoding="utf-8")
    if plan.get("fail"):
        print("Resolved 43 packages in 11ms", flush=True)
        print("error: Failed to build `pygobject==3.58.0`", flush=True)
        print('  Dependency "cairo" not found', flush=True)
        sys.exit(2)
    if plan.get("hang"):
        time.sleep(600)
    name_extra, version = args[-1].split("==")
    extra = name_extra[name_extra.index("[") + 1 : -1]
    bin_dir = Path(os.environ["UV_TOOL_BIN_DIR"])
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / ("owocr.exe" if os.name == "nt" else "owocr")
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    receipt = RECEIPT.format(
        extra=extra,
        version=plan.get("version", version),
        overrides=OVERRIDES if "--overrides" in args else "",
        exe=exe.as_posix(),
    )
    (tool_env / "uv-receipt.toml").write_text(receipt, encoding="utf-8")
    print("Installed 43 packages in 34ms", flush=True)


if __name__ == "__main__":
    main()
