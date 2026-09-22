# Contributing to Anki Miner Game

Thanks for helping out. Anki Miner Game is a solo-maintained companion to [Anki Miner](https://github.com/0xzerolight/anki_miner): it records a game session through OBS and writes the video/subtitle pair Anki Miner mines. Contributions of any size are welcome - bug reports, fixes, support for another text hooker, GUI polish, doc improvements.

## Before you start

- Bugs and feature requests: open an [Issue](https://github.com/0xzerolight/anki_miner_game/issues) using the appropriate template.
- General questions and chat: use [Discord](https://discord.com/invite/aDtQyZzUVP).
- Security vulnerabilities: see [SECURITY.md](SECURITY.md). Do not open a public issue.
- Code of Conduct: see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Development setup

```bash
git clone https://github.com/0xzerolight/anki_miner_game.git
cd anki_miner_game

uv sync --extra dev --no-install-project
uvx pre-commit install
```

The project is managed with [uv](https://docs.astral.sh/uv/). `--no-install-project` matches CI: the package is never installed, and `pytest` and `mypy` find it from the repository root. Run the app from the checkout with:

```bash
.venv/bin/python -m anki_miner_game.launch
```

Anki Miner Game requires Python 3.12 or newer; CI runs the suite on 3.12 and 3.13 on Linux and Windows, with lint and type checks on 3.12.

External dependencies:

- OBS Studio 30.0 or newer, only for manual testing and the `obs_live` tests. The rest of the suite talks to a fake OBS.
- Headless Linux (and CI) needs the Qt runtime libs `libegl1 libpulse0 libxkbcommon0` for any test that imports a PyQt6 widget (`sudo apt-get install -y libegl1 libpulse0 libxkbcommon0`).
- The `vad` tests need the VAD add-on environment at the repository root, which the main `.venv` never holds. Build it with `uv venv --python 3.12 .venv-vad` and `uv pip install --python .venv-vad/bin/python -r anki_miner_game/vad/worker/requirements.txt`, and save the model named in `anki_miner_game/vad/model_pin.py` into it. Without it those tests skip.

## Workflow

1. Fork the repo and create a branch from `main`. Branch names like `feat/...`, `fix/...`, or `docs/...` are appreciated but not required.
2. Keep PRs focused - one feature or fix per PR.
3. Style (`black` + `ruff`) is auto-fixed on your PR by [pre-commit.ci](https://pre-commit.ci). Installing the local hook (`uvx pre-commit install`) gives faster feedback but is not required.
4. Run `scripts/health.sh`. See [Tests](#tests).
5. Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
6. Open the PR against `main`. The PR template will populate automatically.

## Code style

- **black** with 120-character line length.
- **ruff** for linting; `ruff check . --fix` for autofixes.
- **mypy** must pass on the `anki_miner_game/` package. `models/` and `interfaces/` are checked strictly.
- Conventional Commits are preferred (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`). Not enforced - the maintainer may normalise commit messages on merge.

`scripts/health.sh` runs the full local gate in one command: black, ruff, mypy, then pytest, all from `./.venv/bin/`. It runs every step even after a failure, prints `PASS`/`FAIL` per step and a summary, and exits nonzero if any step failed.

## Tests

Unit tests mirror the package: `tests/session/` tests `anki_miner_game/session/`, and so on. Alongside them:

- `tests/fakes/` - a fake obs-websocket server (live, or replaying a session recorded from a real OBS), a fake text hooker, a fake owocr, a fake `uv` and a fake VAD worker.
- `tests/integration/` - scripted sessions: the real composition against a replayed OBS and a hooker.
- `tests/contract/` - the session naming checked against Anki Miner's own episode-number matcher, vendored.
- `tests/fixtures/` - OBS transcripts and provisioning captures, owocr logs, VAD audio.

Shared fixtures go in `tests/conftest.py`.

```bash
# What CI runs, and what to run before pushing
.venv/bin/pytest

# Single file
.venv/bin/pytest tests/session/test_cues.py
```

Bare `pytest` **is** the gate. `pyproject.toml` sets `addopts` to `-n auto --dist loadfile --max-worker-restart=0` plus the marker deselect, and a `-m` on the command line *replaces* that marker expression instead of adding to it - so `pytest -m vad` also drops the `not obs_live` and `not network` exclusions. Spell out the full expression when you need one. The run is parallel (pytest-xdist, one file per worker); pass `-n0` to force it serial.

### Markers

| Marker | Use |
|---|---|
| `vad` | Needs the VAD add-on environment (onnxruntime, numpy, PyAV). Deselected by default. |
| `obs_live` | Needs a real, reachable OBS instance. Deselected by default. |
| `network` | Genuinely needs the network beyond loopback; suppresses the socket tripwire in `tests/_network_tripwire.py`. Deselected by default. |
| `windows_only` | Exercises Windows-only behaviour (hotkey registration, job objects). Deselected by default; the Windows CI leg runs it. |
| `e2e` | End-to-end across real components over loopback. Runs in the gate. |

Register new markers in `[tool.pytest.ini_options].markers` in `pyproject.toml`.

### Headless Qt

Any test importing a PyQt6 widget needs the offscreen platform plugin (`QT_QPA_PLATFORM=offscreen`). `tests/conftest.py` sets it, and so does CI. Widget tests take pytest-qt's `qtbot` fixture and call `qtbot.addWidget()` on every top-level widget they build, so teardown stays deterministic. Use pytest-qt's `qapp`; never construct a `QApplication`.

### Isolation

- Every test runs with its own `ANKI_MINER_GAME_HOME`, `HOME`/`USERPROFILE` and platform config/data dirs (autouse in `tests/conftest.py`), so `~` never reaches your real home.
- Tests reach no network beyond loopback: the tripwire fails any test that tries.
- A test that opens a real socket binds port `0`, never a default port such as 4455 (OBS), 6677/9001/2333 (hookers) or 6678/6679 (the feed), so parallel runs cannot collide.
- A test that needs `numpy`, `onnxruntime` or `av` calls `pytest.importorskip` for it: those live only in `.venv-vad`.

New code should add tests where reasonable; refactors should not regress existing coverage by a meaningful amount.

## Changelog

Add an entry under `## [Unreleased]` in `CHANGELOG.md` using the [Keep a Changelog](https://keepachangelog.com/) sections (Added / Changed / Fixed / Removed). Entries explain *what* changed and *why it matters to a user*, not just the implementation detail.

## Architecture

The session flow and package layout are documented in [ARCHITECTURE.md](ARCHITECTURE.md). Worth a skim before any contribution larger than a one-file change.
