# anki_miner_game.spec - PyInstaller one-folder build of Anki Miner Game (spec 19).
#
# Build from the repo root with the build env (runtime dependencies plus the `build` extra):
#   UV_PROJECT_ENVIRONMENT=.venv-build uv sync --python 3.12 --no-install-project --extra build
#   .venv-build/bin/pyinstaller --noconfirm anki_miner_game.spec
# The bundle is dist/AnkiMinerGame/: the AnkiMinerGame executable (AnkiMinerGame.exe on Windows)
# beside _internal/. scripts/bundle_smoke.sh dist/AnkiMinerGame checks it.
#
# PyInstaller runs this file with Analysis, PYZ, EXE, COLLECT and SPECPATH defined.

import os

ROOT = SPECPATH  # noqa: F821 - defined by PyInstaller

# Files the code opens beside its own modules: feed/http_server.py serves page.html, the VAD add-on
# builds its environment from requirements.txt and runs vad_worker.py with that environment's Python
# (the app never imports it). A frozen module's __file__ points into the bundle as if the module sat
# there, so each file goes to the folder its module looks in.
DATA_FILES = [
    "anki_miner_game/feed/page.html",
    "anki_miner_game/vad/worker/requirements.txt",
    "anki_miner_game/vad/worker/vad_worker.py",
]

# The frozen app carries PyQt6, obsws-python and websockets and nothing else (spec 4.3). The VAD
# and OCR add-ons install these into environments of their own; excluding them makes their absence
# a guarantee, and scripts/bundle_smoke.sh asserts it (runtime/bundle_smoke.py ABSENT_MODULES).
EXCLUDES = ["onnxruntime", "numpy", "av", "owocr"]

# obsws-python imports tomli only where the stdlib has no tomllib (Python < 3.11), but PyInstaller
# maps the missing tomli to setuptools' vendored copy and would bundle all of setuptools with it.
EXCLUDES += ["setuptools"]

NAME = "AnkiMinerGame"

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "anki_miner_game", "launch.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, path), os.path.dirname(path)) for path in DATA_FILES],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # one folder: the libraries go beside the executable (COLLECT)
    name=NAME,
    debug=False,
    strip=False,
    upx=False,  # a packed executable is a common antivirus false positive
    console=False,  # a window app on Windows; no effect on Linux
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
