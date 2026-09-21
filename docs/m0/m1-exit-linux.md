# E1: Linux M1 exit probe

Run 2026-09-22 on the Linux host (KDE Plasma on Wayland, RTX 5070 Ti Laptop GPU, driver
595.91.07). OBS Studio **32.2.2** Flatpak (obs-websocket **5.7.4**), the build R1 and R2 used, as an
X11 client of an isolated nested display. The app is this branch: integration/wave-2b-early with
T16 merged (`anki_miner_game.launch`, the minimal window, CLI verbs, auto mode wired). Anki Miner at
`ea4a30ce`, run read-only from its own environment.

Master plan card E1; spec 3.1, 11.3, 12, 16 ("Global control"), 18.3, 18.4, 20 (M1 exit).

## Result

| Check | Result |
|---|---|
| Sync probe `--app`, cue start against the first white frame | **Pass**: 27 of 27 flashes within 150 ms, largest error +37 ms, a pause included (section 1) |
| Hand-off to Anki Miner, three sessions of one game | **Pass**: same-stem auto-fill, batch pairing, the subtitle read back, 38 words mined, three audio clips of the expected length (section 2) |
| E1-PROVISION-REPLAY | Recorded: `tests/fixtures/obs_transcripts/app_provision.jsonl`, three arms, replayed request by request against `ObsProvisioner` (section 3) |
| `INPUT_RELEASE_TIMEOUT_S` | Measured 3.6 to 33.7 ms over 21 removals, never more than a frame; set to **1.0 s** (section 3) |
| Window pick, `list_windows` | Pass: the placeholder input lists the live windows; the flasher's item was picked and pinned (section 4) |
| Auto mode | Pass: the first line started the recording; closing the window stopped it 9.4 s later, two polls (section 4) |
| Session driven by the CLI verbs | Pass: every `--arm`, `--start` and `--stop` answered 0; `StartRecord` or `StopRecord` reached OBS 200 to 220 ms after the verb process started |
| Owner environment | Unchanged (section "Owner environment") |

M1's exit on Windows (a Textractor session mined in Anki Miner) stays with H5 (D2).

Findings for the orchestrator are in section 5; item 1 is an app defect (not fixed here, T15's file).

## Setup

- **Displays.** Two, each from `tools/nested_display.py serve --caller e1-linux-exit --width 1920
  --height 1080` (private XDG dirs under `.orchestration/m0/data/e1-linux-exit/xdg`, a private
  `/tmp/amg-*` runtime dir, a private bus without service activation): a **rootless** one (kwin is
  the window manager, so OBS lists X11 windows) for the provisioning replay, the window pick and
  auto mode, and a **rootful** one (`--rootful`, XSHM works, no window manager) for the sync probe
  and the hand-off sessions (`tools/m0/README.md`, "Rootless or rootful Xwayland").
- **OBS.** Started first in each display with `nested_display.flatpak_x11_argv("com.obsproject.Studio")`,
  ready once `GetVersion` answered (a direct client), quit with SIGINT once no output was active.
  The config root started from R1's final snapshot (as R2 left it: profile and collection
  `Untitled` only) with `[Audio] SampleRate=44100` seeded in `Untitled`'s `basic.ini`; it was
  snapshotted before the run and restored at the end (`diff -r` empty).
- **The app.** `anki_miner_game.launch.main()` through a harness that adds one subscriber writing
  every `SessionEvent` with its `time.monotonic()` to a JSONL file (the app logs neither state
  changes nor banners; finding 4), started with `nested_display.py exec --setenv
  ANKI_MINER_GAME_HOME=<run>/home` and an `output_root` inside the run folder, so it never touched
  `~/.anki_miner_game` or `~/Videos/Anki Miner Game`. Its `config.json` points `obs.port` at
  `tools/obs_transcript_recorder.py` on 4456 (the password still comes from OBS's own
  `config.json`), lists only the probe's hookers on non-default loopback ports, and moves the feed
  to 26678/26679. Arms, starts and stops were CLI verbs (`anki_miner_game --arm SLUG | --start |
  --stop`) run in the same display with the same home; quitting was the window's close
  (`WM_DELETE_WINDOW`).
- **Game profiles.** All `capture.kind` `xcomposite`. `e1-provision` (no window pinned),
  `e1-auto` (the picked window pinned, auto mode on with `start_on_first_line`), `e1-sync`
  (hooker `textractor` = the flasher, no window pinned), `e1-handoff` (hooker `agent` = a
  `FakeHookerServer` the driver ran, no window pinned).
- **Capture.** `xcomposite_input` still records black here (R1 side finding 1; OBS logged
  `Cannot create EGLImage` nine times in the rootless run). In the rootful display, once the app
  was armed, a direct client added an `xshm_input_v2` named `E1 XSHM` to scene `Game`, screen 0,
  cursor off, cropped with `cut_*` to the flasher window (640x360 at 0,0, from `xwininfo`).
  Provisioning left it alone at every later arm, as it leaves every input that is not the app's.
- **Throwaway driver** (not in the repository): `.orchestration/m0/data/e1-linux-exit/driver/`
  (`e1_driver.py`, `app_harness.py`, `xclose.py`, `build_fixture.py`). Raw data per phase under
  `.orchestration/m0/data/e1-linux-exit/runs/`: `rootless/` and `rootful-2/` (steps, app events,
  app and OBS logs, transcripts, flasher logs, the recordings); `rootful/` is a first rootful attempt
  that stopped before the app started (the driver did not wait out OBS's 207 while loading).

## 1. Sync probe (spec 18.3)

Flasher: 20 s initial delay, a flash every 2.5 s plus up to 0.5 s of seeded jitter, three white
frames at 30 fps. Session 1 was started (`--start`) halfway between flashes 0 and 1 and stopped
halfway between 12 and 13; session 2 ran over flashes 16 to 33, and a direct client paused OBS
halfway between flashes 21 and 22 and resumed halfway between 24 and 25 (the app's profile records
with its own encoder, so OBS sends `PAUSED` and `RESUMED`).

`python -m tools.sync_probe.analyse --app --recording "Sync Probe - NN.mkv" --srt "Sync Probe - NN.srt"`:

| Session | Flashes in the file | Cues | Error (cue - flash), ms | Unmatched | Result |
|---|---|---|---|---|---|
| `Sync Probe - 01` | 12 | 12 | +8 to +37, median +23 | none | PASS |
| `Sync Probe - 02` (pause over flashes 22-24) | 15 | 15 | +1 to +34, median +9 | none | PASS |

The three lines sent during the pause were dropped as `paused` (manifest `counts.paused` 3), and
their flashes are not in the file; the offsets after the resume stay inside the bound (+3 to +24).
The error is positive throughout, up to about one frame. R1's `--raw` medians for NVENC with the
same zero (`STARTED`) and `capture_latency_ms` 10 were -15 and -6 ms; the app's cues sit about 20 ms
later. The app stamps a line when its websocket frame arrives, after the flasher's repaint and the
hop, where R1 used the flasher's own timestamp; not traced further, and well inside the bound.
Reports: `runs/rootful-2/sync-app-01.json`, `sync-app-02.json`.

## 2. Hand-off to Anki Miner (spec 3.1, 18.4)

`tools/handoff_probe.py`, run with Anki Miner's interpreter from this repository's root, `-B`, with
`HOME`, `ANKI_MINER_HOME` and `TMPDIR` in `.orchestration/m0/data/e1-linux-exit/handoff/`, on the folder
`Handoff Probe` holding three sessions of four Japanese lines each from the fake hooker:

| Check (Anki Miner code) | Result |
|---|---|
| `sessions` | `Handoff Probe - 01` to `- 03`, each `.mkv` + `.srt` + `.session.json` |
| `same_stem_autofill` (`file_pairing.find_sibling_subtitle`, Video -> Single) | each video fills in its own `.srt`; the `.session.json` is never offered |
| `batch_pairing` (`FilePairMatcher.find_pairs_by_episode_number`, Video -> Batch with the folder as both) | 3 pairs, each session with its own subtitle |
| `subtitle_parse` (`SubtitleParserService.parse_raw_entries`) | 12 cues read back with the app's times and text |
| `subtitle_words` (`parse_subtitle_file`) | 38 words, each with its line's timing |
| `audio_clips` (`MediaExtractorService.extract_media`, config defaults) | cue 1 of each session: 2.451, 2.450 and 2.451 s, the cue plus 0.3 s either side; a screenshot each |

Anki Miner warned "No Japanese audio found ... using first audio stream" once per video: OBS writes
no language tag on its audio track, and Anki Miner falls back to the first stream, which is the
right one. OBS had no audio devices in the display, so the clips are silence of the right length.
`~/.anki_miner` kept its mtime and Anki Miner's checkout gained no file
(`handoff/anki-miner-home-mtime-before.txt`, `handoff/handoff.log`, `handoff/handoff-report.json`).

## 3. E1-PROVISION-REPLAY (spec 11.3; T14's follow-up)

`tests/fixtures/obs_transcripts/app_provision.jsonl` is the app's own traffic through the proxy, in
the rootless display, on an OBS that had never seen the app (its README row says what was done
between the arms). `tests/obs/test_provision_replay.py` finds each arm's provisioning in it and
drives `ObsProvisioner.ensure_profile` and `ensure_collection` against a gateway that answers from
those frames, request by request (`TranscriptGateway`); arm 1 is also replayed against `FakeObs`.

| Arm | What changed in OBS before it | Provisioning's mutating requests | Took |
|---|---|---|---|
| 1 | nothing: no app profile or collection, `Untitled` at 44.1 kHz | `CreateProfile`, `SetProfileParameter` `Audio/SampleRate=44100`, `SetRecordDirectory`, `SetVideoSettings` 1920x1080, both `RecFormat2=mkv`, `RecQuality=Small`, `RecEncoder=obs_x264`, `SetCurrentProfile` `Untitled` and back, `CreateSceneCollection`, `CreateScene Game`, `SetCurrentProgramScene Game`, `CreateInput` x2 | 377 ms |
| 2 | a direct client replaced `Window Capture (X11)` with an `xshm_input_v2` of that name; a muted `Desktop Audio` special input written into the collection file | `RemoveInput`, `CreateInput` (the name was still held at the first check, free at the second) | 210 ms, 202 of them between `RemoveInput` and the free name |
| 3 | a direct client unmuted the special input | `SetInputMute` | 4 ms |

What the transcript settles:

- **The audio copy avoids OBS's restart question** (R2 item 3, proposed from source and not run
  until now). `GetProfileParameter Audio/SampleRate` read 44100 on `Untitled`; after `CreateProfile`
  the new profile read 48000 and got 44100; the switch to `Untitled` and back answered in 50 and 49
  ms with no question, and the quit's restore switched back unasked too.
- **The re-activation gives a Matroska file at once** (R2 item 2): the first recording made on the
  app's profile after arm 1's provisioning (`Auto Probe - 01.mkv`) starts with the EBML header
  `1a45dfa3` and ffprobe reads it as `matroska,webm`.
- **A fresh profile.** `GetProfileParameter` on the new profile answers `parameterValue` equal to
  the default for keys OBS defaults (`SampleRate` 48000, `RecFormat2` `hybrid_mp4`, `RecQuality`
  `Stream`, `RecEncoder` `none`, `Encoder` `obs_x264`) and `null`/`null` for `AdvOut/RecSplitFile`
  and `Video/AutoRemux`, which provisioning reads as off and leaves alone. The new profile's record
  folder is the home folder (the Flatpak's `user-dirs.dirs` is empty).
- **`SetCurrentProgramScene`** is needed and answered 100 (a new collection's program scene is
  `Scene`, R2 item 13).
- **Special inputs.** A collection the app creates has none (`GetSpecialInputs` all null), as R2
  read in source; one written into the collection file loads when OBS switches to the collection,
  and provisioning then reads its mute (`GetInputMute` `{"inputMuted": true}`) and sets it when off.
- **Release of a removed input's name.** A direct client removed an input and polled
  `GetInputSettings` every 2 ms until 600: 22.3 ms in the rootless run; in the rootful run 10
  removals of an invisible `image_source` while armed took 3.6 to 33.5 ms and 10 while recording
  15.3 to 33.7 ms (`runs/*/steps.jsonl`, `xshm_replacement` and `release_delays`). Never more than
  one frame at 30 fps: the name goes with the next render. `INPUT_RELEASE_TIMEOUT_S` was 5.0 s,
  provisional; it is now 1.0 s, about 30 times the longest.

The app's gateway subscribes to no input events, so `InputRemoved`/`InputCreated` are not in the
transcript. The xshm replacement and the unmute went to OBS directly and are not in it either.

## 4. Window pick, `list_windows`, auto mode (spec 11.3, 12)

- **`list_windows`.** While the app was armed with `e1-provision`, `ObsProvisioner.list_windows()`
  over the app's own gateway class listed the placeholder input's windows
  (`runs/rootless/list_windows.json`): the placeholder, disabled, and the live X11 windows, among
  them the flasher, `amg-sync-probe`, value `10485767\r\namg-sync-probe\r\nflasher.py`. That value
  was pinned as `e1-auto`'s `capture.window`, as the game profile dialog stores it verbatim; the
  next arm set it on `Window Capture (X11)` with one `SetInputSettings`.
- **Auto-start.** Armed with `e1-auto`, the first flasher line started the recording
  (`StartRecord` 1 ms after the line; `STARTED` 176 ms later) and became cue 1 at 0 ms, as spec 12
  says.
- **Window-closed stop.** The flasher was ended (its window closed) 12 s into the recording. The
  5 s poll before it listed the pinned item enabled; the next two listed item 0 disabled and no
  enabled item with the xid; `StopRecord` went out 9.41 s after the close, `STOPPED` 0.58 s later,
  and the session finalised as `Auto Probe - 01` with six cues (`runs/rootless/auto-raw.jsonl`,
  `events.jsonl`). The capture itself was black (`xcomposite_input`, above), so whether capture
  follows a retitled window stays with H5 (R2 Limits 3).

## 5. Findings

1. **A quit within about 170 ms of `STOPPED` leaves OBS on the app's profile and collection**
   (T15, `session/session.py`). After the auto-stop the driver quit the app 114 ms after `STOPPED`:
   the quit's restore sent the four output checks, `GetRecordStatus` still answered
   `outputActive: true` (R2 item 9), `_restore_obs` put the restore off, and the app exited without
   retrying (`runs/rootless/auto-raw.jsonl`, the frames after `STOPPED`; `steps.jsonl` `obs_state` "after
   quit 3"). OBS stayed on `Anki Miner Game` / `Anki Miner Game` and started there at its next
   launch. `obs_restore.json` is kept, so the next launch of the app with that home restores; with
   another home (the rootful run) nothing does. The quit-while-recording path already waits for
   `GetRecordStatus` to go inactive (`_await_record_inactive`); the quit while armed does not. A
   small fix: await it in `_shutdown` before `_to_idle()`. A disarm in the same window is
   retried by the idle tick, so only the quit is affected. Not fixed here: outside the card's files,
   and it did not block the probe (the rootful run waited 1.5 s before its quit).
2. **An `--arm` verb given at launch is handled before the first connection's reconcile.** The
   actor queues the verb during its launch duties, before the gateway's `_Connected`, so arm 1 and 2
   ran their provisioning first and the reconcile (`GetVersion`, `GetRecordStatus`) followed
   (`app_provision.jsonl`). Harmless here, since arming refuses while any output is active (spec 6.2
   step 1); worth knowing for T25's scripted launches.
3. **`xcomposite_input` records black** with OBS 32.2.2 on EGL/X11 here, as R1 and R2 found; E1
   used XSHM of a rootful display. Real NVIDIA X11 desktops and PipeWire capture stay with H5.
4. **The app logs no state change and no banner.** A failed arm, a start that timed out or a
   foreign recording shows only in the window; the log has nothing for troubleshooting. E1 needed a
   harness to see them. For T19/T26 to decide.

## Limits

- One host, Linux, the Flatpak build, X11 through a nested display, NVENC (OBS's default here),
  30 fps. The Windows sync probe and hand-off are H5 (D2).
- OBS had no audio devices in the display: the hand-off's audio clips are silent and their length
  is what was checked; the application-audio path is not exercised on Linux.
- The flasher and the hookers are the probe's own; no real hooker (Textractor, Agent,
  LunaTranslator) ran. The hand-off used Anki Miner's services with its default configuration, not
  its GUI.
- Release delays were measured on an idle 32-thread machine; a game loading the GPU was not
  simulated.

## Owner environment

Guard baseline before the first nested run (`guard-before.json`, `owner-mtimes-before.txt`), a guard
check before and after each phase (the driver's `guard` steps) and throughout (each `serve`), and a
final check (`guard-after.txt`, `owner-mtimes-after.txt`): `kwinrc`, `kwinoutputconfig.json`,
`kglobalshortcutsrc`, `gtkrc`, `gtkrc-2.0`, `Trolltech.conf`, the GTK and xsettingsd files,
`~/.anki_miner_game` and `~/Videos/Anki Miner Game` (both absent throughout) unchanged;
`/run/user/1000/doc` stayed `fuse.portal`; `~/.anki_miner` kept its mtime. No nested kwin,
Xwayland, dbus-daemon, OBS, flasher or `/tmp/amg-*` dir was left. `~/.config/owocr_config.ini` does
not exist. The OBS config root was restored to the snapshot taken before the run (`diff -r` empty);
the state it reached is kept in `obs-root-after-runs/`.
