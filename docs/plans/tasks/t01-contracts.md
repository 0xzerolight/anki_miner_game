# T01 contracts: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (one implementer, tasks in
> the order below; later tasks import earlier modules, so do not split them across agents) with
> superpowers:test-driven-development inside every task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Codify master-plan section 4 as importable, strictly typed code: the frozen models in
`anki_miner_game/models/`, the Protocols in `anki_miner_game/interfaces/`, `paths.py` and
`store.py`, so about thirty parallel tasks can code against fixed names.

**Architecture:** Models are frozen dataclasses and `StrEnum`s with no I/O and no app imports
outside `models`. One pure codec (`models/codec.py`) converts any model to and from JSON, so
`store.py` (config and game profiles) and T06's manifest I/O share one decoder and one error type.
Protocols are `typing.Protocol` classes, one module per family, importing only from `models`.
`paths.home()` reads `ANKI_MINER_GAME_HOME` at every call; `store.py` writes through a temporary
file and `os.replace`.

**Tech Stack:** Python 3.12 (`StrEnum`, PEP 695 generics, `typing.Self`), stdlib `dataclasses`,
`json`, `tempfile`; dev: pytest, black, ruff, mypy 2.3.1.

**Spec:** `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/docs/specs/2026-09-20-anki-miner-game-design.md`
(sections 3.3, 5, 6.1, 7, 8.1, 8.2, 10.2, 11.1-11.4, 12, 13, 16, 17) and the master plan
`/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/docs/plans/2026-09-21-master-plan.md`
(sections 1-4 and the card `### T01 contracts`).

## Global Constraints

Verbatim from the master plan; every task below inherits them.

- Standalone app, own repository. **No Anki Miner changes, ever.** Contract = files on disk.
- Windows first-class, Linux best-effort, **macOS unsupported**.
- Python 3.12, PyQt6, PyInstaller. Licence **GPL-3.0-only**.
- Frozen runtime deps: `PyQt6`, `obsws-python`, `websockets`. "Nothing else in the frozen app."
- `models` imports nothing from the app; `session`, `text`, `obs`, `vad`, `feed`, `lifecycle` never
  import `gui`; `gui` reaches the rest only through `interfaces`. `session/cues.py` and
  `vad/assign.py` are pure. Composition lives in `app.py`; no DI container.
- All models are frozen dataclasses; JSON on disk carries `"schema": 1`.
- `SKIP_MS = 300`, `MIN_CUE_MS = 500` and every VAD threshold are module constants, not settings.
  `START_SHIFT_MS = {"hook": 0, "ocr": -1000}`.
- A line's arrival time is `time.monotonic()` read inside the source at frame receipt; nothing
  downstream re-stamps it.
- Cue invariant: `0 <= start < end <= next.start`.
- SRT: UTF-8 without BOM, `\n`, index from 1, `HH:MM:SS,mmm` from integer ms, one text line per cue,
  written to a temporary name then `os.replace`.
- Feed binds `127.0.0.1`. The OBS password is never logged and never stored unless typed as an override.
- The app never reads or writes `~/.config/owocr_config.ini`.
- The bundle contains no onnxruntime, numpy, PyAV or owocr (spec `excludes` + smoke assertion).
- Recoverable failures are banners, never modal dialogs. No `pynput`.
- Tests: pytest-qt offscreen, `qtbot.addWidget` on every top-level widget, isolated
  `ANKI_MINER_GAME_HOME` per test, no network except loopback.
- Ported code keeps a header naming the source file and commit (GSM `479747fe`, faster-whisper,
  owocr 1.26.8, Anki Miner commit).

---

## Working context

- Worktree `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts`, branch
  `feat/t01-contracts`, BASE `963dbf90b25bb707fe7a1c749393faece3f415f3` (main + `feat/t00-scaffold`
  merged). `.venv` is a symlink to `/home/light/Projects/anki_miner_game/.venv`.
- Read `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/CLAUDE.md` first. Never bare
  `python3`, never `uv run`, never `pip install -e`, no installs at all. Every Bash call starts with
  `cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts &&`.
- Status file: `/home/light/Projects/anki_miner_game/.orchestration/status/t01-contracts.json`
  (keep `base_sha`).
- Single-file test runs below pass `-n0 -p no:cacheprovider` to skip xdist start-up; the gate in
  Task 11 runs the real configuration.
- Imports inside `anki_miner_game/` are absolute (`from anki_miner_game.models.cue import Cue`);
  `tests/test_contracts.py` enforces it for `models/` and `interfaces/`.
- No `from __future__ import annotations` in `models/`: the codec resolves field types with
  `typing.get_type_hints`, and every field type must be a real import at module level.
- Every piece of code in this plan was run in a scratch copy of this worktree on 2026-09-21: black,
  ruff and mypy clean, 173 tests green under `scripts/health.sh`. Paste it as written.

## Decisions taken while planning

Answered from the card, the spec and the orchestrator notes; nobody else rules on these.

1. **One reflective codec, not hand-written `to_dict`/`from_dict` per class.** About twenty nested
   records sit under three documents (`AppConfig`, `GameProfile`, `SessionManifest`); one
   ~120-line codec handles `bool`, `int`, `float`, `str`, `StrEnum`, nested dataclasses,
   `tuple[X, ...]` and `X | None`, and turns every bad value into one `DecodeError` naming the
   field path (`Outer.items[0].n`). Missing keys take defaults; unknown keys are ignored, so a
   config written before a field existed still loads.
2. **Collections are tuples.** Frozen means immutable all the way down; JSON lists decode to tuples.
3. **Enums are `StrEnum`.** JSON stays readable and a member compares equal to its string, so
   `START_SHIFT_MS[TextMode.OCR]` works on the plain-string-keyed mapping.
4. **Documents are `kw_only`.** `SessionManifest` keeps the spec 5 key order while mixing required
   and defaulted fields; `AppConfig` and `GameProfile` follow for uniform keyword construction.
5. **Invariants that make an object meaningless are checked in `__post_init__`** (`Cue`, `Region`,
   `CueSettings` 5-60 s). The codec turns that `ValueError` into `DecodeError`, so a hand-edited
   `max_cue_seconds: 99` surfaces as `CorruptFileError`. Cross-field rules a user can reach in the
   profile dialog (OCR mode with hook sources) live in `validate(profile) -> list[str]` instead, as
   the card names it.
6. **Platform defaults read `sys.platform` at construction** through `default_factory`
   (`default_audio_mode`, `default_ocr_engine`), so a test can patch the profile module's `sys`.
   Consequence, per spec 5: a default profile on Windows (`audio.mode="app"`, no window) fails
   `validate` until a window is pinned or the mode is switched to desktop. Tests pass `audio`
   explicitly so they behave the same on the Windows CI runner.
7. **Passwords are `field(repr=False)`** in `ObsSettings.password_override`,
   `ObsCredentials.password` and `WsConfig.password`, so logging a record never logs a password.
8. **`output_root` is stored as typed (`"~/Videos/Anki Miner Game"`)**; `paths.output_root(cfg)` is
   the single place that expands `~`. `paths.home()` creates nothing.
9. **`load_profiles()` never raises for one bad file**: it returns `LoadedProfiles(profiles, errors)`
   so one corrupt game does not hide the others; `load_config()` raises `CorruptFileError` /
   `FutureSchemaError` (both `StoreError`, carrying `path`). `save_profile` refuses a profile that
   `validate` rejects (`InvalidProfileError`).
10. **mypy strict is scoped by listing the strict flags** in a `[[tool.mypy.overrides]]` block for
    `anki_miner_game.models.*` and `anki_miner_game.interfaces.*`. Measured on mypy 2.3.1: a
    per-module `strict = true` leaks to every module (an untyped def in an unrelated module started
    failing), while the explicit flags stay scoped (checked with a probe in both places).
11. **`GameProfile.source_ids = None` means every enabled source** (spec: "all enabled"); `()` means
    none. `AutoSettings.enabled` is added: spec 5 says the idle and window-close stops apply "only
    when auto mode is on", which needs a switch of its own.
12. **`LiveCue` keeps the manifest's key names** (`i`, `source`) so the spec 5 JSON example parses
    unchanged; `LiveCue.from_cue` / `to_cue` convert.

## Contract surface beyond section 4

Section 4 names are kept exactly. These additions are type refinements or shared values that two or
more parallel tasks would otherwise invent separately; each names who needs it.

| Name | Module | Needed by |
|---|---|---|
| `DecodeError`, `UnsupportedSchemaError`, `to_dict`, `from_dict`, `dump_document`, `load_document` | `models/codec.py` | `store.py`; T06 `write_manifest_atomic` / `load_manifest` |
| `MAX_CUE_SECONDS_MIN`, `MAX_CUE_SECONDS_MAX` | `models/constants.py` | `CueSettings`; T20 settings spin box |
| `OBS_PROFILE_NAME`, `OBS_COLLECTION_NAME`, `OBS_SCENE_NAME`, `INCOMING_DIRNAME` | `models/constants.py` | T14 provisioning, T15 arming and manifest `obs` record, T06 finalise |
| `DEFAULT_TEXT_SOURCES` | `models/config.py` | `AppConfig` default; T21 wizard reset |
| `CaptureKind`, `AudioMode`, `OcrEngine`, `default_audio_mode`, `default_ocr_engine`, `is_safe_slug` | `models/profile.py` | T14, T20, T24; `paths.profile_path` |
| `GameRef`, `ClockKind`, `VadState`, `COUNT_NAMES`, `Counts.incremented`, `LiveCue.from_cue` / `to_cue` | `models/manifest.py` | T06, T15, T23 |
| `PipelineResult` (`Accepted \| Replaced \| Dropped`) | `models/pipeline.py` | T05 return type, T15 |
| `CommandKind`, `BannerLevel`, `OBS_SOURCE_ID`, `SessionInput` union | `models/messages.py` | T15, T16, T17, T19 |
| `SourceStatusChanged`, `BannerRaised`, `BannerCleared` (in `SessionEvent`) | `models/messages.py` | T15 publishes source lights and its banners (no-source, free-space, split, zero cues); T16 maps each event to one `Presenter` call |
| `LineAccepted.replaces_previous` | `models/messages.py` | typewriter merge reaching the live list (T19) and the feed (T16) |
| `OutputState`, `ObsEventName` incl. `_Connected` / `_ConnectionLost` | `models/obs.py` | T12 emits the two connection events, T15 reconciles on `_Connected` (spec 6.3), T25 |
| `ObsInstall`, `ObsInfo.missing_requests()` | `models/obs.py` | T13 returns `ObsInstall`; T12 names the missing request |
| `ObsError`, `ObsConnectError`, `ObsAuthError`, `ObsRequestError`, `ObsUnsupportedError` | `models/obs.py` | T12 raises; T15, T16, T21 catch them against the Protocol (error matrix rows 4, 5, 8) |
| `AddonStatus` | `models/addons.py` | `AddonService.status()`; T21, T23, T24 |
| `LineSink`, `ProgressCallback` | `interfaces/text_source.py`, `interfaces/addons.py` | callback shapes for T07, T18, T24, T11, T23 |
| `ObsGateway.close()` | `interfaces/obs.py` | T16 shutdown, T12 tests |
| `HOME_ENV`, `config_path`, `games_dir`, `profile_path`, `output_root`, `incoming_dir` | `paths.py` | `store.py`, T06, T14, T15, T16 |
| `StoreError`, `CorruptFileError`, `FutureSchemaError`, `InvalidProfileError`, `LoadedProfiles` | `store.py` | T16 banners, T20 save |

## File map

Created (all under `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/`):

| File | Responsibility |
|---|---|
| `anki_miner_game/models/constants.py` | module constants |
| `anki_miner_game/models/codec.py` | dataclass <-> JSON, schema check |
| `anki_miner_game/models/lines.py` | `GameLine`, `TimedLine` |
| `anki_miner_game/models/cue.py` | `Cue`, `Region` with their invariants |
| `anki_miner_game/models/profile.py` | `GameProfile` and its settings, enums, `validate` |
| `anki_miner_game/models/config.py` | `AppConfig` and its settings |
| `anki_miner_game/models/manifest.py` | `SessionManifest` records, `to_json` / `from_json` |
| `anki_miner_game/models/pipeline.py` | pipeline results, `DROP_COUNTER` |
| `anki_miner_game/models/messages.py` | actor inputs and outputs, `AppState`, `SourceStatus`, `Banner` |
| `anki_miner_game/models/obs.py` | OBS records, request list, event names, errors |
| `anki_miner_game/models/addons.py` | `AddonStatus` |
| `anki_miner_game/interfaces/text_source.py` | `TextSource` |
| `anki_miner_game/interfaces/record_clock.py` | `RecordClock` |
| `anki_miner_game/interfaces/obs.py` | `ObsGateway`, `ObsDiscovery`, `Provisioner`, `Recorder` |
| `anki_miner_game/interfaces/presenter.py` | `Presenter` |
| `anki_miner_game/interfaces/session.py` | `SessionControl` |
| `anki_miner_game/interfaces/addons.py` | `AddonService`, `VadJobs`, `OcrAreaPicker` |
| `anki_miner_game/paths.py` | home and file locations |
| `anki_miner_game/store.py` | atomic JSON persistence of config and profiles |
| `tests/models/__init__.py` | empty; `tests/` is a package, so sub-folders need one |
| `tests/models/test_constants.py`, `test_codec.py`, `test_lines_cue.py`, `test_profile.py`, `test_config.py`, `test_manifest.py`, `test_pipeline.py`, `test_messages_obs.py` | model tests |
| `tests/test_contracts.py` | every section 4 name importable; Protocol members; dependency rule |
| `tests/test_paths.py`, `tests/test_store.py` | paths and store |

Modified: `pyproject.toml` (one mypy override block, Task 1). Nothing else. The existing
`anki_miner_game/models/__init__.py` and `anki_miner_game/interfaces/__init__.py` stay empty: every
name is imported from its own module, so there is one import path per name.

## Tasks


### Task 1: strict typing for the contracts, and the constants

**Files:**
- Modify: `pyproject.toml` (append one `[[tool.mypy.overrides]]` block after the `obsws_python.*` block)
- Create: `anki_miner_game/models/constants.py`
- Create: `tests/models/__init__.py` (empty), `tests/models/test_constants.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SCHEMA`, `SKIP_MS`, `MIN_CUE_MS`, `START_SHIFT_MS: Mapping[str, int]` (read-only), `MAX_LINE_CHARS`, `TYPEWRITER_WINDOW_S`, `MAX_CUE_SECONDS_MIN`, `MAX_CUE_SECONDS_MAX`, `OBS_PROFILE_NAME`, `OBS_COLLECTION_NAME`, `OBS_SCENE_NAME`, `INCOMING_DIRNAME`.

- [ ] **Step 1: Add the strict mypy override**

Append this block to `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/pyproject.toml`, directly after the `[[tool.mypy.overrides]]` block whose module is `["obsws_python.*"]` (keep one blank line between blocks):

```toml
# T01 acceptance: the contracts type-check strictly. Per-module `strict = true`
# leaks to every module in mypy 2.3, so the strict flags are listed one by one
# (warn_redundant_casts / warn_unused_ignores / strict_equality are already global).
[[tool.mypy.overrides]]
module = ["anki_miner_game.models.*", "anki_miner_game.interfaces.*"]
disallow_untyped_defs = true
disallow_incomplete_defs = true
disallow_untyped_calls = true
disallow_untyped_decorators = true
disallow_any_generics = true
disallow_subclassing_any = true
warn_return_any = true
no_implicit_reexport = true
extra_checks = true
```

Then create the empty package marker:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && mkdir -p tests/models && : > tests/models/__init__.py && .venv/bin/mypy anki_miner_game
```

Expected: `Success: no issues found` (the override matches no module yet; the note about unused sections lists it and is harmless).

- [ ] **Step 2: Write the failing test `tests/models/test_constants.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_constants.py`:

```python
import pytest

from anki_miner_game.models import constants


def test_spec_values():
    assert constants.SCHEMA == 1
    assert constants.SKIP_MS == 300
    assert constants.MIN_CUE_MS == 500
    assert constants.MAX_LINE_CHARS == 300
    assert constants.TYPEWRITER_WINDOW_S == 2.0
    assert (constants.MAX_CUE_SECONDS_MIN, constants.MAX_CUE_SECONDS_MAX) == (5, 60)
    assert constants.OBS_PROFILE_NAME == "Anki Miner Game"
    assert constants.OBS_COLLECTION_NAME == "Anki Miner Game"
    assert constants.OBS_SCENE_NAME == "Game"
    assert constants.INCOMING_DIRNAME == "_incoming"


def test_start_shift_values():
    assert dict(constants.START_SHIFT_MS) == {"hook": 0, "ocr": -1000}


def test_start_shift_is_read_only():
    with pytest.raises(TypeError):
        constants.START_SHIFT_MS["hook"] = 5  # type: ignore[index]
```

- [ ] **Step 3: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_constants.py -q
```

Expected: `1 error` during collection, `ImportError: cannot import name 'constants' from 'anki_miner_game.models'`.

- [ ] **Step 4: Implement `anki_miner_game/models/constants.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/constants.py`:

```python
"""Shared constants (spec 5, 8.2, 9). Module constants, never settings."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

SCHEMA: Final = 1
"""Value of the ``"schema"`` key in every JSON document the app writes."""

SKIP_MS: Final = 300
MIN_CUE_MS: Final = 500
START_SHIFT_MS: Final[Mapping[str, int]] = MappingProxyType({"hook": 0, "ocr": -1000})
"""Keyed by ``TextMode`` value; a ``TextMode`` member works as the key."""

MAX_LINE_CHARS: Final = 300
TYPEWRITER_WINDOW_S: Final = 2.0

MAX_CUE_SECONDS_MIN: Final = 5
MAX_CUE_SECONDS_MAX: Final = 60

OBS_PROFILE_NAME: Final = "Anki Miner Game"
OBS_COLLECTION_NAME: Final = "Anki Miner Game"
OBS_SCENE_NAME: Final = "Game"
INCOMING_DIRNAME: Final = "_incoming"
```

- [ ] **Step 5: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_constants.py -q
```

Expected: `3 passed`.

- [ ] **Step 6: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q tests/models/__init__.py anki_miner_game/models/constants.py tests/models/test_constants.py && .venv/bin/ruff check tests/models/__init__.py anki_miner_game/models/constants.py tests/models/test_constants.py && .venv/bin/mypy anki_miner_game \
  && git add pyproject.toml tests/models/__init__.py anki_miner_game/models/constants.py tests/models/test_constants.py \
  && git commit -m "feat(models): add contract constants and strict typing for contracts"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 2: JSON codec

**Files:**
- Create: `anki_miner_game/models/codec.py`
- Test: `tests/models/test_codec.py`

**Interfaces:**
- Consumes: `models.constants.SCHEMA`.
- Produces: `class DecodeError(ValueError)`; `class UnsupportedSchemaError(DecodeError)` with `.found: int`; `to_dict(obj: object) -> dict[str, Any]`; `from_dict[T](cls: type[T], data: object) -> T`; `dump_document(obj: object) -> str` (`"schema": 1` first, `ensure_ascii=False`, indent 2, trailing newline); `load_document[T](cls: type[T], text: str) -> T`. T06 builds `write_manifest_atomic` / `load_manifest` on `manifest.to_json` / `from_json`, which wrap these.

Field types the codec accepts: `bool`, `int`, `float` (a JSON int is accepted), `str`, any `Enum` (by value), a nested dataclass, `tuple[X, ...]`, `X | None`. Anything else raises `TypeError` (a programming error, not bad data). A later task that needs another field type in a persisted model files a CONTRACT-CHANGE-REQUEST.

- [ ] **Step 1: Write the failing test `tests/models/test_codec.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_codec.py`:

```python
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

import pytest

from anki_miner_game.models.codec import (
    DecodeError,
    UnsupportedSchemaError,
    dump_document,
    from_dict,
    load_document,
    to_dict,
)


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class Inner:
    n: int
    label: str | None = None


@dataclass(frozen=True, kw_only=True)
class Outer:
    name: str
    ratio: float = 0.5
    flag: bool = False
    colour: Colour = Colour.RED
    items: tuple[Inner, ...] = ()
    tags: tuple[str, ...] = ()
    maybe: Inner | None = None
    inner: Inner = field(default_factory=lambda: Inner(n=1))


@dataclass(frozen=True)
class Positive:
    n: int

    def __post_init__(self) -> None:
        if self.n <= 0:
            raise ValueError("n must be positive")


@dataclass(frozen=True)
class WithDict:
    d: dict[str, int]


SAMPLE = Outer(
    name="名前",
    ratio=1.25,
    flag=True,
    colour=Colour.BLUE,
    items=(Inner(1, "a"), Inner(2)),
    tags=("x", "y"),
    maybe=Inner(3),
    inner=Inner(4, "b"),
)


def test_to_dict_encodes_enums_tuples_and_nesting():
    assert to_dict(SAMPLE) == {
        "name": "名前",
        "ratio": 1.25,
        "flag": True,
        "colour": "blue",
        "items": [{"n": 1, "label": "a"}, {"n": 2, "label": None}],
        "tags": ["x", "y"],
        "maybe": {"n": 3, "label": None},
        "inner": {"n": 4, "label": "b"},
    }


def test_from_dict_inverts_to_dict():
    assert from_dict(Outer, to_dict(SAMPLE)) == SAMPLE


def test_missing_keys_take_defaults_and_unknown_keys_are_ignored():
    assert from_dict(Outer, {"name": "n", "added_later": 1}) == Outer(name="n")


def test_int_is_accepted_for_float():
    decoded = from_dict(Outer, {"name": "n", "ratio": 2})
    assert decoded.ratio == 2.0
    assert isinstance(decoded.ratio, float)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "Outer.name: missing"),
        ({"name": 5}, "Outer.name: expected str, got int"),
        ({"name": "n", "flag": 1}, "Outer.flag: expected bool, got int"),
        ({"name": "n", "ratio": True}, "Outer.ratio: expected float, got bool"),
        ({"name": "n", "colour": "green"}, "Outer.colour: 'green' is not a valid Colour"),
        ({"name": "n", "tags": "xy"}, "Outer.tags: expected a list, got str"),
        ({"name": "n", "items": [{"n": "1"}]}, "Outer.items[0].n: expected int, got str"),
        ({"name": "n", "inner": None}, "Outer.inner: expected an object, got NoneType"),
        ([], "Outer: expected an object, got list"),
    ],
)
def test_bad_data_raises_decode_error_naming_the_field(data, message):
    with pytest.raises(DecodeError, match=re.escape(message)):
        from_dict(Outer, data)


def test_post_init_rejection_becomes_decode_error():
    with pytest.raises(DecodeError, match="n must be positive"):
        from_dict(Positive, {"n": 0})


def test_unsupported_field_type_is_a_programming_error():
    with pytest.raises(TypeError, match="does not support"):
        from_dict(WithDict, {"d": {}})


def test_to_dict_rejects_non_dataclasses():
    with pytest.raises(TypeError):
        to_dict({"n": 1})
    with pytest.raises(TypeError):
        to_dict(Inner)


def test_dump_document_puts_schema_first_and_keeps_unicode():
    text = dump_document(Inner(7, "名"))
    assert text.endswith("\n")
    assert "名" in text
    assert list(json.loads(text)) == ["schema", "n", "label"]
    assert json.loads(text)["schema"] == 1


def test_load_document_inverts_dump_document():
    assert load_document(Outer, dump_document(SAMPLE)) == SAMPLE


def test_load_document_rejects_a_newer_schema():
    with pytest.raises(UnsupportedSchemaError) as info:
        load_document(Inner, '{"schema": 2, "n": 1}')
    assert info.value.found == 2
    assert isinstance(info.value, DecodeError)


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[1]",
        '{"n": 1}',
        '{"schema": "1", "n": 1}',
        '{"schema": true, "n": 1}',
        '{"schema": 0, "n": 1}',
        '{"schema": 1}',
    ],
)
def test_load_document_rejects_bad_text(text):
    with pytest.raises(DecodeError) as info:
        load_document(Inner, text)
    assert not isinstance(info.value, UnsupportedSchemaError)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_codec.py -q
```

Expected: `1 error` during collection, `ModuleNotFoundError: No module named 'anki_miner_game.models.codec'`.

- [ ] **Step 3: Implement `anki_miner_game/models/codec.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/codec.py`:

```python
"""Conversion between the frozen models and JSON documents.

Pure: builds and parses strings, never touches a file. ``store`` and
``session.manifest`` do the file I/O. Supported field types: ``bool``, ``int``,
``float``, ``str``, str-valued ``Enum``, nested dataclasses, ``tuple[X, ...]``
(a JSON list) and ``X | None``.
"""

import dataclasses
import json
import types
from enum import Enum
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

from anki_miner_game.models.constants import SCHEMA


class DecodeError(ValueError):
    """The data does not describe the expected document."""


class UnsupportedSchemaError(DecodeError):
    """The document carries a ``schema`` newer than :data:`SCHEMA`."""

    def __init__(self, found: int) -> None:
        super().__init__(f"schema {found} is newer than the supported schema {SCHEMA}")
        self.found = found


def to_dict(obj: object) -> dict[str, Any]:
    """Encode a dataclass instance: enums by value, tuples as lists, nested dataclasses as dicts."""
    if isinstance(obj, type) or not dataclasses.is_dataclass(obj):
        raise TypeError(f"expected a dataclass instance, got {type(obj).__name__}")
    return {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}


def _encode(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_dict(value)
    if isinstance(value, tuple | list):
        return [_encode(item) for item in value]
    return value


def from_dict[T](cls: type[T], data: object) -> T:
    """Decode parsed JSON into ``cls``.

    A missing key takes the field default and an unknown key is ignored. A
    missing required key, a value of the wrong JSON type, an unknown enum value,
    or a value the model's ``__post_init__`` rejects raises :class:`DecodeError`.
    """
    return cast(T, _decode(cls, data, cls.__name__))


def _decode(tp: Any, value: object, where: str) -> Any:
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        return _decode_optional(tp, value, where)
    if origin is tuple:
        return _decode_tuple(tp, value, where)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _decode_dataclass(tp, value, where)
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except (ValueError, TypeError):
            raise DecodeError(f"{where}: {value!r} is not a valid {tp.__name__}") from None
    return _decode_scalar(tp, value, where)


def _decode_optional(tp: Any, value: object, where: str) -> Any:
    args = get_args(tp)
    inner = [arg for arg in args if arg is not type(None)]
    if len(args) != 2 or len(inner) != 1:
        raise TypeError(f"{where}: the codec supports only `X | None` unions, not {tp!r}")
    return None if value is None else _decode(inner[0], value, where)


def _decode_tuple(tp: Any, value: object, where: str) -> tuple[Any, ...]:
    args = get_args(tp)
    if len(args) != 2 or args[1] is not Ellipsis:
        raise TypeError(f"{where}: the codec supports only tuple[X, ...], not {tp!r}")
    if not isinstance(value, list):
        raise DecodeError(f"{where}: expected a list, got {type(value).__name__}")
    return tuple(_decode(args[0], item, f"{where}[{i}]") for i, item in enumerate(value))


def _decode_dataclass(cls: type[Any], value: object, where: str) -> Any:
    if not isinstance(value, dict):
        raise DecodeError(f"{where}: expected an object, got {type(value).__name__}")
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        if f.name in value:
            kwargs[f.name] = _decode(hints[f.name], value[f.name], f"{where}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise DecodeError(f"{where}.{f.name}: missing")
    try:
        return cls(**kwargs)
    except ValueError as exc:
        raise DecodeError(f"{where}: {exc}") from exc


def _decode_scalar(tp: Any, value: object, where: str) -> Any:
    if tp is bool:
        ok = isinstance(value, bool)
    elif tp is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif tp is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif tp is str:
        ok = isinstance(value, str)
    else:
        raise TypeError(f"{where}: the codec does not support {tp!r}")
    if not ok:
        raise DecodeError(f"{where}: expected {tp.__name__}, got {type(value).__name__}")
    return float(cast(float, value)) if tp is float else value


def dump_document(obj: object) -> str:
    """JSON text for a top-level document, ``"schema"`` first, UTF-8 characters kept, trailing newline."""
    return json.dumps({"schema": SCHEMA, **to_dict(obj)}, ensure_ascii=False, indent=2) + "\n"


def load_document[T](cls: type[T], text: str) -> T:
    """Parse text written by :func:`dump_document`.

    Raises :class:`UnsupportedSchemaError` for a newer ``schema`` and
    :class:`DecodeError` for anything else that is wrong.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecodeError(f"not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise DecodeError(f"expected a JSON object, got {type(data).__name__}")
    schema = data.pop("schema", None)
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise DecodeError(f"missing or invalid schema: {schema!r}")
    if schema > SCHEMA:
        raise UnsupportedSchemaError(schema)
    if schema < SCHEMA:
        raise DecodeError(f"unknown schema {schema}")
    return from_dict(cls, data)
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_codec.py -q
```

Expected: `26 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/codec.py tests/models/test_codec.py && .venv/bin/ruff check anki_miner_game/models/codec.py tests/models/test_codec.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/codec.py tests/models/test_codec.py \
  && git commit -m "feat(models): add JSON codec for frozen model documents"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 3: lines, cues and regions

**Files:**
- Create: `anki_miner_game/models/lines.py`, `anki_miner_game/models/cue.py`
- Test: `tests/models/test_lines_cue.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GameLine(text: str, raw: str, t_mono: float, source_id: str)`; `TimedLine(offset_ms: int, text: str, source_id: str)`; `Cue(index: int, start_ms: int, end_ms: int, text: str, source_id: str)` raising `ValueError` unless `index >= 1` and `0 <= start_ms < end_ms`; `Region(start_ms: int, end_ms: int)` raising `ValueError` unless `0 <= start_ms < end_ms`. T02 `build_cues` and T09 `assign` return `list[Cue]`; the spec 9 formula never produces `end <= start` (the skip rule guarantees `D >= 300`).

- [ ] **Step 1: Write the failing test `tests/models/test_lines_cue.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_lines_cue.py`:

```python
import dataclasses

import pytest

from anki_miner_game.models.cue import Cue, Region
from anki_miner_game.models.lines import GameLine, TimedLine


def test_game_line_fields_and_frozen():
    line = GameLine(text="こんにちは", raw="【太郎】こんにちは", t_mono=12.5, source_id="textractor")
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.text = "x"  # type: ignore[misc]
    changed = dataclasses.replace(line, text="やあ")
    assert changed.text == "やあ"
    assert changed.raw == line.raw
    assert line.text == "こんにちは"


def test_timed_line_fields():
    assert TimedLine(offset_ms=5230, text="…", source_id="agent") == TimedLine(5230, "…", "agent")


def test_cue_accepts_a_valid_span():
    cue = Cue(index=1, start_ms=0, end_ms=1, text="a", source_id="luna")
    assert (cue.start_ms, cue.end_ms) == (0, 1)


@pytest.mark.parametrize(
    ("index", "start", "end"),
    [(0, 0, 10), (1, -1, 10), (1, 10, 10), (1, 11, 10)],
)
def test_cue_rejects_a_broken_invariant(index, start, end):
    with pytest.raises(ValueError, match="cue"):
        Cue(index=index, start_ms=start, end_ms=end, text="a", source_id="luna")


def test_cue_replace_revalidates():
    cue = Cue(index=1, start_ms=100, end_ms=200, text="a", source_id="luna")
    with pytest.raises(ValueError):
        dataclasses.replace(cue, end_ms=100)


@pytest.mark.parametrize(("start", "end"), [(-1, 5), (5, 5), (6, 5)])
def test_region_rejects_an_empty_or_negative_span(start, end):
    with pytest.raises(ValueError, match="region"):
        Region(start_ms=start, end_ms=end)


def test_region_valid():
    assert Region(5310, 8920) == Region(start_ms=5310, end_ms=8920)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_lines_cue.py -q
```

Expected: `1 error` during collection, `ModuleNotFoundError: No module named 'anki_miner_game.models.cue'`.

- [ ] **Step 3: Implement `anki_miner_game/models/lines.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/lines.py`:

```python
"""Text lines on their way from a source to the cue builder (spec 5, 9)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GameLine:
    text: str
    """After the pipeline (spec 8.2)."""
    raw: str
    """As received from the source."""
    t_mono: float
    """``time.monotonic()`` read inside the source at frame receipt; nothing downstream re-stamps it."""
    source_id: str
    """``"textractor"``, ``"agent"``, ``"luna"``, ``"clipboard"``, ``"ocr"``, or a user source id."""


@dataclass(frozen=True)
class TimedLine:
    """A journalled line: its record-clock offset in place of a monotonic time. Input of ``build_cues``."""

    offset_ms: int
    text: str
    source_id: str
```

- [ ] **Step 4: Implement `anki_miner_game/models/cue.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/cue.py`:

```python
"""Subtitle cues and VAD regions (spec 5, 9, 13)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    """One subtitle cue.

    Checked here: ``index >= 1`` and ``0 <= start_ms < end_ms``. The rule across
    cues, ``end_ms <= next.start_ms``, is the cue builder's job (spec 9).
    """

    index: int
    """1-based, final numbering."""
    start_ms: int
    end_ms: int
    text: str
    source_id: str

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError(f"cue index must be >= 1, got {self.index}")
        if not 0 <= self.start_ms < self.end_ms:
            raise ValueError(f"cue needs 0 <= start_ms < end_ms, got {self.start_ms}..{self.end_ms}")


@dataclass(frozen=True)
class Region:
    """A voiced span reported by the VAD worker (spec 13.2)."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_ms < self.end_ms:
            raise ValueError(f"region needs 0 <= start_ms < end_ms, got {self.start_ms}..{self.end_ms}")
```

- [ ] **Step 5: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_lines_cue.py -q
```

Expected: `12 passed`.

- [ ] **Step 6: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/lines.py anki_miner_game/models/cue.py tests/models/test_lines_cue.py && .venv/bin/ruff check anki_miner_game/models/lines.py anki_miner_game/models/cue.py tests/models/test_lines_cue.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/lines.py anki_miner_game/models/cue.py tests/models/test_lines_cue.py \
  && git commit -m "feat(models): add line, cue and region records"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 4: game profile

**Files:**
- Create: `anki_miner_game/models/profile.py`
- Test: `tests/models/test_profile.py`

**Interfaces:**
- Consumes: `models.constants.START_SHIFT_MS` (test only), `models.codec` (test only).
- Produces: `TextMode` (`hook`, `ocr`), `CaptureKind` (`auto`, `game`, `window`, `pipewire`, `xcomposite`), `AudioMode` (`app`, `desktop`), `OcrEngine` (`oneocr`, `meikiocr`, `glens`, `bing`); `default_audio_mode(platform: str | None = None) -> AudioMode`; `default_ocr_engine(platform: str | None = None) -> OcrEngine`; `is_safe_slug(slug: str) -> bool`; `CaptureSettings(kind, window: str | None)`, `AudioSettings(mode)`, `FilterSettings(speaker_strip, typewriter_merge)`, `OcrSettings(engine, language, window_title: str | None, rects: str | None)`, `AutoSettings(enabled, start_on_first_line, stop_idle_minutes: int, stop_on_window_close)`, `GameProfile(*, slug, title, text_mode, source_ids: tuple[str, ...] | None, clipboard, capture, audio, filters, ocr, auto)`; `validate(profile: GameProfile) -> list[str]`. The exact problem strings are part of the contract (T20 shows them; the tests pin them).

- [ ] **Step 1: Write the failing test `tests/models/test_profile.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_profile.py`:

```python
import dataclasses
import json
from types import SimpleNamespace

import pytest

from anki_miner_game.models import profile as profile_module
from anki_miner_game.models.codec import dump_document, load_document
from anki_miner_game.models.constants import START_SHIFT_MS
from anki_miner_game.models.profile import (
    AudioMode,
    AudioSettings,
    AutoSettings,
    CaptureKind,
    CaptureSettings,
    FilterSettings,
    GameProfile,
    OcrEngine,
    OcrSettings,
    TextMode,
    default_audio_mode,
    default_ocr_engine,
    is_safe_slug,
    validate,
)


def _hook_profile(**changes) -> GameProfile:
    base = GameProfile(slug="steins-gate", title="Steins;Gate", audio=AudioSettings(mode=AudioMode.DESKTOP))
    return dataclasses.replace(base, **changes)


def test_spec_defaults(monkeypatch):
    monkeypatch.setattr(profile_module, "sys", SimpleNamespace(platform="linux"))
    profile = GameProfile(slug="g", title="G")
    assert profile.text_mode is TextMode.HOOK
    assert profile.source_ids is None
    assert profile.clipboard is False
    assert profile.capture == CaptureSettings(kind=CaptureKind.AUTO, window=None)
    assert profile.filters == FilterSettings(speaker_strip=True, typewriter_merge=False)
    assert profile.ocr == OcrSettings(engine=OcrEngine.MEIKIOCR, language="ja", window_title=None, rects=None)
    assert profile.auto == AutoSettings(
        enabled=False, start_on_first_line=False, stop_idle_minutes=10, stop_on_window_close=True
    )


@pytest.mark.parametrize(
    ("platform", "audio", "engine"),
    [("win32", AudioMode.APP, OcrEngine.ONEOCR), ("linux", AudioMode.DESKTOP, OcrEngine.MEIKIOCR)],
)
def test_platform_defaults(monkeypatch, platform, audio, engine):
    assert default_audio_mode(platform) is audio
    assert default_ocr_engine(platform) is engine
    monkeypatch.setattr(profile_module, "sys", SimpleNamespace(platform=platform))
    profile = GameProfile(slug="g", title="G")
    assert profile.audio.mode is audio
    assert profile.ocr.engine is engine


def test_start_shift_has_one_entry_per_text_mode():
    assert set(START_SHIFT_MS) == {mode.value for mode in TextMode}
    assert START_SHIFT_MS[TextMode.HOOK] == 0
    assert START_SHIFT_MS[TextMode.OCR] == -1000


def test_frozen_and_replace():
    profile = _hook_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.title = "x"  # type: ignore[misc]
    renamed = dataclasses.replace(profile, title="Steins;Gate 0")
    assert renamed.title == "Steins;Gate 0"
    assert profile.title == "Steins;Gate"


def test_valid_hook_profile_has_no_problems():
    assert validate(_hook_profile(source_ids=("textractor",), clipboard=True)) == []


def test_valid_ocr_profile_has_no_problems():
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=None)) == []
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=())) == []


def test_ocr_mode_rejects_hook_sources():
    assert validate(_hook_profile(text_mode=TextMode.OCR, source_ids=("textractor",))) == [
        "OCR mode cannot use hook sources"
    ]


def test_ocr_mode_rejects_clipboard():
    assert validate(_hook_profile(text_mode=TextMode.OCR, clipboard=True)) == ["OCR mode cannot use the clipboard"]


def test_app_audio_needs_a_window():
    no_window = _hook_profile(audio=AudioSettings(mode=AudioMode.APP))
    assert validate(no_window) == ["application audio needs a pinned window"]
    pinned = dataclasses.replace(
        no_window, capture=CaptureSettings(kind=CaptureKind.GAME, window="Game:UnityWndClass:game.exe")
    )
    assert validate(pinned) == []


def test_slug_title_and_idle_minutes_are_checked():
    problems = validate(_hook_profile(slug="../x", title="  ", auto=AutoSettings(stop_idle_minutes=-1)))
    assert problems == [
        "slug '../x' cannot be a file name",
        "title is empty",
        "auto stop idle minutes cannot be negative",
    ]


@pytest.mark.parametrize("slug", ["", ".", "..", ".hidden", "a/b", "a\\b", "c:d", "a\0b"])
def test_unsafe_slugs(slug):
    assert is_safe_slug(slug) is False


@pytest.mark.parametrize("slug", ["steins-gate", "persona-5", "シュタインズゲート", "a.b"])
def test_safe_slugs(slug):
    assert is_safe_slug(slug) is True


def test_json_round_trip_with_schema():
    profile = GameProfile(
        slug="zero-escape",
        title="Zero Escape",
        text_mode=TextMode.OCR,
        source_ids=None,
        clipboard=False,
        capture=CaptureSettings(kind=CaptureKind.WINDOW, window="Zero:Class:zero.exe"),
        audio=AudioSettings(mode=AudioMode.APP),
        filters=FilterSettings(speaker_strip=False, typewriter_merge=True),
        ocr=OcrSettings(engine=OcrEngine.GLENS, language="ja", window_title="Zero", rects="0,0,640,480"),
        auto=AutoSettings(enabled=True, start_on_first_line=True, stop_idle_minutes=0, stop_on_window_close=False),
    )
    text = dump_document(profile)
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["text_mode"] == "ocr"
    assert data["capture"] == {"kind": "window", "window": "Zero:Class:zero.exe"}
    assert load_document(GameProfile, text) == profile


def test_json_round_trip_keeps_hook_source_list():
    profile = _hook_profile(source_ids=("textractor", "luna"))
    data = json.loads(dump_document(profile))
    assert data["source_ids"] == ["textractor", "luna"]
    assert load_document(GameProfile, dump_document(profile)) == profile
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_profile.py -q
```

Expected: `1 error` during collection, `ImportError: cannot import name 'profile' from 'anki_miner_game.models'`.

- [ ] **Step 3: Implement `anki_miner_game/models/profile.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/profile.py`:

```python
"""Per-game profile (spec 5), stored as ``<home>/games/<slug>.json``."""

import sys
from dataclasses import dataclass, field
from enum import StrEnum


class TextMode(StrEnum):
    HOOK = "hook"
    """Hooker websockets and the clipboard."""
    OCR = "ocr"
    """owocr only."""


class CaptureKind(StrEnum):
    AUTO = "auto"
    GAME = "game"
    WINDOW = "window"
    PIPEWIRE = "pipewire"
    XCOMPOSITE = "xcomposite"


class AudioMode(StrEnum):
    APP = "app"
    """One application's audio; needs ``capture.window``."""
    DESKTOP = "desktop"


class OcrEngine(StrEnum):
    ONEOCR = "oneocr"
    MEIKIOCR = "meikiocr"
    GLENS = "glens"
    BING = "bing"


def default_audio_mode(platform: str | None = None) -> AudioMode:
    """``app`` on Windows, ``desktop`` elsewhere; ``platform`` defaults to ``sys.platform`` read at call time."""
    return AudioMode.APP if (platform or sys.platform) == "win32" else AudioMode.DESKTOP


def default_ocr_engine(platform: str | None = None) -> OcrEngine:
    """``oneocr`` on Windows, ``meikiocr`` elsewhere; ``platform`` defaults to ``sys.platform`` read at call time."""
    return OcrEngine.ONEOCR if (platform or sys.platform) == "win32" else OcrEngine.MEIKIOCR


def is_safe_slug(slug: str) -> bool:
    """Whether ``slug`` can be used as a file name: not empty, no leading dot, no ``/ \\ :`` or NUL."""
    return bool(slug) and not slug.startswith(".") and not any(ch in slug for ch in "/\\:\0")


@dataclass(frozen=True)
class CaptureSettings:
    kind: CaptureKind = CaptureKind.AUTO
    window: str | None = None
    """The OBS window string picked in the profile dialog, stored verbatim (spec 11.3)."""


@dataclass(frozen=True)
class AudioSettings:
    mode: AudioMode = field(default_factory=default_audio_mode)


@dataclass(frozen=True)
class FilterSettings:
    speaker_strip: bool = True
    typewriter_merge: bool = False


@dataclass(frozen=True)
class OcrSettings:
    engine: OcrEngine = field(default_factory=default_ocr_engine)
    language: str = "ja"
    window_title: str | None = None
    rects: str | None = None
    """owocr's ``Selected coordinates`` / ``Selected window coordinates`` value, verbatim (spec 14)."""


@dataclass(frozen=True)
class AutoSettings:
    enabled: bool = False
    """Auto mode for this game (spec 12); the other fields apply only while it is on."""
    start_on_first_line: bool = False
    stop_idle_minutes: int = 10
    """``0`` disables the idle stop."""
    stop_on_window_close: bool = True


@dataclass(frozen=True, kw_only=True)
class GameProfile:
    slug: str
    title: str
    """What the user typed; the folder and stem use the sanitised form (spec 10.1)."""
    text_mode: TextMode = TextMode.HOOK
    source_ids: tuple[str, ...] | None = None
    """Hook mode: ids from ``AppConfig.text_sources``; ``None`` means every enabled source."""
    clipboard: bool = False
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    auto: AutoSettings = field(default_factory=AutoSettings)


def validate(profile: GameProfile) -> list[str]:
    """Problems that stop the profile from being saved or armed; empty when it is valid."""
    problems: list[str] = []
    if not is_safe_slug(profile.slug):
        problems.append(f"slug {profile.slug!r} cannot be a file name")
    if not profile.title.strip():
        problems.append("title is empty")
    if profile.text_mode is TextMode.OCR and profile.source_ids:
        problems.append("OCR mode cannot use hook sources")
    if profile.text_mode is TextMode.OCR and profile.clipboard:
        problems.append("OCR mode cannot use the clipboard")
    if profile.audio.mode is AudioMode.APP and not profile.capture.window:
        problems.append("application audio needs a pinned window")
    if profile.auto.stop_idle_minutes < 0:
        problems.append("auto stop idle minutes cannot be negative")
    return problems
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_profile.py -q
```

Expected: `25 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/profile.py tests/models/test_profile.py && .venv/bin/ruff check anki_miner_game/models/profile.py tests/models/test_profile.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/profile.py tests/models/test_profile.py \
  && git commit -m "feat(models): add game profile with validation and platform defaults"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 5: app config

**Files:**
- Create: `anki_miner_game/models/config.py`
- Test: `tests/models/test_config.py`

**Interfaces:**
- Consumes: `models.constants.MAX_CUE_SECONDS_MIN` / `MAX_CUE_SECONDS_MAX`; `models.codec` (test only).
- Produces: `ObsSettings(host="127.0.0.1", port: int | None = None, password_override: str | None = None)` (password not in `repr`); `TextSourceConfig(id, name, uri, enabled=True)` where `uri` is `host:port[/path]` without `ws://`; `DEFAULT_TEXT_SOURCES`; `FeedSettings(enabled=True, ws_port=6678, http_port=6679)`; `RecordingSettings(max_height=1080, fps=30)`; `CueSettings(max_cue_seconds=15, end_gap_ms=350)` raising `ValueError` outside 5-60 s or for a negative gap; `VadSettings(enabled=True)`; `AppConfig(*, output_root="~/Videos/Anki Miner Game", obs, text_sources, feed, hotkey="Ctrl+Shift+F9", recording, cue, vad, last_game: str | None = None)`. T02 `build_cues(lines, stop_ms, shift_ms, cfg)` and T09 `assign(..., cfg)` take `cfg: CueSettings`.

- [ ] **Step 1: Write the failing test `tests/models/test_config.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_config.py`:

```python
import dataclasses
import json

import pytest

from anki_miner_game.models.codec import DecodeError, dump_document, load_document
from anki_miner_game.models.config import (
    DEFAULT_TEXT_SOURCES,
    AppConfig,
    CueSettings,
    FeedSettings,
    ObsSettings,
    RecordingSettings,
    TextSourceConfig,
    VadSettings,
)


def test_spec_defaults():
    cfg = AppConfig()
    assert cfg.output_root == "~/Videos/Anki Miner Game"
    assert cfg.obs == ObsSettings(host="127.0.0.1", port=None, password_override=None)
    assert cfg.feed == FeedSettings(enabled=True, ws_port=6678, http_port=6679)
    assert cfg.hotkey == "Ctrl+Shift+F9"
    assert cfg.recording == RecordingSettings(max_height=1080, fps=30)
    assert cfg.cue == CueSettings(max_cue_seconds=15, end_gap_ms=350)
    assert cfg.vad == VadSettings(enabled=True)
    assert cfg.last_game is None


def test_default_text_sources_are_the_three_hookers():
    assert AppConfig().text_sources == DEFAULT_TEXT_SOURCES
    assert [(s.id, s.name, s.uri, s.enabled) for s in DEFAULT_TEXT_SOURCES] == [
        ("textractor", "Textractor", "localhost:6677", True),
        ("agent", "Agent", "localhost:9001", True),
        ("luna", "LunaTranslator", "localhost:2333", True),
    ]


@pytest.mark.parametrize("seconds", [5, 15, 60])
def test_max_cue_seconds_accepts_5_to_60(seconds):
    assert CueSettings(max_cue_seconds=seconds).max_cue_seconds == seconds


@pytest.mark.parametrize("seconds", [4, 61, 0, -15])
def test_max_cue_seconds_rejects_outside_5_to_60(seconds):
    with pytest.raises(ValueError, match="max_cue_seconds must be 5-60"):
        CueSettings(max_cue_seconds=seconds)


def test_end_gap_cannot_be_negative():
    with pytest.raises(ValueError, match="end_gap_ms"):
        CueSettings(end_gap_ms=-1)


def test_out_of_range_cue_in_json_is_a_decode_error():
    with pytest.raises(DecodeError, match="max_cue_seconds must be 5-60"):
        load_document(AppConfig, '{"schema": 1, "cue": {"max_cue_seconds": 61}}')


def test_frozen_and_replace():
    cfg = AppConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.hotkey = "F10"  # type: ignore[misc]
    changed = dataclasses.replace(cfg, cue=dataclasses.replace(cfg.cue, max_cue_seconds=30))
    assert changed.cue.max_cue_seconds == 30
    assert cfg.cue.max_cue_seconds == 15


def test_password_override_never_appears_in_repr():
    cfg = AppConfig(obs=ObsSettings(password_override="hunter2"))
    assert "hunter2" not in repr(cfg)
    assert "hunter2" not in repr(cfg.obs)


def test_json_round_trip_with_schema():
    cfg = AppConfig(
        output_root="D:/Recordings",
        obs=ObsSettings(host="192.168.1.5", port=4460, password_override="typed"),
        text_sources=(TextSourceConfig(id="mine", name="Mine", uri="localhost:7000/ws", enabled=False),),
        feed=FeedSettings(enabled=False, ws_port=7001, http_port=7002),
        hotkey="Ctrl+F10",
        recording=RecordingSettings(max_height=720, fps=60),
        cue=CueSettings(max_cue_seconds=20, end_gap_ms=400),
        vad=VadSettings(enabled=False),
        last_game="steins-gate",
    )
    text = dump_document(cfg)
    data = json.loads(text)
    assert data["schema"] == 1
    assert data["text_sources"] == [{"id": "mine", "name": "Mine", "uri": "localhost:7000/ws", "enabled": False}]
    assert load_document(AppConfig, text) == cfg


def test_empty_document_loads_as_defaults():
    assert load_document(AppConfig, '{"schema": 1}') == AppConfig()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_config.py -q
```

Expected: `1 error` during collection, `ModuleNotFoundError: No module named 'anki_miner_game.models.config'`.

- [ ] **Step 3: Implement `anki_miner_game/models/config.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/config.py`:

```python
"""App-wide settings (spec 5), stored as ``<home>/config.json``."""

from dataclasses import dataclass, field
from typing import Final

from anki_miner_game.models.constants import MAX_CUE_SECONDS_MAX, MAX_CUE_SECONDS_MIN


@dataclass(frozen=True)
class ObsSettings:
    host: str = "127.0.0.1"
    port: int | None = None
    """``None``: read from OBS's websocket ``config.json`` at every connect."""
    password_override: str | None = field(default=None, repr=False)
    """Stored only when the user typed one; otherwise read from OBS at connect time and never stored."""


@dataclass(frozen=True)
class TextSourceConfig:
    id: str
    name: str
    uri: str
    """``host:port[/path]`` without the scheme; the source connects to ``ws://<uri>`` (spec 8.1)."""
    enabled: bool = True


DEFAULT_TEXT_SOURCES: Final = (
    TextSourceConfig(id="textractor", name="Textractor", uri="localhost:6677"),
    TextSourceConfig(id="agent", name="Agent", uri="localhost:9001"),
    TextSourceConfig(id="luna", name="LunaTranslator", uri="localhost:2333"),
)


@dataclass(frozen=True)
class FeedSettings:
    enabled: bool = True
    ws_port: int = 6678
    http_port: int = 6679


@dataclass(frozen=True)
class RecordingSettings:
    max_height: int = 1080
    fps: int = 30


@dataclass(frozen=True)
class CueSettings:
    max_cue_seconds: int = 15
    end_gap_ms: int = 350
    """Anki Miner's ``audio_padding`` (300 ms) + 50 ms; raise it by as much as that padding is raised."""

    def __post_init__(self) -> None:
        if not MAX_CUE_SECONDS_MIN <= self.max_cue_seconds <= MAX_CUE_SECONDS_MAX:
            raise ValueError(
                f"max_cue_seconds must be {MAX_CUE_SECONDS_MIN}-{MAX_CUE_SECONDS_MAX}, got {self.max_cue_seconds}"
            )
        if self.end_gap_ms < 0:
            raise ValueError(f"end_gap_ms cannot be negative, got {self.end_gap_ms}")


@dataclass(frozen=True)
class VadSettings:
    enabled: bool = True
    """Takes effect only while the VAD add-on is installed."""


@dataclass(frozen=True, kw_only=True)
class AppConfig:
    output_root: str = "~/Videos/Anki Miner Game"
    """May start with ``~``; ``paths.output_root`` expands it. ``_incoming/`` lives inside."""
    obs: ObsSettings = field(default_factory=ObsSettings)
    text_sources: tuple[TextSourceConfig, ...] = DEFAULT_TEXT_SOURCES
    feed: FeedSettings = field(default_factory=FeedSettings)
    hotkey: str = "Ctrl+Shift+F9"
    """Windows only (spec 16)."""
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    cue: CueSettings = field(default_factory=CueSettings)
    vad: VadSettings = field(default_factory=VadSettings)
    last_game: str | None = None
    """Slug of the last selected game."""
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_config.py -q
```

Expected: `15 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/config.py tests/models/test_config.py && .venv/bin/ruff check anki_miner_game/models/config.py tests/models/test_config.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/config.py tests/models/test_config.py \
  && git commit -m "feat(models): add app config with spec defaults"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 6: session manifest

**Files:**
- Create: `anki_miner_game/models/manifest.py`
- Test: `tests/models/test_manifest.py`

**Interfaces:**
- Consumes: `models.codec.dump_document` / `load_document`, `models.cue.Cue`, `models.profile.TextMode`.
- Produces: `ManifestState` (`recording`, `finalise_pending`, `ready`, `vad_running`); `Flag` (`split_unsupported`, `clock_degraded`, `obs_exited`, `no_cues`); `ClockKind` (`event`, `output_duration`); `VadState` (`queued`, `done`, `failed`, `unavailable`, `restored`); `GameRef(slug, title)`; `ObsRecord(version, websocket, profile, collection, output_path)`; `DriftSample(at_ms, output_duration_ms)`; `ClockRecord(kind=EVENT, zero_event="STARTED", capture_latency_ms=0, degraded=False, drift_samples=())`; `Counts(received, accepted, duplicate, no_letters, junk, paused, skip)` all `0` by default, with `incremented(name: str, by: int = 1) -> Counts`; `COUNT_NAMES: frozenset[str]`; `LiveCue(i, start_ms, end_ms, text, source)` with `from_cue(cue: Cue)` / `to_cue() -> Cue`; `VadRecord(state, model=None, trimmed=0, no_speech=0, message=None)`; `FilesRecord(video: str, subtitle: str | None)`; `SessionManifest(*, app_version, game, index, state, started_at, stopped_at=None, obs, clock, text_mode, sources_used=(), counts, flags=(), live_cues=(), vad=None, files=None)`; `to_json(manifest) -> str`; `from_json(text: str) -> SessionManifest` (raises `DecodeError` / `UnsupportedSchemaError`).

- [ ] **Step 1: Write the failing test `tests/models/test_manifest.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_manifest.py`:

```python
import dataclasses
import json

import pytest

from anki_miner_game.models.codec import DecodeError, UnsupportedSchemaError
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.manifest import (
    COUNT_NAMES,
    ClockKind,
    ClockRecord,
    Counts,
    DriftSample,
    FilesRecord,
    Flag,
    GameRef,
    LiveCue,
    ManifestState,
    ObsRecord,
    SessionManifest,
    VadRecord,
    VadState,
    from_json,
    to_json,
)
from anki_miner_game.models.profile import TextMode

# The example from spec section 5, verbatim.
SPEC_EXAMPLE = """
{
  "schema": 1,
  "app_version": "0.1.0",
  "game": {"slug": "steins-gate", "title": "Steins;Gate"},
  "index": 3,
  "state": "ready",
  "started_at": "2026-10-02T18:04:11Z",
  "stopped_at": "2026-10-02T19:31:40Z",
  "obs": {"version": "31.0.2", "websocket": "5.5.4", "profile": "Anki Miner Game",
          "collection": "Anki Miner Game", "output_path": ".../_incoming/2026-10-02 18-04-11.mkv"},
  "clock": {"kind": "event", "zero_event": "STARTED", "capture_latency_ms": 0,
            "degraded": false, "drift_samples": [{"at_ms": 0, "output_duration_ms": 0}]},
  "text_mode": "hook",
  "sources_used": ["textractor"],
  "counts": {"received": 1412, "accepted": 1260, "duplicate": 96, "no_letters": 31,
             "junk": 4, "paused": 0, "skip": 21},
  "flags": [],
  "live_cues": [{"i": 1, "start_ms": 5230, "end_ms": 9410, "text": "…", "source": "textractor"}],
  "vad": {"state": "done", "model": "silero_vad_v6", "trimmed": 1104, "no_speech": 156},
  "files": {"video": "Steins;Gate - 03.mkv", "subtitle": "Steins;Gate - 03.srt"}
}
"""


def _recording_manifest() -> SessionManifest:
    return SessionManifest(
        app_version="0.1.0",
        game=GameRef(slug="steins-gate", title="Steins;Gate"),
        index=3,
        state=ManifestState.RECORDING,
        started_at="2026-10-02T18:04:11Z",
        obs=ObsRecord(
            version="31.0.2",
            websocket="5.5.4",
            profile="Anki Miner Game",
            collection="Anki Miner Game",
            output_path="/v/_incoming/2026-10-02 18-04-11.mkv",
        ),
        text_mode=TextMode.HOOK,
    )


def test_spec_example_parses():
    manifest = from_json(SPEC_EXAMPLE)
    assert manifest.game == GameRef(slug="steins-gate", title="Steins;Gate")
    assert manifest.index == 3
    assert manifest.state is ManifestState.READY
    assert manifest.clock == ClockRecord(
        kind=ClockKind.EVENT,
        zero_event="STARTED",
        capture_latency_ms=0,
        degraded=False,
        drift_samples=(DriftSample(at_ms=0, output_duration_ms=0),),
    )
    assert manifest.text_mode is TextMode.HOOK
    assert manifest.sources_used == ("textractor",)
    assert manifest.counts == Counts(
        received=1412, accepted=1260, duplicate=96, no_letters=31, junk=4, paused=0, skip=21
    )
    assert manifest.flags == ()
    assert manifest.live_cues == (LiveCue(i=1, start_ms=5230, end_ms=9410, text="…", source="textractor"),)
    assert manifest.vad == VadRecord(state=VadState.DONE, model="silero_vad_v6", trimmed=1104, no_speech=156)
    assert manifest.files == FilesRecord(video="Steins;Gate - 03.mkv", subtitle="Steins;Gate - 03.srt")


def test_spec_example_round_trips():
    manifest = from_json(SPEC_EXAMPLE)
    assert from_json(to_json(manifest)) == manifest


def test_json_keys_follow_the_spec_order():
    data = json.loads(to_json(from_json(SPEC_EXAMPLE)))
    assert list(data) == list(json.loads(SPEC_EXAMPLE))


def test_recording_manifest_defaults_and_round_trip():
    manifest = _recording_manifest()
    assert manifest.stopped_at is None
    assert manifest.clock == ClockRecord()
    assert manifest.counts == Counts()
    assert (manifest.sources_used, manifest.flags, manifest.live_cues) == ((), (), ())
    assert (manifest.vad, manifest.files) == (None, None)
    data = json.loads(to_json(manifest))
    assert data["schema"] == 1
    assert data["state"] == "recording"
    assert data["vad"] is None
    assert from_json(to_json(manifest)) == manifest


def test_flags_and_states_round_trip():
    manifest = dataclasses.replace(
        _recording_manifest(),
        state=ManifestState.FINALISE_PENDING,
        flags=(Flag.NO_CUES, Flag.OBS_EXITED, Flag.SPLIT_UNSUPPORTED, Flag.CLOCK_DEGRADED),
        clock=ClockRecord(kind=ClockKind.OUTPUT_DURATION, degraded=True),
        vad=VadRecord(state=VadState.UNAVAILABLE, message="VAD add-on is not installed"),
        files=FilesRecord(video="Steins;Gate - 03.mkv", subtitle=None),
    )
    data = json.loads(to_json(manifest))
    assert data["flags"] == ["no_cues", "obs_exited", "split_unsupported", "clock_degraded"]
    assert data["clock"]["kind"] == "output_duration"
    assert from_json(to_json(manifest)) == manifest


def test_unknown_flag_is_a_decode_error():
    text = to_json(_recording_manifest()).replace('"flags": []', '"flags": ["bogus"]')
    with pytest.raises(DecodeError, match="bogus"):
        from_json(text)


def test_newer_schema_is_refused():
    with pytest.raises(UnsupportedSchemaError):
        from_json(to_json(_recording_manifest()).replace('"schema": 1', '"schema": 2'))


def test_live_cue_converts_to_and_from_cue():
    cue = Cue(index=4, start_ms=100, end_ms=900, text="はい", source_id="luna")
    live = LiveCue.from_cue(cue)
    assert live == LiveCue(i=4, start_ms=100, end_ms=900, text="はい", source="luna")
    assert live.to_cue() == cue


def test_counts_incremented():
    counts = Counts().incremented("received").incremented("skip", by=3)
    assert (counts.received, counts.skip) == (1, 3)
    assert Counts().received == 0
    with pytest.raises(KeyError):
        Counts().incremented("empty")


def test_count_names_match_the_spec():
    assert set(COUNT_NAMES) == {"received", "accepted", "duplicate", "no_letters", "junk", "paused", "skip"}


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _recording_manifest().index = 4  # type: ignore[misc]
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_manifest.py -q
```

Expected: `1 error` during collection, `ModuleNotFoundError: No module named 'anki_miner_game.models.manifest'`.

- [ ] **Step 3: Implement `anki_miner_game/models/manifest.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/manifest.py`:

```python
"""Session manifest ``<stem>.session.json`` (spec 5, 10). Field names are the JSON keys."""

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Self

from anki_miner_game.models.codec import dump_document, load_document
from anki_miner_game.models.cue import Cue
from anki_miner_game.models.profile import TextMode


class ManifestState(StrEnum):
    RECORDING = "recording"
    FINALISE_PENDING = "finalise_pending"
    READY = "ready"
    VAD_RUNNING = "vad_running"


class Flag(StrEnum):
    SPLIT_UNSUPPORTED = "split_unsupported"
    CLOCK_DEGRADED = "clock_degraded"
    OBS_EXITED = "obs_exited"
    NO_CUES = "no_cues"


class ClockKind(StrEnum):
    EVENT = "event"
    OUTPUT_DURATION = "output_duration"


class VadState(StrEnum):
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    """The add-on is not installed; the live subtitle stands."""
    RESTORED = "restored"
    """The user restored the untrimmed subtitle from ``live_cues``."""


@dataclass(frozen=True)
class GameRef:
    slug: str
    title: str


@dataclass(frozen=True)
class ObsRecord:
    version: str
    websocket: str
    profile: str
    collection: str
    output_path: str
    """``RecordStateChanged.outputPath`` at STARTED, verbatim."""


@dataclass(frozen=True)
class DriftSample:
    at_ms: int
    """Record-clock offset when sampled."""
    output_duration_ms: int
    """``GetRecordStatus.outputDuration`` at that moment; a check, never an input (spec 7)."""


@dataclass(frozen=True)
class ClockRecord:
    kind: ClockKind = ClockKind.EVENT
    zero_event: str = "STARTED"
    """The moment that is offset 0; M0 measures which one (spec 7)."""
    capture_latency_ms: int = 0
    degraded: bool = False
    drift_samples: tuple[DriftSample, ...] = ()


@dataclass(frozen=True)
class Counts:
    received: int = 0
    accepted: int = 0
    duplicate: int = 0
    no_letters: int = 0
    junk: int = 0
    paused: int = 0
    skip: int = 0

    def incremented(self, name: str, by: int = 1) -> Self:
        """A copy with counter ``name`` raised by ``by``; ``KeyError`` for a name that is not a counter."""
        if name not in COUNT_NAMES:
            raise KeyError(name)
        return replace(self, **{name: getattr(self, name) + by})


COUNT_NAMES: frozenset[str] = frozenset(f.name for f in fields(Counts))


@dataclass(frozen=True)
class LiveCue:
    """A cue as computed before the VAD pass, with the manifest's key names (spec 5)."""

    i: int
    start_ms: int
    end_ms: int
    text: str
    source: str

    @classmethod
    def from_cue(cls, cue: Cue) -> Self:
        return cls(i=cue.index, start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text, source=cue.source_id)

    def to_cue(self) -> Cue:
        return Cue(index=self.i, start_ms=self.start_ms, end_ms=self.end_ms, text=self.text, source_id=self.source)


@dataclass(frozen=True)
class VadRecord:
    state: VadState
    model: str | None = None
    trimmed: int = 0
    no_speech: int = 0
    message: str | None = None
    """Why the pass failed or is unavailable; shown in the session row (spec 13, 17)."""


@dataclass(frozen=True)
class FilesRecord:
    """Final names inside the game folder, set by finalise step 4 (spec 10.3)."""

    video: str
    subtitle: str | None
    """``None`` when the session has no cues (flag ``no_cues``)."""


@dataclass(frozen=True, kw_only=True)
class SessionManifest:
    app_version: str
    game: GameRef
    index: int
    """The reserved session number NN (spec 10.2)."""
    state: ManifestState
    started_at: str
    """UTC, ISO 8601 with a ``Z`` suffix."""
    stopped_at: str | None = None
    obs: ObsRecord
    clock: ClockRecord = field(default_factory=ClockRecord)
    text_mode: TextMode
    sources_used: tuple[str, ...] = ()
    counts: Counts = field(default_factory=Counts)
    flags: tuple[Flag, ...] = ()
    live_cues: tuple[LiveCue, ...] = ()
    vad: VadRecord | None = None
    files: FilesRecord | None = None


def to_json(manifest: SessionManifest) -> str:
    """The manifest as JSON text with ``"schema": 1`` first."""
    return dump_document(manifest)


def from_json(text: str) -> SessionManifest:
    """Parse manifest text; raises ``codec.DecodeError`` (``UnsupportedSchemaError`` for a newer schema)."""
    return load_document(SessionManifest, text)
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_manifest.py -q
```

Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/manifest.py tests/models/test_manifest.py && .venv/bin/ruff check anki_miner_game/models/manifest.py tests/models/test_manifest.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/manifest.py tests/models/test_manifest.py \
  && git commit -m "feat(models): add session manifest records and JSON round trip"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 7: pipeline results, actor messages, OBS records, add-on status

**Files:**
- Create: `anki_miner_game/models/pipeline.py`, `anki_miner_game/models/messages.py`, `anki_miner_game/models/obs.py`, `anki_miner_game/models/addons.py`
- Test: `tests/models/test_pipeline.py`, `tests/models/test_messages_obs.py`

**Interfaces:**
- Consumes: `models.lines.GameLine`; `models.manifest.Counts` / `COUNT_NAMES` (test only).
- Produces (pipeline): `DropReason` (`empty`, `no_letters`, `junk`, `duplicate`); `DROP_COUNTER: Mapping[DropReason, str]` (read-only; `EMPTY -> "no_letters"`); `Accepted(line)`, `Replaced(line)`, `Dropped(reason)`; `PipelineResult`. T05 `TextPipeline(filters).process(msg: LineReceived) -> PipelineResult`.
- Produces (messages): `AppState`, `SourceStatus`, `BannerLevel`, `CommandKind`; `OBS_SOURCE_ID = "obs"`; `Banner(key, level, text)`; inputs `LineReceived(raw, t_mono, source_id)`, `ObsEvent(name, data: Mapping[str, Any], t_mono)`, `UserCommand(kind, slug=None)`, `Tick(t_mono)`, union `SessionInput`; outputs `StateChanged(state)`, `LineAccepted(line, offset_ms: int | None, replaces_previous=False)`, `RecordingStarted(stem)`, `RecordingStopped(stem)`, `SessionFinalised(manifest_path: Path)`, `SourceStatusChanged(source_id, status)`, `BannerRaised(banner)`, `BannerCleared(key)`, union `SessionEvent`. Both unions are runtime `UnionType`s, so `isinstance(msg, SessionInput)` works.
- Produces (obs): `REQUIRED_REQUESTS` (the 26 of spec 3.3, in spec order); `OutputState`; `ObsEventName` (seven OBS events plus the gateway's own `_Connected` and `_ConnectionLost`); `ObsInfo(obs_version, websocket_version, available_requests: frozenset[str])` with `missing_requests() -> tuple[str, ...]`; `ObsCredentials(host, port: int, password=None)`; `WsConfig(server_enabled, port, password, auth_required)`; `ObsInstall(argv: tuple[str, ...], cwd: Path | None, flatpak: bool)`; `WindowItem(name, value)`; `ProvisionResult(changed, needs_restart)`; errors `ObsError` > `ObsConnectError` > `ObsAuthError`, `ObsRequestError(request, code, comment)`, `ObsUnsupportedError(obs_version, missing)`.
- Produces (addons): `AddonStatus` (`missing`, `installing`, `ready`, `broken`).

- [ ] **Step 1: Write the failing test `tests/models/test_pipeline.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_pipeline.py`:

```python
import dataclasses

import pytest

from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.manifest import COUNT_NAMES, Counts
from anki_miner_game.models.pipeline import DROP_COUNTER, Accepted, Dropped, DropReason, Replaced


def test_every_drop_reason_maps_to_a_counts_field():
    assert set(DROP_COUNTER) == set(DropReason)
    assert set(DROP_COUNTER.values()) <= COUNT_NAMES
    for reason in DropReason:
        assert Counts().incremented(DROP_COUNTER[reason]) != Counts()


def test_empty_lines_count_under_no_letters():
    assert DROP_COUNTER[DropReason.EMPTY] == "no_letters"
    assert DROP_COUNTER[DropReason.NO_LETTERS] == "no_letters"
    assert DROP_COUNTER[DropReason.JUNK] == "junk"
    assert DROP_COUNTER[DropReason.DUPLICATE] == "duplicate"


def test_drop_counter_is_read_only():
    with pytest.raises(TypeError):
        DROP_COUNTER[DropReason.JUNK] = "skip"  # type: ignore[index]


def test_results_are_frozen_values():
    line = GameLine(text="え", raw="え", t_mono=1.0, source_id="agent")
    assert Accepted(line) == Accepted(line=line)
    assert Replaced(line).line is line
    assert Dropped(DropReason.JUNK).reason is DropReason.JUNK
    with pytest.raises(dataclasses.FrozenInstanceError):
        Dropped(DropReason.JUNK).reason = DropReason.EMPTY  # type: ignore[misc]
```

- [ ] **Step 2: Write the failing test `tests/models/test_messages_obs.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/models/test_messages_obs.py`:

```python
from pathlib import Path

from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    LineReceived,
    ObsEvent,
    RecordingStarted,
    RecordingStopped,
    SessionEvent,
    SessionFinalised,
    SessionInput,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConnectError,
    ObsCredentials,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsInstall,
    ObsRequestError,
    ObsUnsupportedError,
    OutputState,
    ProvisionResult,
    WindowItem,
    WsConfig,
)


def test_enum_values_are_the_spec_strings():
    assert [s.value for s in AppState] == ["idle", "armed", "recording", "finalising"]
    assert [s.value for s in SourceStatus] == ["disconnected", "connecting", "connected", "receiving"]
    assert [k.value for k in CommandKind] == ["arm", "disarm", "start", "stop", "toggle"]
    assert [lvl.value for lvl in BannerLevel] == ["info", "warning", "error"]
    assert [s.value for s in AddonStatus] == ["missing", "installing", "ready", "broken"]
    assert OutputState.PAUSED == "OBS_WEBSOCKET_OUTPUT_PAUSED"
    assert OutputState.STARTED == "OBS_WEBSOCKET_OUTPUT_STARTED"
    assert ObsEventName.RECORD_STATE_CHANGED == "RecordStateChanged"
    assert OBS_SOURCE_ID == "obs"


def test_gateway_connection_events_cannot_collide_with_obs_events():
    assert ObsEventName.CONNECTED.startswith("_")
    assert ObsEventName.CONNECTION_LOST.startswith("_")


def test_session_input_and_event_unions_cover_every_message():
    line = GameLine(text="a", raw="a", t_mono=1.0, source_id="agent")
    inputs = [
        LineReceived(raw="a", t_mono=1.0, source_id="agent"),
        ObsEvent(name="ExitStarted", data={}, t_mono=2.0),
        UserCommand(kind=CommandKind.ARM, slug="steins-gate"),
        Tick(t_mono=3.0),
    ]
    events = [
        StateChanged(AppState.ARMED),
        LineAccepted(line=line, offset_ms=None),
        RecordingStarted(stem="2026-10-02 18-04-11"),
        RecordingStopped(stem="2026-10-02 18-04-11"),
        SessionFinalised(manifest_path=Path("x.session.json")),
        SourceStatusChanged(source_id=OBS_SOURCE_ID, status=SourceStatus.CONNECTED),
        BannerRaised(Banner(key="no-source", level=BannerLevel.WARNING, text="No text source")),
        BannerCleared(key="no-source"),
    ]
    assert all(isinstance(msg, SessionInput) for msg in inputs)
    assert all(isinstance(ev, SessionEvent) for ev in events)
    assert not isinstance(Tick(1.0), SessionEvent)


def test_line_accepted_defaults_to_a_new_line():
    line = GameLine(text="a", raw="a", t_mono=1.0, source_id="agent")
    assert LineAccepted(line=line, offset_ms=5).replaces_previous is False
    assert UserCommand(kind=CommandKind.STOP).slug is None


def test_required_requests_are_the_26_of_spec_3_3():
    assert len(REQUIRED_REQUESTS) == 26
    assert len(set(REQUIRED_REQUESTS)) == 26
    assert REQUIRED_REQUESTS[0] == "GetVersion"
    assert REQUIRED_REQUESTS[-1] == "GetInputPropertiesListPropertyItems"


def test_missing_requests_keeps_the_required_order():
    info = ObsInfo(
        obs_version="29.1.3",
        websocket_version="5.3.0",
        available_requests=frozenset(REQUIRED_REQUESTS) - {"SetInputMute", "GetVersion"},
    )
    assert info.missing_requests() == ("GetVersion", "SetInputMute")
    full = ObsInfo(obs_version="31.0.2", websocket_version="5.5.4", available_requests=frozenset(REQUIRED_REQUESTS))
    assert full.missing_requests() == ()


def test_passwords_never_appear_in_repr():
    creds = ObsCredentials(host="127.0.0.1", port=4455, password="hunter2")
    ws = WsConfig(server_enabled=True, port=4455, password="hunter2", auth_required=True)
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in repr(ws)
    assert creds.password == "hunter2"


def test_small_records():
    install = ObsInstall(argv=("flatpak", "run", "com.obsproject.Studio"), cwd=None, flatpak=True)
    assert install.flatpak is True
    assert WindowItem(name="Game", value="Game:UnityWndClass:game.exe").value.endswith(".exe")
    assert ProvisionResult(changed=True, needs_restart=False).changed is True


def test_obs_error_hierarchy_and_messages():
    assert issubclass(ObsAuthError, ObsConnectError)
    assert issubclass(ObsConnectError, ObsError)
    err = ObsRequestError("StartRecord", 500, "Output is already active")
    assert (err.request, err.code, err.comment) == ("StartRecord", 500, "Output is already active")
    assert "Output is already active" in str(err)
    unsupported = ObsUnsupportedError("29.1.3", ("SetInputMute",))
    assert unsupported.missing == ("SetInputMute",)
    assert "SetInputMute" in str(unsupported)
    assert "29.1.3" in str(unsupported)
```

- [ ] **Step 3: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_pipeline.py tests/models/test_messages_obs.py -q
```

Expected: `2 errors` during collection: `ModuleNotFoundError: No module named 'anki_miner_game.models.pipeline'` and `... 'anki_miner_game.models.addons'`.

- [ ] **Step 4: Implement `anki_miner_game/models/pipeline.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/pipeline.py`:

```python
"""Results of the text pipeline (spec 8.2)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from anki_miner_game.models.lines import GameLine


class DropReason(StrEnum):
    EMPTY = "empty"
    NO_LETTERS = "no_letters"
    JUNK = "junk"
    DUPLICATE = "duplicate"


DROP_COUNTER: Final[Mapping[DropReason, str]] = MappingProxyType(
    {
        DropReason.EMPTY: "no_letters",  # the manifest has no ``empty`` counter (master plan section 1)
        DropReason.NO_LETTERS: "no_letters",
        DropReason.JUNK: "junk",
        DropReason.DUPLICATE: "duplicate",
    }
)
"""The ``Counts`` field each drop reason increments."""


@dataclass(frozen=True)
class Accepted:
    line: GameLine


@dataclass(frozen=True)
class Replaced:
    """Typewriter merge: ``line`` replaces the previous accepted line and keeps its offset (step 9)."""

    line: GameLine


@dataclass(frozen=True)
class Dropped:
    reason: DropReason


PipelineResult = Accepted | Replaced | Dropped
```

- [ ] **Step 5: Implement `anki_miner_game/models/messages.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/messages.py`:

```python
"""Messages into and events out of the session actor, plus the states the GUI shows (spec 4.2, 6, 16)."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from anki_miner_game.models.lines import GameLine


class AppState(StrEnum):
    IDLE = "idle"
    ARMED = "armed"
    RECORDING = "recording"
    FINALISING = "finalising"


class SourceStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECEIVING = "receiving"


class BannerLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CommandKind(StrEnum):
    ARM = "arm"
    DISARM = "disarm"
    START = "start"
    STOP = "stop"
    TOGGLE = "toggle"


OBS_SOURCE_ID: Final = "obs"
"""``SourceStatusChanged.source_id`` of the OBS light in the status row; no text source may use it."""


@dataclass(frozen=True)
class Banner:
    key: str
    """Stable id: a banner with the same key replaces the shown one; ``BannerCleared(key)`` removes it."""
    level: BannerLevel
    text: str


# Actor inputs.


@dataclass(frozen=True)
class LineReceived:
    raw: str
    t_mono: float
    source_id: str


@dataclass(frozen=True)
class ObsEvent:
    name: str
    """obs-websocket ``eventType``, or one of the gateway's own ``ObsEventName`` connection events."""
    data: Mapping[str, Any]
    """``eventData`` as OBS sent it (empty for the gateway's own events)."""
    t_mono: float
    """``now()`` read on the library's event thread when the event arrived."""


@dataclass(frozen=True)
class UserCommand:
    kind: CommandKind
    slug: str | None = None
    """The game to arm; used only with ``CommandKind.ARM``."""


@dataclass(frozen=True)
class Tick:
    t_mono: float


SessionInput = LineReceived | ObsEvent | UserCommand | Tick


# Actor outputs.


@dataclass(frozen=True)
class StateChanged:
    state: AppState


@dataclass(frozen=True)
class LineAccepted:
    line: GameLine
    offset_ms: int | None
    """Record-clock offset; ``None`` when no recording is running."""
    replaces_previous: bool = False
    """Typewriter merge: the text replaces the previous accepted line (spec 8.2 step 9)."""


@dataclass(frozen=True)
class RecordingStarted:
    stem: str
    """OBS's file stem in ``_incoming/``."""


@dataclass(frozen=True)
class RecordingStopped:
    stem: str


@dataclass(frozen=True)
class SessionFinalised:
    manifest_path: Path
    """Where the manifest ended up: the game folder, or ``_incoming/`` when ``finalise_pending``."""


@dataclass(frozen=True)
class SourceStatusChanged:
    source_id: str
    status: SourceStatus


@dataclass(frozen=True)
class BannerRaised:
    banner: Banner


@dataclass(frozen=True)
class BannerCleared:
    key: str


SessionEvent = (
    StateChanged
    | LineAccepted
    | RecordingStarted
    | RecordingStopped
    | SessionFinalised
    | SourceStatusChanged
    | BannerRaised
    | BannerCleared
)
```

- [ ] **Step 6: Implement `anki_miner_game/models/obs.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/obs.py`:

```python
"""OBS facts and records shared by discovery, the gateway, provisioning and the actor (spec 3.3, 11)."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

REQUIRED_REQUESTS: Final[tuple[str, ...]] = (
    "GetVersion",
    "GetRecordStatus",
    "StartRecord",
    "StopRecord",
    "GetStreamStatus",
    "GetReplayBufferStatus",
    "GetVirtualCamStatus",
    "GetProfileList",
    "CreateProfile",
    "SetCurrentProfile",
    "GetSceneCollectionList",
    "CreateSceneCollection",
    "SetCurrentSceneCollection",
    "GetProfileParameter",
    "SetProfileParameter",
    "GetVideoSettings",
    "SetVideoSettings",
    "GetRecordDirectory",
    "SetRecordDirectory",
    "CreateScene",
    "GetInputKindList",
    "CreateInput",
    "SetInputSettings",
    "GetSpecialInputs",
    "SetInputMute",
    "GetInputPropertiesListPropertyItems",
)
"""Every request the app sends (spec 3.3); ``GetVersion.availableRequests`` must contain them all."""


class OutputState(StrEnum):
    STARTING = "OBS_WEBSOCKET_OUTPUT_STARTING"
    STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
    STOPPING = "OBS_WEBSOCKET_OUTPUT_STOPPING"
    STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
    PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
    RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"


class ObsEventName(StrEnum):
    RECORD_STATE_CHANGED = "RecordStateChanged"
    RECORD_FILE_CHANGED = "RecordFileChanged"
    CURRENT_SCENE_COLLECTION_CHANGING = "CurrentSceneCollectionChanging"
    CURRENT_SCENE_COLLECTION_CHANGED = "CurrentSceneCollectionChanged"
    CURRENT_PROFILE_CHANGING = "CurrentProfileChanging"
    CURRENT_PROFILE_CHANGED = "CurrentProfileChanged"
    EXIT_STARTED = "ExitStarted"
    CONNECTED = "_Connected"
    """Sent by the gateway itself after every successful connect or reconnect; the actor reconciles on it."""
    CONNECTION_LOST = "_ConnectionLost"
    """Sent by the gateway itself when the connection drops."""


@dataclass(frozen=True)
class ObsInfo:
    obs_version: str
    websocket_version: str
    available_requests: frozenset[str]

    def missing_requests(self) -> tuple[str, ...]:
        """Required requests this OBS lacks, in ``REQUIRED_REQUESTS`` order."""
        return tuple(name for name in REQUIRED_REQUESTS if name not in self.available_requests)


@dataclass(frozen=True)
class ObsCredentials:
    host: str
    port: int
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WsConfig:
    """``plugin_config/obs-websocket/config.json`` under the OBS config root (spec 3.3)."""

    server_enabled: bool
    port: int
    password: str | None = field(repr=False)
    auth_required: bool


@dataclass(frozen=True)
class ObsInstall:
    """How to start the OBS that discovery found (spec 11.1)."""

    argv: tuple[str, ...]
    """The command without ``--minimize-to-tray``."""
    cwd: Path | None
    """The folder OBS must start in (Windows: ``bin\\64bit``); ``None`` when any folder works."""
    flatpak: bool


@dataclass(frozen=True)
class WindowItem:
    name: str
    """Shown to the user."""
    value: str
    """Stored verbatim in ``capture.window``."""


@dataclass(frozen=True)
class ProvisionResult:
    changed: bool
    needs_restart: bool
    """A changed setting takes effect only after OBS restarts (spec 11.3)."""


class ObsError(Exception):
    """Base of the failures the OBS gateway reports."""


class ObsConnectError(ObsError):
    """OBS cannot be reached, or the websocket handshake failed."""


class ObsAuthError(ObsConnectError):
    """Authentication failed after re-reading OBS's config once (spec 17)."""


class ObsRequestError(ObsError):
    """OBS answered a request with a failure status."""

    def __init__(self, request: str, code: int, comment: str) -> None:
        super().__init__(f"{request} failed ({code}): {comment}")
        self.request = request
        self.code = code
        self.comment = comment


class ObsUnsupportedError(ObsError):
    """This OBS lacks a required request (spec 11.1)."""

    def __init__(self, obs_version: str, missing: tuple[str, ...]) -> None:
        super().__init__(f"OBS {obs_version} lacks {', '.join(missing)}")
        self.obs_version = obs_version
        self.missing = missing
```

- [ ] **Step 7: Implement `anki_miner_game/models/addons.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/models/addons.py`:

```python
"""Optional add-on state (spec 13.1, 14)."""

from enum import StrEnum


class AddonStatus(StrEnum):
    MISSING = "missing"
    INSTALLING = "installing"
    READY = "ready"
    BROKEN = "broken"
    """Installed files fail verification; reinstalling repairs it."""
```

- [ ] **Step 8: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/models/test_pipeline.py tests/models/test_messages_obs.py -q
```

Expected: `13 passed`.

- [ ] **Step 9: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/models/pipeline.py anki_miner_game/models/messages.py anki_miner_game/models/obs.py anki_miner_game/models/addons.py tests/models/test_pipeline.py tests/models/test_messages_obs.py && .venv/bin/ruff check anki_miner_game/models/pipeline.py anki_miner_game/models/messages.py anki_miner_game/models/obs.py anki_miner_game/models/addons.py tests/models/test_pipeline.py tests/models/test_messages_obs.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/models/pipeline.py anki_miner_game/models/messages.py anki_miner_game/models/obs.py anki_miner_game/models/addons.py tests/models/test_pipeline.py tests/models/test_messages_obs.py \
  && git commit -m "feat(models): add pipeline results, actor messages and OBS records"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 8: Protocols

**Files:**
- Create: `anki_miner_game/interfaces/text_source.py`, `record_clock.py`, `obs.py`, `presenter.py`, `session.py`, `addons.py` (all under `anki_miner_game/interfaces/`)
- Test: `tests/test_contracts.py` (this task writes it without the `paths` / `store` entries; Task 10 adds them)

**Interfaces:**
- Consumes: every `models` module above.
- Produces: `TextSource` (`id`, `status` properties; `start(sink: LineSink)`, `stop()`), `LineSink = Callable[[str, float, str], None]`; `RecordClock` (`start(zero_mono)`, `pause(at_mono)`, `resume(at_mono)`, `offset_ms(t_mono) -> int | None`); `ObsGateway` (`async connect() -> ObsInfo`, `async request(name, **fields) -> dict[str, Any]`, `subscribe(handler)`, `collection_changing` property, `async close()`); `ObsDiscovery` (`find_install() -> ObsInstall | None`, `config_root() -> Path | None`, `read_ws_config() -> WsConfig | None`, `ensure_server_enabled() -> bool`, `is_running() -> bool`, `launch()`, `async wait_ready(timeout_s=30.0) -> bool`, `credentials(cfg) -> ObsCredentials`); `Provisioner` (`async ensure_profile(cfg)`, `async ensure_collection(profile)` -> `ProvisionResult`; `async list_windows() -> list[WindowItem]`); `Recorder` (`async start()`, `async stop()`); `Presenter` (seven methods, one per `SessionEvent` kind plus `vad_progress`); `SessionControl` (`post(msg: SessionInput)`, `subscribe(cb)`, `state` property); `AddonService` (`status()`, `size_bytes` property, `async install(progress: ProgressCallback)`), `ProgressCallback = Callable[[int, int], None]`; `VadJobs` (`queue` / `rerun` / `restore` by manifest path); `OcrAreaPicker` (`async pick(window_title: str | None) -> str | None`).

The injected-time convention (`now: Callable[[], float] = time.monotonic`) is written into the `TextSource`, `ObsGateway` and `SessionControl` docstrings; `WebsocketSource`, `ClipboardSource`, `ObsClient`, the actor and `FakeHookerServer` all take it.
- [ ] **Step 1: Write the failing test `tests/test_contracts.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/test_contracts.py`:

```python
"""Every contract name from master-plan section 4 exists, and the contract packages keep the dependency rule."""

import ast
import importlib
from pathlib import Path

import pytest

import anki_miner_game

SECTION_4_NAMES = {
    "anki_miner_game.models.constants": [
        "SKIP_MS",
        "MIN_CUE_MS",
        "START_SHIFT_MS",
        "MAX_LINE_CHARS",
        "TYPEWRITER_WINDOW_S",
        "SCHEMA",
    ],
    "anki_miner_game.models.lines": ["GameLine", "TimedLine"],
    "anki_miner_game.models.cue": ["Cue", "Region"],
    "anki_miner_game.models.config": [
        "AppConfig",
        "ObsSettings",
        "TextSourceConfig",
        "FeedSettings",
        "RecordingSettings",
        "CueSettings",
        "VadSettings",
    ],
    "anki_miner_game.models.profile": [
        "GameProfile",
        "CaptureSettings",
        "AudioSettings",
        "FilterSettings",
        "OcrSettings",
        "AutoSettings",
        "TextMode",
        "validate",
    ],
    "anki_miner_game.models.manifest": [
        "SessionManifest",
        "ObsRecord",
        "ClockRecord",
        "DriftSample",
        "Counts",
        "LiveCue",
        "VadRecord",
        "FilesRecord",
        "ManifestState",
        "Flag",
        "to_json",
        "from_json",
    ],
    "anki_miner_game.models.pipeline": ["DropReason", "DROP_COUNTER", "Accepted", "Replaced", "Dropped"],
    "anki_miner_game.models.messages": [
        "LineReceived",
        "ObsEvent",
        "UserCommand",
        "Tick",
        "SessionEvent",
        "StateChanged",
        "LineAccepted",
        "RecordingStarted",
        "RecordingStopped",
        "SessionFinalised",
        "AppState",
        "SourceStatus",
        "Banner",
    ],
    "anki_miner_game.models.obs": [
        "ObsInfo",
        "REQUIRED_REQUESTS",
        "ObsCredentials",
        "WsConfig",
        "WindowItem",
        "ProvisionResult",
    ],
    "anki_miner_game.interfaces.text_source": ["TextSource"],
    "anki_miner_game.interfaces.record_clock": ["RecordClock"],
    "anki_miner_game.interfaces.obs": ["ObsGateway", "ObsDiscovery", "Provisioner", "Recorder"],
    "anki_miner_game.interfaces.presenter": ["Presenter"],
    "anki_miner_game.interfaces.session": ["SessionControl"],
    "anki_miner_game.interfaces.addons": ["AddonService", "VadJobs", "OcrAreaPicker"],
}

PROTOCOL_MEMBERS = {
    ("anki_miner_game.interfaces.text_source", "TextSource"): {"id", "start", "stop", "status"},
    ("anki_miner_game.interfaces.record_clock", "RecordClock"): {"start", "pause", "resume", "offset_ms"},
    ("anki_miner_game.interfaces.obs", "ObsGateway"): {"connect", "request", "subscribe", "collection_changing"},
    ("anki_miner_game.interfaces.obs", "ObsDiscovery"): {
        "find_install",
        "config_root",
        "read_ws_config",
        "ensure_server_enabled",
        "is_running",
        "launch",
        "wait_ready",
        "credentials",
    },
    ("anki_miner_game.interfaces.obs", "Provisioner"): {"ensure_profile", "ensure_collection", "list_windows"},
    ("anki_miner_game.interfaces.obs", "Recorder"): {"start", "stop"},
    ("anki_miner_game.interfaces.presenter", "Presenter"): {
        "state_changed",
        "source_status",
        "line_accepted",
        "banner",
        "banner_cleared",
        "session_finished",
        "vad_progress",
    },
    ("anki_miner_game.interfaces.session", "SessionControl"): {"post", "subscribe", "state"},
    ("anki_miner_game.interfaces.addons", "AddonService"): {"status", "size_bytes", "install"},
    ("anki_miner_game.interfaces.addons", "VadJobs"): {"queue", "rerun", "restore"},
    ("anki_miner_game.interfaces.addons", "OcrAreaPicker"): {"pick"},
}

PACKAGE_ROOT = Path(anki_miner_game.__file__).parent


@pytest.mark.parametrize(("module", "names"), sorted(SECTION_4_NAMES.items()))
def test_section_4_names_are_importable(module, names):
    mod = importlib.import_module(module)
    missing = [name for name in names if not hasattr(mod, name)]
    assert missing == []


@pytest.mark.parametrize(("key", "members"), sorted(PROTOCOL_MEMBERS.items()))
def test_protocols_declare_their_members(key, members):
    module, name = key
    protocol = getattr(importlib.import_module(module), name)
    assert members <= set(dir(protocol))


def _app_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: use absolute imports"
            found.append(node.module or "")
    return [name for name in found if name == "anki_miner_game" or name.startswith("anki_miner_game.")]


@pytest.mark.parametrize(
    ("package", "allowed"),
    [
        ("models", ("anki_miner_game.models.",)),
        ("interfaces", ("anki_miner_game.models.", "anki_miner_game.interfaces.")),
    ],
)
def test_contract_packages_keep_the_dependency_rule(package, allowed):
    offenders = [
        f"{path.name}: {name}"
        for path in sorted((PACKAGE_ROOT / package).glob("*.py"))
        for name in _app_imports(path)
        if not name.startswith(allowed)
    ]
    assert offenders == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_contracts.py -q
```

Expected: `17 failed, 11 passed`: every `interfaces` entry fails with `ModuleNotFoundError: No module named 'anki_miner_game.interfaces.text_source'` (or its sibling); the `models` entries and both dependency-rule tests pass.

- [ ] **Step 3: Implement `anki_miner_game/interfaces/text_source.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/text_source.py`:

```python
"""Text source Protocol (spec 8.1)."""

from collections.abc import Callable
from typing import Protocol

from anki_miner_game.models.messages import SourceStatus

LineSink = Callable[[str, float, str], None]
"""Called as ``sink(raw, t_mono, source_id)``."""


class TextSource(Protocol):
    """A producer of raw text lines.

    Implementations take ``now: Callable[[], float] = time.monotonic`` in their
    constructor and read ``t_mono = now()`` at frame receipt, before any
    queueing; nothing downstream re-stamps it. ``start`` returns at once. The
    sink is called on the source's own thread (the I/O loop for websocket
    sources, the Qt main thread for the clipboard), so the composition passes
    one that only posts ``LineReceived`` through ``SessionControl.post``.
    """

    @property
    def id(self) -> str: ...

    @property
    def status(self) -> SourceStatus: ...

    def start(self, sink: LineSink) -> None: ...

    def stop(self) -> None: ...
```

- [ ] **Step 4: Implement `anki_miner_game/interfaces/record_clock.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/record_clock.py`:

```python
"""Record clock Protocol (spec 7)."""

from typing import Protocol


class RecordClock(Protocol):
    """Maps a line's ``t_mono`` to its offset in the recording.

    All times are ``time.monotonic()`` values. ``offset_ms`` is clamped to be
    non-negative and non-decreasing across calls, so it is stateful; it
    returns ``None`` while paused (the line is dropped and counted).
    """

    def start(self, zero_mono: float) -> None: ...

    def pause(self, at_mono: float) -> None: ...

    def resume(self, at_mono: float) -> None: ...

    def offset_ms(self, t_mono: float) -> int | None: ...
```

- [ ] **Step 5: Implement `anki_miner_game/interfaces/obs.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/obs.py`:

```python
"""OBS Protocols: gateway, discovery, provisioning and recorder (spec 11)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsCredentials, ObsInfo, ObsInstall, ProvisionResult, WindowItem, WsConfig
from anki_miner_game.models.profile import GameProfile


class ObsGateway(Protocol):
    """One obs-websocket v5 connection with reconnect (spec 11.2).

    The implementation takes ``now: Callable[[], float] = time.monotonic``.
    Event callbacks run on obsws-python's thread; they only stamp ``now()``
    and hand an ``ObsEvent`` to the subscribed handlers, which only enqueue.
    After every successful connect or reconnect the handlers receive
    ``ObsEvent(ObsEventName.CONNECTED, {}, t)``, and ``CONNECTION_LOST`` when
    the connection drops. Credentials come from ``ObsDiscovery.credentials``
    at every connect; the password is never logged.
    """

    async def connect(self) -> ObsInfo:
        """``GetVersion`` plus the required-request check.

        Raises ``ObsConnectError``, ``ObsAuthError`` or ``ObsUnsupportedError``.
        """
        ...

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return its ``responseData`` (``{}`` when there is none).

        Waits while ``collection_changing`` is true. Raises ``ObsRequestError``
        when OBS reports failure and ``ObsConnectError`` when not connected.
        """
        ...

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None: ...

    @property
    def collection_changing(self) -> bool: ...

    async def close(self) -> None:
        """Disconnect and stop reconnecting."""
        ...


class ObsDiscovery(Protocol):
    """Finds, configures and starts the user's OBS (spec 11.1). Never logs a password."""

    def find_install(self) -> ObsInstall | None: ...

    def config_root(self) -> Path | None:
        """The config root of the found install (the Flatpak one for a Flatpak install); ``None`` without one."""
        ...

    def read_ws_config(self) -> WsConfig | None:
        """``plugin_config/obs-websocket/config.json``; ``None`` when the file does not exist."""
        ...

    def ensure_server_enabled(self) -> bool:
        """Turn the websocket server on while OBS is closed.

        Generates a password only when auth is required and none exists.
        Returns whether the server is enabled afterwards: ``False`` means it
        is off and OBS is running, so the file must be left alone.
        """
        ...

    def is_running(self) -> bool: ...

    def launch(self) -> None:
        """Start OBS minimised to the tray, in the folder it needs."""
        ...

    async def wait_ready(self, timeout_s: float = 30.0) -> bool:
        """``True`` once the websocket accepts connections; ``False`` after ``timeout_s``."""
        ...

    def credentials(self, cfg: AppConfig) -> ObsCredentials:
        """Host, port and password for the next connect, read from OBS's config each call; ``cfg.obs`` overrides."""
        ...


class Provisioner(Protocol):
    """Creates and updates the app's OBS profile and scene collection, touching only what differs (spec 11.3)."""

    async def ensure_profile(self, cfg: AppConfig) -> ProvisionResult: ...

    async def ensure_collection(self, profile: GameProfile) -> ProvisionResult: ...

    async def list_windows(self) -> list[WindowItem]: ...


class Recorder(Protocol):
    """``StartRecord`` / ``StopRecord``; changes no state itself (spec 11.4)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
```

- [ ] **Step 6: Implement `anki_miner_game/interfaces/presenter.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/presenter.py`:

```python
"""Presenter Protocol: everything the GUI is told (spec 4.2, 16)."""

from pathlib import Path
from typing import Protocol

from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import AppState, Banner, SourceStatus


class Presenter(Protocol):
    """GUI output. Callable from any thread; the Qt implementation re-emits each call as a signal."""

    def state_changed(self, state: AppState) -> None: ...

    def source_status(self, source_id: str, status: SourceStatus) -> None: ...

    def line_accepted(self, line: GameLine, offset_ms: int | None, replaces_previous: bool) -> None: ...

    def banner(self, banner: Banner) -> None: ...

    def banner_cleared(self, key: str) -> None: ...

    def session_finished(self, manifest_path: Path) -> None: ...

    def vad_progress(self, manifest_path: Path, done_ms: int, total_ms: int) -> None: ...
```

- [ ] **Step 7: Implement `anki_miner_game/interfaces/session.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/session.py`:

```python
"""SessionControl Protocol: the session actor as the rest of the app sees it (spec 4.2, 6)."""

from collections.abc import Callable
from typing import Protocol

from anki_miner_game.models.messages import AppState, SessionEvent, SessionInput


class SessionControl(Protocol):
    """The single-threaded session actor.

    The actor takes ``now: Callable[[], float] = time.monotonic`` and consumes
    one queue of ``SessionInput`` messages on the I/O loop.
    """

    def post(self, msg: SessionInput) -> None:
        """Enqueue a message; safe to call from any thread."""
        ...

    def subscribe(self, cb: Callable[[SessionEvent], None]) -> None:
        """Callbacks run on the actor's thread and must return quickly (post, do not block)."""
        ...

    @property
    def state(self) -> AppState: ...
```

- [ ] **Step 8: Implement `anki_miner_game/interfaces/addons.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/interfaces/addons.py`:

```python
"""Add-on Protocols: install services, VAD jobs and the OCR area picker (spec 13, 14)."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from anki_miner_game.models.addons import AddonStatus

ProgressCallback = Callable[[int, int], None]
"""Called as ``progress(done_bytes, total_bytes)``."""


class AddonService(Protocol):
    def status(self) -> AddonStatus: ...

    @property
    def size_bytes(self) -> int:
        """Approximate download size, shown by the wizard."""
        ...

    async def install(self, progress: ProgressCallback) -> None:
        """Download and verify; raises on failure and leaves no partial install behind."""
        ...


class VadJobs(Protocol):
    """VAD pass jobs keyed by manifest path. Each call only queues; progress goes to ``Presenter.vad_progress``."""

    def queue(self, manifest_path: Path) -> None: ...

    def rerun(self, manifest_path: Path) -> None: ...

    def restore(self, manifest_path: Path) -> None: ...


class OcrAreaPicker(Protocol):
    async def pick(self, window_title: str | None) -> str | None:
        """Run owocr's own picker; the selected rectangles text, or ``None`` when cancelled."""
        ...
```

- [ ] **Step 9: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_contracts.py -q
```

Expected: `28 passed`.

- [ ] **Step 10: Prove the strict flags apply**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/mypy --strict anki_miner_game/models anki_miner_game/interfaces
```

Expected: `Success: no issues found in 19 source files` (plus the harmless unused-section note).

- [ ] **Step 11: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/interfaces/text_source.py anki_miner_game/interfaces/record_clock.py anki_miner_game/interfaces/obs.py anki_miner_game/interfaces/presenter.py anki_miner_game/interfaces/session.py anki_miner_game/interfaces/addons.py tests/test_contracts.py && .venv/bin/ruff check anki_miner_game/interfaces/text_source.py anki_miner_game/interfaces/record_clock.py anki_miner_game/interfaces/obs.py anki_miner_game/interfaces/presenter.py anki_miner_game/interfaces/session.py anki_miner_game/interfaces/addons.py tests/test_contracts.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/interfaces/text_source.py anki_miner_game/interfaces/record_clock.py anki_miner_game/interfaces/obs.py anki_miner_game/interfaces/presenter.py anki_miner_game/interfaces/session.py anki_miner_game/interfaces/addons.py tests/test_contracts.py \
  && git commit -m "feat(interfaces): add Protocols for sources, clock, OBS, GUI, add-ons"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 9: paths

**Files:**
- Create: `anki_miner_game/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: `models.config.AppConfig`, `models.constants.INCOMING_DIRNAME`, `models.profile.is_safe_slug`.
- Produces: `HOME_ENV = "ANKI_MINER_GAME_HOME"`; `home() -> Path` (env var when set and non-empty, else `~/.anki_miner_game`; read at every call; creates nothing); `config_path()`; `games_dir()`; `profile_path(slug: str) -> Path` (`ValueError` for an unsafe slug); `output_root(cfg: AppConfig) -> Path` (expands `~`); `incoming_dir(cfg: AppConfig) -> Path`. Later tasks derive their own files from `home()` (log `<home>/anki_miner_game.log` in T16, `<home>/bin/` in T11, `<home>/addons/...` in T23/T24, `<home>/obs_restore.json` in T15).

`tests/conftest.py` (T00) already points `ANKI_MINER_GAME_HOME` at a per-test temporary folder, so these tests never see the real home.

- [ ] **Step 1: Write the failing test `tests/test_paths.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/test_paths.py`:

```python
from pathlib import Path

import pytest

from anki_miner_game import paths
from anki_miner_game.models.config import AppConfig


def test_home_follows_the_env_var_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "one"))
    assert paths.home() == tmp_path / "one"
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "two"))
    assert paths.home() == tmp_path / "two"


@pytest.mark.parametrize("value", [None, ""])
def test_home_defaults_to_dot_anki_miner_game(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ANKI_MINER_GAME_HOME", raising=False)
    else:
        monkeypatch.setenv("ANKI_MINER_GAME_HOME", value)
    assert paths.home() == Path.home() / ".anki_miner_game"


def test_home_is_not_created(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "absent"))
    paths.home()
    paths.config_path()
    paths.profile_path("g")
    assert not (tmp_path / "absent").exists()


def test_files_live_under_home():
    assert paths.config_path() == paths.home() / "config.json"
    assert paths.games_dir() == paths.home() / "games"
    assert paths.profile_path("steins-gate") == paths.home() / "games" / "steins-gate.json"


@pytest.mark.parametrize("slug", ["", "..", ".hidden", "a/b", "a\\b", "c:d"])
def test_profile_path_refuses_unsafe_slugs(slug):
    with pytest.raises(ValueError, match="cannot be a file name"):
        paths.profile_path(slug)


def test_output_root_expands_home_and_holds_incoming():
    cfg = AppConfig()
    assert paths.output_root(cfg) == Path.home() / "Videos" / "Anki Miner Game"
    assert paths.incoming_dir(cfg) == Path.home() / "Videos" / "Anki Miner Game" / "_incoming"


def test_output_root_keeps_an_absolute_path(tmp_path):
    cfg = AppConfig(output_root=str(tmp_path / "rec"))
    assert paths.output_root(cfg) == tmp_path / "rec"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_paths.py -q
```

Expected: `1 error` during collection, `ImportError: cannot import name 'paths' from 'anki_miner_game'`.

- [ ] **Step 3: Implement `anki_miner_game/paths.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/paths.py`:

```python
"""Where the app keeps its files. Every function reads the environment at call time; nothing is created here."""

import os
from pathlib import Path

from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.constants import INCOMING_DIRNAME
from anki_miner_game.models.profile import is_safe_slug

HOME_ENV = "ANKI_MINER_GAME_HOME"


def home() -> Path:
    """``$ANKI_MINER_GAME_HOME`` when set and non-empty, else ``~/.anki_miner_game``."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".anki_miner_game"


def config_path() -> Path:
    return home() / "config.json"


def games_dir() -> Path:
    return home() / "games"


def profile_path(slug: str) -> Path:
    """``<home>/games/<slug>.json``; ``ValueError`` for a slug that cannot be a file name."""
    if not is_safe_slug(slug):
        raise ValueError(f"slug {slug!r} cannot be a file name")
    return games_dir() / f"{slug}.json"


def output_root(cfg: AppConfig) -> Path:
    """``cfg.output_root`` with ``~`` expanded."""
    return Path(cfg.output_root).expanduser()


def incoming_dir(cfg: AppConfig) -> Path:
    """``<output root>/_incoming``, OBS's record directory in the app's profile (spec 10.2)."""
    return output_root(cfg) / INCOMING_DIRNAME
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_paths.py -q
```

Expected: `13 passed`.

- [ ] **Step 5: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/paths.py tests/test_paths.py && .venv/bin/ruff check anki_miner_game/paths.py tests/test_paths.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/paths.py tests/test_paths.py \
  && git commit -m "feat: add paths module honouring ANKI_MINER_GAME_HOME"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 10: store

**Files:**
- Create: `anki_miner_game/store.py`
- Test: `tests/test_store.py`
- Modify: `tests/test_contracts.py` (two entries in `SECTION_4_NAMES`)

**Interfaces:**
- Consumes: `paths.config_path` / `games_dir` / `profile_path`; `models.codec.dump_document` / `load_document` / `DecodeError` / `UnsupportedSchemaError`; `models.config.AppConfig`; `models.profile.GameProfile`, `validate`.
- Produces: `StoreError(path, message)` with `.path`; `CorruptFileError(StoreError)`; `FutureSchemaError(StoreError)` with `.found`; `InvalidProfileError(ValueError)` with `.problems: tuple[str, ...]`; `LoadedProfiles(profiles: Mapping[str, GameProfile], errors: tuple[StoreError, ...])`; `load_config() -> AppConfig` (defaults when the file is absent; `OSError` other than a missing file propagates); `save_config(cfg) -> Path`; `load_profiles() -> LoadedProfiles`; `save_profile(profile) -> Path`. Writes: temporary file `.<name>.*.tmp` in the target folder, `fsync`, `os.replace`; on any failure the temporary is removed and the old file is untouched.
- [ ] **Step 1: Write the failing test `tests/test_store.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/test_store.py`:

```python
import dataclasses
import json

import pytest

from anki_miner_game import paths, store
from anki_miner_game.models.config import AppConfig, CueSettings, ObsSettings
from anki_miner_game.models.profile import AudioMode, AudioSettings, GameProfile, TextMode


def _profile(slug: str = "steins-gate") -> GameProfile:
    return GameProfile(slug=slug, title="Steins;Gate", audio=AudioSettings(mode=AudioMode.DESKTOP))


def _write_config(text: str) -> None:
    paths.home().mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(text, encoding="utf-8")


def test_load_config_without_a_file_returns_defaults():
    assert store.load_config() == AppConfig()


def test_save_config_round_trips_with_schema():
    cfg = AppConfig(last_game="steins-gate", cue=CueSettings(max_cue_seconds=20))
    path = store.save_config(cfg)
    assert path == paths.home() / "config.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1
    assert store.load_config() == cfg


def test_store_follows_the_home_env_var_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MINER_GAME_HOME", str(tmp_path / "elsewhere"))
    path = store.save_config(AppConfig(last_game="x"))
    assert path == tmp_path / "elsewhere" / "config.json"
    assert store.load_config().last_game == "x"


def test_save_leaves_no_temporary_file():
    store.save_config(AppConfig())
    store.save_config(AppConfig(last_game="again"))
    assert [p.name for p in paths.home().iterdir()] == ["config.json"]


def test_failed_replace_keeps_the_old_file_and_removes_the_temporary(monkeypatch):
    store.save_config(AppConfig(last_game="old"))
    before = paths.config_path().read_bytes()

    def fail_replace(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk gone"):
        store.save_config(AppConfig(last_game="new"))
    assert paths.config_path().read_bytes() == before
    assert [p.name for p in paths.home().iterdir()] == ["config.json"]


def test_saved_text_is_utf8_with_lf_line_ends():
    store.save_config(AppConfig(output_root="~/ビデオ"))
    raw = paths.config_path().read_bytes()
    assert b"\r\n" not in raw
    assert "ビデオ".encode() in raw


def test_password_override_is_stored_only_when_set():
    store.save_config(AppConfig())
    assert json.loads(paths.config_path().read_text(encoding="utf-8"))["obs"]["password_override"] is None
    store.save_config(AppConfig(obs=ObsSettings(password_override="typed")))
    assert store.load_config().obs.password_override == "typed"


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"last_game": null}',
        '{"schema": "1"}',
        '{"schema": 1, "last_game": 5}',
        '{"schema": 1, "cue": {"max_cue_seconds": 99}}',
    ],
)
def test_corrupt_config_raises_corrupt_file_error(text):
    _write_config(text)
    with pytest.raises(store.CorruptFileError) as info:
        store.load_config()
    assert info.value.path == paths.config_path()


def test_non_utf8_config_is_corrupt():
    paths.home().mkdir(parents=True, exist_ok=True)
    paths.config_path().write_bytes(b'{"schema": 1, "last_game": "\xff"}')
    with pytest.raises(store.CorruptFileError, match="not UTF-8"):
        store.load_config()


def test_future_schema_config_raises_future_schema_error():
    _write_config('{"schema": 2, "brand_new": true}')
    with pytest.raises(store.FutureSchemaError) as info:
        store.load_config()
    assert info.value.found == 2
    assert info.value.path == paths.config_path()
    assert isinstance(info.value, store.StoreError)


def test_save_profile_writes_slug_json_with_schema():
    path = store.save_profile(_profile())
    assert path == paths.home() / "games" / "steins-gate.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == 1


def test_profiles_round_trip_and_bad_files_are_reported():
    first = _profile("a")
    second = dataclasses.replace(_profile("b"), text_mode=TextMode.OCR)
    store.save_profile(first)
    store.save_profile(second)
    (paths.games_dir() / "broken.json").write_text("{", encoding="utf-8")
    (paths.games_dir() / "future.json").write_text('{"schema": 2}', encoding="utf-8")
    loaded = store.load_profiles()
    assert loaded.profiles == {"a": first, "b": second}
    assert sorted((type(e).__name__, e.path.name) for e in loaded.errors) == [
        ("CorruptFileError", "broken.json"),
        ("FutureSchemaError", "future.json"),
    ]


def test_load_profiles_without_a_games_folder_is_empty():
    loaded = store.load_profiles()
    assert dict(loaded.profiles) == {}
    assert loaded.errors == ()


def test_save_profile_refuses_an_invalid_profile():
    bad = dataclasses.replace(_profile(), text_mode=TextMode.OCR, clipboard=True)
    with pytest.raises(store.InvalidProfileError) as info:
        store.save_profile(bad)
    assert info.value.problems == ("OCR mode cannot use the clipboard",)
    assert not paths.games_dir().exists()
```

- [ ] **Step 2: Name `paths` and `store` in the contract test**

In `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/tests/test_contracts.py`, insert these two lines at the end of the `SECTION_4_NAMES` dict, directly after the `"anki_miner_game.interfaces.addons": [...]` entry and before the closing `}`:

```python
    "anki_miner_game.paths": ["home"],
    "anki_miner_game.store": ["load_config", "save_config", "load_profiles", "save_profile"],
```

- [ ] **Step 3: Run them and watch them fail**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_store.py tests/test_contracts.py -q
```

Expected: `1 error` during collection, `ImportError: cannot import name 'store' from 'anki_miner_game'`.

- [ ] **Step 4: Implement `anki_miner_game/store.py`**

Full content of `/home/light/Projects/anki_miner_game/.worktrees/t01-contracts/anki_miner_game/store.py`:

```python
"""Load and save ``config.json`` and the game profiles under ``paths.home()`` (spec 5).

Every write goes to a temporary file in the same folder and is moved into place
with ``os.replace``. A file that does not describe its model raises
``CorruptFileError``; one written by a newer app raises ``FutureSchemaError``.
Callers turn both into banners.
"""

import contextlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from anki_miner_game import paths
from anki_miner_game.models.codec import DecodeError, UnsupportedSchemaError, dump_document, load_document
from anki_miner_game.models.config import AppConfig
from anki_miner_game.models.profile import GameProfile, validate


class StoreError(Exception):
    """A stored document cannot be used; ``path`` names the file."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


class CorruptFileError(StoreError):
    """Not UTF-8, not JSON, or not a valid document for its model."""


class FutureSchemaError(StoreError):
    """Written by a newer version of the app."""

    def __init__(self, path: Path, found: int) -> None:
        super().__init__(path, f"schema {found} is newer than this app supports")
        self.found = found


class InvalidProfileError(ValueError):
    """``save_profile`` refused a profile that ``validate`` rejects."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class LoadedProfiles:
    profiles: Mapping[str, GameProfile]
    """Keyed by slug."""
    errors: tuple[StoreError, ...]
    """One per file that could not be loaded; those files are skipped."""


def load_config() -> AppConfig:
    """The saved config, or the defaults when none has been saved yet."""
    try:
        return _read(AppConfig, paths.config_path())
    except FileNotFoundError:
        return AppConfig()


def save_config(cfg: AppConfig) -> Path:
    path = paths.config_path()
    _write_atomic(path, dump_document(cfg))
    return path


def load_profiles() -> LoadedProfiles:
    """Every ``<home>/games/*.json``; a file that fails to load is reported in ``errors``, not raised."""
    profiles: dict[str, GameProfile] = {}
    errors: list[StoreError] = []
    folder = paths.games_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            try:
                profile = _read(GameProfile, path)
            except StoreError as exc:
                errors.append(exc)
            else:
                profiles[profile.slug] = profile
    return LoadedProfiles(profiles=profiles, errors=tuple(errors))


def save_profile(profile: GameProfile) -> Path:
    """Write ``<home>/games/<slug>.json``; raises ``InvalidProfileError`` when ``validate`` finds problems."""
    problems = validate(profile)
    if problems:
        raise InvalidProfileError(problems)
    path = paths.profile_path(profile.slug)
    _write_atomic(path, dump_document(profile))
    return path


def _read[T](cls: type[T], path: Path) -> T:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptFileError(path, f"not UTF-8 ({exc.reason})") from exc
    try:
        return load_document(cls, text)
    except UnsupportedSchemaError as exc:
        raise FutureSchemaError(path, exc.found) from exc
    except DecodeError as exc:
        raise CorruptFileError(path, str(exc)) from exc


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
```

- [ ] **Step 5: Run them and watch them pass**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/pytest -n0 -p no:cacheprovider tests/test_store.py tests/test_contracts.py -q
```

Expected: `49 passed`.

- [ ] **Step 6: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/black --check -q anki_miner_game/store.py tests/test_store.py tests/test_contracts.py && .venv/bin/ruff check anki_miner_game/store.py tests/test_store.py tests/test_contracts.py && .venv/bin/mypy anki_miner_game \
  && git add anki_miner_game/store.py tests/test_store.py tests/test_contracts.py \
  && git commit -m "feat: add atomic JSON store for config and game profiles"
```

Expected: black and ruff silent, mypy `Success: no issues found`, one new commit.


### Task 11: gate, evidence, status

**Files:** none created. `gate.log` stays in the worktree (gitignored) as evidence.

- [ ] **Step 1: Run the Definition-of-Done gate**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash scripts/health.sh > /home/light/Projects/anki_miner_game/.worktrees/t01-contracts/gate.log 2>&1; echo "gate exit=$?"; grep -E '^(PASS|FAIL) |passed|failed|all green|FAILED:' /home/light/Projects/anki_miner_game/.worktrees/t01-contracts/gate.log
```

Expected: `gate exit=0`; `PASS black`, `PASS ruff`, `PASS mypy`, `PASS pytest`, `173 passed`
(6 from the scaffold + 167 from this task), `all green`. Read `gate.log` itself; do not pipe it through
`tail`. A red step is fixed on this branch with a `fix:` commit and the gate re-run; never report
done on a red gate.

- [ ] **Step 2: Record the strict-typing evidence**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && .venv/bin/mypy --strict anki_miner_game/models anki_miner_game/interfaces && git log --oneline 963dbf90b25bb707fe7a1c749393faece3f415f3..HEAD && git status --short
```

Expected: `Success: no issues found in 19 source files`; eleven commits on top of BASE (the plan
commit plus Tasks 1-10); `git status --short` empty (only the ignored `gate.log` is new).

- [ ] **Step 3: Update the status file**

Rewrite `/home/light/Projects/anki_miner_game/.orchestration/status/t01-contracts.json`, keeping
`base_sha`, with `stage` `implemented`, `head_sha` from `git rev-parse HEAD`, and `gate_exit` `0`:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t01-contracts && /home/light/Projects/anki_miner_game/.venv/bin/python - <<'PY'
import json
import subprocess
from pathlib import Path

status = Path("/home/light/Projects/anki_miner_game/.orchestration/status/t01-contracts.json")
data = json.loads(status.read_text(encoding="utf-8"))
data.update(
    stage="implemented",
    head_sha=subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
    gate_exit=0,
)
status.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(data)
PY
```

Expected: the printed dict keeps `base_sha` `963dbf90b25bb707fe7a1c749393faece3f415f3` and shows the
new `head_sha`.

## Acceptance map

Card requirement -> the test that proves it.

| T01 card / orchestrator note | Test |
|---|---|
| JSON round trip with `schema`: `AppConfig` | `tests/models/test_config.py::test_json_round_trip_with_schema`, `tests/test_store.py::test_save_config_round_trips_with_schema` |
| JSON round trip with `schema`: `GameProfile` | `tests/models/test_profile.py::test_json_round_trip_with_schema`, `tests/test_store.py::test_save_profile_writes_slug_json_with_schema` |
| JSON round trip with `schema`: `SessionManifest` | `tests/models/test_manifest.py::test_spec_example_parses`, `test_spec_example_round_trips`, `test_recording_manifest_defaults_and_round_trip` |
| Validation rejects hook sources in OCR mode | `tests/models/test_profile.py::test_ocr_mode_rejects_hook_sources` |
| Validation rejects the clipboard in OCR mode | `tests/models/test_profile.py::test_ocr_mode_rejects_clipboard` |
| Validation rejects `audio.mode="app"` without `capture.window` | `tests/models/test_profile.py::test_app_audio_needs_a_window` |
| `max_cue_seconds` 5-60 | `tests/models/test_config.py::test_max_cue_seconds_accepts_5_to_60`, `test_max_cue_seconds_rejects_outside_5_to_60`, `test_out_of_range_cue_in_json_is_a_decode_error` |
| Platform defaults | `tests/models/test_profile.py::test_platform_defaults`, `test_spec_defaults` |
| Frozen + `replace` | `test_frozen_and_replace` in `test_profile.py` and `test_config.py`; `test_game_line_fields_and_frozen`; `test_cue_replace_revalidates`; `test_manifest.py::test_frozen` |
| `DROP_COUNTER` maps every reason to a `Counts` field | `tests/models/test_pipeline.py::test_every_drop_reason_maps_to_a_counts_field`, `test_empty_lines_count_under_no_letters` |
| Store writes atomically | `tests/test_store.py::test_save_leaves_no_temporary_file`, `test_failed_replace_keeps_the_old_file_and_removes_the_temporary` |
| Store honours `ANKI_MINER_GAME_HOME` | `tests/test_store.py::test_store_follows_the_home_env_var_at_call_time`, `tests/test_paths.py::test_home_follows_the_env_var_at_call_time` |
| Corrupt or future-schema file raises a typed error | `tests/test_store.py::test_corrupt_config_raises_corrupt_file_error`, `test_non_utf8_config_is_corrupt`, `test_future_schema_config_raises_future_schema_error`, `test_profiles_round_trip_and_bad_files_are_reported` |
| Every section 4 name importable | `tests/test_contracts.py::test_section_4_names_are_importable`, `test_protocols_declare_their_members` |
| mypy strict on `models/` and `interfaces/` | the `pyproject.toml` override (every gate run) and Task 11 Step 2 |
| Protocols import only from `models`; `models` imports nothing else from the app | `tests/test_contracts.py::test_contract_packages_keep_the_dependency_rule` |
| Password never logged | `test_password_override_never_appears_in_repr`, `test_passwords_never_appear_in_repr` |

## Out of scope

Module functions named in section 4 for later tasks (`session.cues.build_cues`,
`session.srt_writer.*`, `session.clock.*`, `session.naming.*`, `text.pipeline.TextPipeline`,
`session.journal.*`, `session.manifest.*`, `session.finalise.finalise`, `vad.assign.assign`,
`feed.FeedServer`, `addons.bootstrap.ensure_uv`) belong to T02-T11 and are not stubbed here: an
empty stub would pass `test_contracts.py` while lying about behaviour. No `models/__init__.py`
re-exports. No quarantine or backup of a corrupt `config.json` (T16 decides what to show and
whether to keep defaults for the run).
