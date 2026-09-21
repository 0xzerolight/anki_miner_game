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
