# M0 R2: OBS behaviour spike

Run 2026-09-21 on the Linux host (KDE Plasma on Wayland, RTX 5070 Ti Laptop GPU, driver 595.91.07).
OBS Studio **32.2.2** Flatpak (obs-websocket **5.7.4**), the build R1 used. Source cites are against
the clones at obs-studio `ba2f32b` and obs-websocket `1ef34bf`, the same ones S1 used; runtime
claims cite a transcript in `tests/fixtures/obs_transcripts/` (the fixture set, documented in its
`README.md`) or a raw run under
`/home/light/Projects/anki_miner_game/.orchestration/m0/data/r2-obs-behaviour/runs/`.

Spec 20 rows "Profile and collection switch on a user's live OBS" and "Settings without restart",
the Linux half of "OBS discovery"; spec 3.3, 6.2 to 6.4, 7, 11.1 to 11.3, 12, 17 and 18.2; the [R2]
items of `source-findings.md` section 13.

## Summary for the M0 gate

| # | Spec | Finding | Proposed amendment |
|---|---|---|---|
| 1 | 3.3, 11.3 | Keys confirmed in a real `basic.ini` written by the provisioning requests: `[SimpleOutput] RecFormat2=mkv`, `[AdvOut] RecFormat2=mkv`, `[AdvOut] RecSplitFile=false`, `[Video] AutoRemux=false`; `SetRecordDirectory` wrote `[SimpleOutput] FilePath` and `[AdvOut] RecFilePath`; the file holds `[Video] OutputCX`, `OutputCY`, `FPSType=2`, `FPSNum`, `FPSDen` (720p at 30/1 was already OBS's default for this 1080p base) | Drop "unverified" from both 11.3 rows |
| 2 | 11.3 restart question | Record folder, output size, frame rate and split apply at the next `StartRecord` without a restart (after `SetVideoSettings` 854x480 at 24/1 the next file was 852x480 at 24/1, item 16). The container does not: the first recording after provisioning is MP4 data in a `.mkv` file; after switching to another profile and back, the next one is Matroska | Provisioning re-activates the app's profile (switch away and back) after it changes `RecFormat2`; no OBS restart |
| 3 | 6.2 step 3, 11.3 | A profile switch between profiles whose `[Audio] SampleRate` or `ChannelSetup` differ opens OBS's modal "Restart" question. `CurrentProfileChanging` and `CurrentProfileChanged` arrive, the `SetCurrentProfile` answer does not (20 s, nobody answers). OBS writes both keys into every profile it saves, so a user at 44.1 kHz meets it on the first arm. With equal values nothing is asked | The app's profile carries the user profile's `SampleRate` and `ChannelSetup` (read with `GetProfileParameter` before `CreateProfile`, written with `SetProfileParameter` once the new profile is active; not exercised here: the matched run seeded the file before launch). Arming that sees `...Changed` but no answer within a few seconds shows a banner: "OBS is asking to restart" |
| 4 | 6.2 step 3 | Switching to the profile or collection that is already current answers 100 and sends **no** event | Step 3 skips a switch whose target is already current (the names from step 2); otherwise it waits for `...Changed` |
| 5 | 6.2 step 3, 11.2 | Usual order is `...Changing`, `...Changed`, then the answer, but the answer came first twice (by 0.2 and 14 ms). The first event on a newly identified event connection arrives about 40 ms after `Identified`, whatever caused it | A step completes on its `...Changed` event, never on the answer (except item 4). Nothing depends on response-versus-event order |
| 6 | 6.2 step 1, 17 | OBS switches profile and collection under an active recording, stream or replay buffer and keeps them running; a recording started on the user's profile keeps writing into the user's folder. `GetReplayBufferStatus` and `GetVirtualCamStatus` answer 604 when that output is not configured or not installed | Step 1 stays the only guard (as S1 found). 604 from either status request means "not active" |
| 7 | 6.3, 7, 17 | Pause on OBS's default profile ("Same as stream") is a no-op: `PauseRecord` answers 100, no event, and `ResumeRecord` then answers 503. With a separate recording encoder: `PAUSED` (`outputActive: false`) and `RESUMED`. A pause made while the app was disconnected is visible only as `GetRecordStatus.outputPaused: true` after the reconnect | Confirms S1 item 3 and the 6.3 row "paused flag differs"; no new change |
| 8 | 6.3 | After a reconnect, `GetOutputSettings {outputName}` returns the active file's `path`, identical to `STARTED.outputPath`. `outputBytes` against the file size is useless: the file trails by up to 1.9 MB and is 0 bytes 5 s in | Add `GetOutputSettings` to `REQUIRED_REQUESTS` (5.0.0, floor unchanged) and match the manifest by that path, on `simple_file_output` or `adv_file_output` per `[Output] Mode` |
| 9 | 7, 10.2 | The video ends at `StopRecord` / `STOPPING` (within one frame); `STOPPED` arrives 560-620 ms later (Simple, NVENC) or about 1255 ms later (Advanced, x264). `GetRecordStatus` still says active for about 170 ms after `STOPPED`. The last `outputDuration` equals the file's video length within one frame | The stop offset is the `STOPPING` receipt time, not `STOPPED`'s (else the last cue can run up to 1.3 s past the video) |
| 10 | 7 split, 17 | `RecordFileChanged` came 5.6 s after `SplitRecordFile` (the split waits for a keyframe). After a split, `STOPPED.outputPath`, `StopRecord.outputPath` and `GetOutputSettings.path` all still name the **first** file; only `RecordFileChanged` names the second. `RecSplitFile=false` sent at runtime applies to the next start | Matches spec 7's "finalise against the first file"; T15 must not read `STOPPED.outputPath` as the last file |
| 11 | 17 `StartRecord` fails | A failed start answers `StartRecord` with 100. Missing folder: no event at all. Read-only folder: `STARTING`, then nothing. Never `STOPPED`. OBS's message is a modal in OBS, not on the websocket | 17: "no `STARTED` within 10 s of `StartRecord`, and `GetRecordStatus` inactive" is the failure; the banner cannot quote OBS and says to look at OBS's window |
| 12 | 6.4, 11.1 | Clean exit: `ExitStarted`, then close 1001 "Server stopping." 330 ms later, no `STOPPED`, the `.mkv` intact. SIGKILL: close 1006, no `ExitStarted`, `.mkv` intact. The next launch stops at the modal "OBS Studio Crash Detected" with no websocket | Confirms 6.4 and S1 item 11 (`wait_ready`'s timeout banner names the dialog) |
| 13 | 11.3 | `CreateSceneCollection` gives the new collection OBS's default scene `Scene`, which stays the program scene; `CreateScene Game` does not change it, so the app would record an empty scene | Provisioning sets `Game` as program scene (`SetCurrentProgramScene`, already in T14's contract change `f0ee2c6`) |
| 14 | 11.3 | `CreateProfile` over the websocket sets `[Basic] ConfigOnNewProfile=false` in the user's `user.ini` (turns off OBS's offer to run its auto-configuration wizard for new profiles) | State it in the wizard's "what the app changes in OBS" text |
| 15 | 12 | X11 window list, retitled window: item 0 turns disabled with the stored value and the same xid is listed again, enabled, under its new name; after the window closes, item 0 is disabled and no enabled item has the xid | Confirms S1 item 12's X11 rule. Capture continuity could not be checked here (Limits, item 3) |
| 16 | 11.3 output size | OBS aligns the output size down to a width divisible by 4 and an even height. `SetVideoSettings` 854x480 gave a running 852x480, which `GetVideoSettings` reports while `basic.ini` keeps 854 | 11.3: the scaled width is rounded down to a multiple of 4 and the height to a multiple of 2 before comparing or sending, or provisioning re-sends `SetVideoSettings` at every arm (for example 2560x1080 scaled to 720 lines gives 1706, which OBS runs as 1704) |

Items 3, 4, 8, 9, 11, 13 and 16 change the design; the rest confirm it or record a side effect.

## Setup

- **Display and isolation.** `tools/nested_display.py serve --caller r2-obs-behaviour`: a private
  `kwin_wayland --virtual --xwayland` (rootless, so X11 windows have a window manager and
  `xcomposite_input` can list them), private XDG dirs under
  `.orchestration/m0/data/r2-obs-behaviour/xdg`, a private runtime dir `/tmp/amg-*`, a private bus
  without service activation. One display per batch of scenarios, torn down after each batch.
- **OBS.** `flatpak run --die-with-parent --nosocket=wayland --socket=x11 --no-documents-portal
  --no-a11y-bus --env=QT_QPA_PLATFORM=xcb com.obsproject.Studio`, as an X11 client of the nested
  display, no audio devices. The config root `~/.var/app/com.obsproject.Studio/config/obs-studio`
  started from R1's final snapshot (`snapshots/obs-start`); each scenario restored a snapshot
  before it launched OBS (`obs-start`, then `prov-disarmed` and `prov-armed`, taken after
  `provision.jsonl` and `arm_disarm.jsonl`), and the root was restored to `obs-start` at the end
  (`diff -r` empty).
- **Driving OBS.** Every scenario request went through `tools/obs_transcript_recorder.py`, from
  obsws-python 1.8.0 clients (`ReqClient` plus `EventClient`, as T12 will use). Setup and readiness
  polls went to OBS directly. OBS was quit with SIGINT after outputs were inactive, or with SIGTERM
  when a modal was open and no output active. A probe window (PyQt6, title changeable by signal)
  stood in for a game.
- **Transcripts.** 23 real, 1 synthetic; `tests/fixtures/obs_transcripts/README.md` says how each
  was made and what was edited (authentication redacted, host paths rewritten).

## 1. Provisioning, files and `basic.ini` (spec 11.3)

`provision.jsonl` ran spec 11.3's Linux X11 row on a fresh OBS (profile and collection `Untitled`).
The resulting app profile is `runs/provision-183652/basic-ini-amg.txt`. Every key the app sets is
there under the names S1 read in source (summary item 1), and read back through
`GetProfileParameter` in the transcript.

What OBS wrote, from `listing-*.json` (path, size, sha256 and mtime of every file under the config
root after each step, run `provision-183652`):

| Step | Files written |
|---|---|
| Launch | `.sentinel/run_<uuid>` (new, removed at a clean quit), `logs/<date>.txt`; `basic/profiles/Untitled/basic.ini`, `plugin_config/obs-websocket/config.json`, `plugin_manager/modules.json` and `user.ini` rewritten unchanged |
| `CreateProfile` + profile requests | new `basic/profiles/Anki_Miner_Game/basic.ini` (2002 bytes); `basic/profiles/Untitled/basic.ini` rewritten from 287 to 1964 bytes (every output, video and audio key OBS held for the profile it left, e.g. `RecFormat2=hybrid_mp4`, `SampleRate=48000`); `user.ini` (`Profile`, `ProfileDir`, `ConfigOnNewProfile=false`) |
| `CreateSceneCollection`, scene, inputs | new `basic/scenes/Anki_Miner_Game.json` and `.json.bak`; `basic/scenes/Untitled.json` and `.json.bak` rewritten unchanged |
| Switching back to `Untitled` | `Anki_Miner_Game.json` and `.bak` updated, both `basic.ini` rewritten unchanged, `user.ini` |
| Quit | `.sentinel/run_<uuid>` removed, `profiler_data/<date>.csv.gz` added, `user.ini`, `global.ini`, the scene files |

- Profile folder: `Anki Miner Game` becomes `Anki_Miner_Game`, as S1 read.
- A profile is saved each time OBS leaves it (`frontend/widgets/OBSBasic_Profiles.cpp:690`), which
  is why the user's `basic.ini` grows on the first arm. No value changes meaning.
- `ConfigOnNewProfile`: the websocket's `CreateProfile` runs `CreateNewProfile`
  (`frontend/OBSStudioAPI.cpp:209-212`), which calls `SetupNewProfile(name)` with `useWizard`
  defaulting to false (`frontend/widgets/OBSBasic_Profiles.cpp:196-199`,
  `frontend/widgets/OBSBasic.hpp:902`), and that stores `useWizard` in the user's config
  (`frontend/widgets/OBSBasic_Profiles.cpp:70-74`).
- Program scene: after `CreateScene Game`, `GetSceneList` and `GetCurrentProgramScene` still report
  `Scene` (`provision.jsonl`).
- Special inputs: `GetSpecialInputs` answered every slot `null` (no audio devices in the display),
  so provisioning must skip the mic mute when `mic1` is null.
- `xcomposite_input` created with a placeholder `capture_window` (R1 side finding 2) lists the
  placeholder as disabled item 0 and the live windows after it; the probe window's item value was
  `4194311\r\namg-probe-window\r\nprobe_window.py`.

## 2. Settings without a restart (spec 11.3, 20)

`settings_apply.jsonl`, right after provisioning in the same OBS:

| Recording | Container (ffprobe) | Size, rate | File |
|---|---|---|---|
| First, straight after provisioning | `mov,mp4,m4a,3gp,3g2,mj2` (header `ftypiso4`) | 1280x720, 30/1 | `2026-09-21 18-37-05.mkv` |
| After `SetCurrentProfile Untitled` and back | `matroska,webm` (header `1a45dfa3`) | 1280x720, 30/1 | `2026-09-21 18-37-13.mkv` |

(`runs/provision-183652/ffprobe-before-reset.json`, `ffprobe-after-reset.json`, `steps.jsonl`
`magic` records.) The first file went to the new record folder at the new size and rate, so those
rows apply at the next start; the container needed the handler rebuild S1 predicted (source
findings section 2). The switch away and back took 31 ms and 84 ms.

Split, in Advanced mode (`split_off_runtime.jsonl`): OBS launched with `RecSplitFile=true`;
`SetProfileParameter AdvOut/RecSplitFile=false` while it ran; on the next recording
`SplitRecordFile` answered 702 "Verify that file splitting is enabled in the output settings." and
no split happened. Simple mode never splits (S1).

Output size and frame rate (raw run `runs/video_apply-214625`, not kept as a fixture): on an armed
OBS, `SetVideoSettings {outputWidth: 854, outputHeight: 480, fpsNumerator: 24, fpsDenominator: 1}`
took 69 ms; `GetVideoSettings` then reported 852x480 at 24/1, `basic.ini` held `OutputCX=854`,
`OutputCY=480`, `FPSNum=24`, and the recording started right after was 852x480 at 24/1 (ffprobe).
libobs rounds the output width down to a multiple of 4 and the height to a multiple of 2 when it
resets video (`libobs/obs.c:1541-1543`).

## 3. Arming, disarming and switches (spec 6.2)

Durations (request to answer; `...Changing` came 0.1-2.3 ms after the request, or 36-39 ms when it
was the first event on a fresh connection, item 5):

| Switch | No output active | Output active |
|---|---|---|
| `SetCurrentProfile` | 24-84 ms (10 switches) | 1.6-6.2 ms (6) |
| `SetCurrentSceneCollection` | 32-143 ms (4) | 22-114 ms (6) |
| `CreateProfile` | answer 0.1 ms; `CurrentProfileChanged` 36 ms after the request, no `...Changing` | |
| `CreateSceneCollection` | 106 ms, `Changing` then `Changed` then answer | |

Sources: `arm_disarm`, `settings_apply`, `switch_not_ready`, `switch_restart_prompt_matched`
(idle); `switch_recording`, `switch_streaming`, `switch_replay_buffer` (active); `provision`.
A profile switch under an active output is fast because OBS keeps the running output handler.

**Order.** In 25 of the 27 answered switches that sent events the answer followed `...Changed` by
0.05-0.33 ms. Two did not: in `switch_streaming.jsonl` the `SetCurrentSceneCollection` answer came
0.2 ms before `CurrentSceneCollectionChanged`, and in `switch_restart_prompt_matched.jsonl` the
first answer came 14 ms before both events. S1 found no ordering guarantee in source (section 8);
the transcripts show both orders.

**First event on a connection.** In every transcript the first event on a freshly identified
event connection arrived 40.2-41.5 ms after its `Identified` whenever its trigger came earlier
(`normal.jsonl`: `StartRecord` answered at 5.4 ms, `STARTING` received at 42.6 ms; later
`STARTING` events follow their answer within 1 ms, e.g. the second recording in
`settings_apply.jsonl`). This looks like TCP delayed ACK meeting Nagle's algorithm on OBS's
socket; not traced further. It only matters for the first event after a connect.

**Collection change.** `switch_not_ready.jsonl`: a third client polled `GetRecordStatus` every 5
ms; 14 of 278 polls got 207 "OBS is not ready to perform the request.", all between
`CurrentSceneCollectionChanging` and `...Changed`, and no event of any kind arrived in those
windows (S1 summary item 5 confirmed).

**Refused and no-op switches** (`switch_refused.jsonl`, armed OBS): unknown profile or collection
name 600 (`ResourceNotFound`); `CreateProfile`, `CreateSceneCollection` and `CreateScene` with an
existing name 601 (`ResourceAlreadyExists`, the scene with "A source already exists by that scene
name."); switching to the current profile and to the current collection answered 100 in under
1 ms and no event followed in the next 3 s. Codes: `src/requesthandler/types/RequestStatus.h:291,
302` in obs-websocket.

**Outputs active** (item 6): recording (`switch_recording.jsonl`), stream to a local RTMP sink
(`switch_streaming.jsonl`) and replay buffer (`switch_replay_buffer.jsonl`) all stayed active
through both switches and back; the recording's and the stream's `outputBytes` kept growing. The
recording started on `Untitled` kept writing into `Untitled`'s folder (`/home/user/Videos/...` in
the fixture). While the app's profile (`RecRB=false`) was active, `GetReplayBufferStatus` still
answered `outputActive: true` for the running buffer. With no buffer configured it answers 604
"Replay buffer is not available.", and `GetVirtualCamStatus` answers 604 "VirtualCam is not
available." without v4l2loopback (`src/requesthandler/RequestHandler_Outputs.cpp:46-54, 143-151`).

**Restart question** (item 3). `switch_restart_prompt.jsonl`: `Untitled` at 44100 Hz, the app
profile at OBS's 48000. `CurrentProfileChanging` and `CurrentProfileChanged` arrived within 57 ms;
the `SetCurrentProfile` answer never came in the 20 s the client waited, and the display showed a
window titled "Restart". OBS asks after emitting `PROFILE_CHANGED`
(`frontend/widgets/OBSBasic_Profiles.cpp:719-733`), comparing the running profile's `ChannelSetup`
and `SampleRate` with the target file's (`frontend/widgets/OBSBasic_Profiles.cpp:762-785`); OBS
writes both keys into every profile it saves (section 1), so the comparison always has two values.
The comparison is symmetric, so disarming from a 48 kHz app profile back to a 44.1 kHz user profile
asks too (source; not run).
`switch_restart_prompt_matched.jsonl`: both profiles at 44100, four switches in both directions,
each answered in 24-44 ms. obsws-python's `ReqClient` sends one request at a time, so the unanswered
switch also blocks every later request on that client until its timeout.

## 4. Recording, pause and stop (spec 7, 10.2)

States seen, in order, per transcript (`RecordStateChanged.outputState` without the prefix):

| Transcript | States | Notes |
|---|---|---|
| `normal` | STARTING, STARTED, STOPPING, STOPPED | `outputPath` set on STARTED and STOPPED only, the same file |
| `pause_noop` | STARTING, STARTED, STOPPING, STOPPED | `PauseRecord` 100, no event, `outputPaused` stays false, `ResumeRecord` 503 (`OutputNotPaused`) |
| `pause_resume` | ..., PAUSED, RESUMED, ... | `RecQuality=Small`; PAUSED `outputActive: false`, RESUMED `true` |
| `missed_pause` | STARTING, STARTED, RESUMED, STOPPING, STOPPED | the pause happened while no proxied client was connected |
| `start_failed_missing_dir` | none | `StartRecord` 100; modal "Bad File Path"; log "Recording stopped because of bad output path" |
| `start_failed_unwritable` | STARTING | `StartRecord` 100; modal "Failed to start recording" |

**`outputDuration` against the file** (item 9). ffprobe with `-count_packets` on the files:

| Transcript | STARTED to STOPPING | Paused | Video in file | Last `outputDuration` | STOPPING to STOPPED |
|---|---|---|---|---|---|
| `normal` | 15007 ms | 0 | 450 frames = 15000 ms | 15033 (read after STOPPED) | 561 ms |
| `pause_resume` | 12014 ms | 4004 ms | 241 frames = 8033 ms | 7433 at 12.0 s | 603 ms |
| `split_off_runtime` (Advanced, x264) | 10016 ms | 0 | 301 frames = 10033 ms | not read | 1254 ms |

The other Simple-mode stops took 566-620 ms from `StopRecord` to `STOPPED` (`pause_noop`,
`missed_pause`, `reconnect`, `settings_apply`, `switch_recording`, `window_retitle`), the Advanced
split run 1259 ms. So the file's video ends at `STOPPING`, within one frame, with pauses cut out,
and `outputDuration` read after the stop equals it within one frame. During the recording
`outputDuration` trails the event clock by about 540 ms on this NVENC profile (`normal`: 4466 at
5004 ms), R1's lag. Once `GetRecordStatus` reports inactive, `outputDuration` is 0 while
`outputBytes` keeps the final count (`normal.jsonl`, last status).

After `STOPPED`, `GetRecordStatus` answered `outputActive: true` for 173 ms more (`normal.jsonl`,
16 polls every 10 ms): a reconcile or an arm step 1 in that window sees a recording that is over.

During the 4 s pause of `pause_resume`, `outputDuration` moved from 3433 to 3533: frames already
queued before the pause edge (S1 section 6: the edge lands 1-2 frames after the call). R1 measured
the pause's effect on offsets.

## 5. Reconnect and matching a recording to its manifest (spec 6.3)

`reconnect.jsonl`: both connections closed 3 s into a recording; a new pair connected 5 s later.
`GetRecordStatus` said active, `outputBytes` 5748537; `GetOutputSettings simple_file_output`
returned `path` equal to the `STARTED.outputPath` (and to the file OBS finished). `GetOutputList`
lists `simple_file_output` as the active `ffmpeg_muxer`. In Advanced mode the output is
`adv_file_output`, whose settings also carry `path` (`split.jsonl`).

The size fallback S1 offered does not hold: the file on disk trails `outputBytes` by the muxer's
buffering. `reconnect`: 3874816 bytes on disk against 5748537 reported; `normal`: 0 bytes at 5 s
(3437950 reported), 6418432 at 10 s (7263217), 10219520 at 15 s (11113057)
(`runs/reconnect-184034/steps.jsonl`, `runs/normal-184221/steps.jsonl`, `file_size` records).

`missed_pause.jsonl`: the first `GetRecordStatus` after the reconnect returned `outputPaused:
true`, which is spec 6.3's row "active, paused flag differs".

After a split the path is stale (item 10): in `split.jsonl` `GetOutputSettings` after
`RecordFileChanged` still returned the first file, and so did `StopRecord` and `STOPPED`.

## 6. OBS going away (spec 6.4, 11.1)

| Transcript | What happened | On the websocket | The `.mkv` |
|---|---|---|---|
| `obs_exit` | SIGINT, `ConfirmOnExit=false` (the user closing OBS and confirming) | `ExitStarted`; 327 ms later both connections closed by OBS, 1001 "Server stopping."; no `STOPPED`; then connection refused | Matroska, 4.53 s, readable |
| `obs_killed` | SIGKILL | both connections dropped, 1006; no `ExitStarted`; then refused | Matroska, 4.43 s, readable |
| `obs_sigterm` | SIGTERM, `ConfirmOnExit` at its default | nothing for 68 s; a new pair still connected and identified | kept growing (see below) |

- After the SIGKILL, `.sentinel/run_<uuid>` stayed behind and the relaunch showed a window "OBS
  Studio Crash Detected"; the websocket refused connections for the 25 s the driver waited,
  SIGTERM did not end that OBS within 20 s, SIGKILL did (`runs/obs_killed-213106/steps.jsonl`).
- SIGTERM while recording did not end OBS. OBS's handler saves and calls `quit()`
  (`frontend/OBSApp.cpp:1895-1914`); with an output active OBS stayed up, recording, websocket
  serving, logging only websocket connections, until the driver sent SIGINT 98 s later; OBS then
  logged "Shutting down" and died with a segfault in its `libobs: hotkey` thread (host journal,
  18:45:05), leaving its sentinel. The file (`runs/obs_sigterm-184316/rec/_incoming/2026-09-21
  18-43-23.mkv`, 77 MB, 2999 video packets) has no duration in its header, as expected of an
  unfinished Matroska file. The app never signals OBS; for the tools and E1: never SIGTERM an OBS
  with an active output.

## 7. Window list after a retitle (spec 12)

`window_retitle.jsonl` (armed OBS recording the probe window through `xcomposite_input`):

| Moment | Item 0 (stored `4194311\r\namg-probe-window\r\nprobe_window.py`) | Other items with xid 4194311 |
|---|---|---|
| Before | enabled | none |
| Retitled to `amg-probe-window - level 2 - 59 fps` | disabled, value unchanged | one, enabled, new name |
| Window closed | disabled | none |

`GetInputSettings` kept the stored value throughout. This is S1's X11 rule (source findings
section 10): open while item 0 is enabled or an enabled item has the stored xid.

## 8. Discovery and launch (spec 11.1)

- `flatpak run ... com.obsproject.Studio --minimize-to-tray` passes the flag: the process was
  `obs --minimize-to-tray` and OBS logged "Command Line Arguments: --minimize-to-tray"
  (`runs/provision-183652/steps.jsonl` `obs_cmdline`, `obs-stdout.log`).
- The Flatpak config root `~/.var/app/com.obsproject.Studio/config/obs-studio` held every file
  above; `GetVersion` answered `platform: org.freedesktop.platform`.
- Readiness: `GetVersion` first succeeded 2.3-5.5 s after `flatpak run` (27 launches, median 3.0 s;
  the driver polled every 0.5 s).
- `StartReplayBuffer` from a scene without a video source answered 100 and stopped at OBS's modal
  "no sources" question (`frontend/components/UIValidation.cpp:30-59`, called from
  `frontend/widgets/OBSBasic_ReplayBuffer.cpp:80`); a first `switch_replay_buffer` run was lost to
  it. `StartRecord` and `StartStream` over the websocket do not ask: their questions are in the
  button handlers (`frontend/widgets/OBSBasic_Recording.cpp:257-278`,
  `frontend/widgets/OBSBasic_Streaming.cpp:391-421`). The app's scene always has its capture source.

## Limits

1. One host, Linux, the Flatpak build, X11 through a nested display. Windows (`game_capture`,
   registry, rename locks, the restart question's wording) is H5.
2. OBS had no audio devices (no PulseAudio socket in the sandbox): `GetSpecialInputs` was all
   null, so the mic mute and `pulse_output_capture` audio were not exercised.
3. `xcomposite_input` rendered black with both NVIDIA EGL and Mesa llvmpipe, and
   `GetSourceScreenshot` failed with 702, so "capture keeps following a retitled window" is
   unverified here; R1 side finding 1 has the cause. E1 and H5 check it.
4. No v4l2loopback: the virtual camera refusal exists only as the labelled synthetic transcript.
5. Timings are one OBS on an idle 32-thread machine through a proxy that adds well under 1 ms.

## Owner environment

Guard baseline before the resumed runs (`guard-before-2.json`, `owner-mtimes-before-2.txt`), a
fresh baseline per batch with a guard check after every scenario (`driver/campaign.py`,
`incidents.jsonl`), and a final check (`owner-mtimes-after-2.txt`): `kwinrc`,
`kglobalshortcutsrc`, `gtkrc`, `gtkrc-2.0`, `Trolltech.conf`, the GTK `settings.ini` files and
`xsettingsd.conf` unchanged since before R2; `/run/user/1000/doc` stayed `fuse.portal`.

`kwinoutputconfig.json` got a new mtime four times during R2: 18:45:05, 21:13:07, 21:18:48 and
21:41:54. Each write came with the owner's powerdevil activating
`org.kde.powerdevil.backlighthelper` (system journal: 6 to 7 ms after the three later writes;
18:45:04.5 for the first, whose exact mtime is gone). Plasma keeps screen brightness in that
file. At 21:13:07 and 21:41:54 no nested process was running; at 21:18:48 the write came before
that batch's nested display started (its own guard, armed first, recorded the new mtime). The file
lists only `eDP-1`, and its bytes were identical to a copy taken at 21:17 when compared at 21:19
and at the end. So these are the owner's dim cycle, not the spike. The two changes a guard flagged
(18:45:05, 21:18:48) stopped the nested display at once; each later batch started from a fresh
baseline.

No nested kwin, Xwayland, dbus-daemon, OBS, RTMP sink or `/tmp/amg-*` dir was left;
`~/.config/owocr_config.ini` does not exist. The OBS config root was restored to R1's final
snapshot. No Flatpak filter line was added (the NVIDIA GL extension was already installed, as in
R1).
