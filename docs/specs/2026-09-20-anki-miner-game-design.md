# Anki Miner Game: design

Status: design approved 2026-09-20. No code exists. Implementation happens in a new repository,
`anki_miner_game`. This file is the input to that repository's first implementation plan.
Amended at the M0 gate on 2026-09-21 with what the spikes found (`docs/m0/*.md`) and the
orchestrator's rulings on them. The spikes ran on Linux; every Windows-derived value is marked
provisional until the Windows checks of pre-release QA (H5, owner decision D2).

Working name "Anki Miner Game". A standalone desktop app that records a video-game session through
OBS and writes a subtitle file from text-hooker lines, so that Anki Miner can mine games the way it
mines an anime episode.

Contents: 1 Goal, 2 Decisions, 3 External contracts, 4 Architecture, 5 Data models, 6 Session state,
7 Record clock, 8 Text intake, 9 Cue rules, 10 Files and crash safety, 11 OBS, 12 Auto mode,
13 VAD pass, 14 OCR add-on, 15 Text feed, 16 UI and control, 17 Error matrix, 18 Testing,
19 Packaging, 20 Milestones, 21 Risks, 22 GSM reuse map, Appendices A-C.

## 1. Goal

One job: turn a play session into

```
<output root>/<Game>/<Game> - NN.mkv
<output root>/<Game>/<Game> - NN.srt
<output root>/<Game>/<Game> - NN.session.json
```

The `.mkv` and `.srt` share a stem and a folder. That is the whole contract with Anki Miner: the
user picks the video in Video -> Single and the subtitle auto-fills, or points Video -> Batch at the
game folder and every session pairs by number. Anki Miner is never modified.

### Why this exists

GameSentenceMiner (GSM) already has the workflow as an experimental toggle called Longplay
(`GameSentenceMiner/longplay_handler.py`, 310 lines). Reaching it means installing GSM: 336 Python
files and about 144k lines, an Electron shell, a Rust extension, around 20 settings tabs, and a
setup path that assumes Anki, AnkiConnect and Yomitan. Its subtitle timing is coarse: a cue ends
when the next line arrives minus one whole second, seconds are floored, and nothing detects where
the voice stops. Anki Miner cuts the audio clip from the cue span with no cap and no voice
detection, so cue accuracy decides card quality. No other tool turns a hooked game session into
video plus subtitles (searched 2026-09-19).

### Non-goals

Card creation, AnkiConnect, dictionaries, replay buffer, overlay, statistics, AI features, bundled
text hookers, bundled OBS, macOS, per-line editing, user regex filters, OS-level process detection,
interface translation in v1, and any change to Anki Miner. Anki Miner's Word Curator and its own
`subtitle_regex_filter` already reject lines and clean text downstream.

## 2. Decisions

Made by the maintainer on 2026-09-20. Not open for re-litigation in the implementation plan.

| Topic | Decision |
|---|---|
| Shape | Standalone app, own repository. No Anki Miner changes, ever. Contract = files on disk |
| Platforms | Windows first-class, Linux best-effort, macOS unsupported |
| Text sources | Hooker websockets, clipboard, and OCR as an opt-in add-on |
| Cue ends | Live: next line or a cap. After Stop: optional Silero VAD pass, downloaded on demand |
| OBS | User-installed OBS, configured by the app through its own profile and scene collection |
| Live lookup | Built-in text feed page plus a re-broadcast websocket, so Yomitan works while playing |
| Lifecycle | Manual Start/Stop and a hotkey; optional per-game auto start/stop |
| Stack | Python 3.12, PyQt6, PyInstaller; packaging and CI patterns copied from Anki Miner |
| Licence | GPL-3.0-only. GSM is GPLv3, owocr GPL-3.0-only, Anki Miner GPL-3.0 |

## 3. External contracts

Everything here was checked against source on 2026-09-19/20. Line numbers for Anki Miner are at
commit `380e83f3`; for GSM at `479747fe`; owocr at 1.26.8. M0 re-read the OBS facts in obs-studio
32.2.2 and obs-websocket 5.7.4 source and ran them against a real OBS 32.2.2 on Linux
(`docs/m0/source-findings.md`, `clock.md`, `obs-behaviour.md`). Appendix B lists what was verified how.

### 3.1 Anki Miner input contract

| Fact | Source |
|---|---|
| Subtitle extensions `.ass .ssa .srt .vtt`, loaded by pysubs2 | `anki_miner/utils/file_pairing.py:20` |
| Video extensions `.mp4 .mkv .avi .m4v .mov` | `anki_miner/gui/widgets/single_episode_tab.py:72` |
| Picking a video auto-fills a same-stem subtitle from the same folder | `single_episode_tab.py:467`, `file_pairing.py:122` |
| The sibling search only looks at subtitle extensions, so `.jsonl` and `.json` sidecars are invisible | `file_pairing.py:122-178` |
| Two same-stem subtitles of one extension are ambiguous and auto-fill nothing | `file_pairing.py:122-178` |
| Batch pairs by episode number; pattern order `S01E02`, then ` - NN`, then `NNxNN`, then `EpNN`, then the last bare 1-4 digit run with years skipped | `anki_miner/utils/episode_matcher.py:91-134` |
| Audio clip = cue start - padding to cue end + padding, padding 0.3 s, no maximum | `anki_miner/services/media_extractor.py:82`, `config/config.py:207` |
| `MIN_CLIP_SECONDS = 0.2` guards only a manual clip override | `media_extractor.py:66` |
| The parser coerces `end >= start` and nothing else; overlapping cues are not handled | `anki_miner/services/subtitle_parser.py:949` |
| Screenshot = cue start + min(1.0 s, duration / 2) | `media_extractor.py:106`, `config.py:208` |
| Multi-line cues are flattened; `漢字(かな)` ruby and leading `（…）` groups are stripped; `【name】` is not | `anki_miner/utils/text_utils.py:57,110,192` |
| Identical sentences are de-duplicated by default | `config.py:496` |
| The only console script is the GUI; the only argv branch is a private ffsubsync child flag. No file argument, IPC or watched folder | `pyproject.toml:285`, `anki_miner/gui/launch.py:25,283` |

Consequences for this app:

- A cue must end where the voice ends. A cue that spans "line shown" to "player clicked" puts
  trailing music on the card, and a cue that touches the next one puts 0.3 s of the next voice on it.
- An inverted or near-zero cue is not rejected by Anki Miner; it becomes a 0.6 s clip of mostly the
  next line. The cue invariant in section 9 exists to make that impossible.
- The frame one second after a line appears is the frame the card shows. A cue start that is a
  second late shows the wrong line's frame.
- Only one `.srt` may ever sit next to a video, and no stray numbered `.srt` may sit in a game folder.

### 3.2 Text hookers

All three common hookers run a websocket **server** and broadcast to every connected client, so
this app can listen beside a texthooker page without stealing lines.

| Hooker | Default endpoint | Frame |
|---|---|---|
| Textractor with a websocket extension | `ws://localhost:6677` | plain text |
| Agent (0xDC00) | `ws://localhost:9001` | plain text |
| LunaTranslator | `ws://localhost:2333`, fallback path `/api/ws/text/origin` | plain text |
| GSM-style producers | any | JSON `{"sentence": str, "time": iso8601, "source": str}` |

Defaults and the JSON shape are GSM's (`util/config/configuration.py:580-602`, `gametext.py:653,693`).

### 3.3 OBS and obs-websocket v5

- Websocket settings live in `plugin_config/obs-websocket/config.json` under the OBS config root.
  Keys: `server_enabled`, `server_port`, `server_password`, `auth_required`, `alerts_enabled`,
  `first_load`. OBS's own default port is 4455. GSM treats this file as the source of truth
  (`obs/launch.py:440-483`); so does this app. obs-websocket reads it once, when OBS starts, and
  writes it at start and when the user saves its settings dialog, never at exit
  (`docs/m0/source-findings.md` section 5). The server listens on all interfaces.
- Config roots: Windows `%APPDATA%\obs-studio`; Linux `~/.config/obs-studio`; Flatpak
  `~/.var/app/com.obsproject.Studio/config/obs-studio`.
- Requests used: `GetVersion`, `GetRecordStatus`, `StartRecord`, `StopRecord`, `GetStreamStatus`,
  `GetReplayBufferStatus`, `GetVirtualCamStatus`, `GetProfileList`, `CreateProfile`,
  `SetCurrentProfile`, `GetSceneCollectionList`, `CreateSceneCollection`, `SetCurrentSceneCollection`,
  `GetProfileParameter`, `SetProfileParameter`, `GetVideoSettings`, `SetVideoSettings`,
  `GetRecordDirectory`, `SetRecordDirectory`, `CreateScene`, `GetInputKindList`, `CreateInput`,
  `SetInputSettings`, `GetSpecialInputs`, `SetInputMute`, `GetInputPropertiesListPropertyItems`,
  `GetSceneList`, `SetCurrentProgramScene`, `GetInputSettings`, `GetInputMute`, `RemoveInput`,
  `GetOutputSettings` (27; the last one ties an active recording to its manifest after a reconnect,
  section 6.3). The app never sends `PauseRecord` (section 7).
- Minimum OBS: **30.0.0** (obs-websocket 5.3.3). `SetRecordDirectory` (5.3.0) is the newest of the
  27; every other request exists since 5.0.0. `RecordFileChanged` needs OBS 30.2.0; on 30.0 and 30.1
  a split is silent, and the app's profile keeps splitting off (`docs/m0/source-findings.md`
  section 9).
- OBS is ready when `GetVersion` succeeds, not when the websocket accepts a connection. Until OBS
  has loaded, and between `CurrentSceneCollectionChanging` and `...Changed`, every request gets
  status 207 `NotReady` and every event is dropped, not queued (`docs/m0/source-findings.md`
  sections 5 and 8, confirmed by R2 in `switch_not_ready`). The protocol calls requests during a
  collection change undefined behaviour; obs-websocket 5.7.4 rejects them.
- `GetRecordStatus` returns `outputActive`, `outputPaused`, `outputTimecode`, `outputDuration` (ms),
  `outputBytes`, and no path. `outputDuration` counts frames delivered to the output (it trails the
  capture by the encoder's latency, section 7) and is 0 once the output is inactive. After
  `STOPPED` it still reports `outputActive: true` for about 170 ms (`docs/m0/obs-behaviour.md`
  section 4).
- `GetOutputSettings {outputName}` on `simple_file_output` (Simple output mode) or `adv_file_output`
  (Advanced) returns the file being written as `outputSettings.path`, identical to
  `STARTED.outputPath` (R2 `reconnect`). After a split it still names the first file.
- `GetReplayBufferStatus` and `GetVirtualCamStatus` answer 604 when that output is not configured or
  not installed; the app reads 604 as "not active".
- Events used: `RecordStateChanged {outputActive, outputState, outputPath}` where `outputPath` is
  populated on both STARTED and STOPPED (the protocol comment says STOPPED only; the code sets both)
  and null otherwise; `RecordFileChanged {newOutputPath}`;
  `CurrentSceneCollectionChanging` / `CurrentSceneCollectionChanged`; `CurrentProfileChanging` /
  `CurrentProfileChanged`; `ExitStarted`.
- Output states: `OBS_WEBSOCKET_OUTPUT_STARTING`, `_STARTED`, `_STOPPING`, `_STOPPED`, `_PAUSED`,
  `_RESUMED`, all confirmed in source and in R2's transcripts. A `PAUSED` event carries
  `outputActive: false`, so the session keys on `outputState`, never on `outputActive`.
- Order: obs-websocket sends each event and each response from its own thread-pool task, so neither
  two events nor an event and a response have a guaranteed order. R2 saw a response arrive before
  its `...Changed` event twice in 27 switches. The first event on a freshly identified connection
  arrives about 40 ms late (`docs/m0/obs-behaviour.md` section 3).
- Input kinds: Windows `game_capture`, `window_capture`, `monitor_capture`,
  `wasapi_process_output_capture` (one application's audio), `wasapi_output_capture` (desktop audio).
  Linux `pipewire-screen-capture-source`, `xcomposite_input`, `pulse_output_capture`. Always
  feature-detected with `GetInputKindList`, never assumed. `wasapi_process_output_capture` exists
  only on Windows 10 build 19041 or later.
- Client library: `obsws-python` 1.8.0 (`ReqClient`, `EventClient`), the one GSM and owocr both use.
  It logs the password at INFO when it connects and puts it in `repr()`; `ReqClient` does not match
  request ids, so one client allows one request at a time from one thread and must be reconnected
  after a timeout; `EventClient`'s thread ends silently when the connection closes
  (`docs/m0/source-findings.md` section 11). Section 11.2 says how the gateway copes.

### 3.4 owocr

`pip install owocr`, GPL-3.0-only, Python >= 3.11, a CLI with no library API.

- Flags used: `-r screencapture`, `-w websocket`, `-wp <port>`, `-e <engine>`, `-el <engine>`,
  `-l <lang>`, `-sa <area>`, `-swa <rects>`, `-t False` (`owocr/config.py:21-105`). Without `-el`,
  owocr builds every engine it can import, and an unavailable `-e` engine falls back to any other,
  cloud ones included (`run.py:3261-3266,3297-3298`).
- Output: plain-text frames broadcast to every websocket client (`owocr/run.py:486-490,2730`).
- The server binds `0.0.0.0` (`run.py:528`).
- The config file path is fixed at `~/.config/owocr_config.ini` with no override flag
  (`config.py:110`). owocr reads it for every key the command line does not pass, and downloads a
  default copy from GitHub when it is missing (`config.py:188-194`); R3 saw it created on a first
  run. So the app's owocr child runs with a private home (section 14), and neither the app nor its
  child touches the user's file.
- Every start also contacts `pypi.org` (version check, 5 s timeout) and tries to fetch Chrome
  Screen AI into `~/.config/screen_ai`; both only log on failure (`docs/m0/owocr.md`).
- `-r obs` captures the whole program scene with no crop (`run.py:1830-1845`) and needs the OBS
  password on the command line. Not used in v1.
- It logs, to stderr as `HH:MM:SS | <message>`, `Selected coordinates: <rects>` for an explicit
  `-sa` at start and after the screen picker (`run.py:1956,2480`), and `Selected window coordinates:
  <rects>` for an explicit `-swa` and after the window picker (`run.py:2035,2517`). That is how the
  chosen OCR area is read back. Rectangles print as `x1,y1,x2,y2`, several joined with `_`. The
  window line is Windows-only: on Linux X11 a window title in `-sa` is an error, and on Wayland it
  falls back to the screen picker. After a picker selection owocr keeps running and starts OCR.
- Frame stabilisation is on by default (`config.py:140`): a line is emitted only once the text has
  stopped changing, so OCR lines arrive after the voice has started.
- Local engines: OneOCR (Windows 10/11), meikiocr (any platform, onnxruntime). Cloud engines Google
  Lens and Bing are free and need no key.

## 4. Architecture

### 4.1 Packages

```
anki_miner_game/
  models/        frozen dataclasses, no I/O
  text/          sources/ (websocket, clipboard, ocr) and pipeline.py
  obs/           discovery, client, provision, recorder
  session/       clock, cues, srt_writer, naming, manifest, journal, session (the actor)
  lifecycle/     auto.py
  vad/           trimmer.py, assign.py, worker/vad_worker.py
  addons/        bootstrap.py, vad_addon.py, ocr_addon.py
  feed/          ws_server.py, http_server.py, page.html
  gui/           main_window, tray, wizard, game_profile_dialog, settings_dialog, hotkey_win, cli_verbs
  interfaces/    Protocols: TextSource, RecordClock, ObsGateway, Presenter
  app.py, launch.py
```

Dependency rule: `models` imports nothing from the app; `session`, `text`, `obs`, `vad`, `feed` and
`lifecycle` never import `gui`; `gui` reaches the rest only through `interfaces`. `session/cues.py`
and `vad/assign.py` are pure functions with no clock, file or network access. Composition lives in
`app.py`; there is no DI container.

### 4.2 Threads

| Thread | Owns |
|---|---|
| Qt main | widgets, tray, clipboard source (QClipboard must live here), Windows hotkey filter |
| I/O (one asyncio loop in a QThread) | websocket sources, feed websocket server, owocr supervisor, blocking `obsws-python` requests through `run_in_executor` |
| obsws-python event thread | OBS event callbacks; they do nothing except enqueue |
| feed HTTP | stdlib `http.server`, daemon thread |
| VAD job | one subprocess plus a reader, after Stop |

The **session actor** is single-threaded: one queue of `LineReceived`, `ObsEvent`, `UserCommand`
and `Tick` messages, consumed on the I/O loop. Every state change happens there, which makes the
session deterministic and testable with an injected clock. The GUI observes through a `Presenter`
that emits Qt signals; slots run on the main thread.

A line's arrival time is `time.monotonic()` read inside the source, at frame receipt, before any
queueing. Nothing downstream may re-stamp it.

### 4.3 Runtime dependencies

`PyQt6`, `obsws-python`, `websockets`. Nothing else in the frozen app. Dev: `pytest`, `pytest-qt`,
`pytest-asyncio`, `hypothesis`, `pysubs2`, `ruff`, `black`, `mypy`. The VAD and OCR add-ons bring
their own environments (sections 13 and 14).

## 5. Data models

All frozen dataclasses; change through `dataclasses.replace`. JSON on disk carries `"schema": 1`.

```python
@dataclass(frozen=True)
class GameLine:
    text: str            # after the pipeline
    raw: str             # as received
    t_mono: float        # time.monotonic() at frame receipt
    source_id: str       # "textractor", "agent", "luna", "clipboard", "ocr", or a user id

@dataclass(frozen=True)
class Cue:
    index: int           # 1-based, final numbering
    start_ms: int
    end_ms: int
    text: str
    source_id: str
```

`AppConfig` (`~/.anki_miner_game/config.json`):

| Field | Default | Notes |
|---|---|---|
| `output_root` | `~/Videos/Anki Miner Game` | recordings; `_incoming/` lives inside it |
| `obs.host` / `obs.port` | `127.0.0.1` / read from OBS `config.json` | manual override allowed |
| `obs.password_override` | `null` | normally read from OBS `config.json` at connect time, never stored |
| `text_sources` | Textractor 6677, Agent 9001, LunaTranslator 2333 | list of `{id, name, uri, enabled}` |
| `feed.enabled` / `feed.ws_port` / `feed.http_port` | `true` / `6678` / `6679` | bound to `127.0.0.1` |
| `hotkey` | `Ctrl+Shift+F9` | Windows only |
| `recording.max_height` / `recording.fps` | `1080` / `30` | `720` selectable |
| `cue.max_cue_seconds` | `15` | 5-60 |
| `cue.end_gap_ms` | `350` | Anki Miner's `audio_padding` + 50 ms; change it if that padding changes |
| `vad.enabled` | `true` when the add-on is installed | |
| `last_game` | `null` | |

`SKIP_MS = 300`, `MIN_CUE_MS = 500` and every VAD threshold are module constants, not settings.

`GameProfile` (`~/.anki_miner_game/games/<slug>.json`):

| Field | Default | Notes |
|---|---|---|
| `slug`, `title` | | `title` is what the user typed; the folder and stem use the sanitised form |
| `text_mode` | `"hook"` | `"hook"` = websockets and clipboard; `"ocr"` = owocr only. Validation rejects a mix |
| `source_ids` | all enabled | hook mode only |
| `clipboard` | `false` | hook mode only |
| `capture.kind` | `"auto"` | `auto`, `game`, `window`, `pipewire`, `xcomposite` |
| `capture.window` | `null` | the OBS window string chosen in the profile dialog |
| `audio.mode` | `"app"` on Windows, `"desktop"` on Linux | `app` needs `capture.window` |
| `filters.speaker_strip` | `true` | strips one leading `【…】` group |
| `filters.typewriter_merge` | `false` | see section 8 |
| `ocr.engine`, `ocr.language`, `ocr.window_title`, `ocr.rects` | `oneocr` / `meikiocr`, `ja`, `null`, `null` | OCR mode only |
| `auto.start_on_first_line` | `false` | |
| `auto.stop_idle_minutes` | `10` | used only when auto mode is on; `0` disables |
| `auto.stop_on_window_close` | `true` | used only when auto mode is on and a window is pinned |

One session has exactly one start shift: `START_SHIFT_MS = {"hook": 0, "ocr": -1000}`.

`SessionManifest` (`<stem>.session.json`):

```json
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
  "clock": {"kind": "event", "zero_event": "STARTED", "capture_latency_ms": 10,
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
```

`state` is one of `recording`, `finalise_pending`, `ready`, `vad_running`. `flags` may contain
`split_unsupported`, `clock_degraded`, `obs_exited`, `no_cues`. `live_cues` always holds the cues as
computed before the VAD pass, so the pass can be re-run or undone without a second subtitle file.

## 6. Session state

### 6.1 States

```
idle --arm--> armed --STARTED--> recording --STOPPED--> finalising --> armed
  ^             |                    |                                   |
  +---disarm----+                    +--ExitStarted / connection lost----+--> (see 6.4)
```

| State | Meaning |
|---|---|
| `idle` | no game armed; OBS untouched |
| `armed` | a game is selected; OBS is on the app's profile and scene collection; sources are connected |
| `recording` | OBS record output active and owned by the app |
| `finalising` | cues computed, files renamed, VAD job queued |

### 6.2 Arming and ownership

Arming switches OBS to the app's profile and scene collection. The switch happens at arm time and
never at Start, so Start is a single `StartRecord` with no collection reload in front of it.

1. Refuse if any output is active: `GetStreamStatus`, `GetRecordStatus`, `GetReplayBufferStatus`,
   `GetVirtualCamStatus`. Banner names the active output. A 604 answer from the last two means the
   output is not configured or not installed, so not active. OBS itself switches profile and
   collection under a live recording, stream or replay buffer and keeps them running (a recording
   started on the user's profile keeps writing into the user's folder), so this refusal is the only
   guard (`docs/m0/obs-behaviour.md` item 6).
2. Read the current profile and collection names; write them to `obs_restore.json`.
3. `SetCurrentProfile`, wait for `CurrentProfileChanged`. `SetCurrentSceneCollection`, wait for
   `CurrentSceneCollectionChanged`. No other request is sent between a `...Changing` event and its
   `...Changed` event. Timeout 15 s, then restore and report. A switch whose target is already
   current is skipped: OBS answers it and sends no event. A step completes on its `...Changed`
   event, never on the answer, whose order against the events is not guaranteed (section 3.3).
   `CreateProfile` answers before the profile exists and then switches to it with
   `CurrentProfileChanged` and no `...Changing`, so it too waits for the event. A 207 answer is
   retried (section 11.2). When `CurrentProfileChanged` arrives and the answer does not follow
   within a few seconds, OBS is showing its modal restart question (section 11.3): a banner says
   "OBS is asking to restart" and points at OBS's window (`docs/m0/obs-behaviour.md` items 3-5).
4. Apply the game profile to the inputs (section 11.3). Connect text sources. State `armed`.

Disarm restores the names from `obs_restore.json` and deletes the file. If the file still exists at
launch (unclean exit) the app restores once and deletes it. If the user started a stream while
armed, restore waits until no output is active. A user who wants to stay on the app's profile
loses nothing: arming again is idempotent.

Ownership rule: a recording is a session only if it starts while the app is `armed`. Then OBS is on
the app's profile and record directory by construction, whoever pressed the button (app, tray,
hotkey or OBS itself). A `STARTED` event in any other state is ignored, so the app never renames a
file from the user's own setup.

The session keys every record transition on `RecordStateChanged.outputState`, never on
`outputActive` (a `PAUSED` event carries `outputActive: false`), and tolerates events that arrive
out of order (section 3.3).

### 6.3 Reconcile on connect

Run after every connect and reconnect, before any event is trusted.

"Manifest for `outputPath`" means the manifest in `_incoming/` whose `obs.output_path` equals the
active file's path, read with `GetOutputSettings` on `simple_file_output` or `adv_file_output`
according to the profile's `[Output] Mode`. `outputBytes` against the file size is no substitute:
the file on disk trails it by the muxer's buffering, and is 0 bytes 5 s into a recording
(`docs/m0/obs-behaviour.md` item 8 and section 5). `GetRecordStatus` still reads active for about
170 ms after `STOPPED`; a `STOPPED` event already received wins over that reading.

| `GetRecordStatus` | App state | Manifest for `outputPath` in `_incoming/` | Action |
|---|---|---|---|
| inactive | `recording` | | treat as `STOPPED`; stop offset = last clock reading |
| active, paused flag equals tracked flag | `recording` | | continue |
| active, paused flag differs | `recording` | | switch to `OutputDurationClock`, flag `clock_degraded` |
| active | not `recording` | exists, `state = recording` | app restarted mid-session: resume the journal on `OutputDurationClock`, flag `clock_degraded` |
| active | not `recording` | none | not ours; ignore, and refuse arming until it stops |
| inactive | not `recording` | exists | orphan: run finalise (section 10.3) |

### 6.4 Ending without STOPPED

`ExitStarted`, or the connection dropping and reconcile finding OBS gone: the session finalises with
the stop offset set to the last clock reading and the flag `obs_exited`. An `.mkv` survives an OBS
crash, which is why the profile records to `.mkv`.

R2 confirmed both paths (`docs/m0/obs-behaviour.md` item 12). A clean exit sends `ExitStarted`, then
OBS closes the connection with 1001 "Server stopping." about 330 ms later, with no `STOPPED`. A
killed OBS drops the connection with 1006 and sends no `ExitStarted`. The `.mkv` was readable both
times. After a kill the next launch stops at OBS's modal "OBS Studio Crash Detected" with no
websocket until the user answers it (section 17). The app never signals OBS itself.

## 7. Record clock

```python
class RecordClock(Protocol):
    def start(self, zero_mono: float) -> None: ...
    def pause(self, at_mono: float) -> None: ...
    def resume(self, at_mono: float) -> None: ...
    def offset_ms(self, t_mono: float) -> int | None: ...   # None while paused
```

`EventClock` (primary). `offset = (t_mono - zero_mono - paused_total) * 1000 + capture_latency_ms`.
`zero_mono` is the monotonic time at which the `STARTED` event arrived, and `capture_latency_ms` is
**10**. M0 measured both against flash timestamps (`docs/m0/clock.md`): with `STARTED` as the zero
the encoder medians lie within 50 ms of each other (NVENC with lookahead, x264, x264 with
`rc-lookahead = 60`), under the 100 ms criterion, so one constant ships and there is no calibration
step. The `StartRecord` response and `STARTING` arrive before the encoder has started, 7-15 ms early
for x264 and 132-184 ms for NVENC, which would widen the spread to about 183 ms. Against 10 the
largest residual was 50 ms without overload and 133 ms under encoder overload. Both values are
Linux measurements, provisional for Windows until H5 (D2), which also checks the resolution of
`time.monotonic()` on the shipped Windows Python: a coarse tick would add its size to every line's
error (`docs/m0/clock.md` Limits).

Pause edges come only from the `OBS_WEBSOCKET_OUTPUT_PAUSED` and `_RESUMED` events. The app never
sends `PauseRecord`: a pause is whatever OBS reports, whoever caused it. On OBS's default profile
(Simple output, recording "Same as stream") the recording shares the stream encoder and cannot
pause; a `PauseRecord` there answers success and nothing happens, no event, no pause. Provisioning
therefore gives the app's profile a separate recording encoder (section 11.3). With one, the
frame-aligned pause edges moved offsets after a resume by under 10 ms at the median, inside the
150 ms bound.

Under encoder overload OBS keeps its frame timeline and repeats the last frame, so the clock does
not drift; a flash rendered while the encoder was behind is simply missing from the file. A line
can then reach the subtitle before its picture reaches the video. Up to at least 14 % skipped
frames that stayed inside 150 ms; nothing in the clock can correct it.

`OutputDurationClock` (fallback). Anchors on `(monotonic midpoint of the request round trip,
GetRecordStatus.outputDuration + lag)` and re-anchors every 10 s. `outputDuration` counts frames
delivered to the output, so it trails capture time by the encoder's latency: M0 measured 0.46 to
3.3 s with a healthy encoder and up to 5.6 s under overload, varying by at most 110 ms within one
session (`docs/m0/clock.md`). `lag` is `at_ms - output_duration_ms` of the latest drift sample
whose `output_duration_ms` is above 0 (at start no frame has reached the output yet), which brings
the fallback back within the 150 ms bound. A session with no such sample has no lag to add: its
cues may start seconds early, and a banner says so. The fallback is used only when reconcile says
the event history is incomplete (section 6.3).

Rules for both: line offsets are clamped to be non-negative and non-decreasing; a stop reading or a
drift sample is never below the latest line offset but does not move that clamp; an
`outputDuration` anchor whose round-trip midpoint falls before the latest pause edge is carried
forward by the time the recording ran in between (`docs/m0/wave-1-amendments.md` item 7); a line
arriving while paused is dropped and counted; the hooker's own `time` field is ignored, since mixing wall-clock
with monotonic invites skew for a gain of milliseconds on localhost. `outputDuration` is sampled
while the `EventClock` is in use and the recording runs unpaused: at start, 10 s after start, at
each resume and at stop. Each sample is written to the manifest's `clock.drift_samples`, and the
fallback takes its lag from them.

File splitting is off in the app's profile. If `RecordFileChanged` fires anyway, the session is
finalised against the first file, flagged `split_unsupported`, and a banner says later lines were
not subtitled. The event carries only a path and the real split lands on a keyframe, so a rebase
would be a guess. Not in v1. R2 saw `RecordFileChanged` 5.6 s after the split request, and after it
`STOPPED.outputPath`, `StopRecord.outputPath` and `GetOutputSettings` all still name the first file;
only `RecordFileChanged` names the second, so the session never reads `STOPPED.outputPath` as the
last file (`docs/m0/obs-behaviour.md` item 10). With several `stop` records in the journal the
first wins (section 10.3).

The stop offset is the clock reading at the receipt of `STOPPING`, not of `STOPPED`. The video
ends at `StopRecord` / `STOPPING` within one frame, while `STOPPED` follows 560-620 ms later
(Simple, NVENC) or about 1.3 s later (Advanced, x264); stopping on `STOPPED` would let the last cue
run past the end of the video (`docs/m0/obs-behaviour.md` item 9). When no `STOPPING` was seen
(a reconnect), the rules of sections 6.3 and 6.4 apply.

## 8. Text intake

### 8.1 Sources

```python
class TextSource(Protocol):
    id: str
    def start(self, sink: Callable[[str, float, str], None]) -> None: ...   # raw, t_mono, source_id
    def stop(self) -> None: ...
    @property
    def status(self) -> SourceStatus: ...   # disconnected, connecting, connected, receiving
```

- `WebsocketSource`: `websockets` asyncio client. Connect to `ws://<uri>`; on handshake failure try
  `ws://<uri>/api/ws/text/origin` once (LunaTranslator). Reconnect with backoff 1, 2, 5, 10 s, then
  every 10 s. `ping_interval=None`, as GSM does, because hookers do not answer pings, and
  `close_timeout=1.0`, because they do not answer close frames either. Frame parse:
  try JSON; if the result is a dict take `sentence` (fall back to the whole frame when absent);
  `source` and `time` are ignored: a line belongs to the configured source, and its time is read at
  receipt. Any other JSON value, or invalid JSON, means the frame is the line. GSM's version calls
  `.get` on whatever `json.loads` returns (`gametext.py:691-700`); the port guards that.
- `ClipboardSource`: `QClipboard.dataChanged` on the main thread, text only, ignores changes made
  by this app. Works on Windows and X11. On Wayland Qt sees the clipboard only while focused; the
  wizard says so and recommends a websocket.
- `OcrSource`: a `WebsocketSource` on the managed owocr port, plus the subprocess supervisor
  (section 14).

### 8.2 Pipeline

Applied in this order to every received line. Each drop increments one counter in the manifest.

1. Unicode NFC.
2. Remove control characters and zero-width characters (`Cc`, `Cf`) and lone surrogates (`Cs`)
   except newline. `json.loads` turns an unpaired `\udXXX` escape into a lone surrogate, which the
   journal, the subtitle and the feed cannot encode.
3. Newlines to spaces; collapse whitespace; strip.
4. Speaker strip (when enabled): remove one leading `【…】` group and following whitespace.
5. Drop if empty. Counter `no_letters`: an empty line has no letter either.
6. Drop if no character is a letter (`str.isalpha`): punctuation-only and digit-only lines. A
   digit-only cue can also be mistaken for an index line by SRT parsers. Counter `no_letters`.
7. Drop if longer than 300 characters. Counter `junk`.
8. Drop if identical to the previous accepted line. This also covers one hooker arriving by
   websocket and clipboard at once. Counter `duplicate`.
9. Typewriter merge (per game, off by default): if the line starts with the previous accepted
   line's text, arrived within 2 s of it, and is longer, it replaces the previous line's text and
   keeps the previous line's offset. It is opt-in because it would swallow a short real line such
   as え followed by えっと….

"The previous accepted line" in steps 8 and 9 means the previous line journalled in the current
recording. The session resets the pipeline whenever the line it accepted last will not be
journalled: after dropping an accepted line as `paused`, at `STARTED` (unless the auto-start line
held while armed is journalled at offset 0), and after a split stop. A typewriter merge is
journalled as a `replace` record only when its base line is the journal's last `line` record;
otherwise it is journalled as a new line at its own offset, or dropped as `paused` when the clock
reads `None`. A new session's first line is therefore never a duplicate of the last one.

An accepted line is journalled (section 10.2), broadcast to the text feed, and shown in the live list.

## 9. Cue rules

`session/cues.py`, a pure function: `build_cues(lines, stop_ms, shift_ms, cfg) -> list[Cue]`.

Invariant for every cue: `0 <= start < end <= next.start`.

```
start    = max(0, prev.start, line.offset_ms + shift_ms)
D        = next.start - start                      # for the last line, D = stop_ms - start
drop     if D < SKIP_MS (300)                      # skip-mode click-through; counter "skip"
end      = min(start + max_cue_seconds * 1000,
               max(next.start - end_gap_ms,
                   min(next.start, start + MIN_CUE_MS)))
```

The gap shrinks for short lines instead of inverting them. Computed with the defaults
(`end_gap_ms` 350, `MIN_CUE_MS` 500, cap 15 s):

| Line shown for (D) | Cue length | Gap to next cue |
|---|---|---|
| 0.25 s | dropped | |
| 0.32 s | 0.32 s | 0 |
| 0.60 s | 0.50 s | 0.10 s |
| 0.85 s | 0.50 s | 0.35 s |
| 0.90 s | 0.55 s | 0.35 s |
| 4.00 s | 3.65 s | 0.35 s |
| 40 s | 15 s | 25 s |

Order of evaluation: (1) starts for every line; (2) the drop test, with `D` measured to the
**immediately following** line, kept or not, so a burst of click-through lines is dropped as a
whole; (3) ends, with `next` meaning the next **kept** line, so a dropped line never shortens its
neighbour. Starts are non-decreasing. Two lines can share a start only when the OCR shift clamps at
`prev.start`; the earlier of the two then has `D = 0` and is dropped. Finalise passes no line
recorded after the first `stop` record (section 10.3), so no line starts past `stop_ms`.

SRT output (`session/srt_writer.py`): UTF-8 without BOM, `\n` line ends, index from 1, timestamps
`HH:MM:SS,mmm` formatted from integer milliseconds (GSM's formatter floors seconds and takes
milliseconds from a different value; do not port it), one text line per cue, blank line between
cues. Written to a temporary name in the same folder and moved into place with `os.replace`.

## 10. Files and crash safety

### 10.1 Naming

Folder `<output_root>/<sanitised title>/`, stem `<sanitised title> - NN`, NN zero-padded to two
digits and growing naturally to 9999. Anki Miner's extractor reads at most four digits.

Sanitiser, in order (amended at M0, `docs/m0/sanitiser.md`):

1. Replace `< > : " / \ | ? *` and control characters with a space.
2. Collapse every whitespace run to one space; trim both ends.
3. Replace every hyphen that has a space on both sides with `~`, so the only ` - ` in the stem is
   the one before NN.
4. Rewrite any `S<digits>` + optional separators + `E<digits>` token (either case) as
   `S<digits>~E<digits>`, because that pattern outranks ` - NN` in Anki Miner.
5. Strip trailing dots and spaces; empty becomes `Game`.
6. Steps 3 and 4 must also hold for the stem as Anki Miner reads it. Its extractor deletes
   technical tokens (`1080p`, `1280x720`, `x264`, `10bit`, `[1A2B3C4D]`, `v2`) before matching.
   Where that deletion would leave a hyphen between whitespace, the hyphen becomes `~`. Where it
   would join an `S<digits>` to an `E<digits>`, a `~` goes in directly before the `E`. Repeat until
   neither applies.

Step 6 only replaces a hyphen or inserts a `~`, so no letter or digit the user typed is lost; a
title with no hazard passes through unchanged, and the rule is idempotent. `session/naming.py`
simulates the extractor's token deletion (a port of its six patterns) rather than approximating it.
All whitespace, U+3000 included, becomes an ASCII space.

The first rule (` - ` to ` ~ ` before collapsing whitespace, on the title as typed) failed three
ways. A deleted token could join `S1` and `E2` or put spaces around a hyphen (`S1 1080p E2` was
read as season 1, episode 2); U+3000 and U+00A0 around a hyphen are not control characters but
match the extractor's `\s` (`A　-　5` was read as episode 5); two hyphens sharing a space defeated a
plain replace (`A - - 5` was read as episode 5). With the amended rule they give `S1 1080p ~E2 - 03`,
`A ~ 5 - 01` and `A ~ ~ 5 - 01`. `docs/m0/sanitiser.md` lists all seventeen counterexamples.

Verified against Anki Miner's real `EpisodeNumberExtractor` (at `ea4a30ce` for the amended rule);
each extracts the session number with no season:

| Title | Stem |
|---|---|
| `Persona 5` | `Persona 5 - 01` |
| `Steins;Gate 2010` | `Steins;Gate 2010 - 03` |
| `Zero - 3` | `Zero ~ 3 - 01` |
| `Zero Escape S2E1` | `Zero Escape S2~E1 - 05` |
| `S01E05 The Game` | `S01~E05 The Game - 02` |
| `s1 e2 spaced` | `s1~e2 spaced - 08` |
| `Fate/stay night` | `Fate stay night - 12` |
| `2x04 Edition` | `2x04 Edition - 06` |
| `Ep 5 Simulator` | `Ep 5 Simulator - 04` |
| `NieR:Automata 1.1a` | `NieR Automata 1.1a - 9999` |

Without step 3, `Zero - 3 - 01` extracts 3. Without step 4, `S01E05 The Game - 02` extracts season 1
episode 5. The contract test (section 18) keeps this honest.

Known limits in v1, outside the extractor contract: a title that is a Windows device name (`CON`,
`NUL`, `COM1`) makes an invalid folder name on Windows (the stem `CON - 01` is fine); there is no
length cap, so a long CJK title can pass the 255-byte file-name limit on Linux or `MAX_PATH` on
Windows; a leading dot is kept, so `.hack` makes a hidden folder on Linux.

### 10.2 While recording

All three live in `<output_root>/_incoming/`, which is OBS's record directory in the app's profile:

```
<obs stem>.mkv              written by OBS
<obs stem>.session.json     written at STARTED: game, reserved NN, outputPath, state "recording"
<obs stem>.lines.jsonl      journal, append-only
```

NN is reserved at `STARTED` as one more than the highest index found among `.mkv` files and
manifests in the game folder and manifests in `_incoming/` for the same game.

The journal is one JSON object per line, flushed after each write:

```
{"t":"line","offset_ms":5230,"text":"…","source":"textractor"}
{"t":"replace","text":"…"}                     typewriter merge: new text for the previous line
{"t":"pause","offset_ms":61200}
{"t":"resume","offset_ms":61200}
{"t":"stop","offset_ms":5248120}
```

The `stop` record's offset is the reading at `STOPPING` (section 7). `pause` and `resume` records
come only from OBS's `PAUSED` and `RESUMED` events.

Every accepted line is durable the moment it arrives. Cue ends are not journalled; they are a pure
function of the journal (section 9). This replaces GSM's approach of appending SRT cues one line
late, which needs special handling for the last line and loses it on a crash.

### 10.3 Finalise

One routine, idempotent, used by normal stop, by reconcile and at launch:

1. Read the journal. If there is no `stop` record, stop offset = the last record's offset +
   `max_cue_seconds`. With several `stop` records the first wins and every record after it is
   ignored: its lines are neither cues nor `skip`, and a `replace` record there rewrites nothing.
2. `build_cues`. No cues: keep the video, write no subtitle, flag `no_cues`.
3. Write `<obs stem>.srt` atomically; write `live_cues` and counts into the manifest.
4. Rename video, subtitle and manifest to `<Game>/<Game> - NN.*`. Same volume, so the renames are
   instant. On Windows OBS can hold the handle briefly after `STOPPED`: retry for up to 10 s with
   backoff (GSM does the same, `longplay_handler.py:275`). Still failing: manifest state
   `finalise_pending`, banner, retried at next launch.
5. Delete the journal. Manifest state `ready`. Queue the VAD job if enabled.

Each step checks whether it has already happened, so a crash between any two steps is repaired by
running the routine again. At launch the app runs it for every manifest in `_incoming/` whose
recording is not active. Finalise calls under one output root run on one worker, one at a time,
never on a shared pool: the NN bump is check-then-act, and two sessions of one game bumped to the
same NN would both move onto one video.

## 11. OBS

### 11.1 Discovery (`obs/discovery.py`)

1. Find the install. Windows: `%ProgramFiles%\obs-studio\bin\64bit\obs64.exe`, then the install
   folder the OBS installer records in the registry: `HKLM\SOFTWARE\OBS Studio`, default value, read
   in the 64-bit view and then the 32-bit view, with `bin\64bit\obs64.exe` appended. The NSIS
   installer and the Steam build both write that key in both views (`docs/m0/source-findings.md`
   section 3; provisional until H5). Linux: `obs` on PATH, then Flatpak `com.obsproject.Studio`.
   Not found: the wizard links to `https://obsproject.com/download` and re-checks on demand.
2. Find the config root (section 3.3) and read `plugin_config/obs-websocket/config.json`.
3. `server_enabled` false and OBS not running: set it true, leave `auth_required` and the password
   as they are (generate a password only if auth is required and none exists), then launch OBS.
   `server_enabled` false and OBS running: obs-websocket read the file when OBS started and does not
   read it again, so a change takes effect only at the next start, and saving its settings dialog
   would overwrite it (section 3.3). The wizard asks the user to tick Tools -> WebSocket Server
   Settings -> Enable, or to close OBS and press Fix.
4. Port and password are read from that file at every connect. The password is never logged and
   never copied into the app's own config unless the user types an override. The port override
   and the password override each win over the file. `ObsDiscovery.credentials` raises
   `ObsConfigError` only when no port is known, neither an override nor a readable file; with a
   port override and an unreadable file it connects without a password unless one is overridden.

Minimum OBS: **30.0.0** (obs-websocket 5.3.3), the first release whose
`GetVersion.availableRequests` contains every request in section 3.3 (`SetRecordDirectory` is the
newest). The app checks the list, not a version number, and names the missing request when it
refuses: on OBS 29 that is `SetRecordDirectory`.

Launching OBS: `obs64.exe --minimize-to-tray` on Windows (working directory must be the `bin\64bit`
folder), `obs --minimize-to-tray` or `flatpak run com.obsproject.Studio --minimize-to-tray` on Linux
(R2 confirmed that `flatpak run` passes the flag through). The app does not pass `--profile` or
`--collection`; arming does the switch, one code path. Readiness is a successful `GetVersion`
(section 3.3): on the Linux host it first succeeded 2.3-5.5 s after launch, median 3.0 s. Two
modal dialogs can stop a launch before the websocket answers: "already running" for a second
instance, and "OBS Studio Crash Detected" after a crash or kill, which no flag in 32.2.2 skips.
`wait_ready`'s timeout banner names the second (section 17).

### 11.2 Gateway (`obs/client.py`)

```python
class ObsGateway(Protocol):
    async def connect(self) -> ObsInfo: ...            # GetVersion + reconcile input
    async def request(self, name: str, **fields) -> dict: ...
    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None: ...
    @property
    def collection_changing(self) -> bool: ...
```

`request` waits while `collection_changing` is true. Event callbacks run on the library's thread
and only enqueue onto the session actor with the monotonic time at which they arrived. Connection
loss triggers reconnect with backoff, and every successful connect runs reconcile (section 6.3).

A 207 `NotReady` answer (OBS still loading, or a collection change whose `...Changing` event has
not arrived yet) is retried in `connect()` and `request()` until a timeout, then raised as
`ObsRequestError`. Because of obsws-python's behaviour (section 3.3) the gateway sets the
`obsws_python` logger to WARNING and never formats a client with `repr`, sends every request from
one thread (a single-thread executor), drops and reconnects the request client after a timeout,
since a late reply would be read as the next request's, and watches the event thread to detect a
lost connection. Nothing depends on the order of a response against events.

### 11.3 Provisioning (`obs/provision.py`)

Idempotent; runs from the wizard and again at each arm, touching only what differs.

Profile `Anki Miner Game`:

| Setting | Value | How |
|---|---|---|
| Record directory | `<output_root>/_incoming` | `SetRecordDirectory` (writes `[SimpleOutput] FilePath` and `[AdvOut] RecFilePath`) |
| Output size | base size scaled so height <= `recording.max_height`, aspect kept, then the width rounded down to a multiple of 4 and the height to a multiple of 2 | `SetVideoSettings` |
| Frame rate | `recording.fps` / 1 | `SetVideoSettings` |
| Container | `mkv` | `SetProfileParameter` `[SimpleOutput] RecFormat2` and `[AdvOut] RecFormat2` |
| Automatic file splitting | off | `SetProfileParameter` `[AdvOut] RecSplitFile` = `false`. Simple output mode never splits |
| Automatic remux | off | `SetProfileParameter` `[Video] AutoRemux` = `false` |
| Recording encoder | separate from the stream encoder | `SetProfileParameter`: Simple mode `[SimpleOutput] RecQuality` = `Small` when it is `Stream`; Advanced mode `[AdvOut] RecEncoder` set to the stream encoder's type (`[AdvOut] Encoder`) when it is `none` |
| Audio sample rate, channels | the values of the profile provisioning started on | `GetProfileParameter` there, `SetProfileParameter` `[Audio] SampleRate` and `ChannelSetup` here |
| Encoder, bitrate, audio codec | untouched except that recording uses its own encoder | OBS's own defaults suit the machine better than a guess |

`SetProfileParameter` is used only where obs-websocket has no first-class request. It takes a string
and saves the profile at once. The key names are confirmed in source and in a real `basic.ini`
written by these requests (`docs/m0/source-findings.md` section 1, `docs/m0/obs-behaviour.md`
item 1). OBS's default container is `hybrid_mp4`, and `AutoRemux` has no default, so it is off
unless the user turned it on; on, it would remux each finished recording to a second video while
finalise renames the first.

The recording encoder row follows from the pause ruling of section 7: a recording that shares the
stream encoder, OBS's default, cannot pause, and OBS then sends no pause event at all. Simple mode
keeps OBS's own encoder for the `Small` quality (R2's `pause_resume` transcript); Advanced mode
keeps the user's settings of the chosen encoder (R1 validated pause with an Advanced recording
encoder, `docs/m0/clock.md` "Pause"). Like every other row it is re-checked at every arm.

Output size alignment: libobs rounds the running output down to a width divisible by 4 and an even
height, which `GetVideoSettings` reports while `basic.ini` keeps the value sent (854x480 ran as
852x480; 2560x1080 scaled to 720 lines gives 1706, which runs as 1704). Provisioning aligns before
comparing or sending, or it would re-send `SetVideoSettings` at every arm (`docs/m0/obs-behaviour.md`
item 16).

Restarts. GSM logs that one of its profile changes needs an OBS restart (`obs/actions.py:224`). M0
found that record directory, output size, frame rate and splitting apply at the next `StartRecord`.
The container does not: OBS picks the muxer, like the output mode and the recording quality or
encoder, only when it builds its outputs, at launch and when a profile is activated. The first
recording after provisioning was MP4 data in a `.mkv` file (`docs/m0/source-findings.md` section 2,
`docs/m0/obs-behaviour.md` item 2). After changing one of those keys, provisioning re-activates the
app's profile by switching to the profile it started on and back (31 and 84 ms in R2); the next
recording is Matroska. When the app's profile is already current there is nowhere to switch to,
and provisioning reports that the change needs a restart of OBS.

A profile switch between profiles whose `[Audio] SampleRate` or `ChannelSetup` differ stops at
OBS's modal restart question; `...Changed` arrives and the answer does not (`docs/m0/obs-behaviour.md`
item 3). `CreateProfile` itself never asks, and the new profile runs at OBS's defaults (48000,
`Stereo`), so a user at 44.1 kHz meets the question at the first switch from the app's profile back
to their own: the switch-away above. Provisioning therefore reads both values with
`GetProfileParameter` while the user's profile is current, before `CreateProfile`, and writes them
with `SetProfileParameter` right after it, before any switch away. `SetProfileParameter` writes the
running profile, the side OBS compares, so neither the switch away nor the switch back asks. This
order is from source, not run (provisional until E1). The app never restarts OBS; if the question
appears anyway, arming shows the banner of section 6.2.

`CreateProfile` over the websocket also sets `[Basic] ConfigOnNewProfile=false` in the user's
`user.ini`, which turns off OBS's offer to run its auto-configuration wizard for new profiles. The
wizard says so (section 16).

Scene collection `Anki Miner Game`, scene `Game`:

| Platform | Video input | Audio |
|---|---|---|
| Windows, no pinned window | `game_capture`, mode "any fullscreen application" | desktop audio (the app's own `wasapi_output_capture` input) |
| Windows, pinned window | `game_capture` on that window, plus a `window_capture` of the same window underneath as fallback for games that refuse the hook | `wasapi_process_output_capture` on the same window (desktop audio when `audio.mode` is `desktop`) |
| Linux, PipeWire available | `pipewire-screen-capture-source`; OBS shows the portal picker once and keeps the restore token | `pulse_output_capture` |
| Linux, X11 | `xcomposite_input` on the pinned window | `pulse_output_capture` |

The app's collection has none of OBS's special audio inputs: OBS creates them only in the
collection of its first run (`docs/m0/obs-behaviour.md` section 1). A special input the user adds
to it later, desktop or microphone, is muted through `GetSpecialInputs` and `SetInputMute`.

`CreateSceneCollection` gives the new collection OBS's default scene `Scene`, which stays the
program scene after `CreateScene Game`; provisioning sets `Game` with `SetCurrentProgramScene`, or
the app would record an empty scene (`docs/m0/obs-behaviour.md` item 13).

The game profile dialog fills its window list from `GetInputPropertiesListPropertyItems(inputName,
propertyName)`, the same call GSM uses, offers enabled items only, and stores the chosen item value
verbatim in `capture.window`. `propertyName` is `window` for `game_capture`, `window_capture` and
`wasapi_process_output_capture`, whose values share the format `<title>:<class>:<exe>` with `#` and
`:` escaped as `#22` and `#3A` (built by one function, so the `game_capture` string is valid for
application audio; provisional until H5). It is `capture_window` for `xcomposite_input`, value
`<xid>\r\n<name>\r\n<class>` (`docs/m0/source-findings.md` section 10). An `xcomposite_input` is
created with a non-empty placeholder `capture_window`: listing the windows of one whose value is
empty aborts OBS 32.2.2 (`std::logic_error` on a NULL item value, `docs/m0/clock.md` side
finding 2), so the app never lists windows of such an input. Input kinds that `GetInputKindList`
does not report are skipped, and the dialog says which capture method is in use.

On the Linux host `xcomposite_input` recorded black with OBS 32.2.2 on EGL, NVIDIA and Mesa alike
(`docs/m0/clock.md` side finding 1); whether real X11 desktops hit this is an H5 check.

### 11.4 Recorder (`obs/recorder.py`)

`start()` = `StartRecord` while `armed`; `stop()` = `StopRecord`. The recorder changes no state
itself: state follows the `RecordStateChanged` events (section 6), so a recording started from the
OBS window behaves identically.

## 12. Auto mode (`lifecycle/auto.py`)

Per game, off by default. It uses only signals the app already has; there is no process scanning.

- **Auto-start**: the first accepted line while `armed` sends `StartRecord`. That line's offset
  would be negative, so it clamps to 0 and part of its audio is missing from the recording. The
  settings text says so. Users who mind start manually before the first line.
- **Auto-stop, idle**: no accepted line for `auto.stop_idle_minutes`. The idle tail cannot stretch
  the last cue, because the cap in section 9 already bounds it; the video simply carries some dead
  time at the end.
- **Auto-stop, window closed**: every 5 s the window list of section 11.3 is read; two consecutive
  "closed" readings stop the session. Only items with `itemEnabled: true` count, because OBS keeps
  listing the configured value as a disabled item after the window closes and also while it is
  open under a changed title (FPS or level in the title), and capture keeps following it. Windows:
  open while an enabled item has the class and exe of `capture.window` (decode `#3A` and `#22`,
  compare case-insensitively), queried on the `game_capture` input, whose list keeps minimized
  windows (provisional until H5). X11: open while item 0 is enabled or an enabled item has the
  stored xid, which R2 confirmed for a retitled and a closed window (`docs/m0/source-findings.md`
  section 10, `docs/m0/obs-behaviour.md` section 7). Windows and X11 only.
  PipeWire capture exposes no window list, so Wayland relies on the idle timeout.

`auto.py` subscribes to the session actor's events and sends it ordinary `UserCommand`s. No other
module knows auto mode exists.

## 13. VAD pass

Optional. Runs as a background job after finalise. If the add-on is missing or fails, the live
subtitle stands and the manifest records why.

### 13.1 Add-on (`addons/vad_addon.py`)

`uv venv` under `~/.anki_miner_game/addons/vad/` with `onnxruntime`, `numpy`, `av` at pinned
versions, plus `silero_vad_v6.onnx` downloaded from a pinned URL and checked against a pinned
sha256. PyAV wheels carry their own FFmpeg libraries, so the app needs no `ffmpeg` binary. The add-on
is a download rather than part of the bundle: it is optional by decision, and onnxruntime, numpy
and PyAV would add roughly 80 MB to an app that sits in the tray beside a game.

Every uv call of either add-on runs with `addons.bootstrap.uv_environment(home, addon)`: uv's
managed Python (`UV_PYTHON_INSTALL_DIR`), cache (`UV_CACHE_DIR`) and tool folders live under the
add-on's own folder, `UV_NO_CONFIG` keeps the user's `uv.toml` out, and `UV_MANAGED_PYTHON` never
lets a system Python in. Without them uv puts its Python in `~/.local/share/uv/python` and its cache
in `~/.cache/uv` (`docs/m0/owocr.md` amendment 2).

### 13.2 Worker (`vad/worker/vad_worker.py`)

A standalone script shipped as data and run by the add-on's Python:

```
<addon python> vad_worker.py --video <path> --model <onnx> [--track 0]
stdout, one JSON object per line:
  {"t":"region","start_ms":5310,"end_ms":8920}
  {"t":"progress","done_ms":600000,"total_ms":5248120}
  {"t":"done"}            or  {"t":"error","message":"…"}
```

`total_ms` is `null` when the file states no duration (a recording cut off by a crash, which
section 6.4 keeps). `--track` counts audio tracks only. Regions are on the file's timeline: a track
that starts after the file shifts them by its start offset, and audio the demuxer lost is replaced
by silence so later regions keep their place.

It decodes the first audio track with PyAV, resamples to 16 kHz mono, and feeds fixed 512-sample
windows through the stateful Silero model, emitting regions as they close. The track is never held
in memory: Anki Miner documents about 230 MB per hour for the naive float32 buffer and a 6 hour guard
because of it (`media_extractor.py:69-74`), and game sessions run long. The numpy implementation is
ported from faster-whisper's `vad.py` (`SileroVADModel`, `get_speech_timestamps`, `VadOptions`;
MIT), which needs neither torch nor ctranslate2. Parameters: threshold 0.5, minimum speech 250 ms,
minimum silence 300 ms, speech pad 100 ms.

### 13.3 Assignment (`vad/assign.py`)

A pure function: `assign(live_cues, regions, text_mode, cfg) -> list[Cue]`. Fixed order, because the
steps interact.

1. **Chains.** For each cue, window = `[start - 200 ms, min(next.start, start + 30 s)]`. The chain
   starts at the first region that begins inside the window and follows regions while the gap to
   the next one is <= 1500 ms and it begins inside the window. A region already in progress at the
   window's start is skipped when another region begins within 2 s; it is usually the previous
   voice's tail.
2. **Starts.** Hook mode: starts never move. OCR mode: the cue's chain may instead begin at the
   latest region that starts inside `[prev.live_end, start]`; if one exists the start snaps to that
   region's start, and the region is removed from the previous cue's chain. In OCR mode this step
   overrides the skip rule of step 1: a region already in progress at the window's start is the
   expected case there, because the line is only emitted once the text has stopped typing.
3. **Ends.** `end = chain.end + 150 ms`, clamped to `next.final_start - end_gap_ms`, then to the cue
   invariant of section 9 (`end >= start + MIN_CUE_MS` where the next start allows, and never past
   it). No chain: keep the live end. A cue may grow past the live 15 s cap, never past that clamp.

Readings of these steps (`docs/m0/wave-1-amendments.md` item 14):

- Windows are half-open: a region that begins exactly at the next cue's live start belongs to the
  next cue.
- OCR mode keeps step 1's skip rule for a cue that finds no snap region.
- The snap interval of step 2 is `[max(prev.live_end, start - SNAP_LOOKBACK_MS), start]`, and the
  first cue's interval opens at `max(0, start - SNAP_LOOKBACK_MS)`. `SNAP_LOOKBACK_MS` is 10 s,
  provisional until tuned against real OCR sessions (M4).
- A cue whose chain is empty keeps its live start and end, except when the next cue's start snapped
  back: its live end then gets the step 3 clamp (`next.final_start - end_gap_ms`, with the
  `MIN_CUE_MS` floor). Hook mode is unchanged.

Worked example, hook mode, regions R1 5.31-8.92 s, R2 9.60-11.05 s, R3 14.2-16.0 s:

| Cue | Live start-end | Window | Chain | Final end |
|---|---|---|---|---|
| 1 | 5.23-13.85 | 5.03-14.20 | R1, R2 (gap 0.68 s) | 11.05 + 0.15 = 11.20 |
| 2 | 14.20-29.20 | 14.00-44.20 | R3 | 16.15 |

Cue 1 drops from 8.6 s to 6.0 s and no longer carries about 2.6 s of music; cue 2 drops from the 15 s cap
to 2.0 s. The 1500 ms gap is what keeps a dramatic pause inside one voiced line from cutting it
short; it is a constant to tune in M3 against real recordings, like the other thresholds.

The trimmer writes the new `.srt` atomically and updates `vad` in the manifest. `live_cues` is left
alone, so "Re-run VAD" and "Restore untrimmed subtitle" are both a re-write from the manifest and no
second `.srt` ever exists.

## 14. OCR add-on

For games with no working hook. owocr runs as a managed subprocess, so OCR is one more websocket
text source and none of owocr's roughly 20k lines enter this app.

Install (`addons/ocr_addon.py`): `uv tool install --python 3.12 "owocr[<extra>]==<pinned>"` into
`~/.anki_miner_game/addons/ocr/`, extra `oneocr` on Windows and `meikiocr` on Linux, with the uv
environment of section 13.1. owocr 1.26.8 needs Python >= 3.11.

On Linux owocr depends on PyGObject, which has no wheel and builds from source only with a C
toolchain and the cairo and GObject-introspection development packages; R3's install stopped at
`Dependency "cairo" not found`. PyGObject serves only owocr's Wayland capture, so the Linux install
adds `--overrides overrides.txt`, a file holding `pygobject; sys_platform == "never"`. It then
installs without system packages, and **Linux OCR is X11-only: Wayland OCR is unsupported in v1**.
The add-on says so on a Wayland session (`docs/m0/owocr.md` amendment 3). The Windows install
floor with `oneocr` is an H5 check.

Command line, built only from flags:

```
owocr -r screencapture -w websocket -wp <free port> -t False -l <lang> -e <engine> -el <engine> \
      -sa "<window title>" -swa <rects>          # Windows: window-relative rectangles
owocr -r screencapture -w websocket -wp <free port> -t False -l <lang> -e <engine> -el <engine> \
      -sa <rects>                                # Linux X11: screen rectangles
```

`-el` carries the same engine as `-e`, so owocr loads that one engine and cannot fall back to a
cloud engine the user did not choose (section 3.4).

- Neither the app nor its owocr child touches `~/.config/owocr_config.ini`, which belongs to the
  user's own owocr. Managed owocr runs with a private `HOME` (`USERPROFILE` on Windows) at
  `~/.anki_miner_game/addons/ocr/home/`, pre-seeded with a minimal `.config/owocr_config.ini`
  holding only `[general]`. owocr parses it, skips the GitHub download, and takes every other
  setting from the command line and its defaults. owocr's other `~`-relative paths (Screen AI,
  OneOCR) land in that home too; whether the model caches follow `HOME` or `XDG_CACHE_HOME` is
  open. `USERPROFILE` redirection with OneOCR is an H5 check.
- **Select OCR area** in the game profile dialog runs owocr once with `-sa`/`-swa` empty, which
  opens owocr's own picker, and reads `Selected coordinates:` / `Selected window coordinates:` from
  its log output into `ocr.rects`. owocr keeps running after the selection, so the dialog kills it
  once the line is read, on a picker-closed line, or when it exits. A closed window picker keeps
  owocr running on the whole window and prints no coordinate line.
- owocr's frame stabilisation, repetition filter and furigana filter stay at their defaults. They
  are why OCR lines arrive late, which the -1000 ms start shift and the VAD start snap compensate.
  In R3 a changed line reached the websocket 54-65 ms after the change, and the first frame came
  3.4 s after spawn with models cached.
- Supervisor: start at arm, stop at disarm; restart on crash with backoff, three attempts, then a
  banner. The attempt count resets after 60 s of stable running, so only exits in a row add up.
  An exit with a known configuration error (`Invalid coordinate set(s)`, `Window capture is only
  currently supported`, `Picker window was closed`) is not a crash: it shows a banner without
  spending the restarts. The banner quotes owocr's real error, never its closing `Terminated!`
  line, and a traceback in the log (the stdin `termios` one of a non-TTY start) is not a failure.
  The whole process tree is killed through a job object with kill-on-close on Windows (provisional
  until H5) and a process group on POSIX: owocr starts in its own session, and a stop sends SIGTERM
  to the group, waits 2 s for it to empty, then sends SIGKILL to the group. Signalling the parent
  alone orphans the picker child and `multiprocessing.resource_tracker`, and SIGTERM alone is not
  enough because the tracker ignores it while any other member lives (`docs/m0/owocr.md` "Process
  tree"). Disarm and quit wait for the kill (`TextSource.wait_closed`). A crash of the app itself
  is covered on Windows by the job object; on Linux owocr leads its own session and keeps running, still capturing and serving on
  `0.0.0.0`, until the user ends it. Accepted risk for v1; the user guide says how to end it.
- owocr binds `0.0.0.0`. The user guide mentions the Windows firewall prompt and that the OCR text
  is reachable from the local network while it runs.
- Cloud engines (`glens`, `bing`) are selectable per game and off by default. Both are free and
  keyless; the dialog states that screenshots leave the machine.

## 15. Text feed (`feed/`)

Covers the request recorded in `docs/FUTURE_IDEAS.md`: players still need dictionary lookups while
playing.

- `ws_server.py`: `websockets` server on `127.0.0.1:<feed.ws_port>` that sends every accepted line
  to every client as a plain-text frame. Plain text is what Textractor's extension emits, so Renji's
  texthooker-ui and similar pages work when pointed at this port.
- `http_server.py`: stdlib `http.server` on `127.0.0.1:<feed.http_port>` serving one file,
  `page.html`, as `text/html; charset=utf-8`. The page connects to the websocket, appends each line
  as a paragraph, keeps the newest in view, shows a line counter and a connection light, follows the
  system light or dark scheme, and has no dependencies. Yomitan works on it as on any page.
- Two ports, because the `websockets` maintainers' answer to serving HTTP and WebSocket on one port
  is "You don't".
- It matters most for OCR and clipboard sessions, which have no texthooker page of their own.

## 16. UI and control

Main window, compact and single-column:

1. Status row: OBS, each enabled text source, OCR when in OCR mode. Four states per light
   (disconnected, connecting, connected, receiving).
2. Game dropdown, **New game…**, **Edit…**.
3. **Arm** / **Disarm**, then **Start** / **Stop** with the elapsed time and cue count.
4. Live list: the last 200 accepted lines, read-only.
5. Recent sessions: name, duration, cue count, VAD state; one action, **Open folder**.
6. Inline banner area. Recoverable failures are banners, never modal dialogs.

Tray: Arm, Start/Stop, Open text feed, Show window, Quit. Closing the window minimises to tray
while armed or recording.

First-run wizard: (1) OBS found, websocket enabled, connection made, profile and scene collection
created; (2) text sources, each showing "waiting for a line" until one arrives; (3) output folder;
(4) optional add-ons with sizes. Every step can be re-run from Settings.

Step 1 states what the app changes in OBS, before it changes anything:

- It turns on OBS's websocket server when it is off and OBS is closed (section 11.1).
- It creates a profile and a scene collection named `Anki Miner Game` and switches OBS to them
  while armed, back to the user's own when disarmed.
- In its own profile only: the record folder, output size and frame rate, the `.mkv` container,
  file splitting and automatic remux off, a recording encoder separate from the stream encoder so
  that pause works, and the audio sample rate and channels copied from the user's profile. Stream
  settings and the user's own profiles are not touched.
- In its own collection only: the scene `Game` with the capture and audio inputs, set as the
  program scene.
- OBS itself turns off its offer to run the auto-configuration wizard for new profiles
  (`[Basic] ConfigOnNewProfile` in `user.ini`) when the app creates its profile.

Global control:

- Windows: `RegisterHotKey` through `ctypes` with a `QAbstractNativeEventFilter` for `WM_HOTKEY`.
  Toggles Start/Stop while armed.
- Linux: single-instance guard through `QLocalServer`; `anki_miner_game --arm <slug> | --start |
  --stop | --toggle` sends the verb to the running instance and exits. Users bind the command in
  their desktop's shortcut settings, which works on Wayland, where no application can grab a global
  key. The same verbs work on Windows.
- No `pynput`.

The hand-off text shown after each session (Appendix C) tells the user what to do in Anki Miner.

## 17. Error matrix

| Situation | Behaviour |
|---|---|
| OBS not installed | wizard step 1 blocks with a download link; re-check button |
| OBS not running | Arm launches it minimised, waits up to 30 s for a successful `GetVersion` |
| No `GetVersion` within the wait | banner: OBS may be waiting on a dialog in its own window, such as "OBS Studio Crash Detected" after a crash |
| Websocket server disabled | fix automatically when OBS is closed; otherwise instructions and a Fix button |
| Authentication fails | re-read `config.json` once; then a banner asking for the password override |
| A required request is missing | refuse to arm; banner names the request and the OBS version |
| An output is active at Arm | refuse; banner names it |
| Profile or collection switch times out | restore the previous names; banner |
| `CurrentProfileChanged` arrives, the switch's answer does not | banner "OBS is asking to restart", pointing at OBS's window (section 6.2) |
| `StartRecord` fails | failure = no `STARTED` within 10 s of `StartRecord` and `GetRecordStatus` inactive. OBS answers a failed start with success and shows its message as a modal in its own window, never on the websocket (`docs/m0/obs-behaviour.md` item 11), so the banner says to look at OBS's window; state stays `armed` |
| No text source connected at Start | recording starts; persistent warning banner |
| Connection lost while recording | lines keep being journalled on `EventClock`; reconcile on reconnect |
| OBS exits while recording | finalise with flag `obs_exited` |
| `RecordFileChanged` | finalise the first file; flag `split_unsupported`; banner |
| Zero cues at stop | keep the video, no subtitle, flag `no_cues`, banner |
| Rename fails after retries | state `finalise_pending`; retried at next launch |
| Output folder not writable | refuse to arm; banner |
| Under 5 GB free at Arm | arm anyway; warning banner with the free space. OBS itself stops a recording when the disk fills |
| Feed port in use | feed disabled for the run; banner names the port |
| owocr exits repeatedly | three restarts, then a banner; the recording continues |
| VAD add-on missing, or the worker fails | live subtitle stands; session row shows why |
| Unclean previous exit | restore OBS from `obs_restore.json`; finalise orphans; no prompt |

## 18. Testing

### 18.1 Unit (no network, no Qt unless stated)

| Area | Cases |
|---|---|
| `session/cues.py` | every row of the table in section 9; burst of click-through lines dropped as a whole; last line against the stop offset; OCR shift clamping at 0 and at `prev.start`; property: the invariant holds for arbitrary non-decreasing offsets (hypothesis) |
| `session/srt_writer.py` | millisecond formatting at 0, 999, 3 599 999 and beyond one hour; byte-exact output; parses back through pysubs2 to the same cues |
| `session/clock.py` | both clocks; pause spans; lines during a pause return `None`; monotonic clamp; re-anchoring |
| `session/naming.py` | the table in section 10.1; contract test below |
| `session/journal.py` + finalise | each journal record type; finalise after a crash at every step boundary; missing `stop` record; NN reservation with files, manifests and both present |
| `text/pipeline.py` | one test per step in section 8.2, and the order between steps 4-8; typewriter merge on and off, including え then えっと… |
| `models/` | `GameProfile` validation rejects hook sources in OCR mode; JSON round trip with `schema` |
| `vad/assign.py` | the worked example; region in progress at window start; OCR start snap claiming a region from the previous chain; no regions; end clamp against a snapped next start |
| Reconcile | every row of the table in section 6.3 |

**Naming contract test.** The repository vendors `EpisodeInfo`, `_strip_technical_tokens` and the
whole `EpisodeNumberExtractor` class from Anki Miner's `anki_miner/utils/episode_matcher.py` into
`tests/contract/anki_miner_episode_matcher.py`, with the source commit in the file header. The test
is property-based: for arbitrary Unicode titles and NN in 1-9999, the vendored extractor must return
exactly NN and no season for `stem(title, NN)`. An adversarial generator mixes the tokens the
extractor deletes with S/E fragments, spaced hyphens and Unicode whitespace; the port of the
deletion must equal the vendored one. `scripts/diff_vendored_matcher.py <anki_miner checkout>`
diffs the vendored copy against a checkout; it is not part of CI, because CI has no such checkout.

### 18.2 Fakes and integration

- `FakeObsServer`: an obs-websocket v5 server (hello, identify, request/response, events) built on
  `websockets`, in the style of Anki Miner's `tests/e2e/fake_ankiconnect.py`. It replays
  **transcripts recorded from a real OBS** in M0: normal session, pause and resume, reconnect
  mid-session, missed pause event, OBS exit, profile and collection switch, a refused switch.
  Hand-written fakes would only encode the author's assumptions. R2 recorded them into
  `tests/fixtures/obs_transcripts/` (23 real, 1 labelled synthetic; its `README.md` says how each
  was made).
- `FakeHookerServer`: broadcasts scripted lines on a schedule driven by an injected clock.
- Integration: scripted session against both fakes, asserting a byte-exact `.srt`, the manifest
  counts and the final file names. One run per transcript.
- VAD worker (`vad` marker, needs the add-on environment): a synthetic tone yields no regions; a
  public-domain speech clip yields one; a three-hour synthetic track stays under a fixed memory
  ceiling.
- Qt: `pytest-qt`, offscreen platform, every top-level widget registered with `qtbot.addWidget`,
  isolated `ANKI_MINER_GAME_HOME` per test. Same rules as Anki Miner's suite.

### 18.3 Sync probe

The product's one quality claim is that a cue starts when its line appears. Nothing above measures
that, so a probe does, against a real OBS:

1. A script opens a black window, and at scripted moments turns it white for three frames while
   sending a line to a local websocket the app listens to.
2. The app records a session as normal.
3. The script decodes the recording, finds the first white frame of each flash, and compares its
   timestamp with the matching cue start.

Pass: |cue start - flash| < 150 ms for every flash, including flashes after a pause. Run on the
Win11 VM and the Linux host for M0 and again before each release. It needs a display and a real
OBS, so it is a scripted manual gate, not a CI job.

### 18.4 Manual QA before a release

Real OBS with Textractor, Agent and LunaTranslator in turn, one voiced and one unvoiced free visual
novel demo, one OCR session, on Windows and on Linux. Then the pair is mined in Anki Miner through
Video -> Single and through Video -> Batch over a folder of three sessions, and ten cards are
checked by ear and eye: audio starts with the voice, ends with it, screenshot shows the right line.

## 19. Packaging, CI and release

Copied in shape from Anki Miner, not shared as code:

- PyInstaller one-folder build from a `.spec`; Windows Inno Setup installer; Linux AppImage, `.deb`
  and `.tar.gz`. No macOS lane.
- `scripts/health.sh` as the definition-of-done gate: black, ruff, mypy, pytest with the `vad` marker
  excluded, never stopping at the first failure.
- `ci.yml`: lint, typecheck, tests on Python 3.12 and 3.13, Windows and Linux runners, because the
  platform branches are the risky code.
- `release.yml` on `v*` tags: validates the tag against `anki_miner_game/__init__.py:__version__`,
  builds the matrix, smoke-launches each bundle offscreen with an isolated home and asserts that
  `config.json` and a log file appeared. `workflow_dispatch` on the same file is the dry run, as in
  Anki Miner.
- The bundle contains no onnxruntime, numpy, PyAV or owocr; the spec `excludes` them so their
  absence is a guarantee, and the smoke test asserts it.
- `addons/bootstrap.py` downloads a sha256-pinned `uv` for the platform into
  `~/.anki_miner_game/bin/`: HTTPS only, host allowlist, size cap, `.part` file, hash check, atomic
  replace, executable bit. It is a minimal copy of the idea in Anki Miner's
  `services/_install_common.py`. It is deliberately not a port of `mokuro_installer.py`, whose
  imports (`resource_downloader`, `download_resume`, `process_supervisor`, resolvers, receipts) would
  drag a large slice of Anki Miner into a second repository for two optional add-ons. No resume, no
  receipt files, no resolver tiers.

## 20. Milestones

### M0: spikes

Throwaway scripts against a real OBS on the Win11 VM and the Linux host. Each has a pass criterion;
a failure changes this design before code is written.

Results, 2026-09-21: source research (S1) and three spikes on the Linux host (R1 clock, R2 OBS
behaviour, R3 owocr) against Flathub OBS 32.2.2. Windows rows wait for pre-release QA (H5, owner
decision D2); the values they feed are marked provisional where they appear.

| Spike | Pass criterion / output | Result |
|---|---|---|
| Sync probe and clock | Measure the `StartRecord` response, `STARTING` and `STARTED` against flash timestamps on at least two encoders including one hardware encoder, with x264 lookahead on, and under encoder overload. Output: the zero event and `capture_latency_ms`. If the spread across encoders exceeds 100 ms, a per-install calibration step is designed before M1; otherwise one constant ships | Linux pass: zero `STARTED`, `capture_latency_ms` 10, spread 50 ms across NVENC and x264 (lookahead on), so one constant and no calibration. Largest residual 50 ms, 133 ms under overload. `OutputDurationClock` trails by 0.46-5.6 s (section 7). Windows: H5 (`docs/m0/clock.md`) |
| Pause | Offsets after a pause and resume stay within the 150 ms bound | Pass with a separate recording encoder (under 10 ms at the median). OBS's default profile cannot pause at all, hence the recording-encoder row of section 11.3 |
| Profile and collection switch on a user's live OBS | Duration; behaviour with each kind of output active; restore works; events arrive in the documented order. Record the transcripts | Switches take 24-143 ms; OBS switches under any active output and keeps it running; no event for a switch to the current target; the answer came before `...Changed` twice in 27; unequal sample rates stop a profile switch at a modal restart question. 23 real transcripts in `tests/fixtures/obs_transcripts/` (`docs/m0/obs-behaviour.md`) |
| Settings without restart | Which rows of the provisioning table apply to the next `StartRecord`. Confirm the `basic.ini` key names for container and file splitting from a real profile | Keys confirmed. All rows apply at the next start except the container, which needs the profile re-activated (switch away and back); no OBS restart |
| Application audio capture | The window string format `wasapi_process_output_capture` accepts, and that it matches the `game_capture` string | Source: one function builds all three kinds' strings, so they match. Runtime: H5 |
| Rename after `STOPPED` on Windows | How long the handle stays locked; the retry window covers it | H5 |
| `RegisterHotKey` | Fires while a fullscreen game has focus, and while an elevated game has focus | H5 |
| owocr | The coordinate log line parses; the process tree dies through the uv shim on both platforms; exact install floor | Linux: `Selected coordinates:` captured and parsed (the window line is Windows-only, fixtures synthetic); the tree dies with SIGTERM, a grace period and SIGKILL to the process group; the install needs PyGObject overridden out, so Linux OCR is X11-only. Windows: H5 (`docs/m0/owocr.md`) |
| QClipboard on Wayland | What is and is not received, for the wizard text | Dropped from M0: section 8.1 already fixes the wizard text; behaviour is checked at H5 |
| Disk rate | GB per hour at 1080p30 and 720p30 with OBS's default encoder settings, for the user guide and the free-space warning | 2.78 GB/h at both 1080p30 and 720p30 (CBR 6000 kb/s video, 160 kb/s audio; NVENC and x264 alike): with OBS's defaults resolution does not change it |
| OBS discovery | Registry key on Windows; Flatpak launch command; the minimum OBS version that has every request | Registry `HKLM\SOFTWARE\OBS Studio` from the installer source (provisional until H5); `flatpak run com.obsproject.Studio --minimize-to-tray` confirmed; minimum OBS 30.0.0 |

### M1: core loop, on the app's own profile from the first commit

OBS gateway with reconcile; minimal provisioning (profile and record directory only); arming and
the ownership rule; recorder; websocket sources; pipeline; both clocks; journal, cues, SRT writer,
manifest and finalise; naming with the contract test; a bare window with Arm, Start and Stop.
Exit: a Textractor session on Windows produces a pair that Anki Miner mines.

Ownership and the record directory are in M1 rather than later because without them the app would
rename files out of the user's own OBS recordings folder.

### M2: usable app

Full provisioning (scene collection, capture and audio inputs, window picker); game profiles;
wizard; tray; hotkey and CLI verbs; auto mode; text feed; clipboard source; error banners; recent
sessions.

### M3: VAD

Add-on bootstrap; VAD add-on; worker; assignment; threshold tuning against at least three real
recordings (voiced visual novel, voiced RPG with music, unvoiced); re-run and restore actions.

### M4: OCR

OCR add-on; supervisor; area selection; OCR mode in game profiles; start shift and VAD start snap
tuned against real OCR sessions.

### M5: release

Packaging lanes, CI, release workflow and dry run, user guide (quick start per hooker, the Anki
Miner hand-off, troubleshooting), first tagged release.

## 21. Risks

| Risk | Mitigation |
|---|---|
| Cue-to-frame sync is worse than assumed, or varies by encoder | M0 measured a 50 ms spread across encoders on Linux, so one constant ships; the clock is a Protocol with two implementations. A Windows spread over 100 ms would surface only at H5 (accepted risk, D2); calibration stays the designed fallback |
| Switching a user's OBS profile and collection misbehaves | M0 spike; refusal while outputs are active; restore file; ownership rule |
| Hook text is not dialogue (menus, choices, narration bursts) | pipeline drops, the skip rule, and Anki Miner's curator downstream. The text feed makes a bad hook visible immediately |
| Unvoiced games | cards carry music only; the sentence and screenshot are still correct. The VAD pass leaves such cues at their live ends |
| Disk use | 2.8 GB per hour at 1080p30 and at 720p30 with OBS's default encoder settings (measured in M0: OBS's default is constant bitrate, so 720p does not reduce it); free-space warning at Arm |
| owocr changes its flags or log format | pinned version; the parse is covered by a test against captured output; upgrades are deliberate |
| GSM makes Longplay good | this app still differs by install size, setup time and having no Anki coupling; its output would stay compatible |
| A second app to release and support | packaging copied from a working pipeline; no macOS lane; no shared library to keep in step |
| Windows-only hookers, Linux-based development | Win11 QA VM is part of the M0 and release gates |

## 22. GSM reuse map

GSM at `479747fe`. Ported code keeps a header naming the GSM file and commit.

| GSM | Use here | Note |
|---|---|---|
| `GameSentenceMiner/gametext.py::listen_on_websocket` (`:629`) | `text/sources/websocket_source.py` | port: connect, Luna path fallback, plain/JSON parse, reconnect. Add the non-dict JSON guard. Drop rate limiting, overlay, database and pause plumbing |
| `gametext.py::resolve_websocket_source_name` (`:493`), `configuration.py::WELL_KNOWN_WS_SOURCES` (`:580`) | default source list | copy the three defaults |
| `gametext.py::monitor_clipboard` (`:409`) | reference only | replaced by `QClipboard.dataChanged` |
| `longplay_handler.py::LongPlayHandler` (`:17`) | `session/session.py`, `session/journal.py` | port the event-driven lifecycle (`on_record_state_changed` `:54`, `on_record_file_changed` `:74`) and `_rename_path_with_retry` (`:275`). Do not port `_write_srt_line_locked` (`:216`) or `_format_srt_time` (`:302`): one-line-late SRT appends and the floored-seconds formatter are what this design replaces |
| `obs/launch.py::get_obs_websocket_config_values` (`:440`) | `obs/discovery.py` | port the read; keep auth on instead of turning it off |
| `obs/launch.py::parse_obs_window_target` (`:134`), `get_video_source_priority` (`:59`) | `obs/provision.py` | port |
| `electron-src/main/ui/obs.ts::createSceneWithCapture` (`:2099`) | `obs/provision.py` | TypeScript; reference for the request sequence and input settings |
| `obs/actions.py` replay-buffer functions | none | replay buffer is a non-goal |
| `vad.py` | none | GSM's Silero path goes through faster-whisper; this app ports faster-whisper's `vad.py` directly |
| `owocr/` fork, `ocr/` | none | upstream owocr as a subprocess instead |

From faster-whisper (MIT): `faster_whisper/vad.py` (`VadOptions`, `get_speech_timestamps`,
`SileroVADModel`) and the `silero_vad_v6.onnx` asset, for `vad/worker/vad_worker.py`.

From Anki Miner (same maintainer, GPL-3.0): the ideas in `services/_install_common.py`
(`verify_sha256`, `.part` cleanup), the fake-server test style of `tests/e2e/fake_ankiconnect.py`,
the home-isolation fixture pattern of `tests/_home_isolation.py`, the PyInstaller spec, `health.sh`
and the release workflow shape. Copied, not imported.

## Appendix A: what Anki Miner does with the pair

A session folder mined through Video -> Single: the video's folder name becomes the series name and
the stem becomes the episode name (`orchestration/episode_processor.py`, `_resolve_identity`), so
cards and statistics read `Steins;Gate` / `Steins;Gate - 03` with no extra work, and sessions stay
distinct in `stats.db`.

Recommended Anki Miner settings for game sessions, for the user guide: leave `audio_padding` at
0.3 s (the 350 ms end gap assumes it; raise `cue.end_gap_ms` by the same amount if it is raised);
leave `screenshot_offset` at 1.0 s; keep sentence de-duplication on, since games repeat lines.

## Appendix B: verification log

| Claim | How it was checked |
|---|---|
| Anki Miner line numbers in section 3.1 | grep at commit `380e83f3`, 2026-09-20 |
| Naming table in section 10.1 | run through `EpisodeNumberExtractor.extract_episode_info`, 14 of 14 correct |
| Cue table in section 9 | formula evaluated in Python for each row |
| GSM symbols and line numbers | grep in a clone at `479747fe` |
| Longplay behaviour | read `longplay_handler.py` and GSM's `docs/features/longplay.md` |
| owocr flags, bind address, config path, log lines, `-r obs` behaviour | grep and read in a clone of 1.26.8 |
| obs-websocket requests, events and fields | Context7, `/obsproject/obs-websocket` protocol documentation |
| `websockets` stance on one-port HTTP | upstream FAQ, `docs/faq/server.rst` |
| faster-whisper VAD symbols and model file | installed package in Anki Miner's environment |
| OBS facts in sections 3.3, 6, 7, 11 (M0 S1) | read in obs-studio 32.2.2 (`ba2f32bd`), obs-websocket 5.7.4 (`1ef34bf4`), obsws-python 1.8.0, bouf v0.6.5; cites machine-checked; `docs/m0/source-findings.md` |
| Zero event, `capture_latency_ms`, pause, fallback lag, disk rate (M0 R1) | flash probe against real OBS 32.2.2 on Linux, 20 sessions over NVENC and x264, overload included; `docs/m0/clock.md` |
| Profile keys, restart rows, switches, events, reconnect, exit, window list (M0 R2) | real OBS 32.2.2 on Linux through a logging proxy, `ffprobe` on the files; transcripts in `tests/fixtures/obs_transcripts/`; `docs/m0/obs-behaviour.md` |
| owocr log lines, process tree, install, config file (M0 R3) | owocr 1.26.8 installed with uv and run on Linux X11; fixtures in `tests/fixtures/owocr/`; `docs/m0/owocr.md` |
| Sanitiser in section 10.1 (amended) | contract test against the vendored extractor at `ea4a30ce`, 2000 adversarial examples a run; offline campaign of 2 million titles; `docs/m0/sanitiser.md` |
| Readings from the wave 1 integration review | `docs/m0/wave-1-amendments.md`; the code follows each one |

Not verified before M0, and assigned to it: the `basic.ini` key names for container and file
splitting; the OBS registry key on Windows; the pause output-state names; the relation between
`outputDuration` and file timestamps; whether profile changes need an OBS restart; the
application-audio window string; the minimum OBS version. M0 settled all of them on Linux or in
source. Still open until H5 (D2), and marked provisional where they appear: the registry key on a
real install, the application-audio string at runtime, the Windows zero event and latency with
`game_capture`, the job-object kill of owocr, rename-lock timing, `RegisterHotKey` under a game,
and `xcomposite_input` capture on a real X11 desktop.

Two adversarial review rounds were run on this design before it was written up (one judge each,
criteria: correctness, simplicity, reuse, scope, verification). Round 1: 14 findings. Round 2: 8.
All were adopted except one: bundling onnxruntime, numpy and PyAV into the app, which conflicts with
the decision that VAD is an optional download.

## Appendix C: hand-off text shown after a session

> Saved `Steins;Gate - 03`. To mine it in Anki Miner: Video -> Single, choose the `.mkv`; the
> subtitle fills in by itself. To mine every session of this game at once: Video -> Batch, and
> choose this folder for both the video and the subtitle folder.
