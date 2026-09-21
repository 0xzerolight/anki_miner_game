# Contract ledger

Names and shapes in `anki_miner_game/models/` and `anki_miner_game/interfaces/` that go beyond
master plan section 4. Section 4 stays the base list; this file records every accepted addition.
Changes after W0 go through a `CONTRACT-CHANGE-REQUEST` (see `CLAUDE.md`) and are appended here.

## W0 (T01 contracts, accepted by the orchestrator)

| Name | Where | What it is |
|---|---|---|
| `Presenter.vad_finished(manifest_path, state: VadState)` | `interfaces/presenter.py` | Called once per VAD job (done, failed, unavailable, restored); the row re-reads the manifest |
| `ObsGateway.close()` | `interfaces/obs.py` | Async; disconnects and stops reconnecting |
| `ObsInstall(argv, cwd, flatpak)` | `models/obs.py` | What `ObsDiscovery.find_install` returns: how to start OBS |
| `AddonStatus` | `models/addons.py` | `missing`, `installing`, `ready`, `broken`; returned by `AddonService.status()` |
| `ObsError` hierarchy | `models/obs.py` | `ObsError` > `ObsConnectError` > (`ObsAuthError`, `ObsConfigError`); `ObsError` > `ObsRequestError(request, code, comment)`, `ObsUnsupportedError(obs_version, missing)` |
| `ObsEventName` | `models/obs.py` | The OBS events the actor handles, plus the gateway's synthetic `_Connected` (after every connect or reconnect) and `_ConnectionLost` |
| `SourceStatusChanged`, `BannerRaised`, `BannerCleared` | `models/messages.py` | `SessionEvent` members; forwarded to `Presenter.source_status`, `banner`, `banner_cleared` |
| `LineAccepted.replaces_previous` | `models/messages.py` | Typewriter merge: the text replaces the previous accepted line |
| `AutoSettings.enabled` | `models/profile.py` | Per-game auto-mode switch, default `false` (spec 5 amendment pending at the M0 gate) |

## W0 hardening

| Name | Where | What it is |
|---|---|---|
| `ObsConfigError(ObsConnectError)` | `models/obs.py` | `ObsDiscovery.credentials` raises it when no port is known: OBS's websocket `config.json` is missing or unreadable and `cfg.obs.port` is `None`. `read_ws_config` raises it for a file that exists but cannot be read or parsed (a missing file is still `None`) |
| `TextSource.set_status_listener(cb)` | `interfaces/text_source.py` | `cb(source_id, status)` (`StatusListener`) on every status transition, after `status` reports it, on the sink's thread; one listener, set before `start`. The session actor registers it and publishes each change as `SourceStatusChanged` |
| `VadSettings.enabled` (docstring only) | `models/config.py` | Effective value is `enabled and <VAD add-on installed>`, enforced by the VAD add-on; no behaviour change |

## W1 integration (accepted by the orchestrator)

Filed by the wave 1 integration fix (`.orchestration/status/wave-1-integration-fix.json`), applied by C1.

| Name | Where | What it is |
|---|---|---|
| `Presenter.vad_progress(manifest_path, done_ms: int, total_ms: int \| None)` | `interfaces/presenter.py` | `None` = indeterminate progress: the worker sends `total_ms: null` for a recording with no duration (a crash-truncated `.mkv`, spec 6.4) |
| `ObsDiscovery.wait_ready`, `ObsGateway.connect`, `ObsGateway.request` (docstrings) | `interfaces/obs.py` | Ready = `GetVersion` succeeds, not "accepts connections"; `connect` and `request` retry 207 `NotReady` until a timeout, then raise `ObsRequestError` (S1 summary 5 and 11) |
| `WindowItem.enabled: bool` | `models/obs.py` | `itemEnabled` from `GetInputPropertiesListPropertyItems`; `False` on the configured value OBS keeps listing when no live window matches it (S1 summary 12) |
| `Replaced` (docstring) | `models/pipeline.py` | The merge's base is the previous line accepted since the last `TextPipeline.reset()`; the actor journals a `ReplaceRecord` only when that line is the journal's last `LineRecord` |

## T24 OCR add-on (accepted by the orchestrator)

Filed by T24 (`.orchestration/status/t24-ocr-addon.json`), applied by C2.

| Name | Where | What it is |
|---|---|---|
| `AddonService.note -> str \| None` | `interfaces/addons.py` | Property: a platform limitation shown beside the status, or `None`. The OCR add-on on Linux says OCR needs an X11 session; the VAD add-on returns `None` |
| `OcrAreaPicker.pick` (docstring) | `interfaces/addons.py` | Raises `RuntimeError` (the add-on's `OcrError`) with a message fit for the dialog when owocr cannot run or exits without an answer |

## W2a integration (fix round 1)

Found by the wave 2a cross-task reviews (`.orchestration/reviews/wave-2a-cross-*.md`), applied by
the integration fixer on `integration/wave-2a`.

| Name | Where | What it is |
|---|---|---|
| `TextSource.wait_closed()` | `interfaces/text_source.py` | Async; returns once what the last `stop` began has finished (owocr's tree dead). The actor awaits it at disarm, shutdown before the I/O loop stops. `ClipboardSource` returns at once |
| `TextSource` (docstring) | `interfaces/text_source.py` | `start`, `stop` and `wait_closed` are called on the actor's thread; a source living elsewhere moves the call there itself (the clipboard source, to the Qt main thread) |
| `UserCommand.line: GameLine \| None` | `models/messages.py` | `START` from auto mode only: the line that triggered it, journalled at offset 0 on `STARTED`; a `START` without it holds nothing |
| `StateChanged.slug: str \| None` | `models/messages.py` | The armed game in every state but `idle`; a re-arm to another game publishes `StateChanged(ARMED, <new slug>)`. Required field |
| `Presenter.state_changed(state, slug)` | `interfaces/presenter.py` | Forwards `StateChanged.slug`, so the game dropdown follows a CLI `--arm` |
| `START_FAILED_BANNER_KEY` | `models/messages.py` | `"start_failed"`: `Banner.key` of the `StartRecord` failure banner; auto mode lets the next line start again on it |
| `Provisioner.list_windows` (docstring) | `interfaces/obs.py` | Windows reads the `game_capture` input's `window` (never `window_capture`, which drops minimized windows), X11 `xcomposite_input` `capture_window`; `[]` with no such input; raises only `ObsError` |
| `AddonService.install` (docstring) | `interfaces/addons.py` | No-op while `READY`; raises `RuntimeError`, also when an install is already running; failure or cancellation stops the work and removes the attempt; `progress` may come on any thread |
| `VadJobs`, `Presenter` (docstrings) | `interfaces/addons.py`, `interfaces/presenter.py` | Every job that starts ends with `vad_finished`; skipped and shutdown-dropped jobs get no call. A manifest found at launch `vad_running` or `vad.state` `queued` is not live: the app passes it to `rerun` once |

