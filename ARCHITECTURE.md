# Architecture

> Describes Anki Miner Game v1.0.0 (2026-09-22). Names and file lists were taken from the tree at that release; recount before relying on one.

Anki Miner Game is a PyQt6 desktop application that sits in the tray beside a game. While a game is armed it drives OBS over obs-websocket v5, stamps every line a text source delivers with the monotonic time it arrived, and turns a recording into the `<Game>/<Game> - NN.mkv` + `.srt` pair Anki Miner mines like an anime episode. Anki Miner itself is never modified: the pair is its ordinary input.

## Session Flow

```
text hooker / clipboard / owocr              OBS (obs-websocket v5)
             |                                        |
             v                                        v
  text/sources/*  (t_mono stamped at receipt)   obs/client.py  ObsClient
             |                                        |  events enqueued with their t_mono
             v                                        v
  text/pipeline.py  TextPipeline  ------->  session/session.py  SessionActor
     (clean, drop, merge)          lines     (one queue, one thread: the I/O loop)
                                              |            |              |
                              session/clock.py        journal        feed/ (text feed)
                              t_mono -> offset_ms   <obs stem>.lines.jsonl
                                              |
                                          STOPPED
                                              v
                              session/finalise.py  finalise()  (on the FinaliseWorker)
                              journal -> session/cues.py build_cues() -> srt_writer
                              rename into <Game>/<Game> - NN.{mkv,srt,session.json}
                                              |
                                              v  (VAD add-on installed and enabled)
                              vad/trimmer.py VadTrimmer -> vad_worker.py subprocess
                              -> vad/assign.py assign() -> rewritten .srt
```

A recording is a session only if it starts while the app is armed. OBS is then on the app's own profile and record folder by construction, whoever pressed Start (the app, the tray, the hotkey, a CLI verb or OBS's own window). A `STARTED` event in any other state is ignored, so the app never touches a recording from the user's own setup.

## Package Dependencies

```
anki_miner_game/
  models/        frozen dataclasses, no I/O
  interfaces/    Protocols: TextSource, RecordClock, ObsGateway, ObsDiscovery, Provisioner,
                 Recorder, Presenter, SessionControl, AddonService, VadJobs, OcrAreaPicker
  text/          sources/ (websocket, clipboard, ocr) and pipeline.py
  obs/           discovery, client, provision, recorder
  session/       clock, cues, srt_writer, naming, manifest, journal, finalise, restore, session (the actor)
  lifecycle/     auto.py
  vad/           trimmer.py, assign.py, model_pin.py, worker/vad_worker.py
  addons/        bootstrap.py, vad_addon.py, ocr_addon.py
  feed/          ws_server.py, http_server.py, page.html
  gui/           main_window, tray, wizard, game_profile_dialog, settings_dialog, hotkey_win,
                 cli_verbs, presenters/, widgets/
  runtime/       io_thread, child_env, ca_bundle, bundle_smoke
  paths.py, store.py, app.py, launch.py
```

Rules:

- `models` imports nothing from the app.
- `session`, `text`, `obs`, `vad`, `feed` and `lifecycle` never import `gui`.
- `gui` reaches the rest only through `interfaces`.
- `session/cues.py` and `vad/assign.py` are pure functions: no clock, file or network access.
- Composition lives in `app.py` (`App`); there is no DI container.

`models/` and `interfaces/` are type-checked strictly and treated as contracts: a change there is a deliberate, reviewed change, not a side effect of a feature.

## Core Abstractions

| Protocol | Module | Implemented by |
|---|---|---|
| `TextSource` | `interfaces/text_source.py` | `WebsocketSource`, `ClipboardSource`, `OcrSource` |
| `RecordClock` | `interfaces/record_clock.py` | `EventClock`, `OutputDurationClock` |
| `ObsGateway` | `interfaces/obs.py` | `ObsClient` |
| `ObsDiscovery` | `interfaces/obs.py` | `LocalObsDiscovery` |
| `Provisioner` | `interfaces/obs.py` | `ObsProvisioner` |
| `Recorder` | `interfaces/obs.py` | `ObsRecorder` |
| `SessionControl` | `interfaces/session.py` | `SessionActor` |
| `Presenter` | `interfaces/presenter.py` | `QtPresenter` |
| `AddonService` | `interfaces/addons.py` | `VadAddon`, `OcrAddon` |
| `VadJobs` | `interfaces/addons.py` | `VadTrimmer` |

The fakes in `tests/fakes/` stand in for the outside world (OBS, a hooker, owocr, uv, the VAD worker) at the network or process boundary, so the real implementations above run in tests.

## Models

`models/` holds frozen dataclasses and `StrEnum`s, converted to and from JSON by `models/codec.py`:

- `config.py` - `AppConfig`, stored as `<home>/config.json`.
- `profile.py` - `GameProfile`, stored as `<home>/games/<slug>.json`.
- `messages.py` - messages into the actor (`LineReceived`, `ObsEvent`, `UserCommand`, `Tick`), events out of it (`StateChanged`, `LineAccepted`, `RecordingStarted`, `RecordingStopped`, `SessionFinalised`, `SourceStatusChanged`, `BannerRaised`, `BannerCleared`), and `AppState`.
- `manifest.py` - `<stem>.session.json`.
- `lines.py`, `cue.py`, `pipeline.py`, `obs.py`, `addons.py`, `constants.py`.

## Threads

| Thread | Owns |
|---|---|
| Qt main | widgets, tray, dialogs, the presenter's signals, the clipboard source (`QClipboard` must live here), the Windows hotkey, the CLI single-instance server |
| I/O (one asyncio loop in `runtime/io_thread.py` `IoThread`) | the session actor, OBS requests, websocket sources, the owocr supervisor, auto mode, the feed's websocket server |
| obsws-python event thread | OBS event callbacks; they only enqueue onto the actor |
| feed HTTP | stdlib `http.server`, daemon thread |
| VAD job | one worker subprocess at a time, read by the trimmer's job thread |

The session actor is single-threaded: one queue of `LineReceived`, `ObsEvent`, `UserCommand` and `Tick` messages, consumed on the I/O loop. Every state change happens there, which makes a session deterministic and testable with an injected clock. The GUI observes through `QtPresenter`, which re-emits each call as a Qt signal; slots run on the main thread. Dialogs and the wizard hand coroutines to the loop through `IoThread.submit`.

A line's arrival time is `time.monotonic()`, read inside the source at frame receipt, before any queueing. Nothing downstream re-stamps it.

## Session States

```
idle --arm--> armed --STARTED--> recording --STOPPED--> finalising --> armed
  ^             |
  +---disarm----+
```

| State | Meaning |
|---|---|
| `idle` | no game armed; OBS untouched |
| `armed` | a game is selected; OBS is on the app's profile and scene collection; sources are connected |
| `recording` | OBS's record output is active and owned by the app |
| `finalising` | cues computed, files renamed, VAD job queued |

Arming refuses while any OBS output is active (stream, recording, replay buffer, virtual camera), writes the user's current profile and scene collection to `<home>/obs_restore.json` (`session/restore.py`), then switches OBS, waiting for each `...Changed` event. Disarm switches back and deletes the file. The actor keys every record transition on `RecordStateChanged.outputState`, and runs a reconcile after every connect and reconnect before it trusts any event.

## Record Clock

`session/clock.py` maps a line's `t_mono` to its offset in the recording, or `None` while paused.

- `EventClock` (primary): zero at the `STARTED` event, minus paused time, plus a fixed 10 ms capture latency. Pause edges come only from OBS's paused and resumed events; the app never sends `PauseRecord`.
- `OutputDurationClock` (fallback): anchors on `GetRecordStatus.outputDuration` and re-anchors every 10 s. Reconcile switches to it when the event timeline was lost: the app restarted mid-session, or a pause edge was missed while disconnected. The manifest is flagged `clock_degraded`.

Provisioning gives the app's profile a recording encoder separate from the stream encoder, because a recording that shares the stream encoder cannot pause.

## Text Intake

Sources (`text/sources/`) deliver raw lines with their arrival time:

- `WebsocketSource` - a hooker's websocket server (Textractor's extension, Agent, LunaTranslator, GameSentenceMiner-style JSON). The app listens beside the user's texthooker page; it takes nothing away from it.
- `ClipboardSource` - `QClipboard`, on the main thread.
- `OcrSource` - owocr's websocket, with owocr run and restarted by a supervisor.

`text/pipeline.py` `TextPipeline` then normalises (NFC, control and zero-width characters, whitespace, an optional leading speaker tag: `【…】`, or `name: ` before an opening quote) and drops empty, letterless, over-long and duplicate lines, counting each drop in the manifest. An optional typewriter merge folds a line that is typed out gradually into one. An accepted line is journalled, broadcast to the text feed and shown in the live list.

## Files and Crash Safety

While recording, everything lives in `<output root>/_incoming/`, OBS's record folder in the app's profile:

```
<obs stem>.mkv              written by OBS
<obs stem>.session.json     written at STARTED: game, reserved NN, state "recording"
<obs stem>.lines.jsonl      journal, append-only, flushed per line
```

`session/journal.py` `Journal` appends `line`, `replace`, `pause`, `resume` and `stop` records. Every accepted line is durable the moment it arrives; cue ends are never journalled, because they are a pure function of the journal.

`session/finalise.py` `finalise()` is one idempotent routine used by a normal stop, by reconcile and at launch: read the journal, `build_cues()`, write the `.srt` atomically, record `live_cues` in the manifest, rename video, subtitle and manifest to `<Game>/<Game> - NN.*`, delete the journal, queue the VAD job. Each step checks whether it already happened, so a crash between any two steps is repaired by running it again. On Windows, where OBS can hold the video briefly after `STOPPED`, the rename is retried; if it still fails the manifest says `finalise_pending` and the next launch retries. All finalising runs on one `FinaliseWorker`, one session at a time, because reserving NN is check-then-act.

`session/naming.py` sanitises the title so Anki Miner reads every stem as exactly episode NN with no season: it simulates the token deletion Anki Miner's episode extractor does and puts a `~` wherever a ` - ` or an `S1E2`-like pattern would otherwise be read. `tests/contract/` runs Anki Miner's own matcher, vendored, against it.

`session/cues.py` `build_cues()` starts each cue a fixed shift before its line arrived (`START_SHIFT_MS`: 0.4 s in hook mode, 1.25 s in OCR mode) and ends it shortly before the next kept line (`end_gap_ms`), capped at `max_cue_seconds`; lines shown for under 300 ms (skip mode) are dropped. `session/srt_writer.py` writes UTF-8 without BOM, via a temporary file and `os.replace`.

## OBS

- `obs/discovery.py` `LocalObsDiscovery` - finds OBS (Windows install paths and registry; Linux `obs` on PATH, then the Flathub build), reads obs-websocket's own `config.json` for the port and password at every connect, turns the websocket server on when OBS is closed, and starts OBS minimised. The password is never logged and never stored unless the user types an override.
- `obs/client.py` `ObsClient` - one obs-websocket v5 connection with reconnect. Requests go out from one thread; event callbacks only enqueue onto the actor with their arrival time. It waits out OBS's "not ready" answers and scene-collection changes.
- `obs/provision.py` `ObsProvisioner` - creates and updates the `Anki Miner Game` profile and scene collection (record folder, `.mkv`, no splitting or remux, separate recording encoder, capture inputs) and lists capturable windows. Idempotent; runs from the wizard and again at each arm, touching only what differs.
- `obs/recorder.py` `ObsRecorder` - `StartRecord` and `StopRecord`, nothing else. State follows OBS's events, so a recording started from OBS's window behaves identically.

`lifecycle/auto.py` `AutoMode` subscribes to the actor's events and sends it ordinary commands: start at the first line while armed, stop after idle minutes or when the pinned window has been closed for two readings. No other module knows auto mode exists.

## Add-ons

Both add-ons download on demand into `<home>/addons/<name>/`, built with a sha256-pinned `uv` that `addons/bootstrap.py` fetches into `<home>/bin/`. Every uv call keeps its Python, cache and tool folders inside the add-on's own folder.

- **VAD** (`addons/vad_addon.py`, `vad/`) - a uv-built environment with onnxruntime, numpy and PyAV at hash-pinned versions, plus the Silero model pinned in `vad/model_pin.py`. After finalise, `VadTrimmer` runs `vad/worker/vad_worker.py` under that Python, reads the voiced regions it prints, and `vad/assign.py` `assign()` fits the manifest's `live_cues` to them. The subtitle is always rewritten from `live_cues`, so a re-run or a restore never leaves a second `.srt`. If the add-on is missing or fails, the live subtitle stands.
- **OCR** (`addons/ocr_addon.py`) - owocr installed as a uv tool and run as a managed subprocess; `OcrSource` reads its websocket, so OCR is one more text source and none of owocr's code enters the app. Linux OCR is X11-only.

## Text Feed

`feed/` serves every accepted line to this machine only: `WsFeedServer` sends each line as a plain-text frame (so texthooker pages can connect), and `HttpFeedServer` serves `page.html`, a dependency-free page that shows the lines for Yomitan lookups. Two ports, because `websockets` does not serve HTTP. A port in use turns the feed off for that run with a banner.

## Configuration and Entry Point

`launch.py` is the entry point: `anki_miner_game [--arm <slug> | --start | --stop | --toggle]`. With an instance already running, `gui/cli_verbs.py` sends the verb (or "show your window") to it over a `QLocalServer` and exits; otherwise the process becomes the instance, sets up logging and starts `App`. `store.py` loads and saves `config.json` and the game profiles with atomic writes, refusing a file written by a newer schema. `paths.py` resolves every location at call time, so `ANKI_MINER_GAME_HOME` redirects all of it.

## Data Storage

```
~/.anki_miner_game/                 (or $ANKI_MINER_GAME_HOME)
  config.json                       AppConfig
  games/<slug>.json                 one GameProfile per game
  obs_restore.json                  while armed: the user's OBS profile and scene collection
  anki_miner_game.log               rotating log (.1 to .3)
  bin/                              the pinned uv
  addons/vad/, addons/ocr/          add-on environments

<output root>/                      default ~/Videos/Anki Miner Game
  _incoming/                        OBS's record folder: live recordings, journals, pending manifests
  <Game>/<Game> - NN.mkv|.srt|.session.json
```

## Not covered here

- The full design, with every decision and its evidence, lives in the maintainer's local design spec, not in the repository.
- Packaging (`anki_miner_game.spec`, `packaging/`) and the release workflow: see `.github/workflows/release.yml` and `scripts/release_dryrun.sh`.
- Test layout, markers and isolation: see [CONTRIBUTING.md](CONTRIBUTING.md).
