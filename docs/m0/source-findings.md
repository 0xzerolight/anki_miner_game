# M0 source findings (S1)

Task S1 of the master plan, 2026-09-21. Everything below was read in source. Each claim cites
`<repo>@<commit> <path>:<lines>` in the clones listed under Sources. Markers:

- [R1], [R2], [R3]: a runtime question handed to the Linux spikes (clock, OBS behaviour, owocr).
- [H4]: needs the user's OCR sessions. [H5]: needs Windows, so it waits for pre-release QA (D2).
- [cmd]: the fact comes from a command shown inline rather than from one line range.

## Sources

| Name | Upstream | Ref | Commit | Why this ref |
|---|---|---|---|---|
| obs-studio | obsproject/obs-studio | tag `32.2.2` | `ba2f32bd` | O1 installed Flathub OBS 32.2.2; also the newest tag on 2026-09-21 (no `33.*` tag) [cmd: `git ls-remote --tags`] |
| obs-websocket | obsproject/obs-websocket | tag `5.7.4` | `1ef34bf4` | the submodule pin of obs-studio 32.2.2 [cmd: `git -C obs-studio ls-tree HEAD plugins/obs-websocket`] |
| owocr | AuroraWright/owocr | tag `1.26.8` | `3b9706b1` | newest PyPI release; the version the spec cites |
| obsws-python | aatikturk/obsws-python | `main` | `f70583d7` | version 1.8.0, identical to PyPI 1.8.0 and to `.venv` [cmd: `diff -r` against site-packages: no difference] |
| bouf | obsproject/bouf | tag `v0.6.5` | `0d3cc4ab` | holds OBS's NSIS installer script; obs-studio 32.2.2 builds its Windows installer from this tag |

Clones live in `/home/light/Projects/anki_miner_game/.orchestration/clones/<name>`. Older
obs-studio and obs-websocket revisions used in section 9 were fetched into a scratch clone; the
commands are given there. The cites are machine-checked by
`/home/light/Projects/anki_miner_game/.orchestration/m0/s1_cite_check.py` (every cite resolves at
the clone's HEAD, and 110 key facts are matched against the cited lines).

## Summary for the M0 gate

What the source changes in the spec, with the section that holds the evidence.

| # | Spec | Finding | Suggested amendment |
|---|---|---|---|
| 1 | 11.3 container, split | Keys confirmed: `[SimpleOutput] RecFormat2` and `[AdvOut] RecFormat2` (Matroska value `mkv`); `[AdvOut] RecSplitFile`. Simple mode has no file splitting at all. OBS's default container is `hybrid_mp4`. (s1) | Replace "unverified" with the confirmed keys; note that Simple mode cannot split |
| 2 | 11.3 restart question | Record directory, output size, frame rate and split apply at the next `StartRecord`. The container does not when it changes from the default `hybrid_mp4`: the muxer type is fixed when OBS builds its output handler, which a websocket request never rebuilds. Re-activating the profile (switch away and back) or restarting OBS rebuilds it. (s2) | Wizard: after provisioning changes `RecFormat2`, `Output/Mode`, `RecQuality` or `RecEncoder`, re-activate the app's profile once (or restart OBS) |
| 3 | 7 pause, 6.3 | Pause is impossible when the recording shares the stream encoder, which is OBS's default (Simple mode, quality "Same as stream"). `PauseRecord` then returns success and nothing happens: no event, no pause. (s6) | Gate decision: accept "no pause" on the app's profile, or set `[SimpleOutput] RecQuality` to a separate-encoder preset (touches the encoder, against "Encoder untouched") |
| 4 | 3.3 events | `OBS_WEBSOCKET_OUTPUT_PAUSED` / `_RESUMED` confirmed. A PAUSED event carries `outputActive: false`. `outputPath` is set on STARTED and STOPPED (the protocol comment says STOPPED only; the code sets both). (s6, s7) | Key the actor on `outputState`, never `outputActive` |
| 5 | 6.2 step 3, 11.2 | `CreateProfile` answers before the profile exists, then switches to it with `CurrentProfileChanged` and no `...Changing`. `SetCurrentProfile`, `SetCurrentSceneCollection` and `CreateSceneCollection` block until the switch is done. During a collection change every request gets code 207 `NotReady` and every event is dropped. (s8) | Wait for `CurrentProfileChanged` after `CreateProfile`; treat 207 as "retry" |
| 6 | 6.2 step 1 | OBS itself does not refuse a profile or collection switch while outputs are active; the app's refusal is the only guard. (s8) | None; R2 observes what a switch does to a live stream |
| 7 | 11.2, 4.2 | obs-websocket handles each request and sends each event as a separate thread-pool task, so events have no guaranteed order relative to each other or to responses. (s8) | Actor tolerates reordered edges; R2 records real order |
| 8 | 11.1 registry | `HKLM\SOFTWARE\OBS Studio`, default value = install folder, written in both the 64-bit and 32-bit registry views; exe `<folder>\bin\64bit\obs64.exe`. (s3) | Write the key into 11.1 (provisional until H5) |
| 9 | 11.1 minimum | OBS 30.0.0 (obs-websocket 5.3.3). `SetRecordDirectory` (added in 5.3.0) is the newest of the 26. `RecordFileChanged` needs 30.2.0. (s9) | "Minimum OBS 30.0" |
| 10 | 11.1 step 3 | obs-websocket reads `config.json` once at start and writes it at start and when the user saves its settings dialog, not at exit. Editing it while OBS runs has no effect until a restart. (s5) | Reword the reason; the behaviour (ask the user, or close OBS and Fix) stands |
| 11 | 11.1 launch | Flatpak: `flatpak run com.obsproject.Studio --minimize-to-tray`. Until OBS finishes loading, every request gets 207. After a crash, OBS shows a modal unclean-shutdown dialog on the next launch; no flag skips it in 32.2.2. (s4, s5) | `wait_ready` polls `GetVersion` until success; its timeout banner mentions an OBS dialog |
| 12 | 11.3 window list, 12 auto-stop | Linux `xcomposite_input` uses property `capture_window`, not `window`, with a different value format. On every platform the configured window stays in the list, disabled, when no live window has exactly its stored string. That also happens while the window is open with a changed title (FPS or level in the title), and capture keeps following it by exe or class (Windows) or xid (X11). Neither presence nor the stored item's `itemEnabled` means "closed". (s10) | Per-kind property name; auto-stop counts the window open while an enabled item matches class and exe (Windows) or item 0 is enabled or an enabled item has the stored xid (X11); query the `game_capture` input; R2 and H5 check a retitled window |
| 13 | 11.3 app audio | `game_capture`, `window_capture` and `wasapi_process_output_capture` build the value with the same function: `title:class:exe`, `#` and `:` escaped. The strings match. (s10) | State it; runtime confirm at H5 |
| 14 | 6.3 reconcile | `GetOutputSettings` on `simple_file_output` / `adv_file_output` returns the active file's `path`. (s7) | Add it as a 27th required request (5.0.0, floor unchanged), or match by manifest `outputPath` plus `outputBytes` |
| 15 | 11.3 | If `[Video] AutoRemux` is on, OBS remuxes each finished recording to a sibling `.mp4`. No default is set, so it is off unless the user turns it on in the app's profile. (s7) | Provisioning pins `Video/AutoRemux` = `false` |
| 16 | 3.3, 11.2 | obsws-python 1.8.0 logs the password at INFO when it connects and puts it in `repr()`. `ReqClient` is not thread-safe and does not match request ids. `EventClient` dies silently on disconnect. (s11) | Gateway sets the `obsws_python` logger to WARNING, runs requests on one thread, reconnects after a timeout, and watches the event thread |
| 17 | 3.4, 14 | owocr 1.26.8 is current. It reads the user's `~/.config/owocr_config.ini` for every key the app does not pass, and downloads that file when it is missing. Without `-el`, an unavailable `-e` engine falls back to any other engine, cloud ones included. The picker run keeps running after the selection. (s12) | Pass `-el <engine>` with `-e`; point `HOME`/`USERPROFILE` of the owocr child at an app-owned folder (R3 checks side effects); kill owocr after reading the coordinate line |
| 18 | 7 zero event | The `StartRecord` response is sent when the start is queued, before STARTING. STARTING fires before the output starts; STARTED fires after the output's start signal. (s6) | Input for R1 |

## 1. Profile keys for container and file split (`basic.ini`)

- A profile lives in `<config root>/basic/profiles/<dir>/basic.ini`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:32-33`). The folder name comes from
  the profile name with whitespace turned into `_` and other non-alphanumerics dropped, so
  `Anki Miner Game` becomes `Anki_Miner_Game` (`obs-studio@ba2f32bd frontend/OBSApp.cpp:1771-1792`).
- Output mode: `[Output] Mode`, default `Simple`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:743`).
- Container, Simple mode: `[SimpleOutput] RecFormat2`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:751`). Advanced mode: `[AdvOut] RecFormat2`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:775`). Both default to `hybrid_mp4` on
  Windows and Linux (`hybrid_mov` on macOS)
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:601-605`). The Matroska value is `mkv`
  (`obs-studio@ba2f32bd frontend/settings/OBSBasicSettings.cpp:1098`, Advanced list at
  `obs-studio@ba2f32bd frontend/settings/OBSBasicSettings.cpp:1108`). A pre-30 `RecFormat` key is
  migrated to `RecFormat2` when a profile loads
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:693-720`).
- File splitting: `[AdvOut] RecSplitFile` (bool), with `RecSplitFileType`, `RecSplitFileTime`
  (default 15) and `RecSplitFileSize` (default 2048)
  (`obs-studio@ba2f32bd frontend/settings/OBSBasicSettings.cpp:3548`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:798-799`). `RecSplitFile` has no default, and
  `config_get_bool` returns false for an absent key and true for `true` or a non-zero integer
  (`obs-studio@ba2f32bd libobs/util/config-file.c:682-689`).
- Simple mode has no splitting: `SimpleOutput.cpp` never sets `split_file`
  [cmd: `grep -ci split frontend/utility/SimpleOutput.cpp` prints 0]. Only
  `AdvancedOutput::StartRecording` reads the key
  (`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:827`) and passes `split_file` to the
  output (`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:833-850`).
- Record folder keys: `[SimpleOutput] FilePath` and `[AdvOut] RecFilePath`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:750`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:774`).
- `SetProfileParameter` accepts only a string value, writes it into the active profile and saves
  the file at once (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:405-417`),
  so `SetProfileParameter("AdvOut", "RecSplitFile", "false")` is read back as false.
- The key names in a real `basic.ini` written through the app's provisioning are R2's to confirm
  [R2].

## 2. Provisioning rows that apply without a restart

How OBS holds recording settings: `ResetOutputs` builds a Simple or Advanced output handler from
`[Output] Mode`. While any output is active it keeps the old handler and only calls `Update()`
(`obs-studio@ba2f32bd frontend/widgets/OBSBasic_OutputHandler.cpp:26-48`). Each handler chooses its
muxer output when it is constructed: `hybrid_mp4` gives `mp4_output`, `hybrid_mov` gives
`mov_output`, anything else gives `ffmpeg_muxer`
(`obs-studio@ba2f32bd frontend/utility/SimpleOutput.cpp:249-256`,
`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:113-120`). Folder, container extension and
split settings are read at every start
(`obs-studio@ba2f32bd frontend/utility/SimpleOutput.cpp:824-827`,
`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:827`).

`ResetOutputs` runs at startup (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:1080`), when a
profile is activated (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:742-748`), and
when the Settings dialog saves output settings
(`obs-studio@ba2f32bd frontend/settings/OBSBasicSettings.cpp:3608`). No websocket request calls it.

| Row (spec 11.3) | Request and what it writes | Next `StartRecord`? |
|---|---|---|
| Record directory | `SetRecordDirectory` writes `AdvOut/RecFilePath` and `SimpleOutput/FilePath`, refused while recording | Yes |
| Output size, frame rate | `SetVideoSettings` refused while any output is active, writes `Video/FPSType=2`, `FPSNum`, `FPSDen`, `OutputCX`, `OutputCY`, saves, resets video | Yes, applied at once |
| Container | `SetProfileParameter` `RecFormat2=mkv` | Extension yes; muxer no when leaving `hybrid_mp4`/`hybrid_mov` |
| File split | `SetProfileParameter` `AdvOut/RecSplitFile=false` | Yes (Advanced mode only; Simple never splits) |
| Output mode, recording quality or encoder | `SetProfileParameter` `Output/Mode`, `SimpleOutput/RecQuality`, `AdvOut/RecEncoder` | No: handler type and pausability are set in `ResetOutputs` |

- Record directory: `obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:642-655`.
- Video: refused when `obs_video_active()`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:473-475`); applied by
  `obs_frontend_reset_video()`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:510-515`), which calls
  `OBSBasic::ResetVideo` (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:613-616`).
- Container: with the default `hybrid_mp4` handler, a later `RecFormat2=mkv` changes only the file
  name; `mp4_output` writes whatever `path` it is given
  (`obs-studio@ba2f32bd plugins/obs-outputs/mp4-output.c:297-316`), so the first session would be
  MP4 data in a `.mkv` name until the handler is rebuilt. Inferred from source; R2 confirms with
  `ffprobe` on the file [R2].
- Rebuilding without an OBS restart: activating the profile again (switch to another profile and
  back) runs `ResetProfileData`, which calls `ResetOutputs`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:742-748`).
- Profile activation asks the user to restart OBS only when the audio channel setup or sample rate
  differ between the two profiles
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:762-785`; the modal question at
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:719-733`). The comparison reads the
  target profile's file without defaults, so switching to a fresh profile never asks; switching
  back to a user profile whose saved sample rate or channels differ from OBS's defaults (48000,
  Stereo; `obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:871-872`) can [R2].

## 3. Windows discovery

- OBS's Windows installer is built with NSIS from obsproject/bouf; obs-studio 32.2.2's signing
  action downloads bouf `v0.6.5`
  (`obs-studio@ba2f32bd .github/actions/windows-signing/action.yaml:34`).
- That installer's default folder is `$PROGRAMFILES64\obs-studio` and it remembers the folder in
  `HKLM\Software\OBS Studio` (`bouf@0d3cc4ab extra/nsis/mp-installer.nsi:43-44`; `APPNAME` is
  `OBS Studio` at `bouf@0d3cc4ab extra/nsis/mp-installer.nsi:25`).
- At the end of the install it writes `HKLM\Software\OBS Studio` (default value = install folder)
  in the 64-bit view and in the 32-bit view
  (`bouf@0d3cc4ab extra/nsis/mp-installer.nsi:321-336`), then again in the default view together
  with the Uninstall key, whose `DisplayIcon` is `$INSTDIR\bin\64bit\obs64.exe`
  (`bouf@0d3cc4ab extra/nsis/mp-installer.nsi:358-365`).
- The updater opens the Uninstall key in the 32-bit view (`KEY_WOW64_32KEY`)
  (`obs-studio@ba2f32bd frontend/updater/updater.cpp:1320`,
  `obs-studio@ba2f32bd frontend/updater/updater.cpp:1340`), which matches a 32-bit NSIS installer
  writing to `WOW6432Node`. The Steam build writes the same `HKLM\SOFTWARE\OBS Studio` key in both
  views (`obs-studio@ba2f32bd build-aux/steam/scripts_windows/install.bat:16-18`).
- So discovery reads `HKLM\SOFTWARE\OBS Studio` (default value) with `KEY_WOW64_64KEY`, falls back
  to the 32-bit view, and appends `bin\64bit\obs64.exe`. Provisional until a real install is read
  at H5 [H5].
- The working directory must be `bin\64bit`: libobs and the frontend find data and plugins through
  paths relative to it (`obs-studio@ba2f32bd libobs/obs-windows.c:37-39`,
  `obs-studio@ba2f32bd cmake/windows/defaults.cmake:19`,
  `obs-studio@ba2f32bd frontend/utility/platform-windows.cpp:51-58`).
- `--minimize-to-tray` exists (`obs-studio@ba2f32bd frontend/obs-main.cpp:1009`).

## 4. Linux discovery and launch

- The Flatpak's command is `obs` (`obs-studio@ba2f32bd build-aux/com.obsproject.Studio.json:6`); the
  installed app on this host says the same [cmd: `grep command=
  ~/.local/share/flatpak/app/com.obsproject.Studio/current/active/metadata` prints `command=obs`].
  Launch: `flatpak run com.obsproject.Studio --minimize-to-tray`; that `flatpak run` passes the
  trailing flag to `obs` is Flatpak behaviour, confirmed in R2 [R2].
- The Flatpak shares the host network and the whole host filesystem, and gets Wayland with an X11
  fallback (`obs-studio@ba2f32bd build-aux/com.obsproject.Studio.json:7-15`). So the websocket
  port is reachable from the host and OBS can record into any `_incoming` folder. Under a Wayland
  session OBS runs as a Wayland client, so `xcomposite_input` is unavailable there; the nested X11
  display of the spikes avoids that [R2].
- Config root: libobs uses `$XDG_CONFIG_HOME`, else `$HOME/.config`
  (`obs-studio@ba2f32bd libobs/util/platform-nix.c:228-248`). Flatpak points `XDG_CONFIG_HOME` at
  `~/.var/app/com.obsproject.Studio/config`, giving the spec's Flatpak root; O1 found it not yet
  created [R2].
- Two modal dialogs can stop a launch before the main window, with no websocket until they are
  answered:
  - a second instance asks "already running" unless `--multi` is given
    (`obs-studio@ba2f32bd frontend/obs-main.cpp:565-576`);
  - after a crash or kill, release builds ask how to launch (safe mode or normal)
    (`obs-studio@ba2f32bd frontend/OBSApp.cpp:1139-1147`). The trigger is a leftover
    `run_<uuid>` sentinel in `obs-studio/.sentinel` under the config folder
    (`obs-studio@ba2f32bd frontend/utility/CrashHandler.cpp:36-44`,
    `obs-studio@ba2f32bd frontend/utility/CrashHandler.cpp:203-227`). 32.2.2 has no flag that skips
    it (flag list: `obs-studio@ba2f32bd frontend/obs-main.cpp:959-1027`). R2 kills OBS once and
    relaunches to see it [R2].

## 5. obs-websocket config file

- Path: `obs_module_config_path("config.json")`, i.e.
  `<config root>/plugin_config/obs-websocket/config.json`
  (`obs-websocket@1ef34bf4 src/utils/Obs_StringHelper.cpp:43-47`).
- Keys `first_load`, `server_enabled`, `server_port`, `alerts_enabled`, `auth_required`,
  `server_password` (`obs-websocket@1ef34bf4 src/Config.cpp:37-43`). Built-in defaults: server off,
  port 4455, auth on, first load true (`obs-websocket@1ef34bf4 src/Config.h:36-42`).
- On first load it generates a password if none is set and saves; when a config file exists it is
  written back at every load (`obs-websocket@1ef34bf4 src/Config.cpp:79-92`).
- It is read once, when the module loads (`obs-websocket@1ef34bf4 src/obs-websocket.cpp:74`), and
  the server starts after load only if enabled
  (`obs-websocket@1ef34bf4 src/obs-websocket.cpp:119-122`). The only other write is the WebSocket
  settings dialog's save (`obs-websocket@1ef34bf4 src/forms/SettingsDialog.cpp:197`)
  [cmd: `grep -rn "Save()" src` lists these call sites only]. Nothing writes it at exit.
  Consequence: a change made while OBS runs takes effect at the next start and survives unless the
  user saves that dialog.
- Command-line overrides exist: `--websocket_port`, `--websocket_password`,
  `--websocket_ipv4_only`, `--websocket_debug`; the overridden port and password are not saved
  (`obs-websocket@1ef34bf4 src/Config.cpp:94-128`, save guards at
  `obs-websocket@1ef34bf4 src/Config.cpp:131-146`). The app launches OBS without them.
- The server listens on all interfaces (IPv4 and IPv6) unless `--websocket_ipv4_only`
  (`obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer.cpp:103-111`), so it is reachable
  from the local network; one line for the user guide.
- Until OBS finishes loading, the server accepts connections but answers every request, including
  `GetVersion`, with status 207 `NotReady`
  (`obs-websocket@1ef34bf4 src/obs-websocket.cpp:119-122`,
  `obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer_Protocol.cpp:212-220`). So readiness
  is a successful `GetVersion`, not a successful connect.

## 6. Record output states, pause and start

- State strings: `OBS_WEBSOCKET_OUTPUT_UNKNOWN`, `_STARTING`, `_STARTED`, `_STOPPING`, `_STOPPED`,
  `_RECONNECTING`, `_RECONNECTED`, `_PAUSED`, `_RESUMED`
  (`obs-websocket@1ef34bf4 src/utils/Obs.h:131-141`).
- Record events map one to one from OBS's frontend events: STARTING, STARTED, STOPPING, STOPPED,
  and `RECORDING_PAUSED` to PAUSED, `RECORDING_UNPAUSED` to RESUMED
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler.cpp:372-397`).
- `outputActive` is true for STARTED, RESUMED and RECONNECTED and false for everything else,
  PAUSED included (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Outputs.cpp:22-38`).
- When the pause pair fires: `OBSBasic::PauseRecording` emits the frontend event right after
  `obs_output_pause` succeeds, and `UnpauseRecording` likewise
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:289-316`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:324-351`). The websocket requests,
  the OBS button and hotkeys all end there; the request path is a queued call to the UI thread
  (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:254-257`).
- A recording is pausable only when it has its own encoder. Simple mode shares the stream encoder
  when `RecQuality` is `Stream`; Advanced mode shares it when `RecEncoder` is `none`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:371-392`). Both are the defaults
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:757`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:778`). Other `RecQuality` values are `Small`,
  `HQ` and `Lossless` (`obs-studio@ba2f32bd frontend/settings/OBSBasicSettings.cpp:4943-4952`).
  Pausability is computed only in `ResetOutputs`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_OutputHandler.cpp:26-48`).
- When not pausable, `PauseRecording` returns silently
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:289-293`) while the `PauseRecord`
  request still reports success
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Record.cpp:161-170`). On the app's
  default profile a recording therefore never pauses, and OBS shows no pause button
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasicControls.cpp:180-186`).
- libobs places a pause edge on a video frame 1 to 2 frame intervals after the call
  (`obs-studio@ba2f32bd libobs/obs-output.c:656-663`), while the event goes out at once; R1
  measures what that does to offsets [R1].
- `StartRecord` only queues the start: it calls `obs_frontend_recording_start()` and answers
  success (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Record.cpp:90-100`), which is
  a queued `StartRecording` call (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:239-242`).
- `OBSBasic::StartRecording` first checks the output folder and free disk space; on failure it
  shows a modal and returns with no event at all. Otherwise it emits STARTING before it starts the
  output (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:113-137`). STARTED follows
  from the output's `start` signal through a queued `RecordingStart`
  (`obs-studio@ba2f32bd frontend/utility/BasicOutputHandler.cpp:74-81`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:162-172`). If the output itself
  fails to start, OBS shows a modal after STARTING
  (`obs-studio@ba2f32bd frontend/utility/SimpleOutput.cpp:888-905`); whether a STOPPED follows is
  for R2 [R2]. The actor needs a timeout between `StartRecord` and STARTED.
- Zero-event candidates in time order, for R1: `StartRecord` response (queue time), STARTING (UI
  thread, before the output starts), STARTED (after the output's start signal) [R1].
- `GetRecordStatus` returns `outputActive`, `outputPaused`, `outputTimecode`, `outputDuration`,
  `outputBytes` (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Record.cpp:38-50`).
  `outputDuration` is delivered frames times the frame interval, and 0 when inactive
  (`obs-websocket@1ef34bf4 src/utils/Obs_NumberHelper.cpp:26-36`).
- `ExitStarted` is sent on the frontend's scripting-shutdown event, after which obs-websocket stops
  broadcasting (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler.cpp:460-470`). OBS fires that
  event while closing (`obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:2034`), so a STOPPED
  caused by the shutdown is never delivered, which spec 6.4 already assumes. Closing with an active
  output asks for confirmation by default (`obs-studio@ba2f32bd frontend/OBSApp.cpp:352`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic.cpp:1944-1952`).

## 7. Output path and matching after a reconnect

- `RecordStateChanged.outputPath` is set on STARTED and STOPPED from `GetLastRecordFileName()` and
  is null otherwise (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Outputs.cpp:77-88`).
  The doc comment above it says "if record stopped"
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Outputs.cpp:63-75`); the code wins.
- `GetLastRecordFileName()` reads the recording output's current `url` or `path` setting
  (`obs-websocket@1ef34bf4 src/utils/Obs_StringHelper.cpp:73-91`), which OBS sets to the new file's
  name before each start (`obs-studio@ba2f32bd frontend/utility/SimpleOutput.cpp:853-857`,
  `obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:829-833`).
- `StopRecord`'s response also carries `outputPath`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Record.cpp:113-125`).
- A split announces the next file as `RecordFileChanged {newOutputPath}`, added in 5.5.0
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Outputs.cpp:90-110`); the signal is
  connected at STARTED (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler.cpp:375-385`).
- `GetRecordStatus` has no path (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Record.cpp:38-50`).
  Two source-backed ways to tie an active recording to its manifest after a reconnect:
  - `GetOutputSettings {outputName}` returns the output's settings
    (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Outputs.cpp:442-455`), the same
    object `GetLastRecordFileName` reads, so `outputSettings.path` is the file being written. The
    names are `simple_file_output`
    (`obs-studio@ba2f32bd frontend/utility/SimpleOutput.cpp:249-256`), `adv_file_output`
    (`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:113-120`) and `adv_ffmpeg_output`
    (`obs-studio@ba2f32bd frontend/utility/AdvancedOutput.cpp:82`). The request exists since 5.0.0
    (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler.cpp:169`), so adding it to
    `REQUIRED_REQUESTS` does not move the floor. R2 confirms the value mid-recording [R2].
  - Without a new request: the manifest written at STARTED holds `outputPath`; the `_incoming`
    manifest in state `recording` whose file exists is the candidate, and `outputBytes`
    (`obs_output_get_total_bytes`) against the file size is a plausibility check [R2].
- Auto-remux: when `[Video] AutoRemux` is true, OBS remuxes each finished recording to a sibling
  file and shows a remux window
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:47-55`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:98-110`). It runs right after the
  STOPPED event (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Recording.cpp:238-244`), racing
  finalise's rename and leaving a second video. 32.2.2 sets no default
  [cmd: `grep -rn '"AutoRemux"'` finds only reads and the dialog's save], so it is off unless the
  user enables it on the app's profile. With it on and container `mp4`, OBS records to `.mkv`
  instead (`obs-studio@ba2f32bd frontend/utility/BasicOutputHandler.cpp:433-439`).

## 8. Profile and scene collection switches

- `SetCurrentProfile` runs the switch on OBS's UI thread and waits for it
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:257-262`). The UI task
  triggers the profile menu action (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:191-207`), and
  `ChangeProfile` emits `PROFILE_CHANGING`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:277`), activates the profile, then
  emits `PROFILE_CHANGED` (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:719`). If
  the restart question of section 2 appears, `CurrentProfileChanged` is already out but the
  response waits for the user.
- Neither the request (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:243-265`)
  nor `ChangeProfile` (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:250-284`) checks
  for active outputs, and `ResetOutputs` keeps a running handler
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_OutputHandler.cpp:26-48`). OBS switches profile
  under a live recording or stream; the app's step-1 refusal is the only guard [R2].
- `CreateProfile` calls `obs_frontend_create_profile`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:292`), a queued call that
  does not wait (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:209-212`), so the response comes
  before the profile exists. OBS then creates it (`PROFILE_LIST_CHANGED`,
  `obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:175`) and activates it
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_Profiles.cpp:70-76`), which ends in
  `PROFILE_CHANGED` with no `PROFILE_CHANGING` on this path. The request's own doc says it switches
  to the new profile (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:268`).
- `SetCurrentSceneCollection` also waits for the UI task
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:165-171`):
  `SCENE_COLLECTION_CHANGING`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_SceneCollections.cpp:366`), load, then
  `SCENE_COLLECTION_LIST_CHANGED` and `SCENE_COLLECTION_CHANGED`
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_SceneCollections.cpp:783-790`).
- `CreateSceneCollection` blocks (`obs-studio@ba2f32bd frontend/OBSStudioAPI.cpp:161-167`) and
  switches to the new collection with the same CHANGING ... CHANGED pair
  (`obs-studio@ba2f32bd frontend/widgets/OBSBasic_SceneCollections.cpp:141-150`).
- During a collection change obs-websocket broadcasts `CurrentSceneCollectionChanging`, then marks
  OBS not ready, and marks it ready again before broadcasting `CurrentSceneCollectionChanged`
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler.cpp:270-298`). While not ready, requests
  get 207 `NotReady` (`obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer_Protocol.cpp:212-220`)
  and events are dropped, not queued
  (`obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer_Protocol.cpp:356-362`). A client
  therefore sees Changing then Changed and nothing between: `SceneCollectionListChanged` (emitted
  just before Changed) and any record event inside the window are lost. The protocol still calls
  requests during a change undefined behaviour
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Config.cpp:22-27`); the implementation
  rejects them.
- Ordering: every incoming message is processed as its own `QThreadPool` task
  (`obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer.cpp:347-352`), and every event is
  sent from its own pool task (`obs-websocket@1ef34bf4 src/websocketserver/WebSocketServer_Protocol.cpp:356-362`).
  The pool size is Qt's default [cmd: no `setMaxThreadCount` in `src/websocketserver`]. So neither
  two events nor an event and a response have a guaranteed order on the wire. Expected in practice
  for a profile switch: Changing, Changed, response; R2 records it [R2].

## 9. Minimum OBS version

- Of the 26 requests in spec 3.3, 25 are "Added in v5.0.0" and `SetRecordDirectory` is "Added in
  v5.3.0" (`obs-websocket@1ef34bf4 docs/generated/protocol.md:3299-3306`, source tag at
  `obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Config.cpp:636`)
  [cmd: loop over the 26 names reading the "Added in" line under each `### <name>` heading].
- Which obs-websocket each OBS release ships (submodule pin, then the obs-websocket tag or its
  `CMakeLists.txt` version) [cmd: in a scratch repo, `git fetch --depth 1 --filter=blob:none origin
  tag <T>` then `git ls-tree <T> plugins/obs-websocket`; tags from `git ls-remote --tags`]:

  | OBS | obs-websocket pin | Version |
  |---|---|---|
  | 29.1.3 | `6fd18a7e` | 5.2.3 |
  | 30.0.0-beta1 | `6fd18a7e` | 5.2.3 |
  | 30.0.0 | `4ff109b6` | 5.3.3 |
  | 30.1.0, 30.1.2 | `d2d4bfb3` | 5.4.2 (untagged; `CMakeLists.txt` line 5) |
  | 30.2.0 | `f8bc7c4f` | 5.5.1 |
  | 31.0.0 | `eed8a499` | 5.5.4 |
  | 32.2.2 | `1ef34bf4` | 5.7.4 |

- All 26 names are registered in `RequestHandler.cpp` at 5.3.3; at 5.2.3 only
  `SetRecordDirectory` is missing [cmd: `git show <pin>:src/requesthandler/RequestHandler.cpp |
  grep -c '{"<name>",'` for each name].
- Floor: OBS 30.0.0. The app reads `GetVersion.availableRequests`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_General.cpp:47-53`), so on 29.x it
  names `SetRecordDirectory` as missing.
- Events are not in that list. `RecordFileChanged` was added in 5.5.0
  (`obs-websocket@1ef34bf4 src/eventhandler/EventHandler_Outputs.cpp:90-101`), i.e. OBS 30.2.0; on
  30.0 and 30.1 a split is silent. The app's profile keeps splitting off, so the floor stays.

## 10. Window strings and `GetInputPropertiesListPropertyItems`

- Response: `propertyItems`, each `{itemName, itemEnabled, itemValue}`, `itemValue` typed by the
  list's format (a string for window lists)
  (`obs-websocket@1ef34bf4 src/utils/Obs_ArrayHelper.cpp:268-281`). A missing property gives
  `ResourceNotFound`, a non-list property `InvalidResourceType`
  (`obs-websocket@1ef34bf4 src/requesthandler/RequestHandler_Inputs.cpp:1059-1064`).
- The list comes from `obs_source_properties`, which runs each property's modified callback with
  the input's current settings (`obs-studio@ba2f32bd libobs/obs-source.c:1029-1039`,
  `obs-studio@ba2f32bd libobs/obs-properties.c:376-396`).
- Windows value format, shared by all three kinds: `<title>:<class>:<exe>`, each part with `#`
  escaped as `#22` and `:` as `#3A`; the item name is `[<exe>]: <title>`
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:8-12`,
  `obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:264-276`); parsed back by
  `ms_build_window_strings` (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:23-44`).
- `game_capture` (property `window`) fills its list with that function, minimized windows
  included, blacklisted executables skipped, after an empty first item
  (`obs-studio@ba2f32bd plugins/win-capture/game-capture.c:2236-2239`). `window_capture` excludes
  minimized windows (`obs-studio@ba2f32bd plugins/win-capture/window-capture.c:533`) and may put a
  disabled "Select a window" item first
  (`obs-studio@ba2f32bd plugins/win-capture/window-capture.c:527-531`).
  `wasapi_process_output_capture` includes minimized windows
  (`obs-studio@ba2f32bd plugins/win-wasapi/win-wasapi.cpp:1582-1585`). Same function, same bytes:
  the window string of `game_capture` is valid for `wasapi_process_output_capture` as the spec
  hopes. Runtime confirmation at H5 [H5].
- The configured window stays listed when it is gone: each kind's modified callback inserts the
  current value as a disabled item if the live list lacks it
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:46-60`,
  `obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:65-95`; callers at
  `obs-studio@ba2f32bd plugins/win-capture/game-capture.c:2133-2135`,
  `obs-studio@ba2f32bd plugins/win-capture/window-capture.c:498-508`,
  `obs-studio@ba2f32bd plugins/win-wasapi/win-wasapi.cpp:1571-1573`). "Lacks it" means no live
  item is byte-equal to the whole stored `title:class:exe` string (`strcmp`, same cite as above),
  and the live list holds visible windows only, minus minimized ones for `window_capture`
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:292-298`,
  `obs-studio@ba2f32bd plugins/win-capture/window-capture.c:533`).
- Matching priority defaults: `game_capture` matches by executable
  (`obs-studio@ba2f32bd plugins/win-capture/game-capture.c:2100-2101`, where the default mode
  `any_fullscreen` is also set); `window_capture` and `wasapi_process_output_capture` set no
  default, so 0, which is `WINDOW_PRIORITY_CLASS`
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.h:12-16`).
- So `itemEnabled: false` on the configured item means "no listed window has exactly this title,
  class and exe", not "the window closed". It also reads false for a window that is still open
  but changed its title (an emulator showing FPS, a game showing the level), one hidden to the
  tray, and, in the `window_capture` list only, a minimized one. Capture does not follow the
  title: `ms_find_window` requires the exe at exe priority or the class at class priority and
  uses the title only to rank candidates
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:436-460`), and the window it found
  is kept until it is destroyed or its process exits
  (`obs-studio@ba2f32bd plugins/win-capture/game-capture.c:1668-1674`,
  `obs-studio@ba2f32bd plugins/win-capture/game-capture.c:1804-1808`,
  `obs-studio@ba2f32bd plugins/win-capture/window-capture.c:598`). A retitled game stays
  captured while its item reads disabled. At class priority, classes containing `Chrome` or
  `SDL_app` fall back to an exact title match
  (`obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:469-473`); that matters only when
  OBS searches again after losing the window.
- `wasapi_process_output_capture` is registered only on Windows 10 build 19041 or later
  (`obs-studio@ba2f32bd plugins/win-wasapi/plugin-main.cpp:43-56`); `GetInputKindList` detection
  already covers older systems.
- Linux X11: `xcomposite_input` names its list property `capture_window`
  (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:738`), value
  `<xid>\r\n<name>\r\n<class>` (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:782`,
  separator at `obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:25`). The configured
  window is always item 0, matched against live windows by name and class with the xid ignored
  (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:720-729`), and disabled when no
  live match exists (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:815-817`).
  Spec 11.3 and 12 need `propertyName="capture_window"` on Linux.
- X11 has the same title problem: a retitled window disables item 0 and is listed again as a
  separate enabled item with the same xid and its new name
  (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:789-796`), while capture keeps
  the xid and falls back to name plus class only once that xid is gone
  (`obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:302-314`,
  `obs-studio@ba2f32bd plugins/linux-capture/xcomposite-input.c:619-625`).
- For spec 12, a window-closed rule that follows what capture follows and ignores the title.
  Windows: open while an item with `itemEnabled: true` has the class and exe of
  `capture.window` (decode `#3A` and `#22`, compare case-insensitively as `window_rating` does,
  `obs-studio@ba2f32bd libobs/util/windows/window-helpers.c:436-460`). X11: open while item 0 is
  enabled or an enabled item has the stored xid. Only enabled items count, because the disabled
  preserved item always carries the stored value. Query the `game_capture` input, whose list
  keeps minimized windows, not the `window_capture` fallback. Class alone on X11 would also call
  a reopened, retitled window open, which OBS no longer captures. A retitled window is checked
  at R2 (X11) and H5 (Windows) [R2] [H5].

## 11. obsws-python 1.8.0

- API: `ReqClient(host=, port=, password=, timeout=)` and `EventClient(..., subs=)` share one
  connection class (`obsws-python@f70583d7 obsws_python/baseclient.py:18-29`).
  `ReqClient.send(name, data=None, raw=False)` returns `responseData` as a dataclass (or a dict
  with `raw=True`) and raises `OBSSDKRequestError` with `req_name`, `code`, `comment` when
  `requestStatus.result` is false (`obsws-python@f70583d7 obsws_python/reqs.py:46-60`,
  `obsws-python@f70583d7 obsws_python/error.py:9-18`). A socket timeout becomes
  `OBSSDKTimeoutError` (`obsws-python@f70583d7 obsws_python/baseclient.py:118-134`); both derive
  from `OBSSDKError`. Snake-case wrappers exist for all 26 requests and for `GetOutputSettings`
  (e.g. `set_profile_parameter` at `obsws-python@f70583d7 obsws_python/reqs.py:340-361`)
  [cmd: `grep -n 'self.send("<Name>"' obsws_python/reqs.py` for each name].
  `set_video_settings` always sends all six fields
  (`obsws-python@f70583d7 obsws_python/reqs.py:373-404`); `send("SetVideoSettings", {...},
  raw=True)` sends only the pairs given, and obs-websocket treats null as absent
  (`obs-websocket@1ef34bf4 src/requesthandler/rpc/Request.cpp:41-44`). The spec's generic
  `request(name, **fields)` maps straight onto `send(..., raw=True)`.
- Password exposure: the constructor logs `Connecting with parameters: ... password='<password>'`
  at INFO (`obsws-python@f70583d7 obsws_python/baseclient.py:34-38`), and both clients' `__repr__`
  include it (`obsws-python@f70583d7 obsws_python/reqs.py:36-41`,
  `obsws-python@f70583d7 obsws_python/events.py:44-49`). The Global Constraint holds only if the
  app sets the `obsws_python` logger to WARNING or above and never formats a client with `repr`.
- If none of `host`, `port`, `password` is passed, the client reads a `config.toml` from the
  working folder, home or `~/.config/obsws-python`
  (`obsws-python@f70583d7 obsws_python/baseclient.py:27-29`,
  `obsws-python@f70583d7 obsws_python/baseclient.py:50-70`). The app always passes all three.
- `ReqClient` threading: each request sends, then blocks on the next frame, with a random request
  id from 1 to 1000 that is never checked against the reply
  (`obsws-python@f70583d7 obsws_python/baseclient.py:118-134`). One client allows one request at a
  time from one thread; after a timeout the late reply would be taken as the next request's, so the
  client must be dropped and reconnected. Spec 11.2's `run_in_executor` needs a single-thread
  executor or a lock, and a blocking request (section 8) holds that thread.
- `EventClient` identifies with `subs`, default `Subs.LOW_VOLUME`
  (`obsws-python@f70583d7 obsws_python/events.py:22-24`), which includes General, Config and
  Outputs (`obsws-python@f70583d7 obsws_python/subs.py:4-30`). It starts one daemon thread that
  reads frames and runs callbacks on that thread
  (`obsws-python@f70583d7 obsws_python/events.py:54-60`). When the connection closes the thread
  sets its stop flag and ends without telling anyone
  (`obsws-python@f70583d7 obsws_python/events.py:62-83`); the gateway detects loss by checking
  `worker.is_alive()` or by a failing request.
- Callbacks are matched by function name: `on_` plus the snake-cased event type, e.g.
  `on_record_state_changed`, `on_current_profile_changed`,
  `on_current_scene_collection_changing`, `on_exit_started`
  (`obsws-python@f70583d7 obsws_python/callback.py:21-27`,
  `obsws-python@f70583d7 obsws_python/util.py:9-10`). A lambda or `functools.partial` never
  matches. The argument is a dataclass type with snake-case attributes such as `output_state`,
  `output_active`, `output_path` (`obsws-python@f70583d7 obsws_python/util.py:13-26`).
- Context7 (`/aatikturk/obsws-python`) documents the same class names, `send(..., raw=True)`,
  `Subs` flags and the callback naming rule [cmd: Context7 query on 2026-09-21].

## 12. owocr

- Version: 1.26.8 is the newest PyPI release (uploaded 2026-03-28)
  [cmd: `curl -s https://pypi.org/pypi/owocr/json`]; tag `1.26.8` is `3b9706b1`
  (`owocr@3b9706b1 owocr/__init__.py:1`). GitHub `main` (`0339f341`) differs from it only in the
  `ndlocr_lite` submodule pin [cmd: `git diff --stat 1.26.8 0339f341`]. The spec's pin is current
  and its flags are unchanged.
- The spec's flags all exist at 1.26.8: `-r` (`owocr@3b9706b1 owocr/config.py:21`), `-w`
  (`owocr@3b9706b1 owocr/config.py:25`), `-e` (`owocr@3b9706b1 owocr/config.py:29`), `-t`
  (`owocr@3b9706b1 owocr/config.py:41`, parsed by `str2bool`, so `False` works), `-sa`
  (`owocr@3b9706b1 owocr/config.py:49`), `-swa` (`owocr@3b9706b1 owocr/config.py:51`), `-l`
  (`owocr@3b9706b1 owocr/config.py:69`), `-wp` (`owocr@3b9706b1 owocr/config.py:89`). Also relevant:
  `-el`/`--engines` (`owocr@3b9706b1 owocr/config.py:27`) and `-swp` for Wayland session
  persistence (`owocr@3b9706b1 owocr/config.py:63`).
- Log format for a pip install: loguru to stderr, `HH:mm:ss | <message>`
  (`owocr@3b9706b1 owocr/run.py:3097`, `owocr@3b9706b1 owocr/run.py:3100-3101`). Rectangles print
  as `x1,y1,x2,y2`, several joined with `_`.
- `Selected coordinates: <rects>` (screen coordinates) prints in two cases:
  - after the screen picker when at least one rectangle was drawn
    (`owocr@3b9706b1 owocr/run.py:2476-2481`);
  - at every start when `-sa` is given as rectangles
    (`owocr@3b9706b1 owocr/run.py:1945-1956`).
- `Selected window coordinates: <rects>` (relative to the window) prints:
  - after the window picker (`owocr@3b9706b1 owocr/run.py:2513-2518`);
  - at every start when `-swa` is given as rectangles
    (`owocr@3b9706b1 owocr/run.py:2024-2035`).
- Other outcomes: an empty selection prints `Selection is empty, selecting whole screen` or
  `... whole window` (`owocr@3b9706b1 owocr/run.py:2483`, `owocr@3b9706b1 owocr/run.py:2521`). A
  screen picker closed at startup ends owocr with an error
  (`owocr@3b9706b1 owocr/run.py:2444-2448`); a closed window picker warns and uses the whole
  window (`owocr@3b9706b1 owocr/run.py:2502`). The printed form is exactly what `-sa`/`-swa` parse
  (`owocr@3b9706b1 owocr/run.py:2041-2085`), so the value round-trips.
- When the picker opens: empty `-sa` opens the screen picker at start
  (`owocr@3b9706b1 owocr/run.py:1912-1913`, `owocr@3b9706b1 owocr/run.py:1933`). A window title
  in `-sa` (Windows) with empty `-swa` opens the window picker
  (`owocr@3b9706b1 owocr/run.py:2021-2022`). On Wayland (`XDG_SESSION_TYPE=wayland`,
  `owocr@3b9706b1 owocr/run.py:28`) a window title falls back to the screen picker
  (`owocr@3b9706b1 owocr/run.py:1920-1924`); on Linux X11 it is an error
  (`owocr@3b9706b1 owocr/run.py:2017`). After a selection owocr keeps running and starts OCR, so
  "Select OCR area" must stop it once the line is read [R3].
- Config file: owocr reads `~/.config/owocr_config.ini` through `os.path.expanduser('~')`
  (`owocr@3b9706b1 owocr/config.py:110`) and, when the file is missing, downloads the default
  config from GitHub into that path (`owocr@3b9706b1 owocr/config.py:188-194`). Command-line flags
  override only the keys they name; every other key comes from the user's file, then the defaults
  (`owocr@3b9706b1 owocr/config.py:209-217`). So the app's owocr child reads the user's settings
  (engines list, capture delay, regex filter, key combos, auto-pause, output joining) and creates
  the file on a first run. Spec 3.4/14 say the app never reads or writes that file; its child
  does both. Option from source: start owocr with `HOME` (POSIX) or `USERPROFILE` (Windows)
  pointing at an app-owned folder so `expanduser('~')` lands there; R3 checks what else follows
  `HOME` (model caches, the Wayland portal) [R3].
- Engines: with no `-el`, owocr instantiates every engine whose dependencies import
  (`owocr@3b9706b1 owocr/run.py:3261-3266`), and a `-e` engine that is not available falls back to
  the first available one with only a warning (`owocr@3b9706b1 owocr/run.py:3297-3298`). Passing
  `-el <engine>` with `-e <engine>` loads one engine and makes a silent switch to a cloud engine
  impossible. `meikiocr` and `oneocr` are engine names (`owocr@3b9706b1 owocr/ocr.py:2403-2406`,
  `owocr@3b9706b1 owocr/ocr.py:1819-1822`).
- Network at start: owocr fetches its latest version from PyPI with a 5 s timeout on every start
  (`owocr@3b9706b1 owocr/run.py:3038-3044`), plus the config download above. Offline, a start can
  take up to 5 s longer [R3].
- Websocket: the server binds `0.0.0.0` on `websocket_port`
  (`owocr@3b9706b1 owocr/run.py:527-528`) and sends each result as a text frame to every client
  (`owocr@3b9706b1 owocr/run.py:489-491`); default port 7331
  (`owocr@3b9706b1 owocr/config.py:125`).
- Frame stabilisation defaults to -1, "waits for two OCR results to be the same"
  (`owocr@3b9706b1 owocr/config.py:140`, help at `owocr@3b9706b1 owocr/config.py:57-58`). The tray
  icon defaults to on (`owocr@3b9706b1 owocr/config.py:132`), hence `-t False`.
- Process behaviour: on POSIX a second instance only warns about `/tmp/owocr.lock`
  (`owocr@3b9706b1 owocr/run.py:3338-3353`); SIGINT runs the terminate handler
  (`owocr@3b9706b1 owocr/run.py:3318`). The keyboard thread calls `termios.tcgetattr` on stdin
  (`owocr@3b9706b1 owocr/run.py:2969-2970`), which fails when stdin is a pipe or `/dev/null`;
  that should end only that thread, R3 confirms [R3]. On Windows it polls the console with
  `msvcrt` (`owocr@3b9706b1 owocr/run.py:2949-2967`).

## 13. Runtime checks handed on

- [R1] Zero event among the `StartRecord` response, STARTING and STARTED (section 6); effect of
  frame-aligned pause edges on offsets.
- [R2] Key names in a real `basic.ini` after provisioning; MP4-in-`.mkv` before an output reset and
  its cure by re-activating the profile (section 2); restart question when switching back to a
  user profile; whether a failed output start sends STOPPED; event order for profile and
  collection switches with recording, streaming and replay buffer active, and what a switch does
  to a live output (section 8); `GetOutputSettings` path mid-recording (section 7); Flatpak
  argument pass-through and config root; the unclean-shutdown dialog after a kill (section 4);
  an `xcomposite_input` window retitled mid-recording: item 0 disabled, the same xid listed
  enabled, capture uninterrupted (section 10).
- [R3] owocr under a changed `HOME`; stopping the picker run; non-tty stdin; offline start delay.
- [H4] Picker coordinates on the user's real desktop and games.
- [H5] Registry key and exe path on a real Windows install; `game_capture` and
  `wasapi_process_output_capture` strings for one game window; rename-lock timing; a game that
  changes its title while recording, read through the `game_capture` list and the class plus exe
  rule (section 10).
